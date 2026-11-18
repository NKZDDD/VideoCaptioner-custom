"""Frozen delivery constraints; never create word timestamps or infer speakers."""
import copy,json,re
MARKER='; VideoCaptionerDeliveryV1: '
FIELDS={'delivery_max_line_chars':(8,200),'delivery_max_cue_chars':(8,400),'delivery_max_lines':(1,4),'delivery_pause_ms':(100,3000),'delivery_separate_languages':(1,1),'delivery_original_max_lines':(1,4),'delivery_translation_max_lines':(1,4),'delivery_translation_max_chars':(8,400),'delivery_keep_font_size':(1,1),'_translation_visual_chars':(1,400)}

def requires_translation(policy):
 return bool(policy and (policy.get('delivery_separate_languages')==1 or any(
  key in policy for key in ('delivery_translation_max_lines','delivery_translation_max_chars'))))


def read_policy(style):
 rows=[line[len(MARKER):] for line in (style or '').splitlines() if line.startswith(MARKER)]
 if not rows:return None
 if len(rows)!=1:raise ValueError('交付规则重复，未执行')
 p=json.loads(rows[0])
 if not isinstance(p,dict) or set(p)-set(FIELDS):raise ValueError('未知交付规则字段')
 for k,v in p.items():
  a,b=FIELDS[k]
  if type(v)is not int or not a<=v<=b:raise ValueError('交付规则参数无效：'+k)
 if p.get('delivery_keep_font_size')==1:
  row=next((r.split(',') for r in style.splitlines() if r.startswith('Style: Secondary,')),None)
  if not row:raise ValueError('保持字号缺少译文样式')
  canvas=re.search(r'^; VideoCaptionerScriptCanvas:\s*(\d+),\s*(\d+)',style,re.MULTILINE)
  height=int(canvas.group(2)) if canvas else 720
  # Same calibrated width used by the installed exporter; do not invent timing.
  available=(1080-174)*height/1920
  size=float(row[2])
  if size<=0:raise ValueError('译文字号无效')
  p['_translation_visual_chars']=max(1,int(available/size))
 return p

def write_policy(style,policy):
 policy={k:v for k,v in policy.items() if not k.startswith('_')}
 plain='\n'.join(s for s in style.splitlines() if not s.startswith(MARKER))
 result=plain+'\n'+MARKER+json.dumps(policy,sort_keys=True,separators=(',',':'))
 read_policy(result)
 return result

def flat(text):return re.sub(r'\s+',' ',str(text).replace('\\N',' ')).strip()
def key(text):return ''.join(c.casefold() for c in text if c.isalnum())

def wrap(text,width):
 """Only wrap on existing word boundaries; never break an English word."""
 if re.search(r'[\u3400-\u9fff]',flat(text)):
  from app.core.subtitle_processor.split import semantic_split_text
  pieces=semantic_split_text(flat(text),width,visible_chars=True)
  if all(len(p)<=width for p in pieces):return pieces
 words=flat(text).split(' ');out=[];line=''
 for word in words:
  if len(word)>width:raise ValueError('完整单词超过每行字符上限：'+word)
  if line and len(line)+1+len(word)>width:out.append(line);line=word
  else:line=(line+' '+word).strip()
 if line:out.append(line)
 return out

def delivery_text(text):
 original=flat(text)
 original=original.translate(str.maketrans({'，':',','。':'.','！':'!','？':'?','：':':','；':';','（':'(','）':')','“':'"','”':'"'}))
 original=re.sub(r'\s+([,.!?;:])',r'\1',original)
 original=re.sub(r'([,!?;:])(?=[A-Za-z])',r'\1 ',original)
 original=re.sub(r'\.(?=[A-Za-z])', '. ',original)
 return original

def cached_units(cue,native):
 """Validate a page's immutable source edges instead of realigning edited text."""
 import math
 units=getattr(cue,'_delivery_native_units',None)
 if units is None:return None
 starts={w.start_time for w in native if key(w.text) and w.end_time>w.start_time}
 ends={w.end_time for w in native if key(w.text) and w.end_time>w.start_time}
 previous=cue.start_time
 for text,a,b in units:
  if not isinstance(text,str) or not key(text) or not all(type(v) in (int,float) and math.isfinite(v) for v in (a,b)) or a<previous or b<=a or a<cue.start_time or b>cue.end_time or a not in starts or b not in ends:
   raise ValueError('分页原始边界快照无效，未重新猜测时间')
  previous=b
 if key(''.join(u[0] for u in units))!=key(cue.text):raise ValueError('分页文字与原始边界快照不一致，未重新猜测时间')
 return list(units)


def bind_units(cue,units):
 cue._delivery_native_units=tuple(tuple(u) for u in units)
 cue.alignment_units=[dict(text=t,start_ms=a,end_ms=b) for t,a,b in units]
 return cue


