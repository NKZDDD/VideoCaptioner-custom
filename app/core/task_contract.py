"""Freeze selected template and runtime service values without exporting credentials."""
import copy,hashlib,json,uuid
from app.core.llm_permissions import compile_permissions,prompt_conflicts
from types import SimpleNamespace
_RUNTIME={}
_FROZEN_PROFILES={}
_PROTECTED_FIELDS=('id','name','revision','settings','text_source_mode','style_template','english_line_words','program_text_rules','_customer_execution')

def _rule_snapshot(profile):
 return copy.deepcopy({key:profile.get(key) for key in _PROTECTED_FIELDS})

def validate_frozen_profile(profile):
 token=profile.get('_runtime_token')
 saved=_FROZEN_PROFILES.get(token)
 if saved is None or _rule_snapshot(profile)!=saved['rules'] or profile.get('_execution_plan')!=saved['plan']:
  raise ValueError('任务的固定规则已被修改，请按新模板重新创建任务；不会在处理中切换规则')
 return profile

RUNTIME_FIELDS=('batch_size', 'chatglm_api_base', 'chatglm_api_key', 'chatglm_model', 'deeplx_endpoint', 'deepseek_api_base', 'deepseek_api_key', 'deepseek_model', 'faster_whisper_one_word', 'faster_whisper_program', 'faster_whisper_vad_filter', 'faster_whisper_vad_method', 'faster_whisper_vad_threshold', 'gemini_api_base', 'gemini_api_key', 'gemini_model', 'llm_service', 'lm_studio_api_base', 'lm_studio_api_key', 'lm_studio_model', 'multilingual_asr_base', 'multilingual_asr_dialect', 'multilingual_asr_key', 'multilingual_asr_model', 'need_reflect_translate', 'ollama_api_base', 'ollama_api_key', 'ollama_model', 'openai_api_base', 'openai_api_key', 'openai_model', 'public_api_base', 'public_api_key', 'public_model', 'silicon_cloud_api_base', 'silicon_cloud_api_key', 'silicon_cloud_model', 'thread_num', 'use_asr_cache', 'whisper_api_base', 'whisper_api_key', 'whisper_api_model', 'whisper_api_prompt', 'whisper_model')

def freeze_profile(profile):
 from app.common.config import cfg
 from app.core.customer_templates import normalize_profile,require_shared_profile
 from app.core.utils.team_services import refresh_for_new_task
 p=copy.deepcopy(profile)
 token=p.get('_runtime_token')
 if token in _RUNTIME:return validate_frozen_profile(p)
 require_shared_profile(p)
 refresh_for_new_task(cfg)
 p=normalize_profile(p)
 from app.core.customer_execution import compile_profile
 p['_customer_execution']=compile_profile(p)
 from app.core.template_readiness import require_ready
 readiness=require_ready(p)
 if p.get('id') and not str(p.get('style_template','')).strip():
  raise ValueError('客户模板缺少完整外观，请在模板页保存后再开始；不会套用其他样式')
 values={name:copy.deepcopy(getattr(cfg,name).value) for name in RUNTIME_FIELDS}
 token=uuid.uuid4().hex;_RUNTIME[token]=values;p['_runtime_token']=token
 style=p.get('style_template','')
 mode=p.get('text_source_mode')
 plan={'schema':1,'template_id':p.get('id',''),'template_name':p.get('name','自定义'),
       'template_revision':p.get('revision',0),'language':p['settings'].get('transcribe_language'),
       'text_source_mode':mode,'settings':copy.deepcopy(p['settings']),
       'style_sha256':hashlib.sha256(style.encode()).hexdigest(),
       'english_line_words':p.get('english_line_words',6),
       'english_semantic_policy':'; VideoCaptionerEnglishSemanticWrap: 1' in style,
       'service_revision':getattr(cfg,'_team_revision',0),
       'asr_model':str(values.get('multilingual_asr_model','')),
       'llm_model':str(values.get(str(getattr(values.get('llm_service'),'name','')).lower()+'_model','')),
       'llm_service':str(getattr(values.get('llm_service'),'value',values.get('llm_service',''))),
       'policy_source':'已保存模板（兼容原有语言规则，不更改台词算法）'}
 plan['customer_execution']=copy.deepcopy(p['_customer_execution'])
 plan['template_readiness']=readiness
 plan['llm_permissions']=compile_permissions(p['settings'],mode)
 plan['llm_permissions']['conflicts']=prompt_conflicts(p['settings'].get('custom_prompt_text',''),p['settings'].get('needs_remove_punctuation',False))
 if mode=='script_correct':
  from app.core.template_rules import compile_rules
  plan['template_rule_contract']=compile_rules(p['settings'],mode)
 # Public plan contains template rules only; credentials stay in process memory.
 digest=hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
 plan['id']=digest;p['_execution_plan']=plan;p['_profile_frozen']=True
 _FROZEN_PROFILES[token]={'rules':_rule_snapshot(p),'plan':copy.deepcopy(plan)}
 return p

def runtime_config(profile):
 p=freeze_profile(profile)
 return SimpleNamespace(**{name:SimpleNamespace(value=copy.deepcopy(value)) for name,value in _RUNTIME[p['_runtime_token']].items()})

def plan_summary(profile):
 p=profile.get('_execution_plan') or {}
 modes={'script_correct':'原声定位＋剧本校正','voice':'语音为主＋剧本辅助','strict':'剧本完全替换','hybrid':'剧本混合匹配',None:'模板常规流程'}
 return f"模板 {p.get('template_name',profile.get('name',''))} v{p.get('template_revision',profile.get('revision',0))} · {modes.get(p.get('text_source_mode',profile.get('text_source_mode')),'模板流程')} · 外观按模板固定"
