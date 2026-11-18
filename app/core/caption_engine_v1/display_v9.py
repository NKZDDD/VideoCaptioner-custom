"""Legal subtitle pages from real source edges; no proportional timestamps."""
import copy,re,math
from functools import lru_cache
from .anchors import normalized

NUMBER=r'零〇一二两三四五六七八九十百千万亿0-9'
UNITS='元块斤克吨米箱套个岁年月日点分秒％%'
def units(text):return len(re.findall(r'[\u3400-\u9fff]|[A-Za-z0-9]+',text))

@lru_cache(maxsize=256)
def lexical_ranges(text):
 from app.core.subtitle_processor.split import _lexical_token_spans
 return tuple((a,b) for a,b,word in _lexical_token_spans(text))

def protected_cut(text,index):
 left,right=text[:index],text[index:]
 if re.search('['+NUMBER+']$',left) and re.match('['+NUMBER+UNITS+']',right):return True
 # A complete numeric expression must stay with its unit. Never split it
 # merely to meet a preferred length.
 return any(a<index<b for a,b in lexical_ranges(text))

def safe_cut(text,index):
 if index==len(text):return True
 if not 0<index<len(text) or protected_cut(text,index):return False
 # A continuation particle belongs to its preceding phrase unless a real
 # sentence mark already separates it. This is syntax, not an episode word list.
 if re.match(r'^[的地得着过]',text[index:]) and not any(a==index and b>index+1 for a,b in lexical_ranges(text)) and not re.search(r'[。！？!?；;]\s*$',text[:index]):return False
 return True

def required_cuts(cue,boundaries):
 text=cue.text
 return {i for i,e in boundaries.items() if 0<i<len(text)
  and 'start' in e and 'end' in e and safe_cut(text,i)
  and text[i] not in '。！？!?；;，,'
  and (text[i-1] in '。！？!?；;' or e['start']-e['end']>=500)}

def combine_unmatched(data,original,report):
 grouped=[];merged=0
 for cue in data.segments:
  part=copy.deepcopy(cue)
  if grouped:
   prev=grouped[-1]
   fallback=not getattr(prev,'script_corrected',False) and not getattr(part,'script_corrected',False)
   origin=getattr(part,'asr_segment_id',None)
   same_origin=origin is not None and origin==getattr(prev,'asr_segment_id',None)
   same_speaker=getattr(prev,'speaker_id',None)==getattr(part,'speaker_id',None)
   adjacent=0<=part.start_time-prev.end_time<400
   if fallback and same_origin and same_speaker and adjacent and not re.search(r'[。！？!?]$',prev.text.strip()) and part.end_time-prev.start_time<=6000:
    separator=' ' if re.search(r'[A-Za-z]$',prev.text) and re.match(r'[A-Za-z]',part.text) else ''
    prev.alignment_units=getattr(prev,'alignment_units',[dict(text=prev.text,start_ms=prev.start_time,end_ms=prev.end_time)])+[dict(text=part.text,start_ms=part.start_time,end_ms=part.end_time)]
    prev.text+=separator+part.text;prev.end_time=part.end_time;merged+=1;continue
  grouped.append(part)
 data.segments=grouped;report['fallback_word_merges']=merged
 return data

def legal_pages(cue,original,limit,boundary_map):
 text=cue.text.replace('\n','');source=copy.deepcopy(cue);source.text=text
 boundaries=boundary_map(source,original)
 required=required_cuts(source,boundaries)
 allowed=[0]+[k for k,v in boundaries.items() if 0<k<len(text) and 'start' in v and 'end' in v and safe_cut(text,k)]+[len(text)]
 allowed=sorted(set(allowed));best={0:(0,[])}
 for finish in allowed[1:]:
  candidates=[]
  for start in reversed(allowed):
   if start>=finish or start not in best:continue
   if any(start<i<finish for i in required):continue
   fragment=text[start:finish]
   if units(fragment)>limit:continue
   a=cue.start_time if start==0 else boundaries[start]['start']
   b=cue.end_time if finish==len(text) else boundaries[finish]['end']
   if b<=a or b-a>7000:continue
   previous=best[start][1]
   if previous and a<previous[-1].end_time:continue
   # Meaning and readability are scored only among real audio edges.
   cost=2.0
   if len(normalized(fragment))<=1:cost+=20
   if b-a<500:cost+=(500-(b-a))/30
   if re.search(r'[，。！？；：,!?;:]$',fragment):cost-=1.5
   elif re.search(r'(?:的|了|是|不|把|给|在|和|与|将|全成)$',fragment):cost+=5
   if re.match(r'^[的了着过]',text[finish:]):cost+=5
   part=copy.deepcopy(cue);part.text=fragment;part.start_time=a;part.end_time=b
   candidates.append((best[start][0]+cost,previous+[part]))
  if candidates:best[finish]=min(candidates,key=lambda v:v[0])
 return best.get(len(text),(0,None))[1]

