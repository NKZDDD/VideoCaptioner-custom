"""English typography correction, independent of optional script replacement."""
import copy
import difflib
import json
import re
from pathlib import Path
from app.core.concurrency import atomic_text


def tokens(text):
    text = text.replace(r'\N', ' ').replace('’', "'")
    return re.findall(r"[\w]+(?:'[\w]+)*", text.casefold())


def preserve_speaker_boundaries(data, native, native_ok, subtitle_path, group_words):
    """Only native speaker IDs can establish a speaker cut; never guess from words."""
    report = {'speaker_boundaries': 0, 'warnings': []}
    try:
        raw = json.loads(Path(subtitle_path).with_suffix('.asr.json').read_text(encoding='utf-8'))
        stamps = []
        for segment in raw['segments']:
            for word in segment.get('words', []):
                speaker = word.get('speaker', word.get('speaker_id', segment.get('speaker', segment.get('speaker_id'))))
                stamps.append(speaker)
        if not native_ok or len(stamps) != len(native.segments) or any(s is None for s in stamps):
            raise ValueError('没有完整、可验证的原生说话人标记，无法自动确认换人边界；请核对混合对白')
        if tokens(' '.join(c.text for c in native.segments)) != tokens(' '.join(c.text for c in data.segments)):
            raise ValueError('说话人标记与字幕文字不一致，保留原字幕边界供复核')
        if all(len(c.text.split()) == 1 for c in data.segments):
            groups, current = [], []
            for word, speaker in zip(native.segments, stamps):
                if current and speaker != current[-1][1]:
                    groups.append(current); current = []; report['speaker_boundaries'] += 1
                current.append((word, speaker))
            if current:
                groups.append(current)
            return [cue for group in groups for cue in group_words([copy.deepcopy(w) for w, _ in group])], report
        # Existing cue boundaries stay intact. Split only at a verified native
        # word boundary, retaining every word and its original interval.
        output = []
        cursor = 0
        for cue in data.segments:
            contained = []
            while cursor < len(native.segments) and native.segments[cursor].start_time < cue.end_time:
                word = native.segments[cursor]
                if word.start_time < cue.start_time or word.end_time > cue.end_time:
                    raise ValueError('原生说话人词时间跨越原字幕边界，需复核')
                contained.append((word, stamps[cursor])); cursor += 1
            if not contained or tokens(' '.join(w.text for w, _ in contained)) != tokens(cue.text):
                raise ValueError('说话人词范围未完整覆盖原字幕，需复核')
            groups, current = [], []
            for word, speaker in contained:
                if current and speaker != current[-1][1]:
                    groups.append(current); current = []; report['speaker_boundaries'] += 1
                current.append((word, speaker))
            groups.append(current)
            if len(groups) == 1:
                output.extend(group_words([copy.deepcopy(cue)]))
            else:
                for group in groups:
                    part = copy.deepcopy(cue)
                    part.text = ' '.join(w.text for w, _ in group)
                    part.start_time = group[0][0].start_time
                    part.end_time = group[-1][0].end_time
                    output.append(part)
        if cursor != len(native.segments):
            raise ValueError('存在未覆盖的说话人词时间，需复核')
        return output, report
    except (OSError, ValueError, KeyError, TypeError) as error:
        report['speaker_boundaries'] = 0
        report['warnings'].append(str(error))
        # Group each original ASR sentence separately, not across sentence edges.
        return group_words(data.segments), report


def native_token_times(data, native):
    """Associate lexical tokens with WHOLE native words. Never interpolate times."""
    source, times = [], []
    for word in native or []:
        for token in tokens(word.text):
            source.append(token)
            times.append((word.start_time, word.end_time))
    target = [t for cue in data.segments for t in tokens(cue.text)]
    mapped = {}
    for kind,a,b,c,d in difflib.SequenceMatcher(None,source,target,autojunk=False).get_opcodes():
        if kind == 'equal' or (kind == 'replace' and b-a == d-c):
            # Local spelling edits preserve token count; map positions, not durations.
            for offset in range(d-c):
                mapped[c+offset] = times[a+offset]
    return mapped


