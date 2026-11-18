"""Shared bounded matching, follow-up, and mandatory remaining-name check."""
import json,copy
from concurrent.futures import ThreadPoolExecutor
from .request_cache import notify

WINDOW=192

def run_matching(rows,turns,config,request,report):
 from .flow_v8 import ask_json,validate_match,islands
 from .sound_review import looks_like_sound
 if len(json.dumps(turns,ensure_ascii=False))>100000:raise ValueError('本集剧本过大，请先按集整理')
 accepted=[];occupied=set();rejected=[];all_islands=islands(rows)
 report['accepted_matches']=[]
 def accept(answer,allowed):
  if not isinstance(answer.get('matches'),list):raise ValueError('剧本匹配未返回matches列表')
  count=0
  for match in answer['matches']:
   item,reason=validate_match(match,turns,rows,occupied)
   if item and not set(range(item['start_id'],item['end_id']+1))<=allowed:item=None;reason='返回范围超出当前待处理片段'
   if item:
    ordered=sorted(accepted+[item],key=lambda m:m['start_id'])
    previous=None
    for candidate in ordered:
     position=(candidate['script_id'],candidate['script_char_start'])
     if previous is not None and position<previous and not (candidate.get('repeat') is True or candidate.get('order_change') is True):
      item=None;reason='剧本顺序倒退，缺少重复或顺序变化依据';break
     previous=(candidate['script_id'],candidate['script_char_end'])
   if item:
    accepted.append(item);occupied.update(range(item['start_id'],item['end_id']+1));count+=1
    report['accepted_matches']=copy.deepcopy(accepted)
   else:rejected.append(dict(match=match,reason=reason))
  return count
 windows=[];window=[]
 for row in rows:
  if window and len(window)>=WINDOW and (row.get('asr_segment_id') is None or row.get('asr_segment_id')!=window[-1].get('asr_segment_id')):
   windows.append(window);window=[]
  if len(window)>=256:windows.append(window);window=[]
  window.append(row)
 if window:windows.append(window)
 report['matching_windows']=[]
 config.setdefault('_request_metrics',[])
 report['request_metrics']=config['_request_metrics']
 report['matching_concurrency']=min(2,len(windows))
 # Each pair shares the same confirmed prefix. Responses are accepted in source
 # order on this thread; worker threads never mutate accepted/occupied state.
 def propose(index,window,prefix):
  first,last=window[0]['id'],window[-1]['id']
  options=dict(config,_request_label=f'剧本匹配 第{index+1}/{len(windows)}段（两路并行）')
  payload=dict(stage='script_matching',asr=compact_rows(window),script=turns,
   speech_islands=[dict(first_id=max(i['first_id'],first),last_id=min(i['last_id'],last)) for i in all_islands if i['last_id']>=first and i['first_id']<=last],
   confirmed=prefix,
   previous_asr=compact_rows(rows[max(0,first-6):first]),next_asr=compact_rows(rows[last+1:last+7]),
   instruction='Only match IDs within the asr field. previous_asr and next_asr are context only. Preserve canonical order; cross-window conflicts are rechecked by program.')
  return ask_json(options,'script_matching',payload,request)
 with ThreadPoolExecutor(max_workers=2,thread_name_prefix='caption-match') as pool:
  for offset in range(0,len(windows),2):
   prefix=copy.deepcopy(accepted[-12:])
   pending=[(index,window,pool.submit(propose,index,window,prefix)) for index,window in enumerate(windows[offset:offset+2],offset)]
   failures=[]
   for index,window,future in pending:
    first,last=window[0]['id'],window[-1]['id']
    try:
     answer=future.result()
     count=accept(answer,set(range(first,last+1)))
     report['matching_windows'].append(dict(first_id=first,last_id=last,accepted=count,status='completed'))
     notify(config,f'剧本匹配：已完成{len(report["matching_windows"])}/{len(windows)}段')
    except Exception as error:
     failures.append(error)
   if failures:
    if not config['subtitle_config'].get('_customer_execution'):raise failures[0]
    report.setdefault('warnings',[]).append('部分剧本匹配请求未完成，未确认文字保留原声：'+type(failures[0]).__name__)
 missing=[r['id'] for r in rows if r['id'] not in occupied]
 report['initial_uncovered_count']=len(missing);report['rechecks']=[];name_checked=set();integrated_issues=[]
 # Span-bounded windows, not N scattered IDs spanning a whole episode.
 for index,window in enumerate(windows):
  ids=[r['id'] for r in window if r['id'] not in occupied]
  if not ids:continue
  first,last=min(ids),max(ids)
  nearby=rows[max(0,first-6):min(len(rows),last+7)]
  payload=dict(stage='script_recheck',asr=compact_rows(nearby),unresolved_ids=ids,script=scoped_script(turns,accepted,first,last),
   speech_islands=[i for i in all_islands if i['last_id']>=nearby[0]['id'] and i['first_id']<=nearby[-1]['id']],
   confirmed=nearby_matches(accepted,first,last),rejected=rejected[-6:],instruction='Resolve only unresolved IDs. At pauses, use canonical character ranges for each speech span; never omit difficult names or fill silence with invented speech. In this SAME pass also check names against script and return mandatory name_issues: [] or [{start_id,end_id,reason}] for unresolved suspected name errors. Do not replace geographic or common words without script correspondence.')
  notify(config,f'未匹配复核 {index+1}/{len(windows)}')
  config['_request_label']=f'未匹配复核 第{index+1}/{len(windows)}段'
  try:answer=ask_json(config,'script_recheck',payload,request)
  except Exception as error:
   if not config['subtitle_config'].get('_customer_execution'):raise
   report.setdefault('warnings',[]).append('局部复核未完成，保留当前识别：'+type(error).__name__);name_checked.update(ids);continue
  try:count=accept(answer,set(ids))
  except (ValueError,TypeError,KeyError) as error:
   if not config['subtitle_config'].get('_customer_execution'):raise
   report.setdefault('warnings',[]).append('局部复核响应格式异常，保留已确认校正：'+str(error)[:180]);continue
  if isinstance(answer.get('name_issues'),list):
   for issue in answer['name_issues']:
    if not isinstance(issue,dict):
     report.setdefault('warnings',[]).append('复核姓名条目格式无效，已忽略该条目');continue
    a,b=issue.get('start_id'),issue.get('end_id')
    if type(a)is not int or type(b)is not int or not a<=b or not set(range(a,b+1))<=set(ids):
     report.setdefault('warnings',[]).append('复核姓名范围无效，已保留已有校正');continue
    if not set(range(a,b+1))<=occupied:integrated_issues.append(issue)
   name_checked.update(ids)
  report['rechecks'].append(dict(ids=ids,accepted=count,status='completed'))
 # Every remaining spoken fragment gets one name-focused check. Sound-only
 # hypotheses are handled independently; they must not force invented dialogue.
 report['name_consistency']=dict(status='completed_in_recheck' if name_checked else 'not_needed',checks=[],name_issues=integrated_issues)
 for index,window in enumerate(windows):
  ids=[r['id'] for r in window if r['id'] not in occupied and r['id'] not in name_checked and not looks_like_sound(r['text'])]
  if not ids:continue
  first,last=min(ids),max(ids)
  notify(config,'姓名一致性检查')
  payload=dict(stage='name_consistency',asr=compact_rows(rows[max(0,first-6):min(len(rows),last+7)]),unresolved_ids=ids,
   script=scoped_script(turns,accepted,first,last),confirmed=nearby_matches(accepted,first,last),
   instruction='Check canonical people/names in EVERY unresolved ID using nearby confirmed script spans. Return matches with canonical character offsets, not replacement text. Also return name_issues: [] when no suspected person-name error remains; otherwise list {start_id,end_id,reason}. Never replace geographical/common words merely because a name sounds similar. Uncertain naming must appear in name_issues, not silently pass.')
  report['name_consistency']['status']='completed'
  config['_request_label']=f'姓名检查 第{index+1}/{len(windows)}段'
  try:answer=ask_json(config,'name_consistency',payload,request)
  except Exception as error:
   if not config['subtitle_config'].get('_customer_execution'):raise
   report.setdefault('warnings',[]).append('局部姓名检查未完成，保留当前识别：'+type(error).__name__);continue
  try:count=accept(answer,set(ids))
  except (ValueError,TypeError,KeyError) as error:
   if not config['subtitle_config'].get('_customer_execution'):raise
   report.setdefault('warnings',[]).append('局部姓名响应格式异常，保留已确认校正：'+str(error)[:180]);continue
  unresolved=[]
  for issue in answer.get('name_issues',[]) if isinstance(answer.get('name_issues'),list) else []:
   if not isinstance(issue,dict):
    report.setdefault('warnings',[]).append('姓名一致性检查条目格式无效');continue
   a,b=issue.get('start_id'),issue.get('end_id')
   if type(a)is not int or type(b)is not int or not a<=b or not set(range(a,b+1))<=set(ids):
    report.setdefault('warnings',[]).append('姓名检查范围无效，已保留已有校正');continue
   if not set(range(a,b+1))<=occupied:unresolved.append(issue)
  report['name_consistency']['checks'].append(dict(ids=ids,accepted=count,status='completed'))
  report['name_consistency']['name_issues'].extend(unresolved)
 if report['name_consistency']['name_issues']:
  report['name_consistency']['status']='needs_review'
  if config['subtitle_config'].get('_customer_execution'):
   report.setdefault('review_items',[]).extend(dict(issue,kind='姓名待核对，保留原声') for issue in report['name_consistency']['name_issues'])
  else:raise ValueError('姓名一致性检查未通过，已保留完成分段；不会将疑似错名当作校正成功导出')
 report['rejected']=rejected
 return sorted(accepted,key=lambda m:m['start_id'])