def timing_audit(data,native,stage):
 import math
 if not data.segments:raise ValueError(stage+'：没有有效字幕')
 starts={w.start_time for w in native};ends={w.end_time for w in native}
 previous=-1;available=0
 for cue in data.segments:
  a,b=cue.start_time,cue.end_time
  structural=all(type(v) in (int,float) and math.isfinite(v) for v in (a,b)) and a>=0 and b>a and a>=previous
  bounds=getattr(cue,'_program_time_bounds',None)
  estimated=(getattr(cue,'_display_time_estimated',False) is True and isinstance(bounds,(list,tuple)) and len(bounds)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in bounds) and bounds[0] in starts and bounds[1] in ends and bounds[0]<=a<b<=bounds[1])
  if not structural or ((a not in starts or b not in ends) and not estimated):
   raise ValueError(stage+'：字幕时间违反原始边界约定')
  previous=b
  units=cached_units(cue,native)
  if units:available+=1
 return dict(stage=stage,cues=len(data.segments),native_edges=not any(getattr(c,'_display_time_estimated',False) for c in data.segments),estimated_display_cues=sum(bool(getattr(c,'_display_time_estimated',False)) for c in data.segments),verified_page_snapshots=available)


def aligned_units(cue,native):
 """Map corrected text to existing ASR edges; an edited span remains indivisible."""
 from difflib import SequenceMatcher
 import math
 text=delivery_text(cue.text)
 saved=cached_units(cue,native)
 if saved is not None:return saved
 # Preserve confirmed match boundaries even when the desktop merges speech turns.
 alignments=getattr(cue,'alignment_units',None)
 if alignments and key(''.join(u['text'] for u in alignments))==key(text):
  output=[]
  from types import SimpleNamespace
  from app.core.utils.punctuation_boundaries import repair
  parts,_=repair(SimpleNamespace(segments=[SimpleNamespace(text=u['text'],start_time=u['start_ms'],end_time=u['end_ms']) for u in alignments]))
  for part in [dict(text=u.text,start_ms=u.start_time,end_ms=u.end_time) for u in parts.segments]:
   sub=copy.copy(cue);sub.text=part['text'];sub.start_time=part['start_ms'];sub.end_time=part['end_ms'];sub.alignment_units=[]
   units=aligned_units(sub,native)
   if not units:break
   output.extend(units)
  else:
   if output and all(output[i][1]>=output[i-1][2] for i in range(1,len(output))):return output
 candidates=[w for w in native if w.start_time>=cue.start_time and w.end_time<=cue.end_time and key(w.text)]
 if not candidates:return []
 previous=-1
 for w in candidates:
  if not all(math.isfinite(t) for t in (w.start_time,w.end_time)) or w.end_time<=w.start_time or w.start_time<previous:
   raise ValueError('交付分页原生时间无效或重叠')
  previous=w.end_time
 source=''.join(key(w.text) for w in candidates);target=key(text)
 matcher=SequenceMatcher(None,source,target,autojunk=False)
 if source!=target and matcher.ratio()<0.6:return []
 # Only existing source word boundaries can become page boundaries.
 offsets={0:0};n=0
 for i,w in enumerate(candidates):n+=len(key(w.text));offsets[n]=i+1
 boundary={0:0,len(target):len(source)}
 for tag,a,b,c,d in matcher.get_opcodes():
  if tag=='equal':
   for pos in range(c,d+1):boundary[pos]=a+pos-c
  elif tag=='replace':
   boundary[c]=a;boundary[d]=b
  # Inserted/deleted spans have no internal acoustic boundary.
 tokens=list(re.finditer(r'\S+',text));units=[];pos=0;start=0;source_start=0
 for i,m in enumerate(tokens):
  if m.end()<=start:continue
  pos+=len(key(m.group()))
  source_end=offsets.get(boundary.get(pos))
  if source_end is None or source_end<=source_start:continue
  if not key(text[start:m.end()]):continue
  # Punctuation-only tokens belong to the preceding text, never get duration.
  end=m.end()
  while i+1<len(tokens) and not key(tokens[i+1].group()):i+=1;end=tokens[i].end()
  units.append((text[start:end].strip(),candidates[source_start].start_time,candidates[source_end-1].end_time))
  start=end;source_start=source_end
 if source_start!=len(candidates) or key(' '.join(u[0] for u in units))!=target:return []
 return units


def join_source_units(units):
 # Source units are audio fragments, not words separated by typography spaces.
 from app.core.utils.latin_spacing import join
 text=''
 for unit in units:text=join(text,unit[0],digits=True)
 return text