def _case_guard(text, first=False):
    text = text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    text = text.replace(r'\N', ' ').replace('\n', ' ').strip()
    text = re.sub(r"\bi(?=\b|')", 'I', text)
    if first:
        text = re.sub(r'[A-Za-z]', lambda m:m.group().upper(), text, count=1)
    return text


def repair_sentence_units(data, native_segments, reference='', allow_continuations=True):
    """Use only unique, verbatim script matches for punctuation, never words.

    Join explicit lowercase continuations across ASR IDs before display paging.
    Verified differing speaker IDs and existing terminal punctuation block joins.
    """
    result = copy.deepcopy(data)
    report = {'script_punctuation':[], 'joined_continuations':[]}
    stream = tokens(' '.join(c.text for c in result.segments))
    hints = {}
    pattern = re.compile(r"[\w]+(?:'[\w]+)*")
    for line in reference.replace('’', "'").splitlines():
        # Reference storage supplies speaker: dialogue rows. Do not treat headings
        # or actions as dialogue, and never use an active/other video's script.
        if not re.match(r'^[^:：]{1,50}[:：]',line):
            continue
        text = re.split('[:：]',line,maxsplit=1)[1].strip()
        row = tokens(text)
        matches = [i for i in range(len(stream)-len(row)+1) if row and stream[i:i+len(row)] == row]
        if len(matches) != 1:
            continue
        spans = list(pattern.finditer(text))
        for j, word in enumerate(spans):
            tail = text[word.end():spans[j+1].start() if j+1<len(spans) else len(text)]
            mark = re.match(r'\s*([.!?])(?:[\"\u201d\u2019]*\s*)$',tail)
            if mark and word.group().lower() not in {'mr','mrs','ms','dr','prof','jr','sr','st','etc'}:
                hints[matches[0]+j+1] = mark.group(1)
    anchors = native_token_times(data,native_segments)
    output=[]; offset=0
    for cue in result.segments:
        spans=list(pattern.finditer(cue.text.replace('’', "'")))
        text=cue.text.replace('’', "'")
        ends=sorted({e-offset for e in hints if offset < e <= offset+len(spans)} | {len(spans)})
        begin=0
        for end in ends:
            if not spans or end<=begin:
                continue
            boundary=offset+end
            # A script boundary cannot cut a single native compound word in two.
            if (end<len(spans) and (boundary-1 not in anchors or boundary not in anchors
                    or anchors[boundary-1][1]>anchors[boundary][0])):
                continue
            part=copy.deepcopy(cue)
            stop=spans[end].start() if end<len(spans) else len(text)
            value=text[spans[begin].start() if begin else 0:stop].strip()
            mark=hints.get(boundary)
            if mark and not re.search(r'[.!?][\"\u201d\u2019]*$',value):
                value=value.rstrip(' ,;:')+mark
                report['script_punctuation'].append({'token_end':boundary,'text':value})
            if begin or (output and re.search(r'[.!?][\"\u201d\u2019]*$',output[-1].text)):
                value=_case_guard(value,first=True)
            part.text=value
            if begin or end<len(spans):
                if offset+begin not in anchors or boundary-1 not in anchors:
                    raise ValueError('句子边界缺少词时间，未估算')
                part.start_time=anchors[offset+begin][0]
                part.end_time=anchors[boundary-1][1]
            part.voice_source_id=('sentence_unit',len(output))
            output.append(part);begin=end
        offset+=len(spans)
    joined=[]
    for cue in output:
        prior=joined[-1] if joined else None
        speakers=[getattr(c,'speaker_id',getattr(c,'speaker',None)) for c in (prior,cue)] if prior else []
        if (allow_continuations and prior and not re.search(r'[.!?][\"\u201d\u2019]*$',prior.text.rstrip())
                and re.match(r'^[\s\"\u201c\u2018]*[a-z]',cue.text)
                and 0<=cue.start_time-prior.end_time<=250
                and cue.end_time-prior.start_time<=6000
                and not (all(s is not None for s in speakers) and speakers[0]!=speakers[1])):
            report['joined_continuations'].append([prior.text,cue.text])
            prior.text+=' '+cue.text;prior.end_time=cue.end_time
        else:
            joined.append(cue)
    result.segments=joined
    if tokens(' '.join(c.text for c in joined))!=stream:
        raise ValueError('句子修复改变了台词，已拒绝')
    if any(a.end_time>b.start_time for a,b in zip(joined,joined[1:])):
        raise ValueError('句子修复导致时间重叠，已拒绝')
    return result,report


