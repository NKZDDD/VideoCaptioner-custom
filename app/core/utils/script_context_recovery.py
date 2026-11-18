"""Recover explicit script substrings from unused neighboring ASR words."""
import re
from difflib import SequenceMatcher
from app.core.utils.script_reorder_recovery import _key


def recover_context(rows, segments, matches):
    used = {i for m in matches for i in range(m['start_id'], m['end_id'] + 1)}
    keys = [_key(row) for row in rows]
    tokens = [list(re.finditer(r'\S+', row)) for row in rows]
    recovered = []
    budget = 0
    for start in range(len(segments)):
        if start in used:
            continue
        previous = [m for m in matches + recovered if m['end_id'] < start]
        following = [m for m in matches + recovered if m['start_id'] > start]
        neighbors = ([max(previous, key=lambda m:m['end_id'])] if previous else []) + ([min(following, key=lambda m:m['start_id'])] if following else [])
        if not neighbors:
            continue
        # Bound candidates by local story context, never by an episode-specific name.
        near = {sid for m in neighbors for sid in range(max(0,m['script_id']-1), min(len(rows),m['script_id']+2))}
        candidates = []
        speech = ''
        for end in range(start, min(len(segments), start + 35)):
            if end in used or (end > start and segments[end].start_time-segments[end-1].end_time >= 1000):break
            if segments[end].end_time-segments[start].start_time > 16000:break
            speech += _key(segments[end].text)
            if len(speech) < 7:continue
            for sid in near:
                tt=tokens[sid]
                for lo in range(len(tt)):
                    for hi in range(lo+2,len(tt)+1):
                        text=rows[sid][tt[lo].start():tt[hi-1].end()];key=_key(text)
                        if len(key)<7 or min(len(key),len(speech))/max(len(key),len(speech)) < .78:continue
                        budget+=1
                        if budget>160000:return recovered
                        if SequenceMatcher(None,_key(segments[start].text),_key(tt[lo].group()),autojunk=False).ratio()<.60:continue
                        if SequenceMatcher(None,_key(segments[end].text),_key(tt[hi-1].group()),autojunk=False).ratio()<.60:continue
                        score=SequenceMatcher(None,speech,key,autojunk=False).ratio()
                        if score < .85:continue
                        negative={'ما','لا','مو','مش','لن','لم','not','never'}
                        heard_negative=sum(_key(v.text) in negative for v in segments[start:end+1])
                        script_negative=sum(_key(t.group()) in negative for t in tt[lo:hi])
                        if heard_negative != script_negative:continue
                        m=dict(script_id=sid,start_id=start,end_id=end,confidence=score,script_word_start=lo,script_word_end=hi,match_method='adjacent_context_recovery')
                        candidates.append((score + min(hi-lo,12)*.012, m, key))
        candidates.sort(key=lambda v:v[0],reverse=True)
        if not candidates:continue
        eligible=[v for v in candidates if v[1]['confidence']>=candidates[0][1]['confidence']-.04]
        chosen=max(eligible,key=lambda v:(v[1]['end_id']-v[1]['start_id'],v[1]['script_word_end']-v[1]['script_word_start'],v[0]))
        _,best,key=chosen
        rivals=[v for v in candidates[1:] if v[1]['script_id']!=best['script_id'] and v[2]!=key]
        if rivals and candidates[0][0]-rivals[0][0]<.06:continue
        # A strong literal repeat is allowed; merely reusing a script ID is not evidence.
        recovered.append(best)
        used.update(range(best['start_id'],best['end_id']+1))
    return recovered


def expand_boundaries(rows,segments,matches,validate):
    """Attach a noisy boundary word only to an already strongly anchored phrase."""
    results=[dict(m) for m in matches];changes=[]
    for index,m in enumerate(results):
        if 'script_word_start' not in m:continue
        tokens=list(re.finditer(r'\S+',rows[m['script_id']]))
        lo0,hi0=m['script_word_start'],m['script_word_end']
        used={i for j,x in enumerate(results) if j!=index for i in range(x['start_id'],x['end_id']+1)}
        best=None
        for start in range(max(0,m['start_id']-4),m['start_id']+1):
            for end in range(m['end_id'],min(len(segments),m['end_id']+5)):
                if any(i in used for i in range(start,end+1)):continue
                if any(b.start_time-a.end_time>=1000 for a,b in zip(segments[start:end],segments[start+1:end+1])):continue
                heard=''.join(_key(v.text) for v in segments[start:end+1])
                for lo in range(max(0,lo0-3),lo0+1):
                    for hi in range(hi0,min(len(tokens),hi0+3)+1):
                        if lo==lo0 and hi==hi0:continue
                        if (lo<lo0 and start==m['start_id']) or (hi>hi0 and end==m['end_id']):continue
                        excerpt=rows[m['script_id']][tokens[lo].start():tokens[hi-1].end()];key=_key(excerpt)
                        if min(len(key),len(heard))/max(len(key),len(heard))<.78:continue
                        score=SequenceMatcher(None,heard,key,autojunk=False).ratio()
                        if score<.85:continue
                        strong_core=(hi0-lo0>=3 and float(m.get('confidence',0))>=.95)
                        left_attached=(strong_core and lo0-lo==1 and m['start_id']-start==1 and hi==hi0)
                        right_attached=(strong_core and hi-hi0==1 and end-m['end_id']==1 and lo==lo0)
                        if not left_attached and SequenceMatcher(None,_key(segments[start].text),_key(tokens[lo].group()),autojunk=False).ratio()<.6:continue
                        if not right_attached and SequenceMatcher(None,_key(segments[end].text),_key(tokens[hi-1].group()),autojunk=False).ratio()<.6:continue
                        negative={'ما','لا','مو','مش','لن','لم','not','never'}
                        if sum(_key(v.text) in negative for v in segments[start:end+1]) != sum(_key(t.group()) in negative for t in tokens[lo:hi]):continue
                        proposal=dict(m,start_id=start,end_id=end,script_word_start=lo,script_word_end=hi,confidence=score,match_method='anchored_boundary_expansion')
                        if validate(proposal,rows,segments):continue
                        rank=(hi-lo,score)
                        if best is None or rank>best[0]:best=(rank,proposal)
        if best:
            results[index]=best[1];changes.append(best[1])
    return sorted(results,key=lambda m:m['start_id']),changes
