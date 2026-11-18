"""Bounded task recovery; transient errors do not authorize rerunning unknown jobs."""
import json,re
from pathlib import Path

DEFAULT_LIMIT=2
MAX_LIMIT=5
DELAYS=(5,15,30,60,120)

def limit(value):
    try:return max(0,min(MAX_LIMIT,int(value)))
    except (TypeError,ValueError):return DEFAULT_LIMIT

def recovery(task):
    profile=task.customer_profile or {}
    if profile.get('cloud_full_process') or profile.get('cloud_synthesis_only'):
        return '核对原云端任务和已确认分块，继续查询、上传或下载'
    if task.checkpoint:
        if task.checkpoint[0]=='subtitled':return '复用现有字幕，仅重新合成'
        if task.checkpoint[0]=='transcribed':return '复用原始识别，继续文字处理；不重新转录'
    return '使用原素材和固定配置重新执行当前任务'

def classify(task,error):
    message=str(error);lower=message.casefold()
    blocked=('已取消','cancelled','canceled','interrupted','缺少','不存在','被移动','被删除','未支持','尚未支持','未启用','未安装','绑定','配置','格式无效','越权','校准结果','规则','不完整','截断','云端任务失败','permission denied','access is denied','no space left','disk full')
    permanent=any(word in lower for word in blocked) or bool(re.search(r'\b(?:400|401|403|404|413|422)\b',lower))
    transient=bool(re.search(r'\b(?:408|429|500|502|503|504)\b',lower)) or any(word in lower for word in ('timeout','timed out','connectionerror','connection reset','connection aborted','temporarily unavailable','remote disconnected','超时','连接中断','连接重置','临时限流','稍后重试'))
    answer={'category':'需人工处理','retryable':False,'recovery':recovery(task),'reason':'此错误没有安全的自动恢复依据，请查看原因后处理'}
    if re.search(r'\b412\b',lower) and any(label in lower for label in ('bilibili','b 接口','b接口')):
        answer.update(category='转录接口拒绝请求',reason='B接口拒绝获取识别结果，具体服务原因尚未确认；停止自动重交，避免重复转录',
                      recovery='有原始识别留存时，复用原始字幕继续文字处理；否则等待接口恢复后再手动重试')
        return answer
    if permanent:answer['reason']='素材、配置、规则或云端任务本身出错，自动重试不能解决';return answer
    if not transient:return answer
    profile=task.customer_profile or {};worker=task.current_thread
    cloud=profile.get('cloud_full_process') or profile.get('cloud_synthesis_only')
    if profile.get('_customer_execution') and not cloud:
        answer.update(reason='请求已由当前阶段有限重试；停止整任务自动重跑，避免重复请求。可从保存的识别或字幕手动恢复')
        return answer
    if cloud:
        job_id=getattr(worker,'_job_id',None);receipt=getattr(worker,'_receipt',None)
        if job_id:
            try:
                record=json.loads(Path(receipt).read_text(encoding='utf-8'))
                if record.get('id')!=job_id or record.get('cancel_pending'):raise ValueError()
            except (OSError,ValueError,TypeError):
                answer['reason']='原云端任务记录无法核对，停止自动提交，避免重复任务';return answer
    else:
        # A timed-out remote ASR call has no resumable identity. Do not resubmit it blindly.
        kind=getattr(task.task_type,'name','')
        if not task.checkpoint and kind not in ('SUBTITLE','RECOMPOSE'):
            answer['reason']='识别请求结果未确认且没有恢复点，停止自动重跑，避免重复转录';return answer
        if task.checkpoint:
            key,args=task.checkpoint
            try:path=args[0].output_path if key=='transcribed' else args[2] if key=='subtitled' else None
            except (AttributeError,TypeError,IndexError):path=None
            if not path or not Path(path).is_file():
                answer['reason']='已保存的恢复材料缺失，请检查字幕或原始识别文件';return answer
    answer.update(category='临时连接或服务异常',retryable=True,reason='可从保留的任务记录或恢复点继续')
    return answer

def error_detail(task):
    info=getattr(task,'failure_info',{})
    count=getattr(task,'auto_retry_count',0);maximum=getattr(task,'auto_retry_limit',DEFAULT_LIMIT)
    return ('失败步骤：'+str(info.get('stage',task.stage))+'\n原因：'+str(task.error_message)+
            '\n恢复方式：'+info.get('recovery',recovery(task))+'\n自动重试：'+str(count)+' / '+str(maximum)+
            '\n'+info.get('reason','请检查后手动恢复'))

def load_limit():
    from app.config import LOCAL_PROFILE_PATH
    try:return limit(json.loads((LOCAL_PROFILE_PATH/'task_retry.json').read_text(encoding='utf-8')).get('limit',DEFAULT_LIMIT))
    except (OSError,ValueError,TypeError,AttributeError):return DEFAULT_LIMIT

def save_limit(value):
    from app.config import LOCAL_PROFILE_PATH
    from app.core.concurrency import atomic_text
    atomic_text(LOCAL_PROFILE_PATH/'task_retry.json',json.dumps({'limit':limit(value)}))
