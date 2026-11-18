import json
import time
from typing import Optional

import requests

from app.config import CACHE_PATH
from app.core.concurrency import shared_lock

from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg
from .base import BaseASR
from . import bcut_recovery as recovery

logger = setup_logger("bcut_asr")

__version__ = "0.0.3"
API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"
# 申请上传
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"
# 提交上传
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"
# 创建任务
API_CREATE_TASK = API_BASE_URL + "/task"
# 查询结果
API_QUERY_RESULT = API_BASE_URL + "/task/result"


class InvalidBcutTaskError(RuntimeError):
    """The provider explicitly rejected this task ID, not a quota response."""


class BcutASR(BaseASR):
    """必剪 语音识别接口"""

    # B counts newly accepted tasks, not cache reads or queries for an existing task.
    RATE_LIMIT_STATE_FILE = CACHE_PATH / "bcut_rate_limit.json"

    headers = {
        "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
        "Content-Type": "application/json",
    }

    def __init__(
        self,
        audio_path: str | bytes,
        use_cache: bool = True,
        need_word_time_stamp: bool = False,
        cancel_requested=None,
    ):
        super().__init__(audio_path, use_cache=use_cache)
        self.session = requests.Session()
        self.cancel_requested = cancel_requested
        self.task_id: Optional[str] = None
        self.__etags: list[str] = []

        self.__in_boss_key: Optional[str] = None
        self.__resource_id: Optional[str] = None
        self.__upload_id: Optional[str] = None
        self.__upload_urls: list[str] = []
        self.__per_size: Optional[int] = None
        self.__clips: Optional[int] = None

        self.__download_url: Optional[str] = None

        self.need_word_time_stamp = need_word_time_stamp

    def _check_cancelled(self):
        if self.cancel_requested and self.cancel_requested():
            raise RuntimeError("B 接口转录已取消")

    def upload(self) -> None:
        """申请上传"""
        if not self.file_binary:
            raise ValueError("none set data")
        payload = json.dumps(
            {
                "type": 2,
                "name": "audio.mp3",
                "size": len(self.file_binary),
                "ResourceFileType": "mp3",
                "model_id": "8",
            }
        )

        self._check_cancelled()
        self.__etags.clear()
        resp = self.session.post(API_REQ_UPLOAD, data=payload, headers=self.headers, timeout=(10,120))
        self._check_response(resp,'upload_request')
        resp = resp.json()
        resp_data = resp["data"]

        self.__in_boss_key = resp_data["in_boss_key"]
        self.__resource_id = resp_data["resource_id"]
        self.__upload_id = resp_data["upload_id"]
        self.__upload_urls = resp_data["upload_urls"]
        self.__per_size = resp_data["per_size"]
        self.__clips = len(resp_data["upload_urls"])

        logger.info(
            f"申请上传成功, 总计大小{resp_data['size'] // 1024}KB, {self.__clips}分片, 分片大小{resp_data['per_size'] // 1024}KB: {self.__in_boss_key}"
        )
        self.__upload_part()
        self.__commit_upload()

    def __upload_part(self) -> None:
        """上传音频数据"""
        for clip in range(self.__clips):
            self._check_cancelled()
            start_range = clip * self.__per_size
            end_range = (clip + 1) * self.__per_size
            logger.info(f"开始上传分片{clip}: {start_range}-{end_range}")
            resp = self.session.put(
                self.__upload_urls[clip],
                data=self.file_binary[start_range:end_range],
                headers=self.headers,
             timeout=(10,120))
            self._check_response(resp,'upload_part')
            etag = resp.headers.get("Etag")
            if not etag:
                raise ValueError(f"B 接口上传分片 {clip} 缺少 ETag，无法确认上传")
            self.__etags.append(etag)
            logger.info(f"分片{clip}上传成功: {etag}")

    def __commit_upload(self) -> None:
        """提交上传数据"""
        data = json.dumps(
            {
                "InBossKey": self.__in_boss_key,
                "ResourceId": self.__resource_id,
                "Etags": ",".join(self.__etags),
                "UploadId": self.__upload_id,
                "model_id": "8",
            }
        )
        self._check_cancelled()
        resp = self.session.post(API_COMMIT_UPLOAD, data=data, headers=self.headers, timeout=(10,120))
        self._check_response(resp,'upload_commit')
        resp = resp.json()
        self.__download_url = resp["data"]["download_url"]
        logger.info(f"提交成功")

    def create_task(self) -> str:
        """开始创建转换任务"""
        self._check_cancelled()
        resp = self.session.post(
            API_CREATE_TASK,
            json={"resource": self.__download_url, "model_id": "8"},
            headers=self.headers,
         timeout=(10,120))
        self._check_response(resp,'create_task')
        resp = resp.json()
        self.task_id = recovery.save_job(self.file_binary, resp["data"]["task_id"])
        logger.info(f"任务已创建: {self.task_id}")
        return self.task_id

    def _submit(self, callback):
        self._check_cancelled()
        duration = self._get_audio_duration_seconds()
        self._check_rate_limit(duration)
        callback(0, "上传中")
        self.upload()
        callback(40, "创建任务中")
        self.create_task()
        self._record_rate_limit_usage(duration)

    def result(self, task_id: Optional[str] = None):
        """查询转换结果"""
        recovery.require_available()
        recovery.pace_query()
        self._check_cancelled()
        query_started=time.time()
        resp = self.session.get(
            API_QUERY_RESULT,
            params={"model_id": 7, "task_id": task_id or self.task_id},
            headers=self.headers,
         timeout=(10,120))
        self._check_response(resp,'result',task_id or self.task_id)
        payload = resp.json()
        if str(payload.get('code')) == '1500806':
            recovery.forget_invalid_probe(task_id or self.task_id,query_started)
            raise InvalidBcutTaskError('B 接口业务拒绝：code=1500806；无效的任务ID')
        if payload.get('code') not in (None, 0, "0"):
            raise RuntimeError('B 接口业务拒绝：code='+str(payload.get('code'))+'；'+str(payload.get('message') or payload.get('msg') or '')[:160])
        data=payload.get('data')
        if not isinstance(data, dict) or data.get("state") is None:
            raise ValueError('B 接口结果响应缺少任务状态，已保留原任务')
        recovery.clear(query_started)
        return data

    def _run(self, callback=None, **kwargs):
        callback=callback or (lambda value,message:None)
        self._check_cancelled()
        cached=recovery.load_result(self.file_binary) if self.use_cache else None
        if cached is not None:
            callback(100,'复用共享B接口成功结果；无需访问接口')
            return cached
        with recovery.provider_queue(callback, self._check_cancelled):
            return self._run_serial(callback,**kwargs)

    def _check_response(self,resp,stage,task_id=None):
        if resp.status_code==412:
            recovery.block(task_id or self.task_id,resp.headers.get('Retry-After'),stage)
            logger.warning('B接口HTTP412：stage=%s content_type=%s retry_after=%s；未推断额度原因',stage,resp.headers.get('Content-Type'),resp.headers.get('Retry-After'))
            raise requests.HTTPError('B 接口返回 HTTP 412：服务端拒绝请求；阶段='+stage+'。已共享退避，已提交任务保留，重试先恢复原任务，不自动换接口。',response=resp)
        resp.raise_for_status()

    def _run_serial(self,callback,**kwargs):
        # One pending receipt per exact audio payload. A failed result query
        # must not cause the next manual retry to upload and create again.
        with shared_lock(recovery.job_path(self.file_binary),wait_seconds=60):
            self._check_cancelled()
            cached=recovery.load_result(self.file_binary) if self.use_cache else None
            if cached is not None:return cached
            pending=recovery.load_job(self.file_binary)
            self.task_id=pending.get('task_id')
            recovered=pending.get('completed_result')
            if recovered is not None:
                if not isinstance(recovered,dict) or not isinstance(recovered.get('utterances'),list):
                    raise ValueError('B接口已保存结果结构无效；未重新上传')
                recovery.save_result(self.file_binary,recovered)
                recovery.complete_job(self.file_binary)
                callback(100,'已恢复此前完成的B接口结果；未重新上传')
                return recovered
            recovery.require_available()
            probe=recovery.blocked_task()
            if probe and probe!=self.task_id:
                callback(20,'B接口恢复检查：查询此前任务，不重新上传')
                try:
                    probe_response=self.result(probe)
                    if not recovery.preserve_probe_result(probe,probe_response):
                        logger.info('B 接口已恢复响应，旧任务仍在处理，保留其回执供原素材续查')
                except InvalidBcutTaskError:
                    logger.warning('此前B接口探测任务已无效，不再以该ID阻止当前任务')
            resumed_id=self.task_id
            replacement_used=False
            if not self.task_id:
                self._submit(callback)
            else:
                logger.info('恢复已提交的B接口任务：%s；未重新上传',self.task_id)
            callback(60,'查询已提交的B接口转录结果')
            deadline=time.monotonic()+500
            while time.monotonic()<deadline:
                self._check_cancelled()
                try:response=self.result()
                except InvalidBcutTaskError:
                    invalid_id=self.task_id
                    recovery.archive_invalid_receipt(recovery.job_path(self.file_binary),invalid_id)
                    self.task_id=None
                    if not resumed_id or replacement_used:
                        raise InvalidBcutTaskError('B接口新任务ID仍无效，已归档并停止自动新提交；请检查服务端状态')
                    replacement_used=True
                    recovery.require_available()
                    callback(40,'此前B接口任务ID已无效；已归档，重新提交一次')
                    self._submit(callback)
                    continue
                if str(response.get('state')) == '4':
                    raw=response.get('result')
                    result=json.loads(raw) if isinstance(raw,str) else raw
                    if not isinstance(result,dict) or not isinstance(result.get('utterances'),list):
                        raise ValueError('B接口完成结果结构无效，已保留原任务；未重新上传')
                    recovery.save_result(self.file_binary,result)
                    recovery.complete_job(self.file_binary)
                    callback(100,'转录成功');logger.info('转换成功')
                    return result
                time.sleep(6)
            raise TimeoutError('B 接口已提交任务，等待结果超过上限；原任务已保留，重试继续取结果：'+str(self.task_id))

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        result=[]
        for index,u in enumerate(resp_data['utterances']):
            words=u['words'] if self.need_word_time_stamp else [dict(label=u['transcript'],start_time=u['start_time'],end_time=u['end_time'])]
            for word in words:
                cue=ASRDataSeg(word['label'].strip(),word['start_time'],word['end_time'])
                cue.asr_segment_id=index
                cue.asr_origin='bcut_utterance'
                result.append(cue)
        return result



if __name__ == "__main__":
    # Example usage
    audio_file = r"test.mp3"
    asr = BcutASR(audio_file)
    asr_data = asr.run()
    print(asr_data)


