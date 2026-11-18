"""Only script_correct is handled here; legacy Arabic processing is untouched."""
import copy,json,re
from pathlib import Path
from types import SimpleNamespace
from .matching import process_script_correction
from .numbers import normalize_arabic_numbers
from .common import atomic_text

def validate_config(config):
 if config.get('text_source_mode')!='script_correct':return
 from .template_rules import resolve_rules
 resolve_rules(config)
 settings=config.get('subtitle_config',{})
 if not isinstance(config.get('script_reference'),str) or not config['script_reference'].strip():
  raise ValueError('原声定位＋剧本校正必须提供本集剧本')
 if not all(isinstance(settings.get(k),str) and settings[k].strip() for k in ('base_url','api_key','llm_model','subtitle_style')):
  raise ValueError('剧本校正需要完整LLM配置和冻结字幕样式')
 if settings.get('need_translate'):
  raise ValueError('此版云端剧本校正不支持同时翻译，请拆分处理；未退回其他模式')
 for key in ('need_split','need_remove_punctuation'):
  if key in settings and type(settings[key])is not bool:raise ValueError('模板开关必须为布尔值')
 limit=settings.get('max_word_count_cjk',12)
 if type(limit)is not int or not 1<=limit<=200:raise ValueError('中文长度上限无效')

def _legacy_v7_process(data,config,job,request=None):
 from .layout import enforce_script_template_layout
 validate_config(config)
 settings=config['subtitle_config']
 values=dict(need_split=True,max_word_count_cjk=12,need_remove_punctuation=False,subtitle_layout=None,custom_prompt_text='')
 values.update(settings);values['text_source_mode']='script_correct'
 options=SimpleNamespace(**values)
 # Zero-length punctuation is metadata, not an audible interval. Attach only
 # to the preceding cue at exactly its edge; reject every other invalid span.
 original=[];attached=0
 for seg in copy.deepcopy(data.segments):
  if seg.start_time==seg.end_time and re.fullmatch(r'[^\w\s]+',seg.text or ''):
   if original and seg.start_time==original[-1].end_time:
    original[-1].text+=seg.text;attached+=1;continue
   raise ValueError('标点无有效所属时间段，保留原识别待检查')
  original.append(seg)
 data.segments=original
 result=process_script_correction(data,config['script_reference'],options,Path(job)/'source.剧本校正报告.json',request=request)
 display=[]
 # Opt-in Chinese template policy only. No Arabic/English/global migrations.
 chinese='【中文通用竖版替换规则V1】' in (options.custom_prompt_text or '')
 if chinese:
  for cue in result.segments:
   before=cue.text;cue.text=normalize_arabic_numbers(before)
   if before!=cue.text:display.append(dict(before=before,after=cue.text,start_ms=cue.start_time,end_ms=cue.end_time))
 result,layout=enforce_script_template_layout(result,original,options)
 if options.need_remove_punctuation:result.remove_punctuation()
 result.to_ass(save_path=str(Path(job)/'subtitle.ass'),style_str=options.subtitle_style,layout=options.subtitle_layout)
 result.to_srt(save_path=str(Path(job)/'source.srt'),layout=options.subtitle_layout)
 atomic_text(Path(job)/'source.模板执行报告.json',json.dumps(dict(mode='script_correct',number_changes=display,layout=layout,attached_zero_length_punctuation=attached,timing='source_span_edges_only'),ensure_ascii=False,indent=2))
 return result

def source_data(response):
 from app.core.bk_asr.asr_data import ASRData,ASRDataSeg
 from app.core.bk_asr.multilingual_asr import parse_segments
 import math
 words=[]
 for segment_id,segment in enumerate(response.get('segments',[])):
  native=segment.get('words')
  if not isinstance(native,list) or not native:
   # Preserve the whole source sentence; never invent internal timestamps.
   cue=parse_segments({'segments':[segment]})[0]
   cue.asr_segment_id=segment_id
   if segment.get('speaker_id') is not None:cue.speaker_id=segment['speaker_id']
   words.append(cue);continue
  for word in native:
   text=str(word.get('word',word.get('text',''))).strip()
   if not text:continue
   a,b=word.get('start'),word.get('end')
   if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)) or a<0 or b<a:
    raise ValueError('原生词时间无效，未推算替代时间')
   cue=ASRDataSeg(text,round(a*1000),round(b*1000))
   cue.asr_segment_id=segment_id
   speaker=word.get('speaker_id',segment.get('speaker_id'))
   if speaker is not None:cue.speaker_id=speaker
   words.append(cue)
 if not words:raise ValueError('原生识别为空')
 return ASRData(words)


def process(data,config,job,request=None):
 from .flow_v8 import process as corrected_process
 return corrected_process(data,config,job,request=request)
