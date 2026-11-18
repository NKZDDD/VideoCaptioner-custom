from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import SegmentedWidget

from app.core.task_factory import TaskFactory
from app.core.customer_templates import task_profile
from app.view.subtitle_interface import SubtitleInterface
from app.view.workbench_interface import WorkbenchInterface
from app.view.transcription_interface import TranscriptionInterface
from app.view.video_synthesis_interface import VideoSynthesisInterface


class HomeInterface(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)

        # 设置对象名称和样式
        self.setObjectName("HomeInterface")
        self.setStyleSheet(
            """
            HomeInterface{background: transparent}
        """
        )

        # 创建分段控件和堆叠控件
        self.pivot = SegmentedWidget(self)
        self.pivot.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

        self.stackedWidget = QStackedWidget(self)
        self.vBoxLayout = QVBoxLayout(self)

        # 添加子界面
        self.task_creation_interface = WorkbenchInterface(self)
        self.transcription_interface = TranscriptionInterface(self)
        self.subtitle_optimization_interface = SubtitleInterface(self)
        self.video_synthesis_interface = VideoSynthesisInterface(self)

        self.addSubInterface(
            self.task_creation_interface, "TaskCreationInterface", self.tr("单条处理")
        )
        self.addSubInterface(
            self.transcription_interface, "TranscriptionInterface", self.tr("转录进度")
        )
        self.addSubInterface(
            self.subtitle_optimization_interface,
            "SubtitleInterface",
            self.tr("字幕编辑器"),
        )
        self.addSubInterface(
            self.video_synthesis_interface,
            "VideoSynthesisInterface",
            self.tr("视频合成"),
        )

        self.vBoxLayout.addWidget(self.pivot)
        self.vBoxLayout.addWidget(self.stackedWidget)
        self.vBoxLayout.setContentsMargins(30, 10, 30, 30)

        self.stackedWidget.currentChanged.connect(self.onCurrentIndexChanged)
        self.stackedWidget.setCurrentWidget(self.task_creation_interface)
        self.pivot.setCurrentItem("TaskCreationInterface")

        self._workflow = None
        self.task_creation_interface.requestReady.connect(self.start_workbench)
        self.transcription_interface.finished.connect(
            self.switch_to_subtitle_optimization
        )
        self.subtitle_optimization_interface.finished.connect(
            self.switch_to_video_synthesis
        )

    def start_workbench(self, request):
        """Freeze the selected customer defaults and this run's overrides."""
        if self._workflow is not None:
            return
        self._workflow = request
        try:
            if request['transcribe']:
                task = TaskFactory.create_transcribe_task(request['video'], need_next_task=True,
                    customer_profile=request['profile'])
                self.transcription_interface.set_task(task)
                self.transcription_interface.process()
                self.transcription_interface.video_info_card.transcript_thread.error.connect(self.workflow_error)
                self.show_step(self.transcription_interface, 'TranscriptionInterface')
            else:
                self.switch_to_subtitle_optimization(request['subtitle'], request['video'])
        except Exception as exc:
            from qfluentwidgets import InfoBar
            InfoBar.error('任务未能开始', str(exc), parent=self)
            self.workflow_error(str(exc))

    def show_step(self, widget, key):
        self.stackedWidget.setCurrentWidget(widget)
        self.pivot.setCurrentItem(key)

    def workflow_error(self, *args):
        self._workflow = None
        self.task_creation_interface.complete(error=True)

    def workflow_complete(self, path):
        self._workflow = None
        self.task_creation_interface.complete(str(path))

    def switch_to_transcription(self, file_path):
        task_profile(file_path, begin=True)
        # 切换到转录界面
        transcribe_task = TaskFactory.create_transcribe_task(
            file_path, need_next_task=True
        )
        self.transcription_interface.set_task(transcribe_task)
        self.transcription_interface.process()
        self.stackedWidget.setCurrentWidget(self.transcription_interface)
        self.pivot.setCurrentItem("TranscriptionInterface")

    def switch_to_subtitle_optimization(self, file_path, video_path):
        if self._workflow is not None:
            request = self._workflow
            if not request['process_subtitles']:
                if request['synthesize']:
                    self.switch_to_video_synthesis(video_path, file_path)
                else:
                    self.subtitle_optimization_interface.task = None
                    self.subtitle_optimization_interface.load_subtitle_file(file_path)
                    self.show_step(self.subtitle_optimization_interface, 'SubtitleInterface')
                    self.workflow_complete(file_path)
                return
            try:
                task = TaskFactory.create_subtitle_task(file_path, video_path or None,
                    need_next_task=request['synthesize'], customer_profile=request['profile'])
                self.subtitle_optimization_interface.set_task(task)
                self.subtitle_optimization_interface.process()
                thread = self.subtitle_optimization_interface.subtitle_optimization_thread
                thread.error.connect(self.workflow_error)
                if not request['synthesize']:
                    thread.finished.connect(lambda video, output: self.workflow_complete(output))
                self.show_step(self.subtitle_optimization_interface, 'SubtitleInterface')
            except Exception as exc:
                from qfluentwidgets import InfoBar
                InfoBar.error('字幕处理未能开始', str(exc), parent=self)
                self.workflow_error(str(exc))
            return
        # 切换到字幕处理界面
        subtitle_task = TaskFactory.create_subtitle_task(
            file_path, video_path, need_next_task=True
        )
        self.subtitle_optimization_interface.set_task(subtitle_task)
        self.subtitle_optimization_interface.process()
        self.stackedWidget.setCurrentWidget(self.subtitle_optimization_interface)
        self.pivot.setCurrentItem("SubtitleInterface")

    def switch_to_video_synthesis(self, video_path, subtitle_path):
        if self._workflow is not None:
            request = self._workflow
            try:
                task = TaskFactory.create_synthesis_task(video_path, subtitle_path,
                    need_next_task=True, customer_profile=request['profile'])
                self.video_synthesis_interface.set_task(task)
                self.video_synthesis_interface.process()
                thread = self.video_synthesis_interface.video_synthesis_thread
                thread.error.connect(self.workflow_error)
                thread.finished.connect(lambda result: self.workflow_complete(result.output_path))
                self.show_step(self.video_synthesis_interface, 'VideoSynthesisInterface')
            except Exception as exc:
                from qfluentwidgets import InfoBar
                InfoBar.error('合成未能开始', str(exc), parent=self)
                self.workflow_error(str(exc))
            return
        # 切换到视频合成界面
        synthesis_task = TaskFactory.create_synthesis_task(
            video_path, subtitle_path, need_next_task=True
        )
        synthesis_task.synthesis_config.need_video=True
        self.video_synthesis_interface.set_task(synthesis_task)
        self.video_synthesis_interface.process()
        self.stackedWidget.setCurrentWidget(self.video_synthesis_interface)
        self.pivot.setCurrentItem("VideoSynthesisInterface")

    def addSubInterface(self, widget, objectName, text):
        # 添加子界面到堆叠控件和分段控件
        widget.setObjectName(objectName)
        self.stackedWidget.addWidget(widget)
        self.pivot.addItem(
            routeKey=objectName,
            text=text,
            onClick=lambda: self.stackedWidget.setCurrentWidget(widget),
        )

    def onCurrentIndexChanged(self, index):
        # 当堆叠控件的当前索引改变时，更新分段控件的当前项
        widget = self.stackedWidget.widget(index)
        if widget:
            self.pivot.setCurrentItem(widget.objectName())

    def closeEvent(self, event):
        # 关闭事件，关闭所有子界面
        self.task_creation_interface.close()
        self.transcription_interface.close()
        self.subtitle_optimization_interface.close()
        self.video_synthesis_interface.close()
        super().closeEvent(event)
