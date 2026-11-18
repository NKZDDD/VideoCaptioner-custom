"""Session choice and explicit script-first processing; old presets stay unchanged."""
import copy
import json
import re
from pathlib import Path
from app.config import SESSION_PATH
import app.config as app_config
from app.core.concurrency import atomic_text

OPTIONS = [('常规处理（兼容原模板）', None), ('按原声出字幕', 'voice'),
           ('原声定位＋剧本校正', 'script_correct'), ('剧本完全替换（独立模式）', 'strict'),
           ('旧版混合改写（兼容原模板）', 'hybrid')]
CHOICE = SESSION_PATH / 'text_source_mode.json'

ALIGN_SYSTEM = (
    'You align spoken ASR fragments with a canonical dialogue script by MEANING and whole-scene context. '
    'This runs inside a subtitle application, with no user copy/paste step. '
    'ASR spelling may be badly corrupted. Arabic ASR may use Modern Standard Arabic spellings while the script is Saudi/Gulf colloquial Arabic. '
    'Understand dialect meaning, people, prior/next lines, and story order; do NOT require character similarity, equal word counts, or identical wording. '
    'The script is the exact wording to display. Never translate or rewrite it. Return IDs and word ranges only. '
    'Use the supplied script speaker labels as context, never claim audio speaker diarization. '
    'ASR start_id and end_id are BOTH INCLUSIVE. Only script_word_end is exclusive. '
    'Return JSON {"matches":[{"script_id":0,"start_id":0,"end_id":4,"decision":"matched","confidence":0.9}],'
    '"unresolved":[{"start_id":5,"end_id":6,"reason":"no supported script counterpart"}],"unmatched":[]}. '
    'For a partial line include script_word_start and script_word_end (zero-based whitespace token indices, end exclusive). '
    'Use the provided script words array and word_count EXACTLY. Do not split Arabic clitics, attached pronouns, or punctuation into additional words. '
    'For a complete row OMIT both script_word_start and script_word_end; word ranges are only for partial rows. '
    'Partial lines can contain one word. A single script row can map to several non-overlapping ASR ranges. '
    'If dialogue repeats, mark repeat:true. If supported spoken order changes, mark order_change:true and provide reason. '
    'Every ASR ID must be either matched or unresolved; do not silently omit difficult fragments. '
    'A partial script excerpt must account for ALL spoken words in its ASR range, not merely a shared prefix. '
    'If only a prefix aligns, shrink start_id/end_id to those word IDs and mark the other IDs unresolved; never consume and erase the remaining words. '
    'Check partial ranges for swallowed final words, pronouns, names and negation before returning. '
    'A matched range uses original global word IDs, '
    'not sentence_context group indices. sentence_context exists only to help read adjacent words together. '
    'Each match MUST fit inside one supplied speech_islands first_id..last_id range. Keep ASR ranges chronological and non-overlapping. '
    'Never cross a silent gap >=1000ms within one range; return separate partial matches for separate islands. '
    'Do not fill pauses with script text. Do not force every script row into the audio. If only part was spoken, select only that script substring. '
    'Distinguish negation, names and speaker turns. If context cannot resolve a fragment, keep it unresolved instead of guessing. '
    'For bounded windows, omit unspoken script rows; they may match later. Do not output partial matches cut off by a window edge.'
)


def sentence_context(transcript):
    groups=[];group=[]
    for word in transcript:
        if group and (word['start_ms']-group[-1]['end_ms']>=600 or len(group)>=24):
            groups.append(dict(start_id=group[0]['id'],end_id=group[-1]['id'],text=' '.join(v['text'] for v in group)));group=[]
        group.append(word)
    if group:groups.append(dict(start_id=group[0]['id'],end_id=group[-1]['id'],text=' '.join(v['text'] for v in group)))
    return groups


def speech_islands(transcript):
    islands=[];group=[]
    for word in transcript:
        if group and word['start_ms']-group[-1]['end_ms']>=1000:
            islands.append({'first_id':group[0]['id'],'last_id':group[-1]['id']});group=[]
        group.append(word)
    if group:islands.append({'first_id':group[0]['id'],'last_id':group[-1]['id']})
    return islands