def repair_pages(data,original,config,report,boundary_map):
 limit=config['subtitle_config'].get('max_word_count_cjk',12);output=[];blocked=[]
 runs=[];run=[]
 def flush():
  if len(run)>=3:
   part=copy.deepcopy(run[0]);part.text=''.join(c.text for c in run);part.end_time=run[-1].end_time;runs.append(part)
   report['single_char_run_repairs']=report.get('single_char_run_repairs',0)+1
  else:runs.extend(run)
  run.clear()
 for cue in data.segments:
  short=len(normalized(cue.text))<=1 and cue.end_time-cue.start_time<500
  compatible=not run or (0<=cue.start_time-run[-1].end_time<400 and ((getattr(cue,'asr_segment_id',None) is not None and getattr(cue,'asr_segment_id',None)==getattr(run[-1],'asr_segment_id',None) and getattr(cue,'speaker_id',None)==getattr(run[-1],'speaker_id',None)) or (getattr(cue,'script_speaker','') and getattr(cue,'script_speaker','')==getattr(run[-1],'script_speaker',None))))
  if short and compatible:run.append(cue)
  else:
   flush()
   if short:run.append(cue)
   else:runs.append(cue)
 flush();data.segments=runs
 for cue in data.segments:
  if '\n' not in cue.text and units(cue.text)<=limit:
   output.append(cue);continue
  pieces=legal_pages(cue,original,limit,boundary_map)
  if pieces:
   output.extend(pieces)
   report.setdefault('display_repairs',[]).append(dict(start_ms=cue.start_time,end_ms=cue.end_time,pages=len(pieces),reason='仅用原生边界重新分页，未估算时间'))
   for item in report.get('review_items',[]):
    if item.get('kind')=='语义断句待核对' and (item.get('start_ms'),item.get('end_ms'))==(cue.start_time,cue.end_time):
     item['reason']='模型分界未通过；程序已用原声边界重新分页，仍需听检'
  else:
   output.append(cue);blocked.append(dict(kind='无法安全分页',start_ms=cue.start_time,end_ms=cue.end_time,text=cue.text,reason='没有满足长度与原声边界的分页方案，停止合成而非输出整段多行'))
 data.segments=output
 report.setdefault('review_items',[]).extend(blocked)
 return data

def export_issues(data,config):
 limit=config['subtitle_config'].get('max_word_count_cjk',12);issues=[];run=[]
 for cue in data.segments:
  if len(cue.text.splitlines())>2 or any(units(line)>limit for line in cue.text.splitlines()):
   issues.append(dict(kind='字幕显示超限',start_ms=cue.start_time,end_ms=cue.end_time,text=cue.text,reason='超长或超过两行，未合成'))
  offset=0;clean=cue.text.replace('\n','')
  for line in cue.text.splitlines()[:-1]:
   offset+=len(line)
   if protected_cut(clean,offset):issues.append(dict(kind='词组或数量被换行拆开',start_ms=cue.start_time,end_ms=cue.end_time,text=cue.text,reason='换行切断词组或数量，未合成'))
  characters=len(re.findall(r'[\u3400-\u9fff]',cue.text))
  duration=(cue.end_time-cue.start_time)/1000
  if characters>=6 and (duration<=0 or characters/duration>25):
   issues.append(dict(kind='分页阅读时长异常',start_ms=cue.start_time,end_ms=cue.end_time,text=cue.text,reason='至少6个汉字却超过每秒25字，需核对分页边界；未合成'))
  short=len(normalized(cue.text))<=1 and cue.end_time-cue.start_time<500
  if short and (not run or cue.start_time-run[-1].end_time<400):run.append(cue)
  else:
   if len(run)>=3:issues.append(dict(kind='连续逐字闪现',start_ms=run[0].start_time,end_ms=run[-1].end_time,text=''.join(c.text for c in run),reason='至少三条连续单字短字幕，未合成'))
   run=[cue] if short else []
 if len(run)>=3:issues.append(dict(kind='连续逐字闪现',start_ms=run[0].start_time,end_ms=run[-1].end_time,text=''.join(c.text for c in run),reason='至少三条连续单字短字幕，未合成'))
 return issues
