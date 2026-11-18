"""Check compiled requirements against the installed executor before preview/save.

Offline fixtures exercise rules, not ASR/translation services or audio alignment.
"""
import copy
from pathlib import Path
from types import SimpleNamespace as N

def inspect_plan(root,draft,plan):
 from app.core.utils.delivery_policy import read_policy,finalize,prepare
 errors=[];checks=[]
 policy=read_policy(draft.get('style_template','')) or {}
 for rule in plan.get('rules',[]):
  field=rule['field']
  if field.startswith('delivery_') and policy.get(field)!=rule['value']:
   errors.append('交付规则未编译到执行快照：'+field)
 # Use the installed executor to test a meaningful boundary for the selected mode.
 if policy.get('delivery_separate_languages')==1:
  if not draft['settings'].get('need_translate'):
   errors.append('分别计数的双语交付规则需要启用翻译')
  try:
   probe=copy.deepcopy(policy);probe.pop('delivery_keep_font_size',None);probe.pop('_translation_visual_chars',None)
   probe.update(delivery_max_line_chars=24,delivery_max_cue_chars=45,
    delivery_original_max_lines=2,delivery_translation_max_lines=1,
    delivery_translation_max_chars=45,delivery_pause_ms=500)
   cue=lambda t,z='',a=0,b=3000:N(text=t,translated_text=z,start_time=a,end_time=b)
   data=lambda *cs:N(segments=list(cs))
   text='I will always be here for you.'
   _,ok=finalize(data(cue(text,'中'*45)),probe)
   _,bad=finalize(data(cue(text,'中'*46)),probe)
   if not ok['passed'] or bad['passed']:raise ValueError('45/46字边界不符合')
   for gap,want in ((500,1),(501,2)):
    native=[cue('Hello',a=0,b=200),cue('world.',a=200+gap,b=1000+gap)]
    split,_=prepare(data(cue('Hello world.',a=0,b=1000+gap)),native,probe)
    if len(split.segments)!=want:raise ValueError('停顿500/501ms边界不符合')
   checks.append('已安装执行器：双语分别计数45/46字与500/501ms边界通过')
  except Exception as exc:errors.append('双语执行能力不足：'+str(exc))
 # Marker producers alone are insufficient: exported ASS must retain explicit bold.
 requested_bold={r:v['bold'] for r,v in plan.get('appearance',{}).items()
                 if isinstance(v,dict) and 'bold' in v}
 if requested_bold:
  try:
   from app.core.bk_asr.asr_data import ASRData
   from app.core.customer_templates import style_with_line_limit
   sample=ASRData.from_json({'1':{'start_time':0,'end_time':1000,
       'original_subtitle':'Test.','translated_subtitle':'测试。'}})
   ass=sample.to_ass(style_str=style_with_line_limit(draft),layout='原文在上')
   for role,bold in requested_bold.items():
    row=next(l for l in ass.splitlines() if l.startswith('Style: '+role+','))
    if (int(row.split(',')[7])!=0)!=bold:raise ValueError(role+'加粗导出不一致')
   checks.append('已安装ASS导出器：显式粗体保留通过')
  except Exception as exc:errors.append('加粗未接通最终导出：'+str(exc))
 return {'passed':not errors,'errors':errors,'checks':checks,
  'delivery_policy':policy,
  'output_scope':'字幕文件，SRT不保存字体/阴影/位置' if not draft['settings'].get('need_video') else '合成视频，需实际预览',
  'not_verified':['真实音频识别、翻译、剧本匹配和100ms对齐','跨集人名一致性','其他电脑字体和云端执行能力'],
  'next':'核对需求覆盖表后，预览正常句和接近上限的长句；程序探针不是成品验收'}
