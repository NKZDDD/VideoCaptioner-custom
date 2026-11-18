"""Template outcome routing and early consistency checks; no service calls."""
from app.core.customer_execution import compile_profile
SCHEMA='template-readiness/v1'
FIELDS={
 'program_text_rules':'display_text','text_source_mode':'source_text','transcribe_language':'transcription','transcribe_model':'transcription',
 'need_translate':'translation','target_language':'translation','translator_service':'translation',
 'need_optimize':'text_improvement','custom_prompt_text':'text_improvement',
 'english_line_words':'pagination','capitalization':'text_improvement','need_split':'pagination','split_type':'pagination','max_word_count_cjk':'pagination','max_word_count_english':'pagination',
 'needs_remove_punctuation':'display_text','need_remove_punctuation':'display_text','number_display_policy':'display_text',
 'subtitle_layout':'layout','subtitle_style_name':'appearance','need_video':'delivery','soft_subtitle':'delivery','video_quality':'delivery'}
def owner(field):
 if field.startswith('delivery_'):return 'pagination'
 if field in ('font_name','font_size_ass','font_color','alignment'):return 'appearance'
 return FIELDS.get(field)
def inspect_profile(profile):
 c=compile_profile(profile);s=profile.get('settings',{});style=profile.get('style_template','');mode=profile.get('text_source_mode');p=c.get('presentation') or {};errors=[];warnings=[]
 protected=any('; VideoCaptionerOriginalLanguage: '+lang in style for lang in ('ar','hi')) or str(s.get('transcribe_language','')).casefold() in ('arabic','ar','hindi','hi')
 translate=bool(s.get('need_translate'));layout=str(s.get('subtitle_layout','仅原文'))
 if translate and not str(s.get('target_language','')).strip():errors.append('启用翻译但未指定目标语言')
 # The two ordering options fall back to the original when there is no translation.
 if not translate and layout=='仅译文':errors.append('当前排列需要译文，但模板未启用翻译')
 if p.get('delivery_separate_languages')==1 and not translate:errors.append('模板设置了双语分别计数，但未启用翻译')
 if p.get('delivery_separate_languages')==1 and layout in ('仅原文','仅译文'):errors.append('双语分别排版与单语交付排列冲突')
 if p.get('delivery_separate_languages')==1:
  cap=p.get('delivery_original_max_lines',2)+p.get('delivery_translation_max_lines',1)
  if p.get('delivery_max_lines',cap)<cap:errors.append('合计行数小于中英文分别允许的行数之和，需要确认总行数含义')
 if p.get('delivery_keep_font_size') and p.get('_translation_visual_chars',1)<1:errors.append('当前字号和安全区域无法容纳一个译文字形')
 if p and p.get('delivery_keep_font_size') and p.get('_translation_visual_chars',999)<p.get('delivery_translation_max_chars',45):warnings.append('字符上限不是单行容量；保持字号时由程序按实际宽度拆条')
 if profile.get('requirements_import',{}).get('pending'):warnings.append('此模板仍有历史待确认项；保持当前启停状态，修改发布前逐项核对')
 if '统一替换为' in str(s.get('custom_prompt_text','')) and c['text_rules']['display']['substitution_policy']=='none' and not profile.get('program_text_rules',{}).get('literal_replacements'):warnings.append('自定义词语替换当前属于模型文字要求；没有结构化程序规则时，不能当成已确定性执行')
 if not mode:warnings.append('文字来源沿用原流程；本次不擅自改为剧本校正或完全替换')
 if translate and layout=='仅原文':warnings.append('会计算译文但当前交付只显示原文，请在建模板时确认是否需要翻译')
 routes=[{'stage':'transcription','owner':'selected_asr','requirements':['实际语音、语言与原生时间']},
 {'stage':'source_text','owner':mode or 'original_workflow','requirements':['剧本用途、原声与剧本文字取舍']},
 {'stage':'display_text','owner':'program','requirements':['已支持的数字、替换与标点规则']},
 {'stage':'pagination','owner':'program','requirements':['语义边界、行数、字符容量、固定字号与时间范围']},
 {'stage':'translation','owner':'selected_translator' if translate else 'skipped','requirements':['目标语言、上下文、对应中文参考']},
 {'stage':'delivery','owner':'shared_ass_export_and_renderer','requirements':['语言上下顺序、字体、位置与输出类型']}]
 return {'schema':SCHEMA,'template_id':profile.get('id'),'name':profile.get('name'),'passed':not errors,'errors':errors,'warnings':warnings,'protected':protected,'executor':'protected_original' if protected else 'customer_execution','source_strategy':mode or 'original_workflow','required_inputs':['对应本集剧本'] if mode in ('strict','hybrid','script_correct') else [],'routes':routes,'presentation':p,'recovery':{'optional_text_stage':'保留本阶段输入并记录','format_response':'保留成功编号，有限上下文恢复','long_cue':'原生边界优先；缺少边界时仅在原句范围估算并标记','essential_missing_content':'停止必要步骤，保留恢复材料，不伪造成已完成'},'verified_scope':'配置与路由检查；不代表音频、译文或外部服务验收'}
def require_ready(profile):
 report=inspect_profile(profile)
 if report['errors']:raise ValueError('模板要求存在冲突，请先在建模板环节解决：'+'；'.join(report['errors']))
 return report
