"""Explicit, directory-scoped temporary delivery with honest quality warnings."""
import copy,json,re,datetime
from pathlib import Path
from app.core.concurrency import atomic_text,resource_key
from app.config import SHARED_APPDATA_PATH
MARKER='; VideoCaptionerTemporaryDelivery: 1'
FLAG=SHARED_APPDATA_PATH/'temporary_delivery.json'

def context_for(video,config):
    if not video or getattr(config,'text_source_mode',None)!='script_correct' or not getattr(config,'need_translate',False):return None
    style=getattr(config,'subtitle_style','') or ''
    if '; VideoCaptionerOriginalLanguage: ar' in style or '; VideoCaptionerOriginalLanguage: hi' in style:return None
    if not re.search(r'^; VideoCaptionerDeliveryV1:',style,re.M):return None
    if not FLAG.is_file():return None
    d=json.loads(FLAG.read_text(encoding='utf-8-sig'))
    if d.get('schema')!='temporary-delivery/v1' or d.get('enabled') is not True:return None
    expires=datetime.datetime.fromisoformat(d['expires_at'])
    if datetime.datetime.now(datetime.timezone.utc)>=expires:return None
    scope=resource_key(Path(video).parent/'__temporary_delivery_scope__')
    if scope not in d.get('directory_keys',[]):return None
    return {'mode':'temporary_delivery','video':str(video),'warnings':[],'expires_at':d['expires_at']}

def style_for(style):
    return style if MARKER in style.splitlines() else style+'\n'+MARKER+'\n'

def warn(context,kind,**details):
    item=dict(kind=kind,**details)
    if item not in context['warnings']:context['warnings'].append(item)

def persist(context):
    if context:
        atomic_text(Path(context['video']).with_suffix('.临时交付检查.json'),json.dumps(context,ensure_ascii=False,indent=2))

def fallback_matching(data,error,context):
    from app.core.caption_engine_v1.source_timing import normalize_source_timing
    result=copy.deepcopy(data);result.segments,_=normalize_source_timing(result.segments)
    warn(context,'剧本校正未完成，使用原声识别',error_type=type(error).__name__)
    result.script_review_report={'status':'completed_with_review','summary':'临时交付：剧本校正未完成，保留原声识别；未经人工验收','review_items':copy.deepcopy(context['warnings']),'warnings':[]}
    persist(context);return result

def prepare_delivery(data,native,policy,context):
    from app.core.utils import delivery_policy as d
    from app.core.bk_asr.asr_data import ASRData
    from app.core.utils.punctuation_boundaries import repair
    # Structural timing remains mandatory, even when text/format checks are advisory.
    d.timing_audit(data,native,'temporary_input')
    output=copy.deepcopy(data);output.segments=[];changes=[]
    for cue in data.segments:
        one=copy.deepcopy(data);one.segments=[copy.deepcopy(cue)]
        try:
            paged,changed=d.prepare(one,native,policy);output.segments.extend(paged.segments);changes.extend(changed);continue
        except ValueError as error:
            spans=[copy.deepcopy(c) for c in native if c.start_time>=cue.start_time and c.end_time<=cue.end_time and d.key(c.text)]
            if not spans:raise ValueError('临时通道也缺少原声时间，未生成猜测字幕') from error
            groups=[];group=[]
            for span in spans:
                proposal=d.delivery_text(' '.join(c.text for c in group+[span]))
                if group and (len(proposal)>min(32,policy.get('delivery_max_cue_chars',45)) or span.start_time-group[-1].end_time>policy.get('delivery_pause_ms',500) or len(group)>=6 or re.search(r'[.!?]["”]?$',group[-1].text)):
                    groups.append(group);group=[]
                group.append(span)
            if group:groups.append(group)
            for group in groups:
                item=copy.deepcopy(group[0]);item.text=d.delivery_text(' '.join(c.text for c in group));item.end_time=group[-1].end_time;item.translated_text=''
                for key in ('_delivery_native_units','alignment_units','script_corrected','asr_ids'):
                    if hasattr(item,key):delattr(item,key)
                # Source-native groups remain anchored to genuine start/end edges.
                output.segments.append(item)
            warn(context,'无法可靠分页的校正片段恢复原声',start_ms=cue.start_time,end_ms=cue.end_time,corrected_text=cue.text,native_text=' '.join(c.text for c in spans),pages=len(groups),reason=str(error))
    output,_=repair(output);d.timing_audit(output,native,'temporary_pagination');persist(context)
    return output,changes

def finalize_delivery(data,policy,layout,context):
    from app.core.utils import delivery_policy as d
    from app.core.llm_permissions import safe_text
    result,report=d.finalize(data,policy,layout)
    for cue in result.segments:
        if not safe_text(cue.text,cue.text) or not safe_text(cue.translated_text,cue.translated_text):
            raise ValueError('临时通道收到空文字或非法控制指令，未写成品')
    hard={'时间无效或与上一条重叠','译文为空','不允许字幕控制标记'}
    if any(hard.intersection(item['problems']) for item in report['issues']):raise ValueError('临时通道结构检查失败：'+str(report['issues'][0]))
    for item in report['issues']:warn(context,'字幕格式待检查',**item)
    capacity={'原文行数超限','原文字符数超限','译文字符数超限','译文行数超限','译文显示宽度超限','总行数超限','单条总字符数超限','单行字符数超限'}
    layout_issues=[i for i in report['issues'] if capacity.intersection(i['problems']) or any(x.startswith('完整单词超过') for x in i['problems'])]
    if layout_issues:raise ValueError('中文一行、英文最多两行尚未满足，需要拆条：'+str(layout_issues[0]))
    report.update(mode='temporary_delivery',quality_passed=report['passed'],passed=True,advisory_issues=report['issues'],font_size='preserved',line_limits='Chinese=1;English<=2')
    context['status']='generated_with_review' if context['warnings'] else 'generated';persist(context)
    return result,report

def repair_translation(data,before,native,policy,layout,translate,on_report,context):
    from app.core.utils.delivery_policy import repair_translation as repair
    # Layout is a customer requirement, including in the temporary channel.
    # Re-translate only newly split cues; never add Chinese display lines.
    result=repair(data,before,native,policy,layout,translate,on_report)
    checked,_=finalize_delivery(result,policy,layout,context)
    return checked


def export_audit(data,native,policy,style,layout,context):
    from app.core.utils.delivery_policy import timing_audit,key
    from app.core.bk_asr.asr_data import ASRData
    result,report=finalize_delivery(data,policy,layout,context);audit=timing_audit(result,native,'temporary_export')
    restored=ASRData.from_ass(result.to_ass(style_str=style,layout=layout))
    if len(result.segments)!=len(restored.segments):raise ValueError('临时交付ASS回读条数不一致')
    for a,b in zip(result.segments,restored.segments):
        if abs(a.start_time-b.start_time)>9 or abs(a.end_time-b.end_time)>9 or key(a.text)!=key(b.text) or key(a.translated_text)!=key(b.translated_text):raise ValueError('临时交付ASS回读文字、语言或时间不一致')
    audit.update(mode='temporary_delivery',ass_roundtrip=True,quality_verified=False,warnings=len(context['warnings']));return audit
