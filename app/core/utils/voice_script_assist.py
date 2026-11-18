"""ASR owns words and timing; scripts supply bounded, auditable local corrections."""
import copy
import json
import re
from pathlib import Path

from app.core.concurrency import atomic_text


SYSTEM = (
    'Correct local ASR recognition mistakes using the supplied dialogue as contextual reference ONLY. '
    'This is NOT script alignment or script replacement. Speech captions are authoritative. '
    'Do not complete script sentences, add unspoken dialogue, delete captions, reorder speech or change times. '
    'Keep off-screen, telephone and background speech even if absent from the script. '
    'Do not fix grammar, paraphrase, remove repetitions or force ASR to agree with the script. '
    'Return JSON {"edits":[{"cue_id":0,"source":"Katherine","replacement":"Catherine",'
    '"script_id":6,"confidence":0.98,"reason":"proper-name spelling"}]}. '
    'Use an exact, uniquely occurring source substring and exact replacement text appearing in that script row. '
    'Propose only strongly supported name/spelling or local recognition corrections, at most 3 words per edit. '
    'Preserve pronouns, negation, numbers, tense, speaker and meaning. When uncertain, return no edit. '
    'Return edits only, never rewritten captions or new subtitle rows.'
)


def _tokens(text):
    return re.findall(r"[\w]+(?:['\u2019][\w]+)?", text.casefold())


def collect_asr_review(subtitle_path):
    path=Path(subtitle_path).with_suffix('.asr.json')
    if not path.exists():return []
    try:
        raw=json.loads(path.read_text(encoding='utf-8'))
        reviews=[]
        for segment in raw.get('segments',[]):
            weak=[]
            for word in segment.get('words',[]):
                value=word.get('probability')
                if type(value) in (int,float) and 0<=value<.35:
                    weak.append({'word':word.get('word','').strip(),'probability':value})
            if weak:
                reviews.append({'start_ms':round(segment['start']*1000),
                    'end_ms':round(segment['end']*1000),'text':segment.get('text',''),
                    'low_probability_words':weak,
                    'reason':'原始转录词概率偏低，需结合原声确认；不是已判定错误，也未自动删改'})
        return reviews
    except (OSError,ValueError,TypeError,KeyError):
        return []


