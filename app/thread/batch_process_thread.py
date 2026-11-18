import copy
from re import S
from typing import List, Dict, Optional
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from pathlib import Path
import queue
import time
from functools import partial


from app.core.task_retry import classify, recovery, limit, load_limit, DELAYS
from app.core.task_factory import TaskFactory
from app.core.customer_templates import task_profile, bind_task_profile
from app.core.task_contract import freeze_profile
from app.common.config import cfg
from app.core.batch_project_store import save_batch_project, update_batch_project
from app.core.entities import (
    TranscribeTask,
    SubtitleTask,
    TranscriptAndSubtitleTask,
    FullProcessTask,
)
from app.thread.transcript_thread import TranscriptThread
from app.thread.subtitle_thread import SubtitleThread
from app.thread.video_synthesis_thread import VideoSynthesisThread
from app.core.utils.logger import setup_logger
from app.core.entities import BatchTaskType, BatchTaskStatus

logger = setup_logger("batch_process_thread")


class BatchTask:
    def __init__(self, file_path: str, task_type: BatchTaskType):
        self.file_path = file_path
        self.task_type = task_type
        self.status = BatchTaskStatus.WAITING
        self.progress = 0
        self.error_message = ""
        self.current_thread: Optional[QThread] = None
        self.project_manifest: Optional[str] = None
        self.customer_profile = None
        self.cancel_requested = False
        self.stage = "等待处理"
        self.checkpoint = None
        self.auto_retry_count=0
        self.auto_retry_limit=None
        self.retry_due=0
        self.retry_pending=False
        self.failure_info={}
        self._failure_handled=False
        self.preflight_stamp=None


