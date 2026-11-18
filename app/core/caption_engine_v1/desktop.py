"""Desktop adapter: same finalization functions as cloud, no second split pass."""
import json,copy
from pathlib import Path
from .flow_v8 import finalize
from .numbers import normalize_arabic_numbers
from .review_policy import classify_report
from .common import atomic_text

def match_desktop(data,reference,settings,report_path,request=None):
 from .flow_v8 import source_rows,script_turns,run_matching,apply_matches
 from .source_timing import normalize_source_timing
 from .template_rules import resolve_rules
 original,repairs=normalize_source_timing(data.segments)
 rows=source_rows(original);turns=script_turns(reference)
 if not rows or not turns:raise ValueError('需要本集剧本和有效原声字幕')
 config=dict(text_source_mode='script_correct',subtitle_config=settings,script_reference=reference,
  _checkpoint_root=Path(report_path).parent/'.caption-checkpoints',_progress=settings.get('_progress'))
 contract=resolve_rules(config)
 report=dict(version='script-v13',mode='script_correct',status='matching',changes=[],review_items=[],warnings=[],
  source_timing_normalization=repairs,rule_contract_sha256=contract['sha256'],rule_schema=contract['schema'],
  rule_delivery={'matching':'pending'},script_speakers={t['id']:t['speaker'] for t in turns},
  timing_policy='asr_boundaries_structurally_checked_not_acoustically_aligned',acoustic_realignment='not_performed')
 raw_path=Path(report_path).with_suffix('.原始识别.json')
 atomic_text(raw_path,json.dumps(rows,ensure_ascii=False,indent=2));report['raw_asr_path']=str(raw_path)
 try:
  matches=run_matching(rows,turns,config,request,report)
  report['matches']=matches;report['rule_delivery']['matching']='request_included_contract'
  report['uncovered_asr_ids']=sorted(set(range(len(rows)))-{i for m in matches for i in range(m['start_id'],m['end_id']+1)})
  report['unmatched']=[t['id'] for t in turns if not any(m['script_id']==t['id'] for m in matches)]
  for sid in report['unmatched']:
   report['review_items'].append(dict(kind='剧本未匹配',script_id=sid,text=turns[sid]['text'],reason='未确认说出，不生成猜测时间'))
  data.segments=copy.deepcopy(original);data=apply_matches(data,turns,matches,report)
  report.update(status='matched_pending_finalize',summary=f'剧本匹配{len(matches)}处，进入统一模板输出处理')
 except Exception as error:
  import traceback
  report.update(status='failed',error=type(error).__name__,error_detail=str(error)[:800],error_traceback=traceback.format_exc(),summary='剧本校正未完成，已保存完成分段；未按成功导出')
  accepted=report.get('accepted_matches',[])
  if settings.get('_customer_execution') and accepted:
   # Revalidate accumulated candidates before recovery; a report is not authority.
   from .flow_v8 import validate_match
   occupied=set();matches=[]
   for candidate in sorted(accepted,key=lambda m:m.get('start_id',-1)):
    item,reason=validate_match(candidate,turns,rows,occupied)
    if item:matches.append(item);occupied.update(range(item['start_id'],item['end_id']+1))
   if not matches:raise
   data.segments=copy.deepcopy(original);data=apply_matches(data,turns,matches,report)
   report['matches']=matches
   report.update(status='partially_matched_with_review',summary=f'局部复核异常；保留{len(matches)}处已确认校正，其余片段待核对')
   report['warnings'].append('局部复核异常，已恢复经程序重新验证的完成分段：'+str(error)[:180])
  else:raise
 finally:atomic_text(report_path,json.dumps(report,ensure_ascii=False,indent=2))
 data.script_review_report=report
 return data

def finalize_desktop(data,original,settings,report_path,request=None,reference=None):
 config={'text_source_mode':'script_correct','subtitle_config':settings,
  '_checkpoint_root':Path(report_path).parent/'.caption-checkpoints','_progress':settings.get('_progress')}
 report=copy.deepcopy(getattr(data,'script_review_report',{}))
 if reference:
  config['script_reference']=reference
  from .flow_v8 import script_turns
  config['script_speakers']={t['id']:t['speaker'] for t in script_turns(reference)}
 config['_request_metrics']=report.setdefault('request_metrics',[])
 report.setdefault('changes',[]);report.setdefault('review_items',[]);report.setdefault('warnings',[])
 report.update(semantic_splits=0,template_changes=[],finalize_version='caption-finalize-v1')
 try:
  data=finalize(data,original,config,report,request)
  classified=classify_report(report,normalize_arabic_numbers)
  report['automatic_format_changes']=classified['automatic'];report['review_items']=classified['manual']
  report['status']='completed_with_review' if report['review_items'] or report['warnings'] else 'completed'
  report['summary']=f"人工核对{len(report['review_items'])}项；自动格式调整{len(classified['automatic'])}项；模板显示替换{len(report['template_changes'])}处；未经人工验收"
 except Exception as error:
  report.update(status='failed',error=type(error).__name__);raise
 finally:
  atomic_text(report_path,json.dumps(report,ensure_ascii=False,indent=2))
 data.script_review_report=report
 review_path=Path(report_path).with_suffix('.待检查.txt')
 atomic_text(review_path,report['summary']+'\n'+'\n'.join(str(i) for i in report['review_items']))
 return data
