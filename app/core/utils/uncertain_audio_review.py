"""Bounded two-context ASR review. Never invent times or overwrite script cues."""
import json
import math
import os
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path


def key(text):
    import unicodedata
    text=unicodedata.normalize('NFKC',text).casefold()
    text=re.sub('[\u064b-\u065f\u0670\u0640]','',text)
    text=text.translate(str.maketrans({'أ':'ا','إ':'ا','آ':'ا','ى':'ي'}))
    return ''.join(c for c in text if c.isalnum())


def target_words(response, offset, first, last):
    words=[]
    for seg in response.get('segments',[]):
        for w in seg.get('words',[]):
            a,b=offset+float(w['start']),offset+float(w['end'])
            p=float(w.get('probability',0))
            if not all(map(math.isfinite,(a,b,p))) or b<=a or not 0<=p<=1:
                continue
            overlap=max(0,min(last,b)-max(first,a))
            if overlap/(b-a)>=.8:
                words.append((w['word'].strip(),p))
    return ' '.join(t for t,p in words if t), min((p for t,p in words if t),default=0)


def decide(original, votes, reference):
    if len(votes)!=2 or any(p<.75 or len(key(t))<6 for t,p in votes):
        return None, '局部识别证据不足'
    a,b=key(votes[0][0]),key(votes[1][0])
    if SequenceMatcher(None,a,b,autojunk=False).ratio()<.97:
        return None, '两种上下文识别不一致'
    if SequenceMatcher(None,key(original),a,autojunk=False).ratio()<.55:
        return None, '与原识别差异过大，需人工核对'
    candidates={}
    count=0
    for row in reference:
        tokens=list(re.finditer(r'\S+',row))
        for start in range(len(tokens)):
            for end in range(start+2,min(len(tokens),start+30)+1):
                text=row[tokens[start].start():tokens[end-1].end()]
                normalized=key(text)
                if not normalized:continue
                if len(normalized)>len(a)/.8:break
                if len(normalized)<len(a)*.8:continue
                count+=1
                if count>20000:return None,'剧本文字候选过多，保留待检查'
                score=SequenceMatcher(None,a,normalized,autojunk=False).ratio()
                if score>=.9:
                    candidates[text]=max(score,candidates.get(text,0))
    ranked=sorted(candidates.items(),key=lambda x:x[1],reverse=True)
    if ranked:
        best,score=ranked[0]
        # Overlapping alternative slices still count as ambiguity.
        if len(ranked)>1 and score-ranked[1][1]<.06:
            return None,'对应剧本范围不唯一'
        return best,'两种上下文一致，使用对应剧本原文'
    # An off-script line may be real, but repeated ASR alone is not verification.
    return None,'两种上下文一致但剧本无唯一对应，保留原文并提供候选'


def review_uncertain_audio(data, video_path, subtitle_path, reference, options, progress=None):
    from app.core.utils.adaptive_script import _save_report
    import requests
    report=getattr(data,'script_review_report',{})
    items=[i for i in report.get('review_items',[]) if i['kind']=='保留语音转录']
    if not items:return
    audit=report.setdefault('audio_rechecks',[])
    base=str(options.get('base_url','')).strip().rstrip('/')
    if not base or not options.get('model') or not Path(subtitle_path).with_suffix('.asr.json').is_file():
        report['warnings'].append('局部音频复核跳过：缺少云端设置或原始词时间文件')
        return
    # No audio is uploaded until the user's configured service is ready.
    with requests.Session() as session:
        session.trust_env=False
        try:
            health=session.get(base.removesuffix('/v1')+'/health',timeout=(3,5),allow_redirects=False)
            if health.status_code!=200 or health.json().get('status')!='ready':raise ValueError()
        except (requests.RequestException,ValueError):
            report['warnings'].append('局部音频复核未执行：云端不可用；保留已有字幕，可稍后重试')
            return
        headers={'Authorization':'Bearer '+options['api_key']} if options.get('api_key') else {}
        for item in items[:3]:
            first,last=item['start_ms']/1000,item['end_ms']/1000
            record={'start_ms':item['start_ms'],'end_ms':item['end_ms'],'before':item['text'],'applied':False}
            audit.append(record)
            if last<=first or last-first>20:
                record['reason']='区间无效或超过20秒，保留待检查';continue
            if progress:progress('正在局部音频复核，每条最多两种上下文')
            votes=[]
            try:
                with tempfile.TemporaryDirectory(prefix='kaka_audio_review_') as temp:
                    for padding in (.6,1.8):
                        offset=max(0,first-padding)
                        audio=Path(temp)/'clip.wav'
                        run=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss',str(offset),'-i',str(video_path),
                            '-t',str(last-offset+padding),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-y',str(audio)],
                            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        if run.returncode:raise ValueError('截取局部音频失败')
                        with audio.open('rb') as f:
                            result=session.post(base+'/audio/transcriptions',headers=headers,
                                data={'model':options['model'],'language':options.get('language') or 'ar','response_format':'verbose_json'},
                                files={'file':('clip.wav',f,'audio/wav')},timeout=(5,90),allow_redirects=False)
                        if result.status_code!=200:raise ValueError('局部转录失败 HTTP '+str(result.status_code))
                        votes.append(target_words(result.json(),offset,first,last))
                replacement,reason=decide(item['text'],votes,reference)
                record['candidates']=[{'text':t,'min_word_probability':p} for t,p in votes]
                record['reason']=reason
                if replacement and replacement!=item['text']:
                    cues=[s for s in data.segments if s.start_time==item['start_ms'] and s.end_time==item['end_ms'] and s.text==item['text']]
                    if len(cues)==1:
                        cues[0].text=replacement
                        item['kind']='局部音频复核纠错';item['text']=replacement;item['reason']=reason
                        record['applied']=True;record['after']=replacement
            except (requests.RequestException,ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired) as error:
                record['reason']=str(error) if not isinstance(error,requests.RequestException) else '云端请求失败或超时，未自动重试'
                report['warnings'].append('局部音频复核中断；保留已有字幕，其余未上传')
                break
    if len(items)>3:report['warnings'].append('本次最多复核3处，其他片段继续保留待检查')
    report['summary']+='；局部音频复核修改 '+str(sum(bool(r['applied']) for r in audit))+' 处'
    data.script_review_report=report
