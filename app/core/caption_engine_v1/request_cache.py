"""Input-bound request checkpoints. No credentials or request text are persisted."""
import hashlib,json,copy,time
from pathlib import Path
from .common import atomic_text

VERSION='caption-core-v2'

def notify(config,message):
 callback=config.get('_progress')
 if callback:
  try:callback(message)
  except Exception:pass

def invoke(config,stage,messages,perform):
 from .template_rules import resolve_rules
 settings=config['subtitle_config']
 identity=dict(version=VERSION,stage=stage,messages=messages,
  template=resolve_rules(config)['sha256'],endpoint=settings.get('base_url'),model=settings.get('llm_model'))
 digest=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 label=config.get('_request_label',{'semantic_split':'语义断句','script_matching':'剧本匹配','script_recheck':'未匹配复核','name_consistency':'姓名检查'}.get(stage,stage))
 root=config.get('_checkpoint_root');path=Path(root)/(digest+'.json') if root else None
 field='splits' if stage=='semantic_split' else 'matches'
 def valid(answer):
  return (isinstance(answer,dict) and isinstance(answer.get(field),list)
   and (stage!='name_consistency' or isinstance(answer.get('name_issues'),list)))
 if path and path.is_file():
  try:
   cached=json.loads(path.read_text(encoding='utf-8'))
   if cached.get('request_hash')==digest and cached.get('version')==VERSION and valid(cached.get('answer')):
    config.setdefault('_request_metrics',[]).append(dict(stage=stage,label=label,cached=True,elapsed_seconds=0,status='reused'))
    notify(config,label+'：复用已完成分段');return copy.deepcopy(cached['answer'])
  except (OSError,ValueError,TypeError):pass
 notify(config,label+'：发送请求')
 started=time.monotonic()
 metric=dict(stage=stage,label=label,request_characters=sum(len(m['content']) for m in messages),cached=False)
 try:
  answer=perform()
  metric['status']='returned'
 except Exception as error:
  metric.update(status='failed',error_type=type(error).__name__)
  raise
 finally:
  diagnostics=getattr(perform,'response_diagnostics',None)
  if diagnostics:
   metric['response_attempts']=len(diagnostics)
   metric['response_diagnostics']=copy.deepcopy(diagnostics)
  metric['elapsed_seconds']=round(time.monotonic()-started,3)
  config.setdefault('_request_metrics',[]).append(metric)
 if not valid(answer):raise ValueError(stage+' 返回结构无效；未保存为已完成分段')
 if path and not (stage in ('name_consistency','script_recheck') and answer.get('name_issues')):atomic_text(path,json.dumps(dict(version=VERSION,stage=stage,request_hash=digest,answer=answer),ensure_ascii=False,indent=2))
 return answer
