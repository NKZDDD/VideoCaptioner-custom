"""Review foreign-script ASR remnants without guessing words or acoustic meaning."""
import copy,re,unicodedata

def english_source(config):
    language=getattr(config,'_source_language',None)
    if language is not None:
        return str(getattr(language,'value',language)).strip().casefold() in {'en','en-us','en-gb','english','英语'}
    style=getattr(config,'subtitle_style','') or ''
    if any('; VideoCaptionerOriginalLanguage: '+lang in style for lang in ('ar','hi')):
        return False
    return '; VideoCaptionerEnglishSemanticWrap: 1' in style or '; VideoCaptionerOriginalLanguage: en' in style

def key(text):
    return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))

def review(data,config,reference=''):
    """Return copied output and evidence. No API requests and no invented translation.

    Exclude only unconfirmed standalone sound tokens absent from the script.
    Keep lexical/mixed/confirmed text for review, even if its script is unusual.
    """
    if not english_source(config):return data,[]
    result=copy.deepcopy(data);kept=[];records=[]
    for cue in result.segments:
        text=cue.text or ''
        if not re.search(r'[\u3400-\u9fff]',text):kept.append(cue);continue
        token=key(text)
        explicit=bool(token and token in (reference or ''))
        sound=bool(re.fullmatch(r'(?:嗯+|呃+|唔+|呣+|啊+)',token))
        quarantine=sound and not explicit and not getattr(cue,'script_corrected',False)
        record={'kind':'英文原声异常语言待核对','text':text,'start_ms':cue.start_time,'end_ms':cue.end_time,
          'source':'asr_or_correction_output','expected_source_language':'en','acoustic_verdict':'unknown',
          'action':'excluded_from_subtitle_pending_review' if quarantine else 'retained_pending_review',
          'reason':'未确认且剧本未明确包含的独立中文拟声识别，保留原文与时间供听检，不猜测英文台词' if quarantine else '可能为真实中文、混合语言或已确认剧本内容，保留原文，不强制翻译或删除'}
        records.append(record)
        if not quarantine:kept.append(cue)
    if records and not kept:
        for record in records:
            record['action']='retained_pending_review'
            record['reason']='全部结果均为待核对内容，保留原识别，避免生成空字幕或阻断整批任务'
        kept=result.segments
    result.segments=kept
    return result,records
