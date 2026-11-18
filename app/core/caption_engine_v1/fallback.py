import copy,re,math
from .anchors import script_excerpt

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