def align_windows(transcript, script, ask, checkpoint):
    """Bound payload sizes, overlap ASR context, and retain global IDs."""
    if len(json.dumps({'script': script, 'asr': transcript, 'sentence_context':sentence_context(transcript),'speech_islands':speech_islands(transcript)}, ensure_ascii=False)) < 65000:
        return ask([{'role': 'system', 'content': ALIGN_SYSTEM},
                    {'role': 'user', 'content': json.dumps({'script': script, 'asr': transcript, 'sentence_context':sentence_context(transcript),'speech_islands':speech_islands(transcript)}, ensure_ascii=False)}])
    accepted=[];known=set();unresolved=[]
    cursor,last_sid,window=0,-1,0
    while cursor<len(transcript):
        chunk=[];budget=0
        for row in transcript[cursor:]:
            cost=len(json.dumps(row,ensure_ascii=False))+2
            if cost>18000:raise ValueError('单段转录超过匹配容量，请先拆分')
            if chunk and budget+cost>18000:break
            chunk.append(row);budget+=cost
        context=[];budget=0
        for row in script[max(0,last_sid-6):]:
            cost=len(json.dumps(row,ensure_ascii=False))+2
            if cost>25000:raise ValueError('单条剧本超过匹配容量，请按对白整理')
            if context and budget+cost>25000:break
            context.append(row);budget+=cost
        payload={'script':context,'asr':chunk,'sentence_context':sentence_context(chunk),'speech_islands':speech_islands(chunk),'window':window,
                 'confirmed_script_ids':sorted({m['script_id'] for m in accepted if m['script_id'] in {r['id'] for r in context}})}
        content=json.dumps(payload,ensure_ascii=False)
        if len(content)>65000:raise ValueError('分段匹配请求超过容量')
        result=ask([{'role':'system','content':ALIGN_SYSTEM},{'role':'user','content':content}])
        allowed_script={r['id'] for r in context};first,final=chunk[0]['id'],chunk[-1]['id']
        matches=result.get('matches',[])
        if not isinstance(matches,list):raise ValueError('匹配返回条目不是列表')
        for match in matches:
            if not isinstance(match,dict):raise ValueError('匹配条目格式错误')
            sid,start,end=match.get('script_id'),match.get('start_id'),match.get('end_id')
            if any(type(x)is not int for x in (sid,start,end)) or sid not in allowed_script or not first<=start<=end<=final:
                raise ValueError('分段匹配返回范围外编号')
            key=(sid,start,end,match.get('script_word_start'),match.get('script_word_end'))
            if key in known:continue
            if final<transcript[-1]['id'] and end==final:continue
            if any(not(end<m['start_id'] or start>m['end_id']) for m in accepted):
                raise ValueError('重叠窗口语义匹配冲突，保留已确认窗口')
            accepted.append(match);known.add(key)
        unresolved.extend(result.get('unresolved',[]) if isinstance(result.get('unresolved',[]),list) else [])
        last_sid=max((m['script_id'] for m in accepted),default=last_sid);window+=1
        checkpoint({'mode':'windowed','windows_completed':window,'matches':accepted[:],'unresolved':unresolved[:],'status':'matching'})
        if final==transcript[-1]['id']:break
        cursor+=max(1,len(chunk)-min(80,len(chunk)//3))
    return {'matches':accepted,'unresolved':unresolved,'unmatched':sorted({r['id'] for r in script}-{m['script_id'] for m in accepted}),'windows':window}


def choices():
    if not CHOICE.exists():
        seed = getattr(app_config, 'SETTINGS_SEED_PATH', None)
        previous = Path(seed).parent / CHOICE.name if seed else None
        if previous and previous.exists():
            atomic_text(CHOICE, previous.read_text(encoding='utf-8'))
    try:
        return json.loads(CHOICE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def select_mode(profile_id, mode):
    if mode not in (None, 'voice', 'strict', 'hybrid', 'script_correct'):
        raise ValueError('字幕文字来源无效')
    data = choices()
    data[str(profile_id or 'custom')] = mode
    atomic_text(CHOICE, json.dumps(data, ensure_ascii=False, indent=2))


def selected_mode(profile):
    if 'text_source_mode' in profile:
        return profile['text_source_mode']
    return choices().get(str(profile.get('id') or 'custom'), profile.get('text_source_mode'))


def script_rows(reference):
    # Store imports already turn structured JSON into speaker：dialogue rows.
    return [line.split('：', 1)[-1].strip() for line in reference.splitlines() if line.strip()]


def process_script_mode(data, reference, config, report_path, request=None):
    if config.text_source_mode == 'script_correct':
        from app.core.utils.script_correction import process_script_correction
        return process_script_correction(data,reference,config,report_path,request=request)
    from app.core.utils.llm_transport import request_chat
    from openai import OpenAI
    rows = script_rows(reference)
    config.script_speakers = {i:line.split('：',1)[0].strip() if '：' in line else ''
        for i,line in enumerate(line for line in reference.splitlines() if line.strip())}
    if not rows:
        raise ValueError('该模式需要先绑定本视频的剧本台词；未生成字幕，也不会使用转录文字代替。')
    if not data.segments:
        raise ValueError('转录没有有效时间段，不能给剧本猜时间。')
    def ask(messages):
        if request:
            response = request(messages)
        else:
            if not config.base_url or not config.api_key or not config.llm_model:
                raise ValueError('剧本匹配需要可用的LLM配置')
            with OpenAI(base_url=config.base_url, api_key=config.api_key) as client:
                response = request_chat(client, request_stage='剧本台词时间匹配', model=config.llm_model,
                    messages=messages, temperature=0, response_format={'type': 'json_object'})
        if isinstance(response, dict):
            return response
        content = response.choices[0].message.content
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip())
        return json.loads(content)
    # Keep stored mode IDs compatible, but no longer abort a whole video for
    # missing rows. The adaptive assembler owns validation and ASR preservation.
    from app.core.utils.adaptive_script import process_adaptive_script
    return process_adaptive_script(data, rows, config, report_path, ask, align_windows)
