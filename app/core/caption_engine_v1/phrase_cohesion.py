"""Recover exact short script phrases only between confirmed speaker anchors."""
import copy,unicodedata,re

def key(text):
 return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))

def repair_exact_script_phrases(data,report,script_speakers=None):
 if not script_speakers:return data
 pending=[i for i in report.get('review_items',[]) if i.get('kind')=='剧本未匹配' and type(i.get('script_id')) is int]
 cues=data.segments;repairs=[];used=set();output=[];cursor=0
 for_start={}
 for row in pending:
  target=key(row.get('text',''));sid=row['script_id']
  if not 2<=len(target)<=11 or not re.fullmatch(r'[\u3400-\u9fff]+',target):continue
  candidates=[]
  for a in range(1,len(cues)-1):
   prev=cues[a-1];prev_id=getattr(prev,'script_row_id',None);owner=getattr(prev,'script_speaker',None)
   if type(prev_id)is not int or not owner or prev_id>=sid or not getattr(prev,'script_corrected',False) or script_speakers.get(sid)!=owner:continue
   text=''
   for b in range(a,min(a+8,len(cues)-1)):
    cue=cues[b]
    if getattr(cue,'script_corrected',False):break
    if b>a and not 0<=cue.start_time-cues[b-1].end_time<1000:break
    # Unknown acoustic IDs are allowed only because both script anchors agree.
    # Known acoustic IDs must be consistent within the complete phrase.
    if b>a:
     earlier={str(next((getattr(c,k) for k in ('speaker_id','speaker') if getattr(c,k,None) not in (None,'',-1)),'')) for c in cues[a:b+1]}
     earlier.discard('')
     if len(earlier)>1:break
    text+=key(cue.text)
    if not target.startswith(text):break
    if text!=target:continue
    following=cues[b+1];next_id=getattr(following,'script_row_id',None)
    if (b>a and type(next_id)is int and prev_id<sid<next_id
      and getattr(following,'script_speaker',None)==owner and getattr(following,'script_corrected',False)
      and cue.end_time-cues[a].start_time<=4500
      and any(len(key(c.text))==1 for c in cues[a:b+1])):
     acoustic={str(next((getattr(c,k) for k in ('speaker_id','speaker') if getattr(c,k,None) not in (None,'',-1)),'')) for c in [prev,*cues[a:b+1],following]}
     acoustic.discard('')
     if len(acoustic)<=1:candidates.append((a,b,owner))
  # Ambiguous occurrences or a script row already recovered stay unresolved.
  if len(candidates)==1:
   a,b,owner=candidates[0]
   if a not in for_start:for_start[a]=(b,row,owner)
   else:for_start[a]=None
 while cursor<len(cues):
  found=for_start.get(cursor)
  if not found:output.append(cues[cursor]);cursor+=1;continue
  b,row,owner=found
  if row['script_id'] in used:output.append(cues[cursor]);cursor+=1;continue
  span=cues[cursor:b+1];part=copy.deepcopy(span[0]);part.text=row['text']
  part.end_time=span[-1].end_time;part.script_speaker=owner;part.script_row_id=row['script_id']
  part.script_corrected=True;part.whole_script_phrase=True
  part.alignment_units=[dict(text=c.text,start_ms=c.start_time,end_ms=c.end_time) for c in span]
  repair=dict(kind='完整短语恢复待听检',script_id=row['script_id'],start_ms=part.start_time,end_ms=part.end_time,
    before=[c.text for c in span],after=part.text,
    source_spans=[dict(text=c.text,start_ms=c.start_time,end_ms=c.end_time) for c in span],
    reason='完整剧本短语与相邻原声逐字一致；前后已确认同一剧本说话人；仅用原声首尾边界合成一条，未猜时间，仍需听检')
  repairs.append(repair);output.append(part);used.add(row['script_id']);cursor=b+1
 if not repairs:return data
 remaining=[];resolved=[]
 for item in report.get('review_items',[]):
  covered=(item.get('kind')=='剧本未匹配' and item.get('script_id') in used)
  if item.get('kind')=='剧本外或未匹配原声':
   covered=any(r['start_ms']<=item.get('start_ms',-1)<item.get('end_ms',-1)<=r['end_ms'] for r in repairs)
  (resolved if covered else remaining).append(item)
 report['review_items']=remaining+repairs
 report.setdefault('resolved_fragment_evidence',[]).extend(resolved)
 report.setdefault('exact_phrase_repairs',[]).extend(repairs)
 report['program_recovered_script_ids']=sorted(used)
 data.segments=output
 return data
