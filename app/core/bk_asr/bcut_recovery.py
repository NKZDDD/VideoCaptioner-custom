"""Shared Bcut backoff and pending-task receipts; no credentials or bypasses."""
import hashlib,json,time,re,io,wave,contextlib
from app.config import SHARED_APPDATA_PATH
from app.core.concurrency import shared_lock,atomic_text

ROOT=SHARED_APPDATA_PATH/'bcut_recovery'

def read(path):
 if not path.exists():return {}
 try:
  value=json.loads(path.read_text(encoding='utf-8'))
  if not isinstance(value,dict):raise ValueError('expected object')
  return value
 except (ValueError,TypeError) as e:raise RuntimeError('B接口恢复记录损坏，已停止新提交，请检查记录') from e

def audio_key(audio):
 # WAV encoder metadata varies between PCs; identify the unchanged decoded PCM.
 try:
  with wave.open(io.BytesIO(audio),'rb') as source:
   spec=(source.getnchannels(),source.getsampwidth(),source.getframerate(),source.getnframes(),source.getcomptype())
   content=b'pcm-wave/v1:'+repr(spec).encode()+b'\0'+source.readframes(source.getnframes())
 except (wave.Error,EOFError):content=b'file/v1\0'+audio
 return hashlib.sha256(content).hexdigest()

def job_path(audio):
 directory=ROOT/'pending';directory.mkdir(parents=True,exist_ok=True)
 return directory/(audio_key(audio)+'.json')
def normalize_task_id(task_id):
 if type(task_id) not in (int, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(task_id)):
  raise ValueError('B接口返回无效任务编号')
 return str(task_id)

def load_job(audio):
 path=job_path(audio);value=read(path)
 if value:
  if value.get('audio_sha256')!=path.stem:raise RuntimeError('B接口恢复记录不匹配，已停止新提交')
  value['task_id']=normalize_task_id(value.get('task_id'))
 return value
def save_job(audio,task_id):
 task_id=normalize_task_id(task_id)
 path=job_path(audio);atomic_text(path,json.dumps({'task_id':task_id,'created_at':time.time(),'audio_sha256':path.stem},ensure_ascii=False))
 return task_id
def complete_job(audio):job_path(audio).unlink(missing_ok=True)

def result_path(audio):return ROOT/'results'/(audio_key(audio)+'.json')

def load_result(audio):
 path=result_path(audio)
 if not path.exists():return None
 record=read(path);result=record.get('result')
 if record.get('audio_sha256')!=audio_key(audio) or not isinstance(result,dict) or not isinstance(result.get('utterances'),list):
  raise RuntimeError('B接口共享成功结果校验失败，未重新提交')
 return result

def save_result(audio,result):
 if not isinstance(result,dict) or not isinstance(result.get('utterances'),list):raise ValueError('B接口结果结构无效')
 atomic_text(result_path(audio),json.dumps(dict(schema='bcut-result/v1',audio_sha256=audio_key(audio),saved_at=time.time(),result=result),ensure_ascii=False))

def preserve_probe_result(task_id,response):
 if str(response.get('state'))!='4':return False
 raw=response.get('result');result=json.loads(raw) if isinstance(raw,str) else raw
 if not isinstance(result,dict) or not isinstance(result.get('utterances'),list):raise ValueError('B接口探测完成结果无效；未提交新任务')
 for path in (ROOT/'pending').glob('*.json'):
  receipt=read(path)
  if receipt.get('task_id')!=task_id:continue
  with shared_lock(path,wait_seconds=10):
   receipt=read(path)
   if receipt.get('task_id')==task_id:
    receipt['completed_result']=result;atomic_text(path,json.dumps(receipt,ensure_ascii=False))
 return True

@contextlib.contextmanager
def provider_queue(callback, check_cancelled):
 """One full B request lifecycle per shared installation, released on crash."""
 from app.core.concurrency import ResourceBusyError
 ROOT.mkdir(parents=True,exist_ok=True);notified=False
 while True:
  check_cancelled()
  stack=contextlib.ExitStack()
  try:stack.enter_context(shared_lock(ROOT/'provider-queue',wait_seconds=0))
  except ResourceBusyError:
   stack.close()
   if not notified:callback(0,'等待共享B接口前一个转录完成；不会重复提交');notified=True
   time.sleep(.25);continue
  break
 try:
  check_cancelled()
  yield
 finally:stack.close()