def apply_edits(data, edits, script, report):
    """Every edit must be localized, script-supported, and preserve all other text."""
    if not isinstance(edits, list):
        raise ValueError('辅助校正返回格式无效：edits必须是列表')
    proposals = {}
    protected = {'no', 'not', 'never', 'nothing', 'nobody', 'without', 'cannot',
                 "can't", "isn't", "wasn't", "don't", "doesn't", "won't", "didn't",
                 'i', 'you', 'he', 'she', 'we', 'they', 'my', 'your', 'his', 'her', 'our', 'their',
                 'me', 'him', 'us', 'them', 'mine', 'yours', 'hers', 'ours', 'theirs'}
    for edit in edits:
        try:
            if not isinstance(edit, dict):
                raise ValueError('编辑条目格式无效')
            cue_id, sid = edit.get('cue_id'), edit.get('script_id')
            if type(cue_id) is not int or not 0 <= cue_id < len(data.segments):
                raise ValueError('字幕编号越界')
            if type(sid) is not int or not 0 <= sid < len(script):
                raise ValueError('参考剧本编号越界')
            source, replacement = edit.get('source'), edit.get('replacement')
            if not isinstance(source, str) or not isinstance(replacement, str):
                raise ValueError('局部文字格式无效')
            if not source.strip() or not replacement.strip() or '\n' in replacement or '\\' in replacement:
                raise ValueError('不能删除文字或插入排版指令')
            src_tokens, dst_tokens = _tokens(source), _tokens(replacement)
            if not (1 <= len(src_tokens) <= 3 and 1 <= len(dst_tokens) <= 3):
                raise ValueError('只能校正局部一至三个词')
            # A spelling fix must not become a sentence-completion operation.
            if len(dst_tokens) != len(src_tokens):
                raise ValueError('辅助校正不能增加或吞掉词数')
            src_protected = [t.replace('\u2019', "'") for t in src_tokens if t.replace('\u2019', "'") in protected or t.replace('\u2019', "'").endswith("n't")]
            dst_protected = [t.replace('\u2019', "'") for t in dst_tokens if t.replace('\u2019', "'") in protected or t.replace('\u2019', "'").endswith("n't")]
            if src_protected != dst_protected or re.findall(r'\d+', source) != re.findall(r'\d+', replacement):
                raise ValueError('不能改动人称、否定或数字')
            confidence = edit.get('confidence')
            if type(confidence) not in (float, int) or not .95 <= confidence <= 1:
                raise ValueError('校正证据不足，保留原转录')
            text = data.segments[cue_id].text
            pattern = r'(?<!\w)' + re.escape(source) + r'(?!\w)'
            positions = list(re.finditer(pattern, text))
            if len(positions) != 1:
                raise ValueError('原文位置不唯一或不匹配')
            if not re.search(r'(?<!\w)' + re.escape(replacement) + r'(?!\w)', script[sid]):
                raise ValueError('改正文字不在指定剧本中')
            start, end = positions[0].span()
            if source == replacement:
                continue
            if any(not (end <= a or start >= b) for a,b,_ in proposals.get(cue_id, [])):
                raise ValueError('校正范围重叠')
            proposals.setdefault(cue_id, []).append((start,end,edit))
        except (ValueError, TypeError) as error:
            report['rejected'].append({'edit': edit, 'reason': str(error)})
    for cue_id, changes in proposals.items():
        cue = data.segments[cue_id]
        before = cue.text
        # Avoid many individually small edits rewriting most of one utterance.
        changed_words = sum(len(_tokens(e['source'])) for _,_,e in changes)
        if changed_words > max(1, len(_tokens(before))//2):
            report['rejected'].append({'cue_id':cue_id, 'reason':'累计改动范围过大，整条保留原转录'})
            continue
        for start,end,edit in sorted(changes, reverse=True, key=lambda x:x[0]):
            cue.text = cue.text[:start] + edit['replacement'] + cue.text[end:]
            report['applied'].append(dict(edit))
        report['changes'].append({'cue_id':cue_id, 'before':before, 'after':cue.text,
                                 'start_ms':cue.start_time, 'end_ms':cue.end_time})
    return data


def process_voice_assist(data, reference, config, report_path, request=None):
    result = copy.deepcopy(data)
    script = [line.split('：',1)[-1].strip() for line in reference.splitlines() if line.strip()]
    report = {'mode':'voice', 'policy':'asr_primary_local_script_corrections_v1',
              'applied':[], 'rejected':[], 'changes':[], 'warnings':[],
              'source_cues':len(data.segments), 'script_rows':len(script)}
    report['asr_review']=copy.deepcopy(getattr(config,'raw_asr_reviews',[]))
    if report['asr_review']:
        report['warnings'].append(f"原始转录有{len(report['asr_review'])}处低词概率片段；未擅自删除或按剧本补写，请听原声核对")
    original_times = [(c.start_time,c.end_time) for c in data.segments]
    if script:
        script_json = json.dumps([{'id':i,'text':s} for i,s in enumerate(script)], ensure_ascii=False)
        if len(script_json) > 40000:
            report['warnings'].append('参考剧本过长，本次保留原转录，未执行辅助校正')
        else:
            if request is None:
                from openai import OpenAI
                from app.core.utils.llm_transport import request_chat
                def request(messages):
                    with OpenAI(base_url=config.base_url, api_key=config.api_key) as client:
                        response=request_chat(client, request_stage='语音为主JSON局部辅助校正',
                            model=config.llm_model,messages=messages,temperature=0,
                            response_format={'type':'json_object'})
                    content=response.choices[0].message.content
                    return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip()))
            batches=[];batch=[];size=len(script_json)
            for i,c in enumerate(result.segments):
                entry={'id':i,'text':c.text,'start_ms':c.start_time,'end_ms':c.end_time}
                cost=len(json.dumps(entry,ensure_ascii=False))
                if batch and size+cost>56000:
                    batches.append(batch);batch=[];size=len(script_json)
                if size+cost>60000:
                    report['warnings'].append(f'字幕{i}超过校正容量，保留原转录')
                    continue
                batch.append(entry);size+=cost
            if batch:batches.append(batch)
            for batch in batches:
                try:
                    response=request([{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(
                        {'script':json.loads(script_json),'captions':batch,
                         'customer_notes':str(getattr(config,'custom_prompt_text',''))[:4000]}, ensure_ascii=False)}])
                    if not isinstance(response,dict) or 'edits' not in response:
                        raise ValueError('未返回局部edits，拒绝整句改写结果')
                    allowed={c['id'] for c in batch}
                    if not isinstance(response['edits'],list):
                        raise ValueError('edits格式无效')
                    edits=[]
                    for edit in response['edits']:
                        if not isinstance(edit,dict) or edit.get('cue_id') not in allowed:
                            report['rejected'].append({'edit':edit,'reason':'不属于本批字幕'})
                        else:edits.append(edit)
                    apply_edits(result,edits,script,report)
                except Exception as error:
                    report['warnings'].append('辅助校正未完成，本批原转录保留：'+str(error))
    else:
        report['warnings'].append('未绑定本视频JSON，按原转录输出，不做剧本替换')
    assert len(result.segments)==len(data.segments)
    assert [(c.start_time,c.end_time) for c in result.segments]==original_times
    report['summary']=f"语音为主；局部校正{len(report['applied'])}处，拒绝{len(report['rejected'])}处；未新增、删除字幕，校正未改时间"
    report['status']='completed_with_review' if report['warnings'] or report['rejected'] else 'completed'
    atomic_text(report_path,json.dumps(report,ensure_ascii=False,indent=2))
    review=Path(report_path).with_suffix('.待检查.txt')
    lines=[report['summary']]+report['warnings']+[str(e) for e in report['asr_review']]+[str(e) for e in report['rejected']]
    atomic_text(review,'\n'.join(lines))
    result.voice_assist_report=report
    return result
