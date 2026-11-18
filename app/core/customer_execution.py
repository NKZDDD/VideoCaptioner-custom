"""One compiled customer rule set; no credentials, directory switches or model calls."""
import copy,hashlib,json
SCHEMA='customer-execution/v1'
def value(v):return getattr(v,'value',v)
def digest(contract):return hashlib.sha256(json.dumps({k:v for k,v in contract.items() if k!='sha256'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def compile_settings(settings,style,mode):
 from app.core.utils.delivery_policy import read_policy
 from app.core.template_rules import compile_rules
 settings={k:value(v) for k,v in settings.items() if not k.startswith('_')}
 rules=compile_rules(settings,mode)
 layout=read_policy(style)
 result={'schema':SCHEMA,'source_mode':mode,'legacy_locked':any('; VideoCaptionerOriginalLanguage: '+lang in style for lang in ('ar','hi')),
  'text_rules':rules,'correction_enabled':bool(settings.get('need_optimize',False)),'need_translate':bool(settings.get('need_translate',False)),
  'target_language':str(settings.get('target_language','')),'layout':str(settings.get('subtitle_layout','原文在上')),
  'presentation':layout,'style_sha256':hashlib.sha256(style.encode('utf-8')).hexdigest(),
  'timing_fallback':'estimate_inside_source_interval_with_review','format_repair_rounds':3,
  'priority':['customer_fixed_layout','preserve_text_meaning','native_time_first','estimated_time_with_review'],
  'result_states':['completed','completed_with_review','failed']}
 result['sha256']=digest(result);return result

def compile_profile(profile):
 from app.core.customer_templates import style_with_line_limit
 style=style_with_line_limit(profile)
 if not style:
  from app.core.task_factory import TaskFactory
  from app.core.customer_templates import profile_value
  style=TaskFactory.get_subtitle_style(profile_value(profile,'subtitle_style_name'))
 settings=copy.deepcopy(profile.get('settings',{}))
 if profile.get('program_text_rules'):settings['program_text_rules']=profile['program_text_rules']
 return compile_settings(settings,style,profile.get('text_source_mode'))

def bind(config,contract):
 config._customer_execution=copy.deepcopy(contract)
 return config

def resolve(config):
 contract=getattr(config,'_customer_execution',None)
 if contract is None:return None
 settings=vars(config).copy()
 # These fields are program display options, never API/service secrets.
 expected=compile_settings(settings,getattr(config,'subtitle_style','') or '',getattr(config,'text_source_mode',None))
 if contract.get('schema')!=SCHEMA or contract.get('sha256')!=digest(contract) or expected!=contract:
  raise ValueError('任务的客户规则在执行前发生变化，请使用同一模板重新开始；未混用规则')
 if contract['legacy_locked']:return None
 return copy.deepcopy(contract)

def translation_guidance(contract):
 policy=contract.get('presentation')
 if not policy:return ''
 maximum=min(policy.get('delivery_translation_max_chars',45),policy.get('_translation_visual_chars',400))
 return '\n程序固定交付要求：保持原文与时间不变；译文保持完整准确，不漏信息。每条译文最多'+str(maximum)+'字符，'+str(policy.get('delivery_translation_max_lines',1))+'行；不要输出排版指令。过长内容由程序拆条后重新请求，不得省略意思。'