def remaining():return max(0,int(read(ROOT/'service.json').get('blocked_until',0)-time.time()+0.999))
def blocked_task():return read(ROOT/'service.json').get('task_id')

def block(task_id,retry_after=None,stage='result'):
 try:delay=max(120,min(3600,int(retry_after)))
 except (ValueError,TypeError):delay=120
 ROOT.mkdir(parents=True,exist_ok=True);path=ROOT/'service.json'
 with shared_lock(path,wait_seconds=10):
  state=read(path);now=time.time()
  streak=min(6,int(state.get('rejection_streak',0))+1) if now-state.get('blocked_at',0)<3600 else 1
  delay=max(delay,min(1800,120*2**(streak-1)))
  state.update(blocked_at=now,blocked_until=max(state.get('blocked_until',0),now+delay),task_id=task_id or state.get('task_id'),stage=stage,rejection_streak=streak,reason='HTTP 412；具体服务限制原因未知')
  atomic_text(path,json.dumps(state,ensure_ascii=False))

def clear(observed_at):
 ROOT.mkdir(parents=True,exist_ok=True);path=ROOT/'service.json'
 with shared_lock(path,wait_seconds=10):
  state=read(path)
  if state.get('blocked_at',0)>observed_at:return
  state.update(blocked_until=0,task_id=None,rejection_streak=0);atomic_text(path,json.dumps(state))

def require_available():
 seconds=remaining()
 if seconds:raise RuntimeError(f'B 接口 HTTP 412 后暂停新请求，还需等待约{seconds}秒；已提交任务会保留，不重复上传')

def pace_query():
 """At most one result query per three seconds across this shared installation."""
 ROOT.mkdir(parents=True,exist_ok=True);path=ROOT/'service.json'
 with shared_lock(path,wait_seconds=15):
  state=read(path)
  if state.get('blocked_until',0)>time.time():raise RuntimeError('B 接口 HTTP 412 后暂停新请求；保留原任务，稍后恢复')
  wait=max(0,min(3,state.get('next_query_at',0)-time.time()))
  if wait:time.sleep(wait)
  state['next_query_at']=time.time()+3;atomic_text(path,json.dumps(state))

def defer_task(task):
 from app.core.entities import BatchTaskType,TranscribeModelEnum
 if getattr(task,'checkpoint',None) or getattr(task,'cancel_requested',False):return 0
 if task.task_type not in (BatchTaskType.TRANSCRIBE,BatchTaskType.TRANS_SUB,BatchTaskType.FULL_PROCESS):return 0
 profile=getattr(task,'customer_profile',None) or {}
 if profile.get('cloud_full_process'):return 0
 backend=profile.get('settings',{}).get('transcribe_model')
 if getattr(backend,'value',backend)!=TranscribeModelEnum.BIJIAN.value:return 0
 return remaining()


def archive_invalid_receipt(path,task_id):
 """Caller holds the receipt lock. Only a confirmed-invalid matching ID moves."""
 import uuid
 value=read(path)
 if not value or value.get('task_id')!=task_id:return False
 if value.get('completed_result') is not None:return False
 archive=ROOT/'invalid';archive.mkdir(parents=True,exist_ok=True)
 target=archive/(path.stem+'-'+uuid.uuid4().hex+'.json')
 record=dict(value,invalidated_at=time.time(),business_code=1500806,
  reason='服务端明确返回无效的任务ID；已归档，不再复用')
 atomic_text(target,json.dumps(record,ensure_ascii=False))
 if read(target)!=record:raise RuntimeError('失效任务归档验证失败，未移除恢复记录')
 path.unlink()
 return True

def forget_invalid_probe(task_id,observed_at):
 """Do not clear a newer 412 block belonging to a concurrent query."""
 path=ROOT/'service.json'
 with shared_lock(path,wait_seconds=10):
  state=read(path)
  if state.get('task_id')==task_id and state.get('blocked_at',0)<=observed_at:
   state.update(blocked_until=0,task_id=None)
   atomic_text(path,json.dumps(state))