def prepare(data,native,policy):
 from app.core.utils.punctuation_boundaries import repair
 data,punctuation_changes=repair(data)
 result=copy.deepcopy(data);result.segments=[];changes=[]
 width=policy.get('delivery_max_line_chars',24);pause=policy.get('delivery_pause_ms',500)
 separate=policy.get('delivery_separate_languages')==1
 limit=policy.get('delivery_max_cue_chars',45) if separate else width
 max_lines=policy.get('delivery_original_max_lines',2)
 for ci,cue in enumerate(data.segments):
  text=delivery_text(cue.text);units=aligned_units(cue,native)
  line_fit=len(wrap(text,width))<=max_lines if separate else True
  gap=any(units[i][1]-units[i-1][2]>pause for i in range(1,len(units)))
  if line_fit and len(text)<=limit and not gap:
   item=copy.deepcopy(cue);item.text=text
   if units:bind_units(item,units)
   result.segments.append(item);continue
  if not units:raise ValueError('交付分页无法关联原生时间：'+text+'；请检查时间来源及剧本改动，未均分时间')
  groups=[];group=[]
  for unit in units:
   proposal=join_source_units(group+[unit])
   gap=bool(group) and unit[1]-group[-1][2]>pause
   if group and (len(proposal)>limit or gap or (separate and len(wrap(proposal,width))>max_lines)):
    groups.append(group);group=[]
   if len(unit[0])>limit or (separate and len(wrap(unit[0],width))>max_lines):
    raise ValueError('校正片段缺少内部原生时间，无法满足交付长度：'+unit[0])
   group.append(unit)
  if group:groups.append(group)
  for group in groups:
   item=copy.deepcopy(cue);item.text=join_source_units(group)
   item.start_time=group[0][1];item.end_time=group[-1][2];item.translated_text=''
   bind_units(item,group)
   result.segments.append(item)
  changes.append({'cue':ci,'pages':len(groups),'timing':'native_edges_with_text_alignment'})
 if ''.join(key(c.text) for c in data.segments)!=''.join(key(c.text) for c in result.segments):raise ValueError('分页文字守恒检查失败')
 return result,changes

def finalize(data,policy,layout='原文在上',need_translation=True):
 from app.core.utils.punctuation_boundaries import repair
 result,punctuation_changes=repair(data);issues=[];previous_end=-1
 quote_unbalanced=sum(len(re.findall(r'(?<![0-9])"',flat(c.text))) for c in result.segments)%2
 width=policy.get('delivery_max_line_chars',24);total=policy.get('delivery_max_cue_chars',45);lines_limit=policy.get('delivery_max_lines',2)
 for i,c in enumerate(result.segments):
  original=flat(c.text);translated=flat(c.translated_text)
  problems=[]
  if c.start_time<0 or c.end_time<=c.start_time or c.start_time<previous_end:problems.append('时间无效或与上一条重叠')
  previous_end=c.end_time
  if need_translation and not translated:problems.append('译文为空')
  if re.search(r'\{[^}]*\}|[\r\n]',original+translated):problems.append('不允许字幕控制标记')
  original=delivery_text(original)
  if i==0 and quote_unbalanced:problems.append('双引号未闭合')
  if re.search(r'[\[【].*?[\]】]',original):problems.append('含可能的SDH/说话人标签，未擅自删除')
  try:english=wrap(original,width)
  except ValueError as e:problems.append(str(e));english=[original]
  # CJK lines may break at characters, but bilingual total-line limits remain checked.
  if policy.get('delivery_separate_languages')==1:
   chinese=[translated] if translated else []
   if len(english)>policy.get('delivery_original_max_lines',2):problems.append('原文行数超限')
   if len(original)>total:problems.append('原文字符数超限')
   if len(translated)>policy.get('delivery_translation_max_chars',45):problems.append('译文字符数超限')
   if policy.get('delivery_keep_font_size')==1 and len(translated)>policy.get('_translation_visual_chars',400):problems.append('译文显示宽度超限')
   if len(chinese)>policy.get('delivery_translation_max_lines',1):problems.append('译文行数超限')
  else:
   chinese=[translated[j:j+width] for j in range(0,len(translated),width)] if translated else []
   selected=english+chinese if layout=='原文在上' else chinese+english if layout=='译文在上' else english if layout=='仅原文' else chinese
   if len(selected)>lines_limit:problems.append('总行数超限')
   if sum(len(t) for t in selected)>total:problems.append('单条总字符数超限')
   if any(len(t)>width for t in selected):problems.append('单行字符数超限')
  if problems:issues.append({'cue':i,'start_ms':c.start_time,'end_ms':c.end_time,'text':original,'problems':problems})
  c.text='\n'.join(english);c.translated_text='\n'.join(chinese)
 return result,{'schema':'delivery-check/v1','passed':not issues,'issues':issues,'policy':policy,'timing':'unchanged','punctuation_repairs':punctuation_changes,'quality_limits':['音频识别正确性和跨集人名一致性需要剧本/词库及实际素材验证','不推断未提供的说话人标签']}


