"""Shared voice-mode export guard; retain text and native timing evidence."""
import copy,re

def exclude_confirmed(data,exclusions):
 """Apply verified source/time/text decisions before matching assigns IDs."""
 removed=set()
 for e in exclusions or []:
  if e.get('confirmed_by_user') is not True:continue
  a,b=e.get('start_ms'),e.get('end_ms')
  if type(a)is not int or type(b)is not int or a<0 or b<=a:raise ValueError('确认排除记录时间无效')
  candidates=[(i,c) for i,c in enumerate(data.segments)
              if c.start_time>=a-20 and c.end_time<=b+20 and c.end_time>a and c.start_time<b]
  if not candidates:continue
  if ''.join(c.text for _,c in candidates)!=e.get('text'):continue
  if abs(candidates[0][1].start_time-a)>20 or abs(candidates[-1][1].end_time-b)>20:continue
  removed.update(i for i,_ in candidates)
 data.segments=[c for i,c in enumerate(data.segments) if i not in removed]
 if not data.segments:raise ValueError('确认排除后没有有效台词，未生成空字幕')
 return data

def confirmed_for_media(media):
 import json,hashlib
 from pathlib import Path
 media=Path(media);path=media.with_suffix('.asr_exclusions.json')
 if not path.is_file():return []
 proof=json.loads(path.read_text(encoding='utf-8'))
 if hashlib.sha256(media.read_bytes()).hexdigest()!=proof.get('source_sha256'):
  raise ValueError('确认误识别记录与当前素材不符，未套用')
 return proof.get('exclusions',[])

def guard_voice(data,original,settings,report,exclusions=None):
 from .display_v9 import safe_cut,legal_pages,export_issues
 from .flow_v8 import boundary_map
 from .template_rules import compile_rules,apply_display_rules,output_issues
 from .numbers import normalize_arabic_numbers
 from .cn_policy import soften
 original=copy.deepcopy(original)
 limit=settings.get('max_word_count_cjk',12)
 report.setdefault('boundary_repairs',[])
 def source_group(cue):
  groups={getattr(s,'asr_segment_id',None) for s in original
          if s.start_time<cue.end_time and s.end_time>cue.start_time}
  return next(iter(groups)) if len(groups)==1 and None not in groups else None
 exclusions=exclusions or []
 data.segments=[c for c in data.segments if not any(
  e.get('confirmed_by_user') is True and c.text==e.get('text')
  and abs(c.start_time-e.get('start_ms',-9999))<=20
  and abs(c.end_time-e.get('end_ms',-9999))<=20 for e in exclusions)]
 report['confirmed_asr_exclusions']=exclusions
 output=[]
 for cue in data.segments:
  part=copy.deepcopy(cue)
  if output:
   prev=output[-1];text=prev.text.replace('\n','')+part.text.replace('\n','')
   group=source_group(prev)
   compatible=(group is not None and group==source_group(part)
    and getattr(prev,'speaker_id',None)==getattr(part,'speaker_id',None)
    and 0<=part.start_time-prev.end_time<=200 and part.end_time-prev.start_time<=7000)
   if settings.get('need_split',True) and compatible and not safe_cut(text,len(prev.text.replace('\n',''))):
    merged=copy.deepcopy(prev);merged.text=text;merged.end_time=part.end_time
    pieces=legal_pages(merged,original,limit,boundary_map)
    if pieces:
     output.pop();output.extend(pieces)
     report['boundary_repairs'].append(dict(start_ms=merged.start_time,end_ms=merged.end_time,
      before=[prev.text,part.text],after=[p.text for p in pieces],reason='词组边界修复；仅使用原声边界'))
     continue
  output.append(part)
 data.segments=output
 contract=compile_rules(settings,'voice')
 changes=apply_display_rules(data,contract,normalize_arabic_numbers,soften)
 issues=export_issues(data,{'subtitle_config':settings})+output_issues(data,contract,normalize_arabic_numbers,soften)
 report.update(mode='voice',rule_contract_sha256=contract['sha256'],template_changes=changes,
  export_blockers=issues,guard_version='voice-export-v17',timing_policy='native_edges_only')
 if issues:raise ValueError('原声字幕输出检查未通过：'+str(len(issues))+'处；未合成')
 return data

def guard_sidecar(data,subtitle_path,settings,report,exclusions=None):
 import json
 from pathlib import Path
 from app.core.bk_asr.asr_data import ASRDataSeg
 path=Path(subtitle_path).with_suffix('.asr.json')
 if not path.is_file():
  report['native_timing']='not_available'
  # No invented word timing. Existing segment edges remain the only evidence.
  return guard_voice(data,data.segments,settings,report,exclusions)
 raw=json.loads(path.read_text(encoding='utf-8'))
 native=[]
 for index,segment in enumerate(raw.get('segments',[])):
  rows=segment.get('words') or [dict(word=segment.get('text',''),start=segment['start'],end=segment['end'])]
  for word in rows:
   text=str(word.get('word',word.get('text',''))).strip()
   if not text:continue
   import math
   a,b=word.get('start'),word.get('end')
   if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)) or a<0 or b<a:
    raise ValueError('原生词时间无效，未推算替代时间')
   cue=ASRDataSeg(text,round(a*1000),round(b*1000));cue.asr_segment_id=index
   cue.speaker_id=word.get('speaker_id',segment.get('speaker_id'));native.append(cue)
 if not native:raise ValueError('原生词记录为空，未执行输出修复')
 return guard_voice(data,native,settings,report,exclusions)
