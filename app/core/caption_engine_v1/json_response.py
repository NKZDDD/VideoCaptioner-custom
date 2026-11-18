"""Strict response decoding with one bounded format-only recovery per segment."""
import hashlib,json,re

class ResponseFormatError(ValueError):
    pass

class ResponseRefusedError(ValueError):
    pass

def _pairs(items):
    result={}
    for key,value in items:
        if key in result:raise ResponseFormatError('模型JSON包含重复字段')
        result[key]=value
    return result

def _constant(value):
    raise ResponseFormatError('模型JSON包含非有限数值')

def decode_response(answer,stage,diagnostic):
    if not isinstance(answer,(dict,str)):
        choices=getattr(answer,'choices',None)
        if not choices:raise ResponseFormatError('接口返回缺少choices')
        choice=choices[0];message=getattr(choice,'message',None)
        finish=getattr(choice,'finish_reason',None)
        diagnostic['finish_reason']=str(finish)[:40]
        if finish=='content_filter' or getattr(message,'refusal',None):
            raise ResponseRefusedError('模型拒绝返回该分段，未作为格式异常重试')
        if finish=='length':raise ResponseFormatError('模型结果截断')
        answer=getattr(message,'content',None)
    if isinstance(answer,str):
        diagnostic['response_characters']=len(answer)
        diagnostic['response_sha256']=hashlib.sha256(answer.encode('utf-8')).hexdigest()
        if len(answer)>500000:raise ResponseFormatError('模型结果过大')
        text=answer.strip().lstrip('\ufeff').strip()
        fenced=re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```',text,re.I|re.S)
        if fenced:text=fenced.group(1).strip();diagnostic['code_fence_removed']=True
        if not text:raise ResponseFormatError('模型返回空内容')
        try:answer=json.loads(text,object_pairs_hook=_pairs,parse_constant=_constant)
        except json.JSONDecodeError as error:
            diagnostic.update(json_line=error.lineno,json_column=error.colno,json_position=error.pos)
            raise ResponseFormatError('模型返回JSON格式无效') from error
    if not isinstance(answer,dict):raise ResponseFormatError('模型结果不是JSON对象')
    try:size=len(json.dumps(answer,ensure_ascii=False,allow_nan=False))
    except (TypeError,ValueError) as error:raise ResponseFormatError('模型JSON包含无效值') from error
    if size>500000:raise ResponseFormatError('模型结果过大')
    field='splits' if stage=='semantic_split' else 'matches'
    if not isinstance(answer.get(field),list):raise ResponseFormatError('模型返回缺少有效'+field+'列表')
    if stage=='name_consistency' and not isinstance(answer.get('name_issues'),list):
        raise ResponseFormatError('模型返回缺少name_issues列表')
    return answer

def _format_failure(error):
    seen=set();current=error;found=False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current,'status_code',None) is not None:return False
        if isinstance(current,ResponseRefusedError):return False
        if isinstance(current,(ResponseFormatError,json.JSONDecodeError)):found=True
        current=current.__cause__ or current.__context__
    return found

def recover_json(config,stage,send,diagnostics):
    from .request_cache import notify
    label=config.get('_request_label',stage)
    for attempt in (1,2):
        diagnostic={'attempt':attempt};diagnostics.append(diagnostic)
        try:
            result=decode_response(send(),stage,diagnostic)
            diagnostic['status']='valid';return result
        except Exception as error:
            is_format=_format_failure(error)
            diagnostic.update(status='invalid_format' if is_format else 'request_failed',error_type=type(error).__name__)
            retry_network=False
            if config.get('subtitle_config',{}).get('_customer_execution'):
                from .llm_transport import retryable
                current=error;seen=set()
                while current is not None and id(current) not in seen:
                    seen.add(id(current));retry_network=retryable(current)
                    if retry_network:break
                    current=current.__cause__ or current.__context__
            if not is_format and not retry_network:raise
            if retry_network and attempt==2:raise RuntimeError(label+'：分段请求已达到重试上限（2次），已保留完成分段') from error
            if attempt==2:
                raise ValueError(label+'：两次返回均无法解析为有效JSON；已保留完成分段，未按成功导出') from error
            notify(config,label+('：临时连接异常' if retry_network else '：返回格式异常')+'，仅重试本分段（1/1）')
