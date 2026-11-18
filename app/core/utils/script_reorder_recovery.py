"""Conservative recovery of complete script rows in unused native word spans."""
import re
from difflib import SequenceMatcher


def _key(text):
    text = re.sub('[\u064b-\u065f\u0670\u0640]', '', text)
    text = text.translate(str.maketrans({'أ':'ا','إ':'ا','آ':'ا','ى':'ي'}))
    return ''.join(c for c in text.casefold() if c.isalnum())


def recover_local_order(rows, segments, accepted, report, validate):
    used = {i for m in accepted.values() for i in range(m['start_id'], m['end_id']+1)}
    candidates = []
    keys = [_key(row) for row in rows]
    checks = 0
    report['order_recoveries'] = []
    for sid, row in enumerate(rows):
        if sid in accepted or len(row.split()) < 2 or len(keys[sid]) < 8:
            continue
        # Identical script rows cannot be disambiguated from text alone.
        if keys.count(keys[sid]) != 1:
            continue
        possible = []
        for start in range(len(segments)):
            if start in used:
                continue
            left = [m for m in accepted.values() if m['end_id'] < start]
            right = [m for m in accepted.values() if m['start_id'] > start]
            neighbors = ([max(left,key=lambda m:m['end_id'])] if left else []) + (
                [min(right,key=lambda m:m['start_id'])] if right else [])
            if not neighbors or min(abs(sid-m['script_id']) for m in neighbors) > 6:
                continue
            speech = ''
            for end in range(start, min(len(segments),start+60)):
                if end in used or (end > start and segments[end].start_time-segments[end-1].end_time >= 1000):
                    break
                if segments[end].end_time-segments[start].start_time > 20000:
                    break
                speech += _key(segments[end].text)
                if len(speech) > len(keys[sid])/0.85:
                    break
                if min(len(speech),len(keys[sid]))/max(len(speech),len(keys[sid])) < .85:
                    continue
                checks += 1
                if checks > 120000:
                    report['warnings'].append('局部顺序恢复达到比较上限，其余保留待检查')
                    return
                score=SequenceMatcher(None,speech,keys[sid],autojunk=False).ratio()
                if score < .88:
                    continue
                match=dict(script_id=sid,start_id=start,end_id=end,confidence=score,
                           match_method='local_order_recovery')
                if not validate(match,rows,segments):
                    possible.append(match)
        possible.sort(key=lambda m:m['confidence'],reverse=True)
        if not possible:
            continue
        best=possible[0]
        independent=[m for m in possible[1:] if m['end_id'] < best['start_id'] or m['start_id'] > best['end_id']]
        if independent and best['confidence']-independent[0]['confidence'] < .06:
            continue
        candidates.append(best)
    for match in sorted(candidates,key=lambda m:m['confidence'],reverse=True):
        ids=set(range(match['start_id'],match['end_id']+1))
        # Two different rows competing for one interval need manual review.
        rivals=[m for m in candidates if m['script_id']!=match['script_id'] and
                not(m['end_id']<match['start_id'] or m['start_id']>match['end_id']) and
                abs(m['confidence']-match['confidence']) < .06]
        if ids & used or rivals:
            continue
        accepted[match['script_id']]=match
        used.update(ids)
        report['order_recoveries'].append(dict(match,
            start_ms=segments[match['start_id']].start_time,
            end_ms=segments[match['end_id']].end_time))
