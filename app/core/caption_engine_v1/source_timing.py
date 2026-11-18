import copy,math,unicodedata

def speaker(cue):
    for key in ('speaker_id', 'speaker', 'script_speaker'):
        value = getattr(cue, key, None)
        if value not in (None, '', -1):
            return str(value)
    return None

def normalize_source_timing(segments):
    """Fold only punctuation metadata exactly at a preceding audio boundary.

 Speech times are never estimated, trimmed or reordered. All other invalid
 spans still fail before requesting a model. Input objects remain unchanged.
 """
    output = []
    repairs = []
    previous = -1
    for index, cue in enumerate(copy.deepcopy(segments)):
        a, b = (cue.start_time, cue.end_time)
        if not all((type(v) in (int, float) and math.isfinite(v) for v in (a, b))) or a < 0:
            raise ValueError('原声时间存在重叠、倒序或无效区间，请先校验时间；未替换剧本')
        if a == b and isinstance(cue.text, str) and cue.text and all((unicodedata.category(c).startswith('P') for c in cue.text)):
            owner = output[-1] if output else None
            if owner is not None and a == owner.end_time and (not (speaker(cue) is not None and speaker(owner) is not None and (speaker(cue) != speaker(owner)))):
                owner.text += cue.text
                repairs.append(dict(source_id=index, text=cue.text, start_ms=a, end_ms=b, target_id=len(output) - 1, action='零时长标点归并到相同结束边界的前一句；未改时间'))
                continue
        if b <= a or a < previous:
            raise ValueError('原声时间存在重叠、倒序或无效区间，请先校验时间；未替换剧本')
        output.append(cue)
        previous = b
    return (output, repairs)
