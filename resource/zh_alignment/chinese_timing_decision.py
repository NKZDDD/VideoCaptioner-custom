"""Conservative acceptance for suspect leading native units; no per-episode rules."""
import re

def key(text):return ''.join(re.findall(r'[\u3400-\u9fff]',text))

def decide(words,first,second):
    reject=lambda reason: {'apply':False,'reason':reason}
    if len(words)<2:return reject('No next native boundary')
    a,b=words[:2]
    if len(key(a['text']))!=1 or a['end_ms']-a['start_ms']<100 or b['start_ms']-a['end_ms']<400:
        return reject('Not an isolated leading native unit')
    target=''.join(key(w['text']) for w in words)
    if not target or len(target)<3:return reject('Insufficient textual context')
    pairs=[]
    for evidence in (first,second):
        spans=evidence.get('character_spans',[])
        if ''.join(x['text'] for x in spans)!=target:return reject('Alignment text mismatch')
        full_supported=target in key(evidence.get('independent_decode',''))
        strong_prefix=full_supported and min(x['acoustic_score'] for x in spans[:2])>=.8 and min(x['acoustic_score'] for x in spans)>=.3
        if not full_supported:return onset_decision(words,first,second)
        if min(x['acoustic_score'] for x in spans)<.5 and not strong_prefix:return reject('Weak acoustic support')
        pairs.append(spans)
    if any(abs(a['start_ms']-b['start_ms'])>80 for a,b in zip(*pairs)):
        return reject('Different context windows disagree')
    start=pairs[0][0]['start_ms']
    if not 250<=start-a['start_ms']<=2000:return reject('Shift outside conservative correction range')
    if not a['start_ms']<start<b['start_ms']-20:return reject('Correction conflicts with next native boundary')
    # Only repair the suspicious prefix. Keep the following native unit boundaries.
    # End at the existing next-unit boundary so no early orphan cue remains.
    return {'apply':True,'start_ms':start,'end_ms':b['start_ms'],
        'reason':'Two acoustic windows agree and both independent decodes support the full phrase',
        'scope':'leading_native_unit_only','estimated':False,'human_verified':False}

def onset_decision(words,first,second):
    """Timing-only alternative when a faint prefix is misdecoded but body agrees."""
    a,b=words[:2];target=''.join(key(w['text']) for w in words)
    reject=lambda reason:dict(apply=False,reason=reason)
    if len(target)<6:return reject('Insufficient body for independent onset check')
    starts=[]
    for evidence in (first,second):
        if target[2:] not in key(evidence.get('independent_decode','')):return reject('Independent body does not support onset review')
        emissions=[e for e in evidence.get('emissions',[]) if key(e['text']) and a['start_ms']<=e['start_ms']<b['start_ms']]
        if not emissions or emissions[0]['score']<.8:return reject('No strong independent prefix onset')
        starts.append(emissions[0]['start_ms'])
    start=starts[0]
    if abs(starts[0]-starts[1])>80 or not 250<=start-a['start_ms']<=2000 or not start<b['start_ms']-20:return reject('Independent onset uncertain or conflicts with native boundary')
    return dict(apply=True,start_ms=start,end_ms=b['start_ms'],scope='leading_native_unit_only',estimated=False,human_verified=False,reason='Two independent acoustic onsets agree; script/source text retained without phonetic rewriting')
