"""Upload a frozen task, poll cloud execution, download artifacts. No local FFmpeg."""
import copy
import dataclasses
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
import zipfile

from PyQt5.QtCore import QThread, pyqtSignal
import requests
from app.config import LOCAL_PROFILE_PATH
from app.core.concurrency import guarded_task, atomic_text
from app.core.entities import TranscribeModelEnum
from app.core.script_reference_store import load_exact_video_script_reference
from app.core.task_factory import TaskFactory
from app.core.utils.logger import setup_logger

logger = setup_logger('cloud_full_process')
UPLOAD_SLOTS=threading.BoundedSemaphore(2)


def json_value(value):
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return json_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k:json_value(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [json_value(v) for v in value]
    return value


class MultipartBody:
    """Bounded-memory multipart upload accepted by requests as a file-like body."""
    def __init__(self, path, payload, stopped=lambda:False, progress=lambda *_:None):
        self.boundary = 'kaka-' + uuid.uuid4().hex
        self.prefix = (f'--{self.boundary}\r\nContent-Disposition: form-data; name="payload"\r\n'
                       f'Content-Type: application/json; charset=utf-8\r\n\r\n{payload}\r\n'
                       f'--{self.boundary}\r\nContent-Disposition: form-data; name="file"; filename="source.mp4"\r\n'
                       f'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
        self.suffix = f'\r\n--{self.boundary}--\r\n'.encode()
        self.file = open(path,'rb',buffering=1024*1024)
        self.file_size = os.fstat(self.file.fileno()).st_size
        self.total = len(self.prefix) + self.file_size + len(self.suffix)
        self.position = 0
        self.stopped, self.progress = stopped, progress
    def __len__(self):
        return self.total
    def read(self, size=65536):
        if self.stopped():
            raise InterruptedError('已停止上传')
        size = 65536 if size is None or size < 0 else min(size,1024*1024)
        result = bytearray()
        if self.position < len(self.prefix):
            chunk=self.prefix[self.position:self.position+size]
            result.extend(chunk);self.position+=len(chunk);size-=len(chunk)
        if size and self.position < len(self.prefix)+self.file_size:
            chunk=self.file.read(min(size,len(self.prefix)+self.file_size-self.position))
            if not chunk:
                raise IOError('素材在上传过程中被截断')
            result.extend(chunk);self.position+=len(chunk);size-=len(chunk)
        if size and self.position >= len(self.prefix)+self.file_size:
            offset=self.position-len(self.prefix)-self.file_size
            chunk=self.suffix[offset:offset+size]
            result.extend(chunk);self.position+=len(chunk)
        self.progress(self.position,self.total)
        return bytes(result)
    def close(self):
        self.file.close()


STAGES = {'queued':'云端排队','starting':'云端准备','extracting':'云端提取音频',
          'transcribing':'云端转录','subtitles':'云端字幕处理',
          'waiting_synthesis':'等待云端合成','synthesizing':'云端合成','packaging':'云端整理结果'}


class CloudFullProcessThread(QThread):
    finished = pyqtSignal(object)
    progress = pyqtSignal(int,str)
    error = pyqtSignal(str)

    def __init__(self, video_path, profile):
        super().__init__()
        from app.core.task_contract import freeze_profile
        self.profile = freeze_profile(profile)
        self.task = TaskFactory.create_synthesis_task(video_path, str(Path(video_path).with_suffix('.ass')),
                                                     customer_profile=self.profile)
        self.task.synthesis_config.need_video = True
        if self.profile.get('cloud_synthesis_only'):
            self.task.output_path = str(Path(video_path).with_name('【重合成】'+Path(video_path).name))
        self.stop_event = threading.Event()
        self._job_id = None
        self._base = None
        self._headers = None
        self._receipt = None
        self._cancel_only = False
        self.cancel_message = ''
        self.cooperative_cancel = True

    def stop(self):
        self.stop_event.set()
        self.requestInterruption()
        if self._receipt and self._receipt.is_file():
            try:
                record=json.loads(self._receipt.read_text(encoding='utf-8'))
                record['cancel_pending']=True
                atomic_text(self._receipt,json.dumps(record,ensure_ascii=False))
            except FileNotFoundError:
                return
            if not self.isRunning():
                self._cancel_only=True
                self.start()

    def _cancel_remote(self):
        if not self._job_id or not self._base or not self._headers:
            self.cancel_message='已取消，素材上传未开始'
            return True
        with requests.Session() as cancel_session:
            cancel_session.trust_env=False
            response=cancel_session.post(self._base+'/cloud/jobs/'+self._job_id+'/cancel',
                headers=self._headers,timeout=(5,15),allow_redirects=False)
            if response.status_code!=200 or response.json().get('status') not in ('cancelled','completed'):
                self.cancel_message='本地已停止，云端取消未确认；任务编号保留，重试时先处理取消'
                return False
        if self._receipt:
            self._receipt.unlink(missing_ok=True)
        self.cancel_message='已取消云端任务，素材占用已释放'
        logger.info('云端取消已确认：%s',self._job_id)
        return True

    def run(self):
        # Cancelling a failed remote job needs no local video/output lock.
        if self._cancel_only:
            try:
                self._cancel_remote()
            except Exception:
                self.cancel_message='本地已停止，暂未确认云端取消；重试会先核对旧任务'
            finally:
                self._cancel_only=False
            return
        self._run_guarded()

    @guarded_task
    def _run_guarded(self):
        session = requests.Session()
        session.trust_env = False
        try:
            if self._cancel_only:
                self._cancel_remote()
                return
            if self.stop_event.is_set():
                self.cancel_message='已取消，未提交任务'
                return
            video=Path(self.task.video_path)
            trans=TaskFactory.create_transcribe_task(str(video),need_next_task=True,customer_profile=self.profile)
            if trans.transcribe_config.transcribe_model != TranscribeModelEnum.MULTILINGUAL_ASR:
                raise ValueError('云端全流程必须选择“多语言 ASR [云端]”，不会回退到本地')
            synthesis_only=bool(self.profile.get('cloud_synthesis_only'))
            if synthesis_only:
                ass=video.with_suffix('.ass')
                if not ass.is_file():
                    raise ValueError('缺少同名ASS字幕：'+ass.name+'；仅重新合成不会转录或重新生成字幕')
                ass_text=ass.read_text(encoding='utf-8-sig')
                if len(ass_text.encode('utf-8'))>1000000 or '[Events]' not in ass_text or '[Script Info]' not in ass_text:
                    raise ValueError('ASS字幕格式无效或过大，未提交')
                reference,mode='',None
            else:
                sub=TaskFactory.create_subtitle_task(trans.output_path,str(video),need_next_task=True,customer_profile=self.profile)
                reference=self.profile['_script_reference'] if '_script_reference' in self.profile else load_exact_video_script_reference(str(video))
                mode=getattr(sub.subtitle_config,'text_source_mode',None)
                if mode in ('strict','hybrid','script_correct') and not reference:
                    raise ValueError('所选模板需要绑定本视频的剧本JSON，云端不会使用其他视频的剧本')
            tc=trans.transcribe_config
            base=tc.multilingual_asr_base.rstrip('/')
            headers={'Authorization':'Bearer '+tc.multilingual_asr_key}
            self._base,self._headers=base,headers
            self.progress.emit(0,'检查云端全流程接口；不会切换到本地处理')
            response=session.get(base+'/cloud/capabilities',headers=headers,timeout=(10,15))
            if response.status_code != 200 or not response.json().get('full_process'):
                raise ValueError('当前地址未启用云端全流程，请检查新机器地址及任务服务')
            capabilities=response.json()
            if getattr(sub.subtitle_config,'text_source_mode',None) in ('strict','hybrid') and not any('; VideoCaptionerOriginalLanguage: '+lang in (sub.subtitle_config.subtitle_style or '') for lang in ('ar','hi')):
                if capabilities.get('customer_execution_revision',0)<2:raise ValueError('云端尚未同步统一剧本后处理，未上传；请先同步云端版本')
            from app.core.utils.bilingual_reference import cloud_payload
            bilingual=None if synthesis_only else cloud_payload(getattr(sub.subtitle_config,'frozen_bilingual_reference',None),capabilities)
            if not synthesis_only:
                from app.core.utils.delivery_policy import read_policy
                if read_policy(sub.subtitle_config.subtitle_style or '') and not capabilities.get('bilingual_delivery_v1'):
                    raise ValueError('此模板需要新版双语交付执行器，当前云端尚未同步；未上传素材，也未忽略模板规则。')

            if not synthesis_only and mode=='script_correct' and not capabilities.get('script_correction_v1'):
                raise ValueError('云端尚未支持“原声定位＋剧本校正”，未上传素材；请使用转录后在本机处理字幕，或等待云端同步升级。不会退回旧模式。')
            if not capabilities.get('cancellation'):
                raise ValueError('云端取消接口待更新，请先安装取消任务补丁；不会回退本地')
            if video.stat().st_size > capabilities['max_upload_bytes']:
                raise ValueError('视频超过当前云端单文件上传限制')
            payload={'version':1,'bilingual_reference':bilingual,'text_source_mode':mode,'script_reference':reference,
                'subtitle_config':({'subtitle_style':'existing-ass'} if synthesis_only else json_value(sub.subtitle_config)),
                'synthesis_config':json_value(self.task.synthesis_config),
                'transcribe_config':{'language_code':tc.transcribe_language,
                    'multilingual_asr_model':tc.multilingual_asr_model,
                    'multilingual_asr_dialect':tc.multilingual_asr_dialect}}
            program=getattr(sub.subtitle_config,'program_text_rules',None) if not synthesis_only else None
            if program:
                if not capabilities.get('program_text_rules_v1'):raise ValueError('云端未同步程序文字规则，未上传；请先同步服务')
                payload['subtitle_config']['program_text_rules']=json_value(program)
            proof_path=video.with_suffix('.asr_exclusions.json')
            if proof_path.is_file() and not synthesis_only:
                proof=json.loads(proof_path.read_text(encoding='utf-8'))
                if hashlib.sha256(video.read_bytes()).hexdigest()!=proof.get('source_sha256'):
                    raise ValueError('确认误识别记录与当前素材不符，未套用')
                payload['confirmed_asr_exclusions']=proof
            if not synthesis_only and mode=='script_correct':
                from app.core.template_rules import compile_rules
                if not capabilities.get('template_rules_v1'):
                    raise ValueError('云端未安装模板规则基础 v1，未上传素材；请先升级云端，或使用云端转录、本机处理与合成。')
                payload['template_rule_contract']=compile_rules(payload['subtitle_config'],mode)
                if payload['template_rule_contract']['display']['substitution_policy']=='cn-general-v1' and not capabilities.get('caption_finalize_v1'):
                    raise ValueError('云端缺少统一字幕处理阶段 v12，未上传；请先升级云端，防止两端规则不一致')
            if synthesis_only:
                if not capabilities.get('synthesis_only'):
                    raise ValueError('云端尚未安装仅重新合成接口，请先更新云端；不会转录或转为本地合成')
                payload.update(operation='synthesis_only', existing_ass=ass_text)
                metadata=video.with_suffix('.云端处理.json')
                if metadata.is_file():
                    try:
                        prior=json.loads(metadata.read_text(encoding='utf-8'))
                        old_id=prior.get('id')
                        if isinstance(old_id,str) and len(old_id)==32 and video.stat().st_mtime<=prior.get('completed_at',0):
                            check=session.get(base+'/cloud/jobs/'+old_id,headers=headers,timeout=(10,15))
                            if check.status_code==200 and check.json().get('source_available') and check.json().get('input_bytes')==video.stat().st_size:
                                payload['reuse_source_id']=old_id
                    except (OSError,ValueError,requests.RequestException):
                        pass
            source_stamp=[video.stat().st_size,video.stat().st_mtime_ns]
            source_index=video.with_suffix('.云端素材缓存.json')
            if capabilities.get('source_cache') and source_index.is_file():
                try:
                    cached=json.loads(source_index.read_text(encoding='utf-8'))
                    if cached.get('base')==base and cached.get('source_stamp')==source_stamp:
                        check=session.get(base+'/cloud/jobs/'+cached['id'],headers=headers,timeout=(10,15))
                        if check.status_code==200 and check.json().get('source_available'):
                            payload['reuse_source_id']=cached['id']
                except (OSError,ValueError,KeyError,requests.RequestException):pass
            identity=json.dumps([str(video.resolve()),video.stat().st_size,video.stat().st_mtime_ns,
                                 base,{k:v for k,v in payload.items() if k!='reuse_source_id'}],ensure_ascii=False,sort_keys=True)
            fingerprint=hashlib.sha256(identity.encode()).hexdigest()
            receipt=LOCAL_PROFILE_PATH/'cloud_jobs'/(fingerprint+'.json')
            self._receipt=receipt
            job_id=None
            resume_upload_id=None
            if receipt.is_file():
                previous=json.loads(receipt.read_text(encoding='utf-8'))
                job_id=previous.get('id')
                self._job_id=job_id
                if job_id and previous.get('cancel_pending'):
                    if not self._cancel_remote():
                        raise ValueError(self.cancel_message)
                    job_id=None
                if job_id:
                    check=session.get(base+'/cloud/jobs/'+job_id,headers=headers,timeout=(10,15))
                    if check.status_code==404:
                        if capabilities.get('resumable_upload'):
                            part=session.get(base+'/cloud/uploads/'+job_id,headers=headers,timeout=(10,15))
                            if part.status_code==200:
                                resume_upload_id=job_id
                            elif part.status_code!=404:
                                raise ValueError('无法确认已传分块，未重复上传，请稍后重试')
                            job_id=None
                        else:
                            if not self._cancel_remote():
                                raise ValueError('上次上传状态未确认，未重复提交')
                            job_id=None
                    elif check.status_code==200 and check.json().get('status') in ('failed','cancelled'):
                        job_id=None
                    elif check.status_code!=200:
                        raise ValueError('无法确认上次云端任务状态，未重复提交，请稍后重试')
            if not job_id:
                job_id=resume_upload_id or uuid.uuid4().hex
                self._job_id=job_id
                payload['submission_id']=job_id
                atomic_text(receipt,json.dumps({'id':job_id,'base':base,'created_at':time.time(),
                                               'status':'uploading'},ensure_ascii=False))
                acquired=False
                self.progress.emit(0,'等待上传通道（最多同时2条）')
                while not acquired:
                    if self.stop_event.is_set():raise InterruptedError('已取消等待上传')
                    acquired=UPLOAD_SLOTS.acquire(timeout=.2)
                try:
                    if capabilities.get('resumable_upload') and not payload.get('reuse_source_id'):
                        if capabilities.get('sftp_upload_v1'):
                            from app.core.utils.sftp_upload import upload_sftp as upload_resumable
                        else:
                            from app.core.utils.cloud_upload import upload_resumable
                        def upload_progress(n,total,rate,label):
                            if rate>0:
                                text=f'{label} · {n/1024**2:.1f}/{total/1024**2:.1f} MB · {rate/1024**2:.2f} MB/s'
                            else:text=f'{label} · 已确认 {n/1024**2:.1f}/{total/1024**2:.1f} MB'
                            self.progress.emit(min(15,int(n*15/max(total,1))),text)
                            metrics=LOCAL_PROFILE_PATH/'cloud_upload_progress'/(job_id+'.json')
                            atomic_text(metrics,json.dumps({'id':job_id,'file':video.name,'confirmed_bytes':n,'total_bytes':total,'bytes_per_second':rate,'label':label,'updated_at':time.time()},ensure_ascii=False))
                        job_id=upload_resumable(session,base,headers,video,payload,self.stop_event.is_set,upload_progress)
                        self._job_id=job_id
                    else:
                        upload_path=video
                        reuse_stub=None
                        if payload.get('reuse_source_id'):
                            reuse_stub=tempfile.NamedTemporaryFile(suffix='.reuse',delete=False)
                            reuse_stub.close()
                            upload_path=Path(reuse_stub.name)
                            self.progress.emit(2,'复用云端原素材，仅提交现有字幕')
                        body=MultipartBody(upload_path,json.dumps(payload,ensure_ascii=False),self.stop_event.is_set,
                            lambda n,total:self.progress.emit(min(15,int(n*15/max(total,1))),'上传素材到云端'))
                        try:
                            response=session.post(base+'/cloud/jobs',headers={**headers,
                                'Content-Type':'multipart/form-data; boundary='+body.boundary,
                                'Content-Length':str(len(body))},data=body,timeout=(120,120),allow_redirects=False)
                            if response.status_code!=200:
                                raise ValueError('云端提交失败（HTTP '+str(response.status_code)+'）：'+response.text[:250])
                            job_id=response.json()['id']
                            self._job_id=job_id
                        finally:
                            body.close()
                            if reuse_stub:upload_path.unlink(missing_ok=True)
                finally:
                    UPLOAD_SLOTS.release()
                atomic_text(receipt,json.dumps({'id':job_id,'base':base,'created_at':time.time()},ensure_ascii=False))
                if capabilities.get('source_cache'):
                    atomic_text(source_index,json.dumps({'id':job_id,'base':base,'source_stamp':source_stamp},ensure_ascii=False))
                logger.info('云端全流程已提交：%s，任务%s；本地不调用FFmpeg或转录模型',video.name,job_id)
            self._job_id=job_id
            failures=0
            while True:
                if self.stop_event.is_set():
                    raise InterruptedError('已停止等待；云端已提交任务继续执行，重新提交相同任务可恢复下载')
                try:
                    response=session.get(base+'/cloud/jobs/'+job_id,headers=headers,timeout=(10,15))
                    response.raise_for_status();state=response.json();failures=0
                except requests.RequestException:
                    failures+=1
                    if failures>=5:
                        raise ValueError('云端连接中断，任务编号已保留；恢复连接后重试可继续查询')
                    self.stop_event.wait(3);continue
                if state['status']=='cancelled':
                    self.stop_event.set()
                    raise InterruptedError('云端任务已取消')
                if state['status']=='failed':
                    raise ValueError('云端任务失败：'+state.get('error','未知错误'))
                if state['status']=='completed':
                    break
                self.progress.emit(15+int(state.get('progress',0)*.75),(state.get('subtitle_detail') or STAGES.get(state.get('stage'),'云端处理中')) if state.get('stage')=='subtitles' else STAGES.get(state.get('stage'),'云端处理中'))
                self.stop_event.wait(2)
            self.progress.emit(92,'下载云端成片和字幕')
            with tempfile.TemporaryDirectory(prefix='kaka_cloud_download_') as temp:
                archive=Path(temp)/'result.zip'
                with session.get(base+'/cloud/jobs/'+job_id+'/result',headers=headers,
                                 stream=True,timeout=(10,60)) as response:
                    response.raise_for_status()
                    with archive.open('wb') as handle:
                        for chunk in response.iter_content(1024*1024):
                            if self.stop_event.is_set():
                                raise InterruptedError('下载暂停，重新提交相同任务可恢复下载')
                            handle.write(chunk)
                mapping={'result.mp4':Path(self.task.output_path),
                         'subtitle.ass':video.with_suffix('.ass'),'subtitle.srt':video.with_suffix('.srt'),
                         'original.srt':video.with_name(video.stem+'.云端原始.srt'),
                         'original.asr.json':video.with_name(video.stem+'.云端原始.asr.json')}
                with zipfile.ZipFile(archive) as bundle:
                    required={'subtitle.ass','subtitle.srt'}
                    if self.task.synthesis_config.need_video:required.add('result.mp4')
                    if not required<=set(bundle.namelist()):
                        raise ValueError('云端结果包不完整，已保留任务编号')
                    entries=[]
                    for name in bundle.namelist():
                        if synthesis_only and name in ('subtitle.ass','subtitle.srt','original.srt','original.asr.json'):continue
                        if name in mapping:target=mapping[name]
                        elif name.startswith('reports/') and name.count('/')==1 and '..' not in name:
                            target=video.parent/(video.stem+'.云端报告.'+name.split('/')[-1])
                        else:continue
                        entries.append((name,target))
                    from app.core.utils.cloud_results import publish_bundle
                    publish_bundle(bundle,entries,self.stop_event.is_set)
            atomic_text(video.with_suffix('.重合成云端处理.json') if synthesis_only else video.with_suffix('.云端处理.json'),json.dumps({'id':job_id,'execution':'cloud_full_process',
                'completed_at':time.time(),'encoder':'libx264','output':str(self.task.output_path),'stage_seconds':state.get('stage_seconds',{})},ensure_ascii=False,indent=2))
            receipt.unlink(missing_ok=True)
            try:
                if capabilities.get('source_cache'):
                    session.post(base+'/cloud/jobs/'+job_id+'/release',headers=headers,timeout=(10,15))
                else:session.delete(base+'/cloud/jobs/'+job_id,headers=headers,timeout=(10,15))
            except requests.RequestException:pass
            self.progress.emit(100,'云端全流程完成，成片和字幕已下载到原视频旁')
            logger.info('云端全流程完成，下载到：%s',self.task.output_path)
            self.finished.emit(self.task)
        except Exception as error:
            if self.stop_event.is_set():
                try:
                    if self._receipt and self._receipt.is_file():
                        pending=json.loads(self._receipt.read_text(encoding='utf-8'))
                        pending['cancel_pending']=True
                        atomic_text(self._receipt,json.dumps(pending,ensure_ascii=False))
                    self._cancel_remote()
                except Exception:
                    self.cancel_message='本地已停止，暂未确认云端取消；重试会先核对旧任务'
                self.progress.emit(0,self.cancel_message)
                return
            message=str(error)
            # Never print credentials from requests/config objects.
            self.error.emit(message)
            logger.error('云端全流程未完成：%s',message)
        finally:
            self._cancel_only=False
            session.close()
