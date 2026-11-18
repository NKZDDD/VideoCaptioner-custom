"""Common local layout/recovery. Estimates are bounded and explicitly reported."""
import copy,json,re,math
from pathlib import Path
from app.core.concurrency import atomic_text
from app.core.llm_permissions import guard_text_result,safe_text

def context_for(config,video):
 from app.core.customer_execution import resolve
 contract=resolve(config)
 if contract is None:return None
 return {'schema':'customer-run/v1','contract':contract,'video':str(video or ''),'warnings':[],'stages':[],'status':'running'}
def note(context,kind,**detail):
 item=dict(kind=kind,**detail)
 if item not in context['warnings']:context['warnings'].append(item)
def save(context,output=None):
 path=Path(context['video']).with_suffix('.客户规则执行.json') if context.get('video') else Path(output).with_suffix('.客户规则执行.json') if output else None
 if path:
  data={k:v for k,v in context.items() if k!='contract'};data['contract_sha256']=context['contract']['sha256']
  data['review_items']=[dict(w,reason=str(w.get('method') or w.get('detail') or w.get('error_type') or w['kind'])) for w in context['warnings']]
  if context.get('video') and Path(context['video']).is_file():
   stat=Path(context['video']).stat();data['source_stamp']={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
  atomic_text(path,json.dumps(data,ensure_ascii=False,indent=2))

def optional_stage(data,operation,context,stage):
 # Only content-improvement stages may degrade. Translation/rendering never do.
 before=copy.deepcopy(data)
 try:return operation(data)
 except Exception as error:
  note(context,'局部校正未完成，保留本阶段输入',stage=stage,error_type=type(error).__name__)
  return before

def _pages(text,policy):
 from app.core.utils.delivery_policy import flat,wrap
 text=flat(text);width=policy.get('delivery_max_line_chars',24);limit=policy.get('delivery_max_cue_chars',45);max_lines=policy.get('delivery_original_max_lines',2)
 if re.search(r'[\u3400-\u9fff]',text):
  from app.core.subtitle_processor.split import semantic_split_text
  pieces=semantic_split_text(text,min(limit,width),visible_chars=True)
  if all(len(p)<=limit and len(wrap(p,width))<=max_lines for p in pieces):return pieces
  raise ValueError('单个词语无法满足客户行宽，需调整模板冲突')
 tokens=list(re.finditer(r'\S+',text));parts=[];start=0;last=0
 for token in tokens:
  candidate=text[start:token.end()]
  fits=len(candidate)<=limit and len(wrap(candidate,width))<=max_lines
  if not fits:
   if last<=start:raise ValueError('单个完整词无法满足客户行宽：'+token.group())
   parts.append(text[start:last].strip());start=token.start()
   if len(token.group())>limit or len(wrap(token.group(),width))>max_lines:raise ValueError('单个完整词无法满足客户版式')
  last=token.end()
 if last>start:parts.append(text[start:last].strip())
 return parts

def estimated_pages(cue,native,policy,context):
 from app.core.utils.delivery_policy import key
 pieces=_pages(cue.text,policy)
 return allocate_pages(cue,pieces,native,context)

def allocate_pages(cue,pieces,native,context):
 from app.core.utils.delivery_policy import key
 if len(pieces)<=1:return [copy.deepcopy(cue)]
 from app.core.utils.delivery_policy import aligned_units,bind_units
 try:units=aligned_units(cue,native)
 except ValueError:units=[]
 native_parts=[];cursor=0
 for text in pieces:
  group=[]
  while cursor<len(units) and key(''.join(u[0] for u in group))!=key(text):group.append(units[cursor]);cursor+=1
  if not group or key(''.join(u[0] for u in group))!=key(text):break
  part=copy.deepcopy(cue);part.text=text;part.start_time=group[0][1];part.end_time=group[-1][2];part.translated_text='';bind_units(part,group);native_parts.append(part)
 if len(native_parts)==len(pieces) and cursor==len(units):return native_parts
 starts={c.start_time for c in native};ends={c.end_time for c in native}
 root=getattr(cue,'_program_time_bounds',(cue.start_time,cue.end_time))
 if root[0] not in starts or root[1] not in ends:raise ValueError('拆条缺少有效原句时间，未估算')
 a,b=cue.start_time,cue.end_time
 if not all(type(x) in (int,float) and math.isfinite(x) for x in (a,b)) or int(b-a)<len(pieces):raise ValueError('原句时长不足以容纳拆条，需检查音频时间')
 weights=[max(1,len(key(p))) for p in pieces];total=sum(weights);position=a;used=0;result=[]
 for index,(text,weight) in enumerate(zip(pieces,weights)):
  used+=weight;finish=b if index==len(pieces)-1 else max(position+1,min(b-(len(pieces)-index-1),a+round((b-a)*used/total)))
  part=copy.deepcopy(cue);part.text=text;part.start_time=position;part.end_time=finish;part.translated_text=''
  for attr in ('_delivery_native_units','alignment_units'):
   if hasattr(part,attr):delattr(part,attr)
  part._program_time_bounds=tuple(root);part._display_time_estimated=True
  result.append(part);position=finish
 note(context,'拆条显示时间待核对',start_ms=a,end_ms=b,pages=len(result),method='原句区间内按文字阅读量估算，未做声学对齐')
 return result

def prepare(data,native,policy,context):
 from app.core.utils import delivery_policy as d
 from app.core.utils.punctuation_boundaries import repair
 data,_=repair(data);d.timing_audit(data,native,'customer_prepare_input')
 result=copy.deepcopy(data);result.segments=[];changes=[]
 for i,cue in enumerate(data.segments):
  one=copy.deepcopy(data);one.segments=[copy.deepcopy(cue)]
  try:
   if getattr(cue,'_display_time_estimated',False):raise ValueError('estimated display interval')
   small,updates=d.prepare(one,native,policy);result.segments.extend(small.segments);changes.extend(dict(x,cue=i) for x in updates)
  except ValueError:
   parts=estimated_pages(cue,native,policy,context);result.segments.extend(parts);changes.append({'cue':i,'pages':len(parts),'timing':'bounded_display_estimate' if len(parts)>1 else 'source_interval'})
 if ''.join(d.key(c.text) for c in data.segments)!=''.join(d.key(c.text) for c in result.segments):raise ValueError('统一排版改变了原文内容')
 d.timing_audit(result,native,'customer_prepared');return result,changes

CAPACITY={'总行数超限','单条总字符数超限','原文行数超限','原文字符数超限','译文字符数超限','译文行数超限','译文显示宽度超限','单行字符数超限'}
HARD={'时间无效或与上一条重叠','译文为空','不允许字幕控制标记'}
def inspect(data,policy,layout,context):
 from app.core.utils import delivery_policy as d
 result,report=d.finalize(data,policy,layout,need_translation=bool(context['contract'].get('need_translate',True)))
 for item in report['issues']:
  if HARD.intersection(item['problems']) or any(x.startswith('完整单词超过') for x in item['problems']):raise ValueError('客户字幕无法有效输出：'+str(item))
  if not CAPACITY.intersection(item['problems']):note(context,'文字格式待核对',**item)
 for cue in result.segments:
  if not safe_text(cue.text,cue.text) or not safe_text(cue.translated_text,cue.translated_text):raise ValueError('字幕文字为空或包含控制指令')
 report['fixed_layout_passed']=not any(CAPACITY.intersection(i['problems']) for i in report['issues'])
 report['quality_verified']=False;report['mode']='customer_execution';report['passed']=report['fixed_layout_passed']
 return result,report

def repair_translation(data,before,native,policy,layout,translate,on_report,context):
 from app.core.utils import delivery_policy as d
 history=[]
 for attempt in range(context['contract']['format_repair_rounds']+1):
  checked,report=inspect(data,policy,layout,context);report['repair_history']=copy.deepcopy(history);report['timing_audit']=d.timing_audit(checked,native,'customer_translation');on_report(report)
  if report['passed']:return checked
  if attempt==context['contract']['format_repair_rounds']:raise ValueError('局部排版修正已达到上限；客户行数、字号未放宽，问题：'+str(report['issues'][0]))
  failing={i['cue'] for i in report['issues'] if CAPACITY.intersection(i['problems'])};pending=[];replacements={}
  for index in sorted(failing):
   cue=data.segments[index];narrow=dict(policy);narrow['delivery_max_cue_chars']=max(1,int(len(d.flat(cue.text))*.6))
   source=copy.deepcopy(data);source.segments=[copy.deepcopy(cue)]
   try:small,_=prepare(source,native,narrow,context)
   except ValueError:small=source
   # A single indivisible word can have an unnecessarily verbose translation:
   # request it once more under the same fixed target capacity; never drop text.
   replacements[index]=(len(pending),len(small.segments));pending.extend(small.segments)
  batch=copy.deepcopy(data);batch.segments=pending
  fixed=guard_text_result(batch,translate(copy.deepcopy(batch)),translation=True)
  result=copy.deepcopy(data);result.segments=[]
  for index,cue in enumerate(data.segments):
   if index in replacements:
    start,count=replacements[index];result.segments.extend(fixed.segments[start:start+count])
   else:result.segments.append(copy.deepcopy(cue))
  history.append({'attempt':attempt+1,'repaired_cues':len(failing),'translated_cues':len(pending),'reused_cues':len(data.segments)-len(failing)});data=result

def finalize(data,policy,layout,context):
 result,report=inspect(data,policy,layout,context)
 if not report['passed']:raise ValueError('固定版式尚未满足，未增加行数或缩小字号：'+str(report['issues'][0]))
 return result,report

def export_audit(data,native,policy,style,layout,context):
 from app.core.utils.delivery_policy import timing_audit,key
 from app.core.bk_asr.asr_data import ASRData
 result,report=finalize(data,policy,layout,context);audit=timing_audit(result,native,'customer_export');restored=ASRData.from_ass(result.to_ass(style_str=style,layout=layout))
 if len(result.segments)!=len(restored.segments):raise ValueError('字幕导出回读条数不一致')
 for a,b in zip(result.segments,restored.segments):
  if abs(a.start_time-b.start_time)>9 or abs(a.end_time-b.end_time)>9 or key(a.text)!=key(b.text) or key(a.translated_text)!=key(b.translated_text):raise ValueError('字幕导出回读时间、文字或语言不一致')
 context['status']='completed_with_review' if context['warnings'] else 'completed';context['stages'].append(audit);save(context)
 audit.update(ass_roundtrip=True,language_roles_preserved=True,mode='customer_execution',quality_verified=False)
 return audit


def prepare_source_fallback(data,context):
 from app.core.caption_engine_v1.source_timing import normalize_source_timing
 result=copy.deepcopy(data)
 result.segments,repairs=normalize_source_timing(result.segments)
 if repairs:
  context.setdefault('stages',[]).append({'stage':'fallback_source_normalization','repairs':repairs,'speech_times_changed':False})
 if result.is_word_timestamp():
  result=group_native_input(result)
 return result


def finalize_single(data,native,config,context):
 from app.core.caption_engine_v1.source_timing import normalize_source_timing
 from app.core.subtitle_processor.split import semantic_split_text
 from app.core.utils.delivery_policy import flat,timing_audit,key
 native,_=normalize_source_timing(native)
 from app.core.utils.punctuation_boundaries import repair
 original_data=data
 data,spacing_repairs=repair(data)
 if not spacing_repairs:data=original_data
 if spacing_repairs:context.setdefault('stages',[]).append({'stage':'single_language_punctuation_spacing','repairs':spacing_repairs,'speech_times_changed':False})
 if not getattr(config,'need_split',True):return data
 # The fallback retains raw ASR rows. Normalize its punctuation metadata with
 # the same policy as the timing reference; never keep zero-duration cues after
 # normalizing only the reference. Spoken spans remain unchanged.
 data=copy.deepcopy(data)
 data.segments,source_repairs=normalize_source_timing(data.segments)
 if source_repairs:
  context.setdefault('stages',[]).append({'stage':'single_language_source_normalization','repairs':source_repairs,'speech_times_changed':False})
 result=copy.deepcopy(data);result.segments=[]
 for cue in data.segments:
  text=flat(cue.text);part=copy.deepcopy(cue);part.text=text
  if re.search(r'[\u3400-\u9fff]',text):pieces=semantic_split_text(text,max(1,int(config.max_word_count_cjk)))
  else:
   from app.core.single_language_layout import pages,latin_text
   if latin_text(text):pieces=pages(text,config.subtitle_style or '',config.max_word_count_english)
   else:
    words=text.split();limit=max(1,int(config.max_word_count_english));pieces=[' '.join(words[i:i+limit]) for i in range(0,len(words),limit)] if words else []
  if not pieces:raise ValueError('字幕没有有效文字')
  if key(''.join(pieces))!=key(text):raise ValueError('断句改变了台词内容')
  # Prefer the existing native layout adapter; estimate only missing splits.
  if len(pieces)>1:
   from app.core.utils.script_template_layout import enforce_script_template_layout
   one=copy.deepcopy(data);one.segments=[part]
   original_result,layout_report=enforce_script_template_layout(one,native,config)
   if len(original_result.segments)>1 and not layout_report['same_time_wraps']:result.segments.extend(original_result.segments)
   else:result.segments.extend(allocate_pages(part,pieces,native,context))
  else:result.segments.append(part)
 timing_audit(result,native,'customer_single_language')
 return result


def group_native_input(data):
 """Group native word fragments without inventing speakers or word times."""
 from app.core.caption_engine_v1.flow_v8 import join_text
 result=copy.deepcopy(data);result.segments=[]
 for cue in data.segments:
  item=copy.deepcopy(cue)
  item.alignment_units=[{'text':item.text,'start_ms':item.start_time,'end_ms':item.end_time}]
  previous=result.segments[-1] if result.segments else None
  origin=getattr(item,'asr_segment_id',None)
  same_origin=previous is not None and (origin is None or origin==getattr(previous,'asr_segment_id',None))
  same_speaker=previous is not None and getattr(previous,'speaker_id',None)==getattr(item,'speaker_id',None)
  if previous and same_origin and same_speaker and 0<=item.start_time-previous.end_time<500 and item.end_time-previous.start_time<=6000 and len(previous.text)<100 and not re.search(r'[。！？.!?]["”]?$',previous.text):
   previous.text=join_text(previous.text,item.text);previous.end_time=item.end_time;previous.alignment_units.extend(item.alignment_units)
  else:result.segments.append(item)
 return result


def group_unmatched_input(data):
 from app.core.caption_engine_v1.flow_v8 import join_text
 result=copy.deepcopy(data);result.segments=[]
 for cue in data.segments:
  item=copy.deepcopy(cue);previous=result.segments[-1] if result.segments else None
  origin=getattr(item,'asr_segment_id',None)
  merge=previous is not None and origin is not None and origin==getattr(previous,'asr_segment_id',None) and not getattr(previous,'script_corrected',False) and not getattr(item,'script_corrected',False) and getattr(previous,'speaker_id',None)==getattr(item,'speaker_id',None)
  if not getattr(item,'script_corrected',False):item.alignment_units=[{'text':item.text,'start_ms':item.start_time,'end_ms':item.end_time}]
  if merge and 0<=item.start_time-previous.end_time<500 and item.end_time-previous.start_time<=6000 and len(previous.text)<100 and not re.search(r'[。！？.!?]["”]?$',previous.text):
   previous.text=join_text(previous.text,item.text);previous.end_time=item.end_time;previous.alignment_units.extend(item.alignment_units)
  else:result.segments.append(item)
 return result
