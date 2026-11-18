"""Resumable upload with small requests and server-acknowledged progress."""
import hashlib,time,logging,re,io
try:
 from app.core.utils.logger import setup_logger
 logger=setup_logger("cloud_upload")
except ImportError:
 logger=logging.getLogger("cloud_upload")
def describe(error):
 response=getattr(error,"response",None)
 detail=str(error)
 detail=re.sub(r"https?://[^\s)\]\"']+",'[服务地址]',detail)
 detail=re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+",'Bearer [隐藏]',detail)
 detail=re.sub(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",r'\1[隐藏]',detail)
 detail=detail.replace('\r',' ').replace('\n',' ')[:400]
 return type(error).__name__+(" HTTP "+str(response.status_code) if response is not None else "")+('：'+detail if detail else '')
from pathlib import Path
import requests
CHUNK=512*1024
MIN_CHUNK=128*1024
MAX_CHUNK=1024*1024
GROW_AFTER=8

def upload_resumable(session,base,headers,video,payload,stopped,progress):
 video=Path(video);stat=video.stat();stamp=(stat.st_size,stat.st_mtime_ns)
 jid=payload['submission_id'];url=base+'/cloud/uploads/'+jid
 def check_stop():
  if stopped():raise InterruptedError('已暂停上传；已确认的分块可在重试时续传')
 def call(method,target,**kwargs):
  last=None
  for attempt in range(4):
   check_stop()
   try:
    r=session.request(method,target,headers=headers,timeout=(10,45),allow_redirects=False,**kwargs)
    if r.status_code==507:raise ValueError('云端空间不足（HTTP 507），请释放空间后再试')
    if r.status_code>=500:raise requests.HTTPError('云端暂时不可用：HTTP '+str(r.status_code),response=r)
    if r.status_code>=400:raise ValueError('分块上传失败（HTTP '+str(r.status_code)+'）：'+r.text[:300])
    return r.json()
   except requests.RequestException as e:
    last=e
    logger.warning("上传请求失败：方法=%s 次数=%s/4 原因=%s",method,attempt+1,describe(e))
    for _ in range(5*(attempt+1)):
     check_stop();time.sleep(.2)
  raise RuntimeError('上传连接连续中断，已确认的分块已保留，可重试续传：'+str(last))
 progress(0,stat.st_size,0,'校验素材，准备断点上传')
 digest=hashlib.sha256()
 with video.open('rb',buffering=1024**2) as f:
  while data:=f.read(1024**2):check_stop();digest.update(data)
 if (video.stat().st_size,video.stat().st_mtime_ns)!=stamp:raise ValueError('素材在校验时发生变化，未上传')
 state=call('POST',url,json={'size':stat.st_size,'sha256':digest.hexdigest(),'payload':payload})
 if state.get('submitted'):return state['id']
 method='POST' if state.get('post_chunks') is True else 'PUT'
 chunk_url=url+'/chunk' if method=='POST' else url
 offset=state['offset'];start_offset=offset;started=time.monotonic();failures=0;chunk_size=CHUNK;success_streak=0;growth_wait=GROW_AFTER
 def emit():
  speed=max(0,offset-start_offset)/max(.01,time.monotonic()-started)
  progress(offset,stat.st_size,speed,'上传素材到云端')
 emit()
 with video.open('rb',buffering=1024**2) as f:
  while offset<stat.st_size:
   check_stop();f.seek(offset);data=f.read(min(chunk_size,stat.st_size-offset))
   if not data:raise ValueError('素材被截断，上传停止')
   try:
    # Keep multipart/Content-Length compatibility, but let requests read the
    # body in small socket writes instead of one large TLS sendall call.
    multipart=requests.Request('POST','http://localhost',files={'file':('chunk.bin',data,'application/octet-stream')}).prepare()
    with io.BytesIO(multipart.body) as body:
     r=session.request(method,chunk_url,params={'offset':offset},headers={**headers,'X-Chunk-SHA256':hashlib.sha256(data).hexdigest(),'Content-Type':multipart.headers['Content-Type'],'Content-Length':str(len(multipart.body))},data=body,timeout=(10,45),allow_redirects=False)
    if r.status_code==409 or r.status_code>=500:raise requests.HTTPError('分块请求返回HTTP '+str(r.status_code),response=r)
    if r.status_code!=200:raise ValueError('分块上传失败：'+r.text[:300])
    new_offset=r.json()['offset']
    if not offset<new_offset<=offset+len(data):raise ValueError('云端返回的断点无效')
    offset=new_offset;failures=0;emit()
    success_streak+=1
    if success_streak>=growth_wait and chunk_size<MAX_CHUNK:
     chunk_size=min(MAX_CHUNK,chunk_size*2);success_streak=0
     logger.info('上传连接稳定，分块恢复为%sKB',chunk_size//1024)
   except requests.RequestException as error:
    failures+=1
    reason=describe(error)
    success_streak=0
    if isinstance(error,(requests.ConnectionError,requests.Timeout)):
     chunk_size=max(MIN_CHUNK,chunk_size//2)
     growth_wait=min(128,max(32,growth_wait*2))
    logger.warning("分块传输失败：方法=%s offset=%s 连续次数=%s 原因=%s",method,offset,failures,reason)
    if failures>4:raise RuntimeError('分块上传连续失败（'+method+'；'+reason+'），已保存断点，请检查云端或网关后重试')
    state=call('GET',url)
    if state.get('submitted'):return state['id']
    confirmed=state['offset']
    if confirmed>offset:failures=0
    offset=confirmed
    if not 0<=offset<=stat.st_size:raise ValueError('云端返回的断点无效')
    progress(offset,stat.st_size,0,'分块重连（'+method+'；'+reason+'；每块'+str(chunk_size//1024)+'KB），已核对断点')
    time.sleep(.5)
 if (video.stat().st_size,video.stat().st_mtime_ns)!=stamp:raise ValueError('素材在上传时被修改，未提交处理')
 progress(offset,stat.st_size,0,'上传完成，云端校验素材')
 result=call('POST',url+'/complete')
 return result['id']