def correct_english(data, config, report_path, speaker_report, request=None,
                    native_segments=None, reference=''):
    report = {'policy':'english_case_punctuation_native_clauses_v2', 'changes':[],
              'semantic_splits':[], **speaker_report}
    result = copy.deepcopy(data)
    anchored = native_token_times(data,native_segments)
    offsets, cursor = {}, 0
    for i,cue in enumerate(data.segments):
        offsets[i] = cursor
        cursor += len(tokens(cue.text))
    outputs = {}
    system = (
        'Restore English capitalization, punctuation and dialogue sentence boundaries ONLY. '
        'Return JSON {"captions":[{"id":0,"parts":[{"text":"You kept it all these years?"},'
        '{"text":"You said I would wear it when I married you."}]}],'
        '"review":[{"id":0,"reason":"uncertain dialogue boundary"}]}. '
        'Include every supplied caption ID exactly once. Parts must partition its existing words '
        'in the exact same order, without adding, deleting or changing any lexical token. '
        'Correct capitalization and punctuation, including at the beginning of the first utterance. '
        'Capitalize actual sentence/response starts, names, acronyms and I. A display page boundary '
        'alone does not require capitalization or punctuation. Retain continuing lowercase text. '
        'Split distinct sentences and question/answer or response exchanges into separate parts, '
        'even if ASR returned one long unpunctuated caption. Never merge caption IDs. '
        'Do not split one grammatical sentence merely to make subtitles shorter: layout runs later. '
        'Script is optional CONTEXT for punctuation/turn boundaries, never a source of unspoken words. '
        'Do not infer or invent speaker identities. Flag uncertain turn boundaries for review. '
        'Keep off-screen speech, repetitions and incomplete utterances. No paraphrase or grammar rewrite. '
        'Use ASCII punctuation and straight apostrophes. Never insert ASS tags, line breaks or timing.'
    )

    def ask(messages):
        if request:
            return request(messages)
        from openai import OpenAI
        from app.core.utils.llm_transport import request_chat
        with OpenAI(base_url=config.base_url,api_key=config.api_key) as client:
            response=request_chat(client,request_stage='英语大小写标点与原生词时间断句',
                model=config.llm_model,messages=messages,temperature=0,
                response_format={'type':'json_object'})
        return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '',response.choices[0].message.content.strip()))

    try:
        for start in range(0,len(data.segments),30):
            captions=[{'id':i,'text':data.segments[i].text} for i in range(start,min(start+30,len(data.segments)))]
            context=[{'id':i,'text':data.segments[i].text} for i in
                     range(max(0,start-2),min(len(data.segments),start+32))]
            payload={'captions':captions,'context_readonly':context,
                     'script_context_readonly':reference[:40000],
                     'customer_notes':str(config.custom_prompt_text or '')[:6000]}
            response=ask([{'role':'system','content':system},
                          {'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
            entries=response.get('captions',[])
            if (not isinstance(entries,list) or len(entries)!=len(captions)
                    or any(not isinstance(c,dict) or type(c.get('id')) is not int for c in entries)
                    or {c['id'] for c in entries}!={c['id'] for c in captions}):
                raise ValueError('英语校验返回编号或数量不一致，未继续导出')
            for entry in entries:
                i=entry['id'];cue=data.segments[i]
                parts=entry.get('parts',[{'text':entry.get('text')}])
                if not isinstance(parts,list) or not parts:
                    raise ValueError('英语断句格式无效')
                texts=[]
                for part in parts:
                    value=part.get('text') if isinstance(part,dict) else None
                    if (not isinstance(value,str) or not tokens(value) or '\\' in value
                            or '{' in value or '}' in value or '\n' in value):
                        raise ValueError('英语断句不能返回空句、标签或排版指令')
                    texts.append(_case_guard(value,first=(i==0 and not texts)))
                if tokens(' '.join(texts))!=tokens(cue.text):
                    raise ValueError('英语校验试图增删或改写台词，已拒绝并停止导出')
                if not getattr(config,'need_split',True):texts=[' '.join(texts)]
                group=[];pos=offsets[i]
                if len(texts)>1:
                    for part_index,text in enumerate(texts):
                        length=len(tokens(text));a=anchored.get(pos);b=anchored.get(pos+length-1)
                        if not a or not b:
                            raise ValueError('断句缺少原生词时间，不能按字数估算；请检查转录时间侧文件')
                        if a[0]<cue.start_time-1 or b[1]>cue.end_time+1 or b[1]<=a[0]:
                            raise ValueError('断句词时间超出原始字幕范围')
                        part=copy.deepcopy(cue);part.text=text
                        part.start_time=int(max(cue.start_time,a[0]));part.end_time=int(min(cue.end_time,b[1]))
                        part.voice_source_id=('english_clause',i,part_index)
                        if group and part.start_time<group[-1].end_time:
                            raise ValueError('断句不能拆开同一个原生词或制造时间重叠')
                        group.append(part);pos+=length
                    report['semantic_splits'].append({'id':i,'parts':[
                        {'text':p.text,'start_ms':p.start_time,'end_ms':p.end_time} for p in group],
                        'basis':'语义句子/应答边界，非音频说话人识别'})
                else:
                    part=copy.deepcopy(cue);part.text=texts[0]
                    part.voice_source_id=('english_clause',i,0)
                    group=[part]
                outputs[i]=group
                if len(texts)>1 or texts[0]!=cue.text:
                    report['changes'].append({'id':i,'before':cue.text,'after':texts})
            review=response.get('review',[])
            if isinstance(review,list):report['warnings'].extend(review)
        result.segments=[part for i in range(len(data.segments)) for part in outputs[i]]
        # ASR can cut a sentence across IDs ("all these" / "years."). Permit
        # bounded readability repair only for explicit lowercase continuation,
        # never across punctuation or a verified native speaker transition.
        if not report.get('speaker_boundaries'):
            for prior,cue in zip(result.segments,result.segments[1:]):
                if (not re.search(r'[.!?][\"\u201d\u2019]*$',prior.text.rstrip())
                        and re.match(r'^[\s\"\u201c\u2018]*[a-z]',cue.text)
                        and 0<=cue.start_time-prior.end_time<=250):
                    cue.voice_source_id=prior.voice_source_id
        assert tokens(' '.join(c.text for c in result.segments))==tokens(' '.join(c.text for c in data.segments))
        if any(a.end_time>b.start_time for a,b in zip(result.segments,result.segments[1:])):
            raise ValueError('英语断句结果有时间重叠，未继续导出')
        report['status']='needs_review' if report['warnings'] else 'checked'
    except Exception as error:
        report.update(status='failed',error=str(error))
        atomic_text(report_path,json.dumps(report,ensure_ascii=False,indent=2))
        raise ValueError('英语文字校验未完成；未继续合成。报告：'+str(report_path)+'；'+str(error)) from error
    atomic_text(report_path,json.dumps(report,ensure_ascii=False,indent=2))
    atomic_text(Path(report_path).with_suffix('.待检查.txt'),
        '英语校验：修改%d条；原生说话人边界%d处；语义拆分%d条\n%s' %
        (len(report['changes']),report['speaker_boundaries'],len(report['semantic_splits']),
         '\n'.join(str(w) for w in report['warnings'])))
    return result,report


