"""Recover independently timed remaining ranges of a partially matched row."""
import re
from difflib import SequenceMatcher
from app.core.utils.script_reorder_recovery import _key


def recover_continuations(rows, segments, matches, validate):
    used={i for m in matches for i in range(m['start_id'],m['end_id']+1)}
    output=[]
    for sid,row in enumerate(rows):
        own=[m for m in matches if m['script_id']==sid]
        if not own or any('script_word_start' not in m for m in own):
            continue
        tokens=list(re.finditer(r'\S+',row))
        occupied={i for m in own for i in range(m['script_word_start'],m['script_word_end'])}
        runs=[]; first=None
        for i in range(len(tokens)+1):
            if i<len(tokens) and i not in occupied:
                if first is None:first=i
            elif first is not None:
                runs.append((first,i));first=None
        for lo,hi in runs:
            if hi-lo<2:continue
            excerpt=row[tokens[lo].start():tokens[hi-1].end()]
            key=_key(excerpt)
            if len(key)<6:continue
            prior=[m for m in own if m['script_word_end']<=lo]
            following=[m for m in own if m['script_word_start']>=hi]
            lower=segments[max(prior,key=lambda m:m['script_word_end'])['end_id']].end_time if prior else max(0,min(segments[m['start_id']].start_time for m in own)-30000)
            upper=segments[min(following,key=lambda m:m['script_word_start'])['start_id']].start_time if following else max(segments[m['end_id']].end_time for m in own)+30000
            # Do not move a row continuation beyond a different matched row.
            for other in matches:
                if other['script_id']==sid:continue
                a,b=segments[other['start_id']].start_time,segments[other['end_id']].end_time
                if prior and a>=lower:upper=min(upper,a)
                if following and b<=upper:lower=max(lower,b)
            candidates=[]
            for start in range(len(segments)):
                if start in used or segments[start].start_time<lower or segments[start].start_time>=upper:continue
                speech=''
                for end in range(start,min(len(segments),start+60)):
                    if end in used or segments[end].end_time>upper:break
                    if end>start and segments[end].start_time-segments[end-1].end_time>=1000:break
                    speech+=_key(segments[end].text)
                    if len(speech)>len(key)/.8:break
                    if min(len(key),len(speech))/max(len(key),len(speech))<.8:continue
                    score=SequenceMatcher(None,speech,key,autojunk=False).ratio()
                    if score<.84:continue
                    m=dict(script_id=sid,start_id=start,end_id=end,confidence=score,
                        script_word_start=lo,script_word_end=hi,match_method='script_continuation')
                    if not validate(m,rows,segments):candidates.append(m)
            candidates.sort(key=lambda m:m['confidence'],reverse=True)
            if not candidates:continue
            best=candidates[0]
            rivals=[m for m in candidates[1:] if m['end_id']<best['start_id'] or m['start_id']>best['end_id']]
            if rivals and best['confidence']-rivals[0]['confidence']<.06:continue
            # Identical wording elsewhere in the script requires review.
            if any(other_sid!=sid and key in _key(other) for other_sid,other in enumerate(rows)):continue
            output.append(best)
            used.update(range(best['start_id'],best['end_id']+1))
    return output
