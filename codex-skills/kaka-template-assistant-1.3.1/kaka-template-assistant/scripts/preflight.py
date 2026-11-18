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
 # One readiness report is shared with actual task creation, not a second policy.
 try:
  from app.core.template_readiness import inspect_profile,owner
  readiness=inspect_profile(draft);errors.extend(readiness['errors'])
  checks.append('需求已映射到实际任务使用的统一执行路由')
  for rule in plan.get('rules',[]):
   if owner(rule['field']) is None:errors.append('规则缺少明确执行环节：'+rule['field'])
 except ImportError:
  readiness=None;errors.append('当前软件缺少统一模板检查模块，需要先安装配套优化，未假装字段已接通')
 # Exercise this customer's real capacities. The 45-char/500-ms example is not universal.
 if policy.get('delivery_separate_languages')==1 and not errors:
  try:
   from app.core.bk_asr.asr_data import ASRData,ASRDataSeg
   maximum=min(policy.get('delivery_translation_max_chars',45),policy.get('_translation_visual_chars',100000)) if policy.get('delivery_keep_font_size') else policy.get('delivery_translation_max_chars',45)
   if maximum<1:raise ValueError('译文没有有效显示容量')
   def sample(text):
    cue=ASRDataSeg('A',0,3000);cue.translated_text=text;return ASRData([cue])
   _,ok=finalize(sample('中'*maximum),policy,draft['settings'].get('subtitle_layout'),True)
   _,bad=finalize(sample('中'*(maximum+1)),policy,draft['settings'].get('subtitle_layout'),True)
   if not ok['passed'] or bad['passed']:raise ValueError('实际译文容量边界不符合')
   checks.append('按当前模板实际容量验证译文边界：'+str(maximum)+'/'+str(maximum+1))
  except Exception as exc:errors.append('双语执行能力不足：'+str(exc))
 # Confirm optional centered-line output and lossless readback use the installed executor.
 requested_geometry={r:v['line_geometry'] for r,v in plan.get('appearance',{}).items()
                     if isinstance(v,dict) and 'line_geometry' in v}
 if requested_geometry:
  try:
   from app.core.utils.line_geometry import parse as parse_geometry
   from app.core.bk_asr.asr_data import ASRData,ASRDataSeg
   style=draft['style_template'];parsed=parse_geometry(style)
   for role,values in requested_geometry.items():
    if parsed.get(role)!=(values['baseline'],values['pitch']):raise ValueError('分行定位字段未编译')
   data=ASRData([ASRDataSeg('First line.\nSecond line.',0,1000,'第一行\n第二行')])
   exported=data.to_ass(style_str=style,layout='原文在上')
   if 'VCLineV1:' not in exported:raise ValueError('导出器未执行整组定位')
   reread=ASRData.from_ass(exported)
   if len(reread.segments)!=1 or ' '.join(reread.segments[0].text.split())!='First line. Second line.':raise ValueError('整组定位丢失原文')
   checks.append('整组定位经正式ASS导出及双行回读验证')
  except Exception as exc:errors.append('整组定位执行能力不足：'+str(exc))
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
 requested_solid={r:v['solid_shadow'] for r,v in plan.get('appearance',{}).items()
                  if isinstance(v,dict) and 'solid_shadow' in v}
 if requested_solid:
  try:
   from app.core.utils import ass_effects
   if not getattr(ass_effects,'SOLID_SHADOW_SUPPORTED',False):raise ValueError('当前软件未提供实体投影执行器')
   from app.core.bk_asr.asr_data import ASRData
   from app.core.customer_templates import style_with_line_limit
   style=style_with_line_limit(draft)
   modes=ass_effects.solid_shadow_styles(style)
   sample=ASRData.from_json({'1':{'start_time':0,'end_time':1000,'original_subtitle':'投影','translated_subtitle':'Shadow'}})
   ass=sample.to_ass(style_str=style,layout='原文在上')
   for role,wanted in requested_solid.items():
    marker='; VideoCaptionerSolidShadow: '+role+','+('1' if wanted else '0')
    if modes.get(role)!=wanted or marker not in ass:raise ValueError(role+'实体投影标记未保留')
    effect=ass_effects.parse_effects(style).get(role)
    if wanted and effect and effect['distance']>0:
     colour=next(l.split(',')[6].replace('&H','').zfill(8) for l in style.splitlines() if l.startswith('Style: '+role+','))
     event=next(l.split(',',9)[9] for l in ass.splitlines() if l.startswith('Dialogue: 0,') and l.split(',')[3]==role)
     if r'\1c&H'+colour[2:]+'&' not in event or r'\1a&H'+colour[:2]+'&' not in event:raise ValueError(role+'实体字形或透明度未接通')
   checks.append('已安装ASS导出器：显式实体投影、颜色与透明度保留通过')
  except Exception as exc:errors.append('实体投影未接通最终导出：'+str(exc))
 return {'passed':not errors,'errors':errors,'checks':checks,
  'delivery_policy':policy,'execution_readiness':readiness,
  'output_scope':'字幕文件，SRT不保存字体/阴影/位置' if not draft['settings'].get('need_video') else '合成视频，需实际预览',
  'not_verified':['真实音频识别、翻译、剧本匹配和100ms对齐','跨集人名一致性','其他电脑字体和云端执行能力'],
  'next':'核对需求覆盖表后，预览正常句和接近上限的长句；程序探针不是成品验收'}
