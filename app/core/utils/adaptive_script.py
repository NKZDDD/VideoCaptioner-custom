"""Partial, conservative script correction with lossless ASR fallbacks."""
import copy
import json
import re
import unicodedata
import math

from app.core.concurrency import atomic_text
from app.core.utils.script_word_timing import script_excerpt


def _normal(text):
    from app.core.utils.script_reorder_recovery import _key
    return _key(unicodedata.normalize('NFKC', text))


def _validate(match, rows, segments):
    if not isinstance(match, dict):
        return '匹配条目格式错误'
    sid, start, end = (match.get(k) for k in ('script_id', 'start_id', 'end_id'))
    if any(type(x) is not int for x in (sid, start, end)):
        return '匹配编号格式错误'
    if not (0 <= sid < len(rows) and 0 <= start <= end < len(segments)):
        return '匹配编号越界'
    if match.get('decision', 'matched') not in ('matched', 'match'):
        return '模型标记为不确定，保留待检查'
    if 'confidence' in match:
        try:
            confidence=float(match['confidence'])
        except (TypeError,ValueError):
            return '置信度格式错误'
        if not math.isfinite(confidence) or not 0<=confidence<=1:
            return '置信度格式错误'
    spans = segments[start:end + 1]
    if any(s.end_time <= s.start_time for s in spans):
        return '转录时间段无效'
    if any(b.start_time - a.end_time >= 1000 for a, b in zip(spans, spans[1:])):
        return '跨越明显静音停顿'
    try:
        excerpt, partial = script_excerpt(rows[sid], match)
    except ValueError as error:
        return str(error)
    if not excerpt.strip():
        return '剧本选取范围为空'
    # Loss prevention for Latin-language partials only. This does not select a
    # script by similarity: a copied excerpt must not consume additional distinct
    # ASR words without displaying them. Retain ASR instead of guessing a suffix.
    if partial:
        source = ' '.join(s.text for s in spans)
        if not re.search(r'[^\x00-\x7f\u2018\u2019\u201c\u201d\u2014\u2026]', source + excerpt):
            tokens = lambda text: re.findall(r"[a-z]+(?:'[a-z]+)?", text.casefold().replace('\u2019', "'").replace('\u2018', "'"))
            src, dst = tokens(source), tokens(excerpt)
            if dst and set(dst).issubset(set(src)) and set(src) - set(dst):
                return '部分替换会吞掉额外的原转录词，已保留原转录'
    # Semantic correspondence belongs to the configured LLM. Neither spelling,
    # character count nor a local fuzzy score may overturn its explicit match.
    return ''


def _accept_semantic(matches, rows, segments, report):
    accepted=[]
    if not isinstance(matches,list):
        report['warnings'].append('匹配条目必须是列表；保留原转录')
        return accepted
    valid=[]
    for match in matches:
        reason=_validate(match,rows,segments)
        if reason:report['rejected'].append({'match':match,'reason':reason})
        else:valid.append(dict(match))
    for match in sorted(valid,key=lambda m:(m['start_id'],m['end_id'])):
        if any(not(match['end_id']<m['start_id'] or match['start_id']>m['end_id']) for m in accepted):
            report['rejected'].append({'match':match,'reason':'转录范围重复覆盖或重叠'})
            continue
        if accepted and match['script_id']<accepted[-1]['script_id'] and not (match.get('repeat') is True or match.get('order_change') is True):
            report['rejected'].append({'match':match,'reason':'剧本顺序倒退且模型未明确标记重复或顺序变化'})
            continue
        accepted.append(match)
    return accepted


def _fallback_cues(spans):
    """Group only adjacent unmatched word times; never extend into silence."""
    def word_like(span):
        text = span.text.strip()
        return len(text) == 1 if re.search(r'[\u3400-\u9fff]', text) else len(text.split()) == 1
    if not spans or sum(word_like(s) for s in spans) / len(spans) < 0.8:
        return copy.deepcopy(spans)
    groups, current = [], []
    for span in spans:
        cjk = bool(re.search(r'[\u3400-\u9fff]', span.text))
        length = sum(len(s.text) if cjk else len(s.text.split()) for s in current)
        if current and (span.start_time - current[-1].end_time >= 400
                        or re.search(r'[.!?。！？؟।]$', current[-1].text.strip())
                        or length >= (11 if cjk else 14)
                        or span.end_time - current[0].start_time > 7000):
            groups.append(current)
            current = []
        current.append(span)
    if current:
        groups.append(current)
    cues = []
    for group in groups:
        cue = copy.deepcopy(group[0])
        separator = '' if any(re.search(r'[\u3400-\u9fff]', s.text) for s in group) else ' '
        cue.text = separator.join(s.text.strip() for s in group)
        cue.end_time = group[-1].end_time
        cues.append(cue)
    return cues


