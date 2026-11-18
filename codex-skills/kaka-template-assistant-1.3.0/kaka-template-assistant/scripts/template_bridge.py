"""Read/check/commit templates through the installed app. No direct store writes."""
import argparse,copy,hashlib,json,os,sys
from pathlib import Path
# Embedded Python may omit the entry script directory from sys.path.
sys.path.insert(0,str(Path(__file__).resolve().parent))
DEFAULT_ROOT=r'\\desktop-8roqjoa\电脑文件储存\工具箱\自动化工具\卡卡字幕工具\VideoCaptioner\VideoCaptioner'
VERSION='1.3.0'

class SaveVerificationError(RuntimeError):
 def __init__(self,message,outcome,identity=None):
  super().__init__(message);self.outcome=outcome;self.identity=identity

def sha(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def text(value):return json.dumps(value,ensure_ascii=False,indent=2)

def load_app(root):
 root=Path(root)
 if not (root/'app/core/customer_templates.py').is_file():raise ValueError('共享软件不可读取；未创建本地模板库')
 sys.path.insert(0,str(root))
 from app.core import customer_templates as store,requirements_import as requirements
 from app.core.template_permissions import assert_assistant_base
 return store,requirements,assert_assistant_base

def get_profile(store,identity):
 rows=[x for x in store.list_profiles(True) if x['id']==identity]
 if len(rows)!=1:raise ValueError('模板ID不存在或不唯一，请先list')
 return copy.deepcopy(rows[0])

def capability(root,requirements):
 schema=requirements.field_schema()
 schema['subtitle_layout']=['仅原文','仅译文','原文在上','译文在上']
 result={'skill_version':VERSION,'supported_fields':schema,'cloud_checked':False,
 'limits':['固定版式由统一执行器处理；原生时间优先，缺失边界时仅在原句范围内估算拆条并记录待核对','字幕源模式和翻译组合需按当前处理器实际支持范围核对；不是字段存在就代表组合可用','云端需要单独检查服务能力；此工具不读取密钥、不部署云端代码','质量标准不等于程序能保证的结果'],'module_hashes':{}}
 from app.core.utils import ass_effects
 try:
  from app.core.template_readiness import FIELDS,SCHEMA
  result['execution_mapping']={'schema':SCHEMA,'fields':FIELDS,'delivery_prefix_owner':'pagination','available':True}
 except ImportError:result['execution_mapping']={'available':False,'reason':'需安装配套统一执行优化'}
 result['appearance_extensions']={'solid_shadow':{'type':'boolean','roles':['Default','Secondary'],'executor_available':getattr(ass_effects,'SOLID_SHADOW_SUPPORTED',False),'default':False}}
 for rel in ('app/core/template_readiness.py','app/core/program_text_rules.py','app/core/customer_execution.py','app/core/requirements_import.py','app/core/utils/delivery_policy.py','app/thread/subtitle_thread.py','app/thread/cloud_full_process_thread.py'):
  f=Path(root)/rel;result['module_hashes'][rel]=hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
 return result

def execution_sha(draft,canvas):
 if not isinstance(canvas,dict) or set(canvas)!={'width','height'} or any(type(v)is not int or v<128 or v>4096 or v%2 for v in canvas.values()):raise ValueError('画布需为128~4096范围的偶数尺寸')
 return sha({'settings':draft['settings'],'program_text_rules':draft.get('program_text_rules',{}),'mode':draft.get('text_source_mode'),'style':draft['style_template'],'line_words':draft.get('english_line_words',6),'glossary':draft.get('glossary',''),'canvas':canvas})

def build(plan,store,requirements,assert_base):
 allowed={'operation','base_id','expected_revision','name','source_text','rules','model_requirements','quality_checks','pending','enable','appearance','canvas','replace_model_requirements'}
 if not isinstance(plan,dict) or set(plan)-allowed:raise ValueError('计划含未允许字段')
 if plan.get('operation') not in ('create','update'):raise ValueError('operation必须为create或update')
 if type(plan.get('expected_revision'))is not int:raise ValueError('必须提供读取时的expected_revision')
 base=get_profile(store,plan.get('base_id'));assert_base(base)
 if base['revision']!=plan['expected_revision']:raise ValueError('模板基础版本已变化，请重新读取并核对差异')
 if not isinstance(plan.get('source_text'),str) or not plan['source_text'].strip():raise ValueError('需要可核对的需求原文/操作者说明')
 if len(plan['source_text'])>45000:raise ValueError('需求文本超过45000字符')
 for k in ('model_requirements','pending'):
  rows=plan.get(k,[])
  if not isinstance(rows,list) or len(rows)>100 or any(not isinstance(x,str) or len(x)>2000 for x in rows):raise ValueError(k+'格式无效')
 quality=plan.get('quality_checks',base.get('requirements_import',{}).get('source',{}).get('quality_checks',[]))
 if not isinstance(quality,list) or len(quality)>100:raise ValueError('质量检查格式无效')
 for item in quality:
  if not isinstance(item,dict) or set(item)!={'requirement','method','coverage'} or item['coverage'] not in ('program','model','external') or any(not isinstance(v,str) or len(v)>2000 for v in item.values()):raise ValueError('质量检查需要requirement/method/coverage，不接受已验收断言')
 name=plan.get('name',base['name'])
 if not isinstance(name,str) or not name.strip():raise ValueError('模板名称不能为空')
 if plan['operation']=='create' and any(x['name'].casefold()==name.strip().casefold() for x in store.list_profiles(True)):raise ValueError('同名模板已存在，请修改原模板或明确另起名称')
 rules=plan.get('rules',[])
 # Reject invalid/conflicting field values; don't quietly downgrade them into a different rule.
 source={'filename':'Codex需求计划','sha256':sha(plan),'text':plan['source_text'],'warnings':[]}
 checked=requirements.validate_extraction({'name':name,'rules':rules,'pending':plan.get('pending',[])},source) if rules or plan.get('pending') else {'name':name,'rules':[],'pending':[],'source':{'filename':source['filename'],'sha256':source['sha256']}}
 if len(checked['rules'])!=len(rules):raise ValueError('部分字段值无效或冲突，未自动采用，请修改计划')
 checked['source']['quality_checks']=copy.deepcopy(quality)
 checked['source']['requirements_text']=plan['source_text']
 checked['source']['assistant_version']=VERSION
 checked['source']['model_requirements']=copy.deepcopy(plan.get('model_requirements',[]))
 if plan['operation']=='create':
  base=copy.deepcopy(base);base['requirements_import']={}
 draft=requirements.make_draft(base,checked,clean_text=plan['operation']=='create')
 if plan['operation']=='update':
  old_prompt=base.get('settings',{}).get('custom_prompt_text','')
  new_prompt=draft['settings'].get('custom_prompt_text','')
  if old_prompt and new_prompt.startswith(old_prompt):
   tail=[line for line in new_prompt[len(old_prompt):].splitlines() if line and line not in old_prompt.splitlines()]
   draft['settings']['custom_prompt_text']=old_prompt+('\n'+'\n'.join(tail) if tail else '')
 if plan.get('appearance'):
  from style_adapter import apply
  draft['style_template']=apply(draft['style_template'],plan['appearance'])
 if plan['operation']=='update':
  draft['id']=base['id'];draft['revision']=base['revision']
  previous=base.get('requirements_import',{})
  old_pending=set(previous.get('pending',[]));new_pending=set(checked['pending'])
  # Prior unresolved items must be explicitly accounted for in this plan's source text.
  for item in old_pending-new_pending:
   if item not in plan['source_text']:raise ValueError('移除旧待确认项必须在source_text逐项说明及交代解决依据：'+item)
  merged={r['field']:copy.deepcopy(r) for r in previous.get('rules',[])}
  merged.update({r['field']:r for r in checked['rules']});checked['rules']=list(merged.values())
  draft['requirements_import']['rules']=checked['rules']
  preserved_source=copy.deepcopy(previous.get('source',{}));preserved_source.update(checked['source']);checked['source']=preserved_source
  checked['source']['previous_record_sha256']=sha(previous)
  draft['requirements_import']['source']=checked['source']
 if plan.get('replace_model_requirements'):
  draft['settings']['custom_prompt_text']=draft['settings'].get('custom_prompt_text','').split('客户模型文字要求（仅处理台词/翻译，不改变时间或程序规则）：')[0].rstrip()
 if plan.get('model_requirements'):
  prompt=draft['settings'].get('custom_prompt_text','')
  additions='\n'.join(x for x in plan['model_requirements'] if x not in prompt.splitlines())
  draft['settings']['custom_prompt_text']=(prompt+'\n\n客户模型文字要求（仅处理台词/翻译，不改变时间或程序规则）：\n'+additions).strip() if additions else prompt
 if 'enable' in plan and type(plan['enable'])is not bool:raise ValueError('enable必须为布尔值')
 draft['enabled']=plan.get('enable',not bool(checked['pending']))
 if checked['pending'] and draft['enabled']:raise ValueError('仍有未解决硬要求，不可启用；可以enable=false保存草稿')
 if not draft.get('style_template','').strip():raise ValueError('缺少可执行的字幕样式快照')
 if draft['settings'].get('need_video') and draft['settings'].get('subtitle_layout')=='译文在上':raise ValueError('当前正式任务的双语SRT回读存在译文在上排列风险；先修复软件交接链路，不能用独立ASS预览冒充已接通，也不自动改布局')
 settings_before=base.get('settings',{}) if plan['operation']=='update' else {}
 changes={k:{'before':settings_before.get(k),'after':v} for k,v in draft['settings'].items() if settings_before.get(k)!=v}
 report={'operation':plan['operation'],'name':draft['name'],'base_id':base.get('id'),'base_revision':plan['expected_revision'],
  'plan_sha256':sha(plan),'settings_changes':changes,'style_changed':draft['style_template']!=base.get('style_template'),'program_rules_changed':draft.get('program_text_rules')!=base.get('program_text_rules'),
  'enabled_after_save':draft['enabled'],'pending':checked['pending'],'quality_checks':quality,
  'status_after_save':'已保存可选（尚未经成品验收）' if draft['enabled'] else '已保存停用草稿（任务中暂不可选）',
  'cloud_status':'创建模板不要求云端开机；实际选用云端时另行检查','episode_acceptance':'未执行','execution_sha256':execution_sha(draft,plan.get('canvas',{'width':1080,'height':1920}))}
 return draft,report

def publish_preview_bundle(receipt_path,appdata):
 import uuid,shutil
 receipt=json.loads(Path(receipt_path).read_text(encoding='utf-8'))
 source=Path(receipt_path).resolve().parent
 # Publish only confirmed derived assets, not original customer attachments/videos.
 relative='template-previews/'+uuid.uuid4().hex;target=Path(appdata)/relative;target.mkdir(parents=True)
 assets={}
 for name in ('preview.png','preview.mp4','canonical.json','confirmed.ass','sample.srt','layout-audit.json'):
  f=source/name
  expected=receipt.get('files',{}).get(str(f.resolve()))
  if expected is None or not f.is_file():raise ValueError('缺少已验证预览资产：'+name)
  if hashlib.sha256(f.read_bytes()).hexdigest()!=expected:raise ValueError('预览资产已变化')
  shutil.copy2(f,target/name)
  if hashlib.sha256((target/name).read_bytes()).hexdigest()!=expected:raise ValueError('共享预览回读校验失败')
  assets[name]=expected
 result={'relative_path':relative,'files':assets,'execution_sha256':receipt['execution_sha256'],'sample_kind':receipt['sample_kind'],'all_decoded_frames_equal':receipt['all_decoded_frames_equal'],'runtime':receipt['runtime']}
 (target/'evidence.json').write_text(text(result),encoding='utf-8')
 return result

def run(args):
 store,requirements,assert_base=load_app(args.app_root)
 if args.action=='list':return [{'id':p['id'],'name':p['name'],'revision':p['revision'],'enabled':p.get('enabled',True),'pending_count':len(p.get('requirements_import',{}).get('pending',[]))} for p in store.list_profiles(True)]
 if args.action=='show':return get_profile(store,args.id)
 if args.action=='capabilities':return capability(args.app_root,requirements)
 if args.action=='audit':
  from app.core.template_readiness import inspect_profile
  reports=[];skipped=[]
  for profile in store.list_profiles(True):
   r=inspect_profile(profile)
   if str(profile.get('settings',{}).get('transcribe_language','')).casefold() in ('arabic','ar') or '; VideoCaptionerOriginalLanguage: ar' in profile.get('style_template',''):skipped.append({'id':profile['id'],'name':profile['name']});continue
   reports.append(dict(r,enabled=profile.get('enabled',True),revision=profile.get('revision',0)))
  return {'templates':reports,'protected_skipped':skipped,'changes_applied':False,'cloud_checked':False}
 plan=json.loads(Path(args.plan).read_text(encoding='utf-8-sig'))
 draft,report=build(plan,store,requirements,assert_base)
 from preflight import inspect_plan
 report['preflight']=inspect_plan(args.app_root,draft,plan)
 if not report['preflight']['passed']:raise ValueError('前置能力自检未通过：'+str(report['preflight']['errors']))
 if args.action in ('check','preflight'):return report
 if not args.apply or args.expect_plan_sha!=report['plan_sha256']:raise ValueError('提交需要--apply及check返回的--expect-plan-sha；未保存')
 from workflow import read,validate
 from render_preview import validate_receipt
 if not getattr(args,'session',None):raise ValueError('需要四环节确认会话；未发布')
 state=read(args.session);receipt=validate(state,plan)
 validate_receipt(receipt,draft,plan,args.app_root)
 if plan.get('pending') or not draft['enabled']:raise ValueError('正式模板仍有未解决问题；请主动提问，不把停用草稿当成发布完成')
 evidence=publish_preview_bundle(receipt,store.STORE.parent)
 draft['requirements_import']['source']['confirmed_workflow']={'confirmed':state['confirmed'],'preview_bundle':evidence,'canvas':plan.get('canvas',{'width':1080,'height':1920})}
 try:
  saved=store.save_profile(draft,plan['expected_revision'] if plan['operation']=='update' else 0)
 except Exception as e:raise SaveVerificationError('共享保存异常，需回读核实：'+str(e),'unknown',draft.get('id')) from e
 try:
  reread=get_profile(store,saved['id'])
  for k in ('id','revision','settings','style_template','enabled'):
   if reread[k]!=saved[k]:raise RuntimeError('保存后回读不一致，请检查共享库，不能宣称发布成功')
  if execution_sha(reread,plan.get('canvas',{'width':1080,'height':1920}))!=report['execution_sha256']:raise RuntimeError('保存后的执行配置与预览不同，请检查，不能宣称完成')
  visible=any(p['id']==saved['id'] for p in store.list_profiles())
  if not visible:raise RuntimeError('保存后模板未出现在启用列表，请检查')
 except Exception as e:raise SaveVerificationError('已保存但验证未完成：'+str(e),True,saved.get('id')) from e
 return {'id':saved['id'],'name':saved['name'],'revision':saved['revision'],'enabled':saved['enabled'],'visible_in_task_list':visible,
  'status':report['status_after_save'],'cloud_status':report['cloud_status'],'episode_acceptance':'预览样例通过；新素材仍须检查','style_sha256':hashlib.sha256(saved['style_template'].encode()).hexdigest()}

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--app-root',default=os.environ.get('KAKA_APP_ROOT',DEFAULT_ROOT))
 parser.add_argument('action',choices=['list','show','capabilities','audit','preflight','check','commit']);parser.add_argument('--id');parser.add_argument('--plan');parser.add_argument('--apply',action='store_true');parser.add_argument('--expect-plan-sha');parser.add_argument('--out');parser.add_argument('--session')
 args=parser.parse_args()
 try:
  import contextlib
  with contextlib.redirect_stdout(sys.stderr):result=run(args)
  body=text(result)
  if args.out:Path(args.out).write_text(body,encoding='utf-8')
  print(body)
 except Exception as e:
  print(text({'error':str(e),'saved':getattr(e,'outcome',False),'id':getattr(e,'identity',None)}),file=sys.stderr);return 1
 return 0
if __name__=='__main__':sys.exit(main())
