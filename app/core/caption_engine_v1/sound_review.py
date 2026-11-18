"""Quarantine uncertain ASR sound tokens, without claiming acoustic recognition."""
import copy,re,unicodedata

POLICY_VERSION='unmatched-sound-review-v1'

def token(text):
 if not isinstance(text,str):return ''
 return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))

def looks_like_sound(text):
 clean=token(text)
 return bool(re.fullmatch(r'(?:哈{2,}|呵{2,}|咳+|呃+|嘶+|嗬+|呼{2,}|笑声|咳嗽|喘息|呻吟)',clean))

def canonical_sound_tokens(reference):
 result=set()
 for row in (reference or '').splitlines():
  text=row.split('：',1)[-1].strip()
  if looks_like_sound(text):result.add(token(text))
 return result

def quarantine_unmatched_sounds(data,report,reference=''):
 """Only unconfirmed, standalone, non-script sound tokens are excluded.

 Keep all source evidence and require listening. Never label the sound as
 laughter/pain, replace it by another interjection, or remove mixed dialogue.
 """
 explicit=canonical_sound_tokens(reference)
 explicit.update(token(i.get('text','')) for i in report.get('review_items',[])
                 if isinstance(i,dict) and i.get('kind')=='剧本未匹配' and looks_like_sound(i.get('text','')))
 kept=[];quarantined=[]
 for cue in data.segments:
  if (getattr(cue,'script_corrected',False) or not looks_like_sound(cue.text)
    or token(cue.text) in explicit):
   kept.append(cue);continue
  quarantined.append(dict(kind='疑似非台词声音待确认',start_ms=cue.start_time,end_ms=cue.end_time,
    text=cue.text,source='unmatched_asr',acoustic_verdict='unknown',
    action='excluded_from_subtitle_pending_review',
    reason='未匹配剧本的独立拟声识别，可能是疼痛、喘息、笑声或误识别；未听检，暂不写入成片字幕。原识别与时间已保留，未改成其他声音文字。'))
 sound_report=report.setdefault('sound_review',{'policy_version':POLICY_VERSION,'quarantined':[]})
 sound_report['quarantined'].extend(copy.deepcopy(quarantined))
 if not quarantined:return data
 remaining=[]
 for item in report.get('review_items',[]):
  duplicate=(item.get('kind') in ('剧本外或未匹配原声','复核后仍未匹配') and any(
    (item.get('start_ms'),item.get('end_ms'),token(item.get('text','')))==
    (q['start_ms'],q['end_ms'],token(q['text'])) for q in quarantined))
  if not duplicate:remaining.append(item)
 report['review_items']=remaining+quarantined
 data.segments=kept
 if not kept:raise ValueError('识别结果仅有疑似非台词声音，已留核对记录；未生成空字幕成片')
 return data
