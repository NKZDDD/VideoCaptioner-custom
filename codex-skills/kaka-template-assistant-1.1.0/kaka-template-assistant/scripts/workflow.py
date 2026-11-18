"""Durable stage confirmations. User replies are recorded by Codex, never inferred."""
import argparse,hashlib,json
from pathlib import Path
PHASES=('goal','rules','preview','publish')
def sha(v):return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name(p.name+'.tmp');t.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8');t.replace(p)
def initial(plan):return {'schema':1,'plan':plan,'plan_sha256':sha(plan),'confirmed':{},'preview_receipt':None,'events':[]}
def sync(state,plan):
 if sha(plan)==state['plan_sha256']:return state
 old=state['plan'];keys={k for k in set(old)|set(plan) if old.get(k)!=plan.get(k)}
 changed_rules={r['field'] for r in old.get('rules',[])+plan.get('rules',[]) if next((x for x in old.get('rules',[]) if x['field']==r['field']),None)!=next((x for x in plan.get('rules',[]) if x['field']==r['field']),None)}
 semantic=keys-{'source_text','expected_revision'}
 if semantic and semantic<= {'name','enable'}:first=3
 elif (semantic and semantic<= {'appearance','name','enable','canvas'}) or (semantic<={'rules','name','enable'} and changed_rules and changed_rules<={'font_name','font_size_ass','font_color','alignment'}):first=2
 elif semantic and semantic<={'rules','model_requirements','quality_checks','pending','name','enable','replace_model_requirements'} and not changed_rules.intersection({'transcribe_language','target_language','need_translate','text_source_mode'}):first=1
 else:first=0
 for phase in PHASES[first:]:state['confirmed'].pop(phase,None)
 if first<3:state['preview_receipt']=None
 state['events'].append({'kind':'changed','recheck_from':PHASES[first]});state.update(plan=plan,plan_sha256=sha(plan));return state

def confirm(state,phase,reply):
 if phase not in PHASES or not isinstance(reply,str) or not reply.strip():raise ValueError('必须记录用户明确确认回复')
 for prior in PHASES[:PHASES.index(phase)]:
  if prior not in state['confirmed']:raise ValueError('先完成环节：'+prior)
 if phase in ('preview','publish') and not state.get('preview_receipt'):raise ValueError('尚未生成实际预览')
 if phase=='publish' and state['plan'].get('pending'):raise ValueError('还有未解决问题，请主动提问并更新方案')
 state['confirmed'][phase]={'reply':reply.strip(),'plan_sha256_at_confirmation':state['plan_sha256']};return state

def validate(state,plan):
 if state['plan_sha256']!=sha(plan):raise ValueError('方案已变化，请sync并重验受影响环节')
 if any(p not in state['confirmed'] for p in PHASES):raise ValueError('四环节尚未全部得到用户确认')
 if not state.get('preview_receipt'):raise ValueError('缺少真实预览验收记录')
 return state['preview_receipt']

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['init','sync','confirm','attach-preview','status']);p.add_argument('--session',required=True);p.add_argument('--plan');p.add_argument('--phase',choices=PHASES);p.add_argument('--reply');p.add_argument('--receipt');a=p.parse_args()
 try:
  if a.action=='init':
   if Path(a.session).exists():raise ValueError('会话已存在，请继续原会话或选择新路径')
   s=initial(read(a.plan))
  else:s=read(a.session)
  if a.action=='sync':s=sync(s,read(a.plan))
  if a.action=='confirm':s=confirm(s,a.phase,a.reply)
  if a.action=='attach-preview':
   if not all(k in s['confirmed'] for k in ('goal','rules')):raise ValueError('先确认目标及规则')
   if not Path(a.receipt).is_file():raise ValueError('预览记录不存在')
   s['preview_receipt']=str(Path(a.receipt).resolve());s['confirmed'].pop('preview',None);s['confirmed'].pop('publish',None)
  if a.action!='status':write(a.session,s)
  print(json.dumps({'confirmed':list(s['confirmed']),'next':next((k for k in PHASES if k not in s['confirmed']),'ready'),'plan_sha256':s['plan_sha256'],'events':s['events'][-3:]},ensure_ascii=False))
 except Exception as e:raise SystemExit(str(e))
if __name__=='__main__':main()