class BatchProcessThread(QThread):
    # 信号定义
    task_progress = pyqtSignal(str, int, str)  # file_path, progress, status
    task_error = pyqtSignal(str, str)  # file_path, error_message
    task_completed = pyqtSignal(str)  # file_path
    task_cancelled = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.task_queue = queue.Queue()
        self.current_tasks: Dict[str, BatchTask] = {}
        self.max_concurrent_tasks = 1
        self.is_running = False
        self.factory = TaskFactory()
        self.threads = []  # 保存所有创建的线程
        self.auto_retry_limit=load_limit()

    def add_task(self, task: BatchTask):
        existing = self.current_tasks.get(task.file_path)
        if existing and ((existing.current_thread and existing.current_thread.isRunning()) or (not existing.cancel_requested and existing.status in (BatchTaskStatus.WAITING, BatchTaskStatus.RUNNING))):
            return
        try:
            task.customer_profile = freeze_profile(task.customer_profile or task_profile(task.file_path, begin=True))
            if '_script_reference' not in task.customer_profile:
                from app.core.script_reference_store import load_exact_video_script_reference
                task.customer_profile['_script_reference']=load_exact_video_script_reference(task.file_path)
            if task.task_type in (BatchTaskType.FULL_PROCESS,BatchTaskType.TRANS_SUB,BatchTaskType.SUBTITLE) and task.customer_profile.get('text_source_mode') in ('strict','hybrid','script_correct') and not task.customer_profile['_script_reference']:
                raise ValueError('此模板要求绑定本集剧本，请先绑定后再开始；不会借用其他视频的剧本')
        except (OSError,ValueError,RuntimeError) as error:
            task.status=BatchTaskStatus.FAILED
            task.error_message=str(error)
            self.current_tasks[task.file_path]=task
            self.task_error.emit(task.file_path,str(error))
            return
        if task.auto_retry_limit is None:task.auto_retry_limit=limit(self.auto_retry_limit)
        bind_task_profile(task.file_path,task.customer_profile)
        try:
            import json
            from app.core.concurrency import atomic_text
            atomic_text(Path(task.file_path).with_suffix('.执行配置.json'),json.dumps(task.customer_profile['_execution_plan'],ensure_ascii=False,indent=2))
        except OSError:
            logger.warning('无法保存执行配置记录；任务仍使用内存中固定的模板配置')
        self.current_tasks[task.file_path] = task
        self.task_queue.put(task)
        if not self.isRunning():
            self.is_running = True
            self.start()

    def run(self):
        while self.is_running:
            # 检查是否有正在运行的任务数量是否达到上限
            running_tasks = sum(
                1
                for task in list(self.current_tasks.values())
                if task.status == BatchTaskStatus.RUNNING
            )

            if running_tasks < self.max_concurrent_tasks:
                try:
                    # 非阻塞方式获取任务
                    task = self.task_queue.get_nowait()
                    if getattr(task,"retry_due",0)>time.monotonic():
                        self.task_queue.put(task);time.sleep(0.05)
                    else:
                        from app.core.bk_asr.bcut_recovery import defer_task
                        try:seconds=defer_task(task)
                        except Exception as error:
                            self._on_error_wrapper(task,str(error));continue
                        if seconds:
                            message=f'B接口请求受限，暂停新提交（约{seconds}秒后检查恢复）'
                            if getattr(task,'_bcut_pause_bucket',None)!=seconds//5:
                                task._bcut_pause_bucket=seconds//5
                                task.stage=message;self.task_progress.emit(task.file_path,task.progress,message)
                            self.task_queue.put(task);time.sleep(0.1)
                        else:self._process_task(task)
                except queue.Empty:
                    time.sleep(0.1)  # 避免CPU过度使用
            else:
                time.sleep(0.1)

    def _process_task(self, batch_task: BatchTask):
        try:
            if batch_task.cancel_requested or self.current_tasks.get(batch_task.file_path) is not batch_task:
                return
            from app.core.task_preflight import validate_stamp
            if batch_task.preflight_stamp:batch_task.stage="执行前复核素材"
            validate_stamp(batch_task)
            batch_task.status = BatchTaskStatus.RUNNING
            batch_task._failure_handled=False
            batch_task.retry_pending=False
            if batch_task.auto_retry_count:batch_task.stage=recovery(batch_task)
            if batch_task.checkpoint:
                kind, args = batch_task.checkpoint
                if kind == "transcribed":
                    self.on_full_process_finished(batch_task, *args) if batch_task.task_type == BatchTaskType.FULL_PROCESS else self._on_trans_sub_finished_wrapper(batch_task, *args)
                elif kind == "subtitled":
                    self.on_full_process_subtitle_finished(batch_task, *args)
                return
            self.task_progress.emit(
                batch_task.file_path, 0, str(BatchTaskStatus.RUNNING)
            )

            if batch_task.task_type == BatchTaskType.TRANSCRIBE:
                self._handle_transcribe_task(batch_task)
            elif batch_task.task_type == BatchTaskType.SUBTITLE:
                self._handle_subtitle_task(batch_task)
            elif batch_task.task_type == BatchTaskType.TRANS_SUB:
                self._handle_trans_sub_task(batch_task)
            elif batch_task.task_type in (BatchTaskType.FULL_PROCESS, BatchTaskType.RECOMPOSE):
                self._handle_full_process_task(batch_task)

        except Exception as e:
            logger.exception(f"处理任务失败: {str(e)}")
            self._on_error_wrapper(batch_task,str(e))

    def _on_progress_wrapper(self, batch_task: BatchTask, progress: int, message: str):
        """进度信号包装器"""
        if self.current_tasks.get(batch_task.file_path) is batch_task and not batch_task.cancel_requested and not batch_task._failure_handled:
            batch_task.progress = progress
            batch_task.stage = message
            self.task_progress.emit(batch_task.file_path, progress, message)

    def _on_error_wrapper(self, batch_task: BatchTask, error: str):
        """错误信号包装器"""
        if self.current_tasks.get(batch_task.file_path) is not batch_task:
            return
        if batch_task.cancel_requested:
            return
        if batch_task._failure_handled:return
        batch_task._failure_handled=True
        batch_task.status = BatchTaskStatus.FAILED
        batch_task.error_message = str(error)
        decision=classify(batch_task,error)
        if 'LLM分段重试已用尽' in str(error):
            decision.update(retryable=False,reason='分段请求已达到重试上限，停止自动重启；手动恢复将复用已完成分段')
        decision['stage']=batch_task.stage
        maximum=limit(batch_task.auto_retry_limit)
        batch_task.auto_retry_limit=maximum
        if decision['retryable'] and batch_task.auto_retry_count<maximum and self.is_running:
            batch_task.retry_pending=True
            decision['reason']='等待当前处理退出后自动恢复；'+decision['reason']
        elif decision['retryable']:
            decision['reason']='已达到自动重试上限，已停止；可查看原因后手动恢复' if maximum else '自动重试已关闭'
        batch_task.failure_info=decision
        self.task_error.emit(batch_task.file_path,str(error))
        worker=batch_task.current_thread
        if batch_task.retry_pending and (worker is None or not worker.isRunning()):self._schedule_retry(batch_task)

    def _schedule_retry(self,task):
        if not task.retry_pending or task.cancel_requested or not self.is_running or self.current_tasks.get(task.file_path) is not task:return
        if task.current_thread and task.current_thread.isRunning():return
        task.retry_pending=False
        task.auto_retry_count+=1
        delay=DELAYS[min(task.auto_retry_count-1,len(DELAYS)-1)]
        task.retry_due=time.monotonic()+delay
        task.current_thread=None
        task.status=BatchTaskStatus.WAITING
        task.stage=f'等待自动重试 {task.auto_retry_count}/{task.auto_retry_limit}（{delay}秒后）'
        self.task_queue.put(task)
        self.task_progress.emit(task.file_path,task.progress,task.stage)

    def _on_finished_wrapper(self, batch_task: BatchTask, task=None, *args):
        """完成信号包装器"""
        if self.current_tasks.get(batch_task.file_path) is not batch_task or batch_task.cancel_requested or batch_task.status == BatchTaskStatus.FAILED or batch_task._failure_handled:
            return
        batch_task.status = BatchTaskStatus.COMPLETED
        batch_task.progress = 100
        if batch_task.project_manifest:
            try:
                output_video = getattr(task, "output_path", "") if task else ""
                values = {"status": "completed"}
                if output_video:
                    values["output_video"] = str(Path(output_video).resolve())
                update_batch_project(batch_task.project_manifest, **values)
            except Exception:
                logger.exception("更新批量工程完成状态失败")
        self.task_completed.emit(batch_task.file_path)
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

    def _handle_transcribe_task(self, batch_task: BatchTask):
        # self.max_concurrent_tasks = 3
        task = self.factory.create_transcribe_task(batch_task.file_path,
            customer_profile=batch_task.customer_profile)
        thread = TranscriptThread(task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self._on_progress_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(self._on_finished_wrapper, batch_task), Qt.QueuedConnection
        )

        thread.start()

    def on_trans_sub_project_finished(
        self,
        batch_task: BatchTask,
        subtitle_task: SubtitleTask,
        video_path: str,
        subtitle_path: str,
    ):
        """转录+字幕任务也留存工程，只是不自动合成视频。"""
        editable_subtitle = str(Path(video_path).with_suffix(".ass"))
        output_video = str(
            Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4"
        )
        try:
            batch_task.project_manifest = save_batch_project(
                video_path=video_path,
                editable_subtitle=editable_subtitle,
                styled_subtitle=editable_subtitle,
                output_video=output_video,
                style_name=batch_task.customer_profile['settings']['subtitle_style_name'],
                style_template=subtitle_task.subtitle_config.subtitle_style,
                subtitle_layout=subtitle_task.subtitle_config.subtitle_layout,
                status="subtitle_ready",
            )
        except Exception:
            logger.exception("保存批量字幕工程索引失败")
        self._on_finished_wrapper(batch_task)

    def _handle_subtitle_task(self, batch_task: BatchTask):
        logger.info(f"开始处理字幕任务: {batch_task.file_path}")

        task = self.factory.create_subtitle_task(batch_task.file_path,
            customer_profile=batch_task.customer_profile)
        thread = SubtitleThread(task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self._on_progress_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(self._on_finished_wrapper, batch_task), Qt.QueuedConnection
        )

        thread.start()

    def _handle_trans_sub_task(self, batch_task: BatchTask):
        task = self.factory.create_transcript_and_subtitle_task(batch_task.file_path)
        trans_task = self.factory.create_transcribe_task(
            batch_task.file_path, need_next_task=True, customer_profile=batch_task.customer_profile
        )
        thread = TranscriptThread(trans_task)
        batch_task.current_thread = thread
        self.current_tasks[batch_task.file_path] = batch_task

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self._on_trans_sub_progress_wrapper, batch_task),
            Qt.QueuedConnection,
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(self._on_trans_sub_finished_wrapper, batch_task),
            Qt.QueuedConnection,
        )

        thread.start()

    def _on_trans_sub_progress_wrapper(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """转录+字幕任务进度包装器"""
        progress = progress // 2  # 转录占50%进度
        self._on_progress_wrapper(batch_task, progress, message)

    def _on_trans_sub_finished_wrapper(
        self, batch_task: BatchTask, task: TranscribeTask
    ):
        """转录+字幕任务转录完成包装器"""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

        if batch_task.cancel_requested:
            self._on_error_wrapper(batch_task, '已取消')
            return

        if self.current_tasks.get(batch_task.file_path) is not batch_task or batch_task.status == BatchTaskStatus.FAILED or batch_task._failure_handled:
            return
        batch_task.checkpoint = ("transcribed", (task,))
        # 创建字幕任务
        subtitle_task = self.factory.create_subtitle_task(
            task.output_path, batch_task.file_path, need_next_task=True, customer_profile=batch_task.customer_profile
        )
        thread = SubtitleThread(subtitle_task)
        batch_task.current_thread = thread
        self.current_tasks[batch_task.file_path] = batch_task

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self._on_trans_sub_subtitle_progress_wrapper, batch_task),
            Qt.QueuedConnection,
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(
                self.on_trans_sub_project_finished,
                batch_task,
                subtitle_task,
            ),
            Qt.QueuedConnection,
        )

        thread.start()

    def _on_trans_sub_subtitle_progress_wrapper(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """转录+字幕任务字幕进度包装器"""
        progress = 50 + progress // 2  # 字幕处理占后50%进度
        self._on_progress_wrapper(batch_task, progress, message)

    def _handle_full_process_task(self, batch_task: BatchTask):
        if batch_task.task_type == BatchTaskType.RECOMPOSE:
            batch_task.customer_profile = dict(batch_task.customer_profile or {})
            cloud=bool(batch_task.customer_profile.get('cloud_full_process'))
            batch_task.customer_profile['cloud_synthesis_only'] = cloud
            if not cloud:
                ass=Path(batch_task.file_path).with_suffix('.ass')
                if not ass.is_file():raise ValueError('重新合成缺少同名ASS字幕；不会重新转录')
                subtitle_task=self.factory.create_subtitle_task(str(ass),batch_task.file_path,need_next_task=True,customer_profile=batch_task.customer_profile)
                self.on_full_process_subtitle_finished(batch_task,subtitle_task,batch_task.file_path,str(ass))
                return
        if (batch_task.customer_profile or {}).get('cloud_full_process'):
            from app.thread.cloud_full_process_thread import CloudFullProcessThread
            thread = CloudFullProcessThread(batch_task.file_path, batch_task.customer_profile)
            batch_task.current_thread = thread
            self.threads.append(thread)
            QThread.finished.__get__(thread).connect(
                partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)
            thread.progress.connect(partial(self._on_progress_wrapper, batch_task), Qt.QueuedConnection)
            thread.error.connect(partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection)
            thread.finished.connect(partial(self._on_finished_wrapper, batch_task), Qt.QueuedConnection)
            thread.start()
            return
        task = self.factory.create_full_process_task(batch_task.file_path)
        # 首先创建转录任务
        trans_task = self.factory.create_transcribe_task(
            batch_task.file_path, need_next_task=True, customer_profile=batch_task.customer_profile
        )
        thread = TranscriptThread(trans_task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self.on_full_process_progress, batch_task), Qt.QueuedConnection
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(self.on_full_process_finished, batch_task), Qt.QueuedConnection
        )

        thread.start()

    def on_full_process_progress(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """处理全流程任务的转录进度"""
        if batch_task.status == BatchTaskStatus.RUNNING:
            progress_value = progress // 3  # 转录占33%进度
            self._on_progress_wrapper(batch_task, progress_value, message)

    def on_full_process_finished(self, batch_task: BatchTask, task: TranscribeTask):
        """处理转录完成后开始字幕任务"""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

        if batch_task.cancel_requested:
            self._on_error_wrapper(batch_task, '已取消')
            return

        if self.current_tasks.get(batch_task.file_path) is not batch_task or batch_task.status == BatchTaskStatus.FAILED or batch_task._failure_handled:
            return
        batch_task.checkpoint = ("transcribed", (task,))
        # 转录完成后创建字幕任务
        subtitle_task = self.factory.create_subtitle_task(
            task.output_path,
            batch_task.file_path,
            need_next_task=True, customer_profile=batch_task.customer_profile,
        )
        thread = SubtitleThread(subtitle_task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self.on_full_process_subtitle_progress, batch_task),
            Qt.QueuedConnection,
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(
                self.on_full_process_subtitle_finished,
                batch_task,
                subtitle_task,
            ),
            Qt.QueuedConnection,
        )

        thread.start()

    def on_full_process_subtitle_progress(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """处理全流程任务中字幕部分的进度"""
        if batch_task.status == BatchTaskStatus.RUNNING:
            progress_value = 33 + progress // 3  # 字幕处理占中间33%进度
            self._on_progress_wrapper(batch_task, progress_value, message)

    def on_full_process_subtitle_finished(
        self,
        batch_task: BatchTask,
        subtitle_task: SubtitleTask,
        video_path: str,
        subtitle_path: str,
    ):
        """处理字幕完成后开始视频合成任务"""
        if batch_task.current_thread in self.threads:
            self.threads.remove(batch_task.current_thread)

        if batch_task.cancel_requested:
            self._on_error_wrapper(batch_task, '已取消')
            return

        if self.current_tasks.get(batch_task.file_path) is not batch_task or batch_task.status == BatchTaskStatus.FAILED or batch_task._failure_handled:
            return
        batch_task.checkpoint = ("subtitled", (subtitle_task, video_path, subtitle_path))
        # 字幕完成后立即留存可编辑工程。即使视频合成失败，本集仍可重新打开。
        editable_subtitle = str(Path(video_path).with_suffix(".ass"))
        output_video = str(
            Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4"
        )
        try:
            batch_task.project_manifest = save_batch_project(
                video_path=video_path,
                editable_subtitle=editable_subtitle,
                styled_subtitle=editable_subtitle,
                output_video=output_video,
                style_name=batch_task.customer_profile['settings']['subtitle_style_name'],
                style_template=subtitle_task.subtitle_config.subtitle_style,
                subtitle_layout=subtitle_task.subtitle_config.subtitle_layout,
            )
        except Exception:
            # 工程索引失败不应中断原有批量合成流程。
            logger.exception("保存批量工程索引失败")

        # 字幕完成后创建视频合成任务
        # 合成与历史工程使用同一份视频旁 ASS，确保“看到、修改、合成”的
        # 始终是同一个带样式文件。
        synthesis_task = self.factory.create_synthesis_task(
            video_path, editable_subtitle, customer_profile=batch_task.customer_profile
        )
        # The selected operation owns whether a video is produced.
        synthesis_task.synthesis_config.need_video = True
        if batch_task.task_type == BatchTaskType.RECOMPOSE:
            synthesis_task.output_path=str(Path(video_path).with_name('【重合成】'+Path(video_path).stem+'.mp4'))
        thread = VideoSynthesisThread(synthesis_task)
        batch_task.current_thread = thread

        # 保存线程引用
        self.threads.append(thread)
        QThread.finished.__get__(thread).connect(
            partial(self._on_thread_settled, batch_task, thread), Qt.QueuedConnection)

        thread.progress.connect(
            partial(self.on_full_process_synthesis_progress, batch_task),
            Qt.QueuedConnection,
        )
        thread.error.connect(
            partial(self._on_error_wrapper, batch_task), Qt.QueuedConnection
        )
        thread.finished.connect(
            partial(self._on_finished_wrapper, batch_task), Qt.QueuedConnection
        )

        thread.start()

    def on_full_process_synthesis_progress(
        self, batch_task: BatchTask, progress: int, message: str
    ):
        """处理全流程任务中视频合成部分的进度"""
        if batch_task.status == BatchTaskStatus.RUNNING:
            progress_value = 66 + progress // 3  # 视频合成占最后34%进度
            self._on_progress_wrapper(batch_task, progress_value, message)

    def _on_thread_settled(self, batch_task, thread):
        if thread in self.threads:
            self.threads.remove(thread)
        if batch_task.current_thread is not thread:return
        if not batch_task.cancel_requested:
            if batch_task.retry_pending:self._schedule_retry(batch_task)
            return
        batch_task.status = BatchTaskStatus.FAILED
        if self.current_tasks.get(batch_task.file_path) is batch_task:
            pass  # retain configuration/checkpoint for an explicit retry
            message=getattr(thread,'cancel_message','') or '已取消；当前阶段结束，素材占用已释放'
            self.task_cancelled.emit(batch_task.file_path,message)

    def stop_task(self, file_path: str):
        task=self.current_tasks.get(file_path)
        if not task:
            self.task_cancelled.emit(file_path,'已移除任务')
            return
        task.cancel_requested=True
        task.retry_pending=False
        task.retry_due=0
        task.stage="正在取消"
        with self.task_queue.mutex:
            pending=[item for item in self.task_queue.queue if item is not task]
            self.task_queue.queue.clear()
            self.task_queue.queue.extend(pending)
        thread=task.current_thread
        if thread:
            thread.requestInterruption()
            # Never terminate a Python thread holding a shared resource lock.
            if getattr(thread,'cooperative_cancel',False):
                if thread not in self.threads:
                    self.threads.append(thread)
                thread.stop()
            if thread.isRunning():
                return
        task.status=BatchTaskStatus.FAILED
        if self.current_tasks.get(file_path) is task:
            pass  # cancelled jobs no longer hold a running slot
        self.task_cancelled.emit(file_path,getattr(thread,'cancel_message','') or '已取消任务')

    def stop_all(self):
        self.is_running = False
        for file_path in list(self.current_tasks):
            self.stop_task(file_path)
        # Keep worker references until their finished signal confirms release.