def compact_rows(rows):
 from .flow_v8 import join_text
 output=[]
 for row in rows:
  origin=row.get('asr_segment_id')
  if output and origin is not None and origin==output[-1].get('asr_segment_id') and row.get('speaker_id')==output[-1].get('speaker_id') and 0<=row['start_ms']-output[-1]['end_ms']<400 and row['end_ms']-output[-1]['start_ms']<=6000:
   last=output[-1];last['text']=join_text(last['text'],row['text']);last['end_ms']=row['end_ms'];last['end_id']=row['id']
  else:output.append(dict(row,end_id=row['id']))
 return output

def nearby_matches(matches,first,last):
 before=[m for m in matches if m['end_id']<first][-4:]
 after=[m for m in matches if m['start_id']>last][:4]
 inside=[m for m in matches if m['start_id']<=last and m['end_id']>=first]
 return before+inside+after

def scoped_script(turns,matches,first,last):
 # Explicit repeats/order changes cannot be narrowed by monotone anchors.
 if any(m.get('repeat') or m.get('order_change') for m in matches):return turns
 before=[m for m in matches if m['end_id']<first]
 after=[m for m in matches if m['start_id']>last]
 lo=max(0,before[-1]['script_id']-2) if before else 0
 hi=min(len(turns)-1,after[0]['script_id']+2) if after else len(turns)-1
 return turns[lo:hi+1] if lo<=hi else turns