def _stamp(ms):
    minutes, seconds = divmod(max(0, int(ms)) // 1000, 60)
    return f'{minutes // 60:02}:{minutes % 60:02}:{seconds:02}'


def _save_report(path, report):
    atomic_text(path, json.dumps(report, ensure_ascii=False, indent=2))
    lines = ['剧本局部纠错检查单', report['summary'],
             '语音决定时间；未匹配剧本不强行添加；以下内容未烧录进视频。', '']
    for entry in report['review_items']:
        lines += [f"[{entry['kind']}] {_stamp(entry['start_ms'])}—{_stamp(entry['end_ms'])}",
                  entry.get('text', ''), entry.get('reason', ''), '']
    from pathlib import Path
    review_path = Path(path).with_suffix('.待检查.txt')
    atomic_text(review_path, '\n'.join(lines))
    report['review_path'] = str(review_path)
    atomic_text(path, json.dumps(report, ensure_ascii=False, indent=2))


def process_adaptive_script(data, rows, config, report_path, ask, aligner):
    original = data.segments
    transcript = [{'id': i, 'start_ms': s.start_time, 'end_ms': s.end_time, 'text': s.text}
                  for i, s in enumerate(original)]
    script = [{'id': i, 'text': text, 'speaker': getattr(config,'script_speakers',{}).get(i,''),
               'words':text.split(), 'word_count':len(text.split())} for i, text in enumerate(rows)]
    report = {'mode': config.text_source_mode, 'policy': 'llm_semantic_script_exact_timing_v8_loss_guard',
              'matches': [], 'rejected': [], 'unmatched': [], 'uncovered_asr_ids': [],
              'changes': [], 'review_items': [], 'local_rechecks': [], 'warnings': []}
    checkpoint = {}

    def save_checkpoint(progress):
        checkpoint.update(progress)
        atomic_text(report_path, json.dumps(progress, ensure_ascii=False, indent=2))

    try:
        mapping = aligner(transcript, script, ask, save_checkpoint)
    except Exception as error:
        # A failed window must not discard already verified windows or ASR.
        mapping = checkpoint
        report['warnings'].append('匹配请求未完成，保留已校验匹配及原转录：' + str(error))
    if not isinstance(mapping, dict):
        report['warnings'].append('匹配返回格式错误，保留转录待检查')
        mapping = {}
    accepted_list = _accept_semantic(mapping.get('matches', []), rows, original, report)
    # A malformed ID/range is a protocol error, not a semantic disagreement.
    # Repair only those ranges once, inside the app, without another ASR pass.
    structural=[r for r in report['rejected'] if r['reason'] in ('跨越明显静音停顿','剧本词范围无效（结束编号不包含在内）')]
    repair_scope=[]
    for rejected in structural[:8]:
        m=rejected['match'];sid,a,b=(m.get(k) for k in ('script_id','start_id','end_id'))
        if all(type(v)is int for v in (sid,a,b)) and 0<=sid<len(rows) and 0<=a<=b<len(original) and b-a<200:
            repair_scope.append(rejected)
    report['structural_repairs']=[]
    if repair_scope and ask is not None:
        ids={i for r in repair_scope for i in range(r['match']['start_id'],r['match']['end_id']+1)}
        sids={r['match']['script_id'] for r in repair_scope}
        occupied={i for m in accepted_list for i in range(m['start_id'],m['end_id']+1)}
        ids-=occupied
        ranges=[];current=[]
        for i in sorted(ids):
            if current and (i!=current[-1]+1 or original[i].start_time-original[current[-1]].end_time>=1000):
                ranges.append({'start_id':current[0],'end_id':current[-1]});current=[]
            current.append(i)
        if current:ranges.append({'start_id':current[0],'end_id':current[-1]})
        payload={'invalid_matches':repair_scope,'allowed_speech_ranges':ranges,'script':[r for r in script if r['id'] in sids],
                 'asr':[r for r in transcript if r['id'] in ids],
                 'occupied_ranges':[dict(start_id=m['start_id'],end_id=m['end_id']) for m in accepted_list]}
        encoded=json.dumps(payload,ensure_ascii=False)
        if len(encoded)<=30000:
            try:
                repair=ask([{'role':'system','content':
                    'Repair malformed subtitle alignment ranges, not dialogue wording. Return JSON {"matches":[{"script_id":0,"start_id":0,"end_id":1,"script_word_start":0,"script_word_end":2,"decision":"matched"}]}. '
                    'The supplied script_id semantic choices are already made. Use ONLY supplied ASR IDs and script IDs. '
                    'ASR start_id AND end_id are INCLUSIVE: an allowed range 94..106 ends at 106, NOT 107. Each match must lie inside ONE allowed_speech_ranges entry. '
                    'For script words use EXACT keys script_word_start and script_word_end; script_word_end is EXCLUSIVE. '
                    'Use the supplied words array; never split Arabic clitics. Omit BOTH script word fields only for a complete row. '
                    'Split any match across a silent gap >=1000ms into separate supported partial script ranges. '
                    'Do not fill silence, overlap occupied ranges, invent words, or guess unsupported word boundaries. '
                    'Keep chronological ranges. Mark decision:matched; if repair is not supported, omit it.'},
                    {'role':'user','content':encoded}])
                repaired=[]
                for m in repair.get('matches',[]) if isinstance(repair,dict) and isinstance(repair.get('matches',[]),list) else []:
                    if _validate(m,rows,original):continue
                    if m['script_id'] not in sids or any(i not in ids for i in range(m['start_id'],m['end_id']+1)):continue
                    repaired.append(m)
                if repaired:
                    report['structural_repairs']=repaired
                    accepted_list=_accept_semantic(accepted_list+repaired,rows,original,report)
            except Exception as error:
                report['warnings'].append('匹配范围自动修正未完成；原转录保留待检查：'+str(error))
    report.update(semantic_alignment=True,local_text_similarity_filter=False,
                  order_recoveries=[],continuation_matches=[],context_recoveries=[],boundary_expansions=[])
    report['model_unresolved']=mapping.get('unresolved',[])
    for rejected in report['rejected']:
        m=rejected.get('match',{})
        if all(type(m.get(k))is int for k in ('script_id','start_id','end_id')):
            resolved={i for match in accepted_list if match['script_id']==m['script_id'] for i in range(match['start_id'],match['end_id']+1)}
            if all(i in resolved for i in range(m['start_id'],m['end_id']+1)):
                rejected['resolved_by']='internal_structural_repair'
    report['active_rejections']=[r for r in report['rejected'] if not r.get('resolved_by')]
    report['matches'] = accepted_list
    if report['warnings'] and not accepted_list:
        report['status']='failed'
        report['summary']='GPT语义匹配未完成；未执行剧本替换，原始转录已保留'
        _save_report(report_path,report)
        raise RuntimeError(report['summary']+'：'+report['warnings'][0])
    covered = {i for m in accepted_list for i in range(m['start_id'], m['end_id'] + 1)}
    report['uncovered_asr_ids'] = [i for i in range(len(original)) if i not in covered]
    report['unmatched'] = [sid for sid in range(len(rows)) if not any(m['script_id']==sid for m in accepted_list)]
    starts = {m['start_id']: m for m in accepted_list}
    output, corrected_ids, cursor = [], [], 0
    while cursor < len(original):
        match = starts.get(cursor)
        if match:
            cue = copy.deepcopy(original[cursor])
            cue.end_time = original[match['end_id']].end_time
            cue.text, partial = script_excerpt(rows[match['script_id']], match)
            if partial:
                report['review_items'].append({
                    'kind': '剧本部分匹配', 'script_id': match['script_id'],
                    'script_word_start': match['script_word_start'],
                    'script_word_end': match['script_word_end'],
                    'start_ms': cue.start_time, 'end_ms': cue.end_time,
                    'text': cue.text,
                    'reason': '仅替换已匹配的剧本范围；其他范围仅在另有独立匹配时添加，不代表已确认视频未说',
                })
            cue.script_row_id = match['script_id']
            cue.script_speaker = getattr(config, 'script_speakers', {}).get(match['script_id'], '')
            cue.script_corrected = True
            cue.translated_text = ''
            corrected_ids.append(len(output))
            output.append(cue)
            cursor = match['end_id'] + 1
        else:
            end = cursor + 1
            while end < len(original) and end not in starts:
                end += 1
            # Do not mix fallback speech with corrected dialogue.
            for cue in _fallback_cues(original[cursor:end]):
                output.append(cue)
                report['review_items'].append({'kind': '保留语音转录', 'start_ms': cue.start_time,
                    'end_ms': cue.end_time, 'text': cue.text,
                    'reason': '没有可靠的剧本匹配，未强行替换；请听此处音频确认'})
            cursor = end
    report['duplicate_script_rows'] = []
    for sid in report['unmatched']:
        identical = [m for m in accepted_list
                     if _normal(rows[m['script_id']]) == _normal(rows[sid])]
        if identical:
            item = {'script_id': sid, 'same_as_script_ids': [m['script_id'] for m in identical]}
            report['duplicate_script_rows'].append(item)
            first = min(original[m['start_id']].start_time for m in identical)
            last = max(original[m['end_id']].end_time for m in identical)
            report['review_items'].append({
                'kind': '剧本重复条目待核对', 'script_id': sid,
                'start_ms': first, 'end_ms': last, 'text': rows[sid],
                'reason': '同文台词已在其他剧本编号匹配；这里只指向已有匹配位置，不代表视频再次说出。未重复添加字幕，也未删除原剧本条目。',
                'same_as_script_ids': item['same_as_script_ids'],
            })
            continue
        before = [m for m in accepted_list if m['script_id'] < sid]
        after = [m for m in accepted_list if m['script_id'] > sid]
        prior = max(before, key=lambda m:m['script_id']) if before else None
        following = min(after, key=lambda m:m['script_id']) if after else None
        first = original[prior['end_id']].end_time if prior else original[0].start_time
        last = original[following['start_id']].start_time if following else original[-1].end_time
        if last <= first:
            first, last = original[0].start_time, original[-1].end_time
        report['review_items'].append({'kind': '剧本未使用', 'script_id': sid,
            'start_ms': first, 'end_ms': max(first, last), 'text': rows[sid],
            'reason': '仅为前后锚点间的检查范围，不是推测的字幕时间；可能未说或转录遗漏，未添加字幕'})

    if config.text_source_mode == 'hybrid' and getattr(config, 'custom_prompt_text', '').strip():
        # Customer rewriting is optional; failures cannot destroy the core result.
        # Fallback ASR is never passed to this rewriting request.
        pending = []
        budget = len(config.custom_prompt_text)
        if budget <= 15000:
            batches = []
            for i in corrected_ids:
                item = {'id': i, 'text': output[i].text}
                cost = len(json.dumps(item, ensure_ascii=False))
                if pending and budget + cost > 45000:
                    batches.append(pending); pending = []; budget = len(config.custom_prompt_text)
                pending.append(item); budget += cost
            if pending:
                batches.append(pending)
            for batch in batches:
                try:
                    result = ask([{'role': 'system', 'content':
                        'Correct captions only according to customer requirements. Preserve language, meaning, speaker, '
                        'IDs and caption count. Never add or remove spoken content. Return JSON {"captions":[{"id":0,"text":"..."}]}.'},
                        {'role': 'user', 'content': json.dumps({'requirements': config.custom_prompt_text,
                                                              'captions': batch}, ensure_ascii=False)}])
                    entries = result.get('captions', [])
                    if len(entries) != len(batch) or {x.get('id') for x in entries} != {x['id'] for x in batch}:
                        raise ValueError('提示词校正改变了条目编号')
                    if any(not isinstance(x.get('text'), str) or not x['text'].strip() for x in entries):
                        raise ValueError('提示词校正返回空文字')
                    for entry in entries:
                        cue = output[entry['id']]
                        if entry['text'] != cue.text:
                            report['changes'].append({'id': entry['id'], 'before': cue.text, 'after': entry['text']})
                            cue.text = entry['text']
                except Exception as error:
                    report['warnings'].append('客户提示词校正未完成，保留剧本纠错结果：' + str(error))
                    break
        else:
            report['warnings'].append('提示词超过15000字符，跳过附加改写，保留纠错结果')

    report['status'] = 'completed_with_review' if report['review_items'] or report['warnings'] else 'completed'
    report['summary'] = (f'剧本可靠匹配 {len({m["script_id"] for m in accepted_list})} 句；未匹配剧本 {len(report["unmatched"])} 句（其中重复条目 {len(report.get("duplicate_script_rows", []))} 句）；'
                         f'保留原转录 {sum(v["kind"]=="保留语音转录" for v in report["review_items"])} 段；GPT语义匹配，未使用字面相似度拦截；未强行添加台词')
    _save_report(report_path, report)
    data.segments = output
    data.script_review_report = report
    return data