def repair_translation(data,before,native,policy,layout,translate,on_report):
 """Only repage failing cues; preserve successful translations and original edges."""
 limit=5 if policy.get('delivery_keep_font_size')==1 else 3
 capacity={'总行数超限','单条总字符数超限','原文行数超限','原文字符数超限','译文字符数超限','译文行数超限','译文显示宽度超限'}
 snapshots={(c.start_time,c.end_time,key(c.text)):copy.deepcopy(c) for c in before.segments}
 history=[]
 for attempt in range(limit):
  checked,report=finalize(data,policy,layout);report['repair_history']=copy.deepcopy(history)
  report['timing_audit']=timing_audit(data,native,'translation_check')
  on_report(report)
  if report['passed']:return checked
  if attempt==limit-1 or any(set(i['problems'])-capacity for i in report['issues']):
   raise ValueError('双语交付规则未通过：'+str(report['issues'][0])+'；未缩小字号或合成')
  failing={i['cue'] for i in report['issues']};replacements={};pending=[]
  for index in sorted(failing):
   cue=copy.deepcopy(data.segments[index])
   if getattr(cue,'_delivery_native_units',None) is None:
    saved=snapshots.get((cue.start_time,cue.end_time,key(cue.text)))
    if saved is not None and getattr(saved,'_delivery_native_units',None) is not None:bind_units(cue,saved._delivery_native_units)
   units=aligned_units(cue,native)
   if len(units)<2:raise ValueError('当前超限字幕没有更多原始时间边界可拆分：'+cue.text+'；未改写或均分时间')
   narrower=dict(policy)
   # Tighten the cue capacity, never the word/line width or confirmed font size.
   minimum=max(len(t) for t,a,b in units)
   narrower['delivery_max_cue_chars']=max(minimum,8,int(len(flat(cue.text))*.65))
   if policy.get('delivery_separate_languages')!=1:narrower['delivery_max_line_chars']=min(policy.get('delivery_max_line_chars',24),narrower['delivery_max_cue_chars'])
   if narrower['delivery_max_cue_chars']>=len(flat(cue.text)):
    raise ValueError('超限片段不可再分，已达到可用原始边界：'+cue.text)
   source=copy.deepcopy(data);source.segments=[cue]
   smaller,_=prepare(source,native,narrower)
   if len(smaller.segments)<=1:raise ValueError('双语交付分页无法继续缩短；未改写或均分时间')
   replacements[index]=(len(pending),len(smaller.segments));pending.extend(smaller.segments)
  request=copy.deepcopy(data);request.segments=pending
  translated=translate(copy.deepcopy(request))
  # Enforce stage ownership even if an adapter bypasses its usual guard.
  from app.core.llm_permissions import guard_text_result
  translated=guard_text_result(request,translated,translation=True)
  result=copy.deepcopy(data);result.segments=[]
  for index,cue in enumerate(data.segments):
   if index in replacements:
    start,count=replacements[index];result.segments.extend(translated.segments[start:start+count])
   else:result.segments.append(copy.deepcopy(cue))
  timing_audit(result,native,'repartitioned_translation')
  history.append(dict(attempt=attempt+1,repaired_cues=len(failing),translated_cues=len(pending),reused_cues=len(data.segments)-len(failing)))
  data=result
 raise AssertionError('unreachable')


def export_audit(data,native,policy,style,layout,need_translation=None):
 """Check final role/timing round-trip before writing a production artifact."""
 checked,report=finalize(data,policy,layout,need_translation=(layout!='仅原文') if need_translation is None else need_translation)
 if not report['passed']:raise ValueError('输出前交付检查未通过：'+str(report['issues'][0]))
 audit=timing_audit(data,native,'pre_export')
 from app.core.bk_asr.asr_data import ASRData
 restored=ASRData.from_ass(checked.to_ass(style_str=style,layout=layout))
 if len(restored.segments)!=len(checked.segments):raise ValueError('ASS输出回读条数不一致，未写成品')
 for before,after in zip(checked.segments,restored.segments):
  # ASS uses centiseconds, so its serialization may round by at most 9 ms.
  if abs(before.start_time-after.start_time)>9 or abs(before.end_time-after.end_time)>9 or key(before.text)!=key(after.text) or key(before.translated_text)!=key(after.translated_text):
   raise ValueError('ASS输出回读的语言角色、文字或时间不一致，未写成品')
 audit.update(ass_roundtrip=True,language_roles_preserved=True,layout=layout)
 return audit
