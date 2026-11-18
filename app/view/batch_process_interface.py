from pathlib import Path
from app.view.background_jobs import BackgroundJobs
from collections import deque
from PyQt5.QtCore import Qt, QTimer
from app.view.studio_controls import TaskConcurrency
from app.core.script_reference_store import load_exact_video_script_reference
from app.view.customer_template_interface import CustomerTemplateSelector
from app.core.customer_templates import task_profile, effective_profile
from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.core.entities import TranscribeModelEnum
import copy
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QProgressBar,
    QFileDialog,
    QSizePolicy,
    QMessageBox,
    QSpinBox,
    QInputDialog,
)
from PyQt5.QtGui import QDesktopServices, QColor, QFont
from PyQt5.QtCore import QUrl
from qfluentwidgets import (
    ComboBox,
    PushButton,
    TableWidget,
    ProgressBar,
    InfoBar,
    InfoBarPosition,
    RoundMenu,
    Action,
    FluentIcon as FIF,
)
import os

from app.thread.batch_process_thread import (
    BatchProcessThread,
    BatchTask,
    BatchTaskStatus,
)
from app.core.entities import (
    SupportedAudioFormats,
    SupportedVideoFormats,
    SupportedSubtitleFormats,
)
from app.core.entities import BatchTaskType, BatchTaskStatus
from app.common.video_quality_ui import fill_video_quality_combo
from app.core.batch_project_store import (
    find_batch_project,
    list_batch_projects,
    load_batch_project,
)
from qfluentwidgets import BodyLabel


class BatchProcessInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("batchProcessInterface")
        self.setWindowTitle(self.tr("批量处理"))
        self.setAcceptDrops(True)
        self.batch_thread = BatchProcessThread()
        self._pending_progress = {}
        self._io = BackgroundJobs(self)
        self._ass_available = {}
        self._quality_cache = {}
        self._preflight_results = {}
        self._preflight_generation = 0
        self._row_queue = deque()
        self._import_pending = set()
        self._binding_pending = set()
        self._binding_timer = QTimer(self)
        self._binding_timer.setSingleShot(True)
        self._binding_timer.setInterval(20)
        self._binding_timer.timeout.connect(self._start_binding_scan)
        self._row_timer = QTimer(self)
        self._row_timer.setInterval(10)
        self._row_timer.timeout.connect(self._drain_rows)
        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.setInterval(100)
        self._progress_timer.timeout.connect(self.flush_task_progress)

        self.init_ui()
        self.setup_connections()

    def init_ui(self):
        # 创建主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(8)
        heading = QHBoxLayout()
        title = BodyLabel("任务工作台", self)
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        heading.addWidget(title, 1)
        self.editor_btn = PushButton("预览与编辑", self, icon=FIF.EDIT)
        self.editor_btn.clicked.connect(self.open_selected_editor)
        heading.addWidget(self.editor_btn)
        self.settings_btn = PushButton("调整本次设置", self, icon=FIF.SETTING)
        self.settings_btn.setCheckable(True)
        heading.addWidget(self.settings_btn)
        main_layout.addLayout(heading)
        self.customer_selector = CustomerTemplateSelector(self)
        main_layout.addWidget(self.customer_selector)

        route = QHBoxLayout()
        route.addWidget(BodyLabel('本批转录方式'))
        self.backend_combo = ComboBox(self)
        for value in cfg.transcribe_model.validator.options:
            self.backend_combo.addItem(value.value, userData=cfg.transcribe_model.serializer.serialize(value))
        route.addWidget(self.backend_combo, 1)
        route.addWidget(BodyLabel('云端并发提交数'))
        self.concurrency_spin = TaskConcurrency(self)
        self.concurrency_spin.setRange(1, 10)
        self.concurrency_spin.setToolTip('同时处理的任务数，最多10条；可一次添加整部视频，完成一条自动补下一条。云端接收容量需配套调整。')
        self.concurrency_spin.setValue(2)
        self.concurrency_spin.setMinimumHeight(34)
        route.addWidget(self.concurrency_spin)
        self.route_panel = QWidget(self)
        self.route_panel.setLayout(route)
        main_layout.addWidget(self.route_panel)
        self.route_panel.hide()
        self.settings_btn.toggled.connect(self.route_panel.setVisible)
        self.device_panel = QWidget(self)
        device_row = QHBoxLayout(self.device_panel)
        device_row.setContentsMargins(0, 0, 0, 0)
        device_row.addWidget(BodyLabel('本地运行设备', self.device_panel))
        self.device_combo = ComboBox(self.device_panel)
        self.device_combo.addItem('CPU（已装 CPU 版请选择此项）', userData='cpu')
        self.device_combo.addItem('GPU（CUDA，需安装 GPU 版）', userData='cuda')
        self.device_combo.setToolTip('仅用于 FasterWhisper；修改后用于新提交的任务，已运行的任务不变。')
        device_row.addWidget(self.device_combo, 1)
        main_layout.addWidget(self.device_panel)
        self.device_panel.hide()
        self.device_combo.activated.connect(lambda *args: self.mark_run_override('faster_whisper_device'))
        self.device_combo.currentIndexChanged.connect(self.update_backend_hint)
        self.settings_btn.toggled.connect(self.update_backend_hint)
        self.route_hint = BodyLabel('云端失败直接报错，不切换本地；并发提交后由服务端调度，实际同时推理数取决于云端配置。')
        self.route_hint.setWordWrap(True)
        main_layout.addWidget(self.route_hint)
        self.route_summary = BodyLabel(self)
        self.route_summary.setWordWrap(True)
        main_layout.addWidget(self.route_summary)
        execution_row = QHBoxLayout()
        execution_row.addWidget(BodyLabel('全流程执行位置'))
        self.execution_combo = ComboBox(self)
        self.execution_combo.addItem('云端全流程（上传素材，下载成片）', userData='cloud')
        self.execution_combo.addItem('本地处理与合成（仅转录可走云端）', userData='local')
        self.execution_combo.setToolTip('云端全流程需要已部署任务服务；连接失败直接提示，不会回退到本地。')
        execution_row.addWidget(self.execution_combo, 1)
        main_layout.addLayout(execution_row)
        self.execution_combo.currentIndexChanged.connect(self.update_backend_hint)
        self.route_hint.hide()
        self.settings_btn.toggled.connect(self.route_hint.setVisible)
        self.backend_combo.activated.connect(lambda *args: self.mark_run_override('transcribe_model'))
        self.customer_selector.mode_combo.activated.disconnect(self.customer_selector.change_mode)
        self.customer_selector.mode_combo.activated.connect(lambda *args: self.mark_run_override('text_source_mode'))
        self.backend_combo.currentIndexChanged.connect(self.update_backend_hint)
        self.concurrency_spin.valueChanged.connect(self.update_backend_hint)
        signalBus.customer_template_applied.connect(self.reload_backend)
        self.reload_backend()

        # 顶部控制区域
        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)

        # 任务类型选择
        self.task_type_combo = ComboBox()
        self.task_type_combo.addItems([str(task_type) for task_type in BatchTaskType])
        self.task_type_combo.setCurrentText(str(BatchTaskType.FULL_PROCESS))
        self.task_type_combo.currentIndexChanged.connect(self.update_backend_hint)

        # 控制按钮
        self.add_file_btn = PushButton("添加文件", icon=FIF.ADD)
        self.history_btn = PushButton("历史项目", icon=FIF.HISTORY)
        self.start_all_btn = PushButton("开始处理", icon=FIF.PLAY)
        self.clear_btn = PushButton("清理已结束", icon=FIF.DELETE)
        self.cancel_all_btn = PushButton("取消全部任务", icon=FIF.CLOSE)
        self.cancel_all_btn.clicked.connect(self.cancel_all_tasks)

        self.quality_label = BodyLabel(self.tr("输出质量"))
        self.quality_combo = ComboBox()
        self.quality_combo.setMinimumWidth(220)
        fill_video_quality_combo(self.quality_combo, self.tr)
        self.quality_combo.activated.connect(lambda *args: self.mark_run_override('video_quality'))
        self.reload_backend()

        # 添加到顶部布局
        top_layout.addWidget(self.task_type_combo)
        top_layout.addWidget(self.quality_label)
        top_layout.addWidget(self.quality_combo)
        top_layout.addWidget(self.add_file_btn)
        top_layout.addWidget(self.history_btn)
        top_layout.addWidget(self.clear_btn)
        top_layout.addWidget(self.cancel_all_btn)

        top_layout.addStretch()
        top_layout.addWidget(self.start_all_btn)

        # 创建任务表格
        self.task_table = TableWidget()
        self.task_table.setColumnCount(4)
        self.task_table.setHorizontalHeaderLabels(["文件名", "进度", "状态", "剧本绑定"])

        # 设置表格样式
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.task_table.setColumnWidth(1, 180)  # 进度条列宽
        self.task_table.setColumnWidth(2, 135)  # 状态列宽

        # 设置行高
        self.task_table.verticalHeader().setDefaultSectionSize(58)  # 设置默认行高

        # 设置表格边框
        self.task_table.setBorderVisible(True)
        self.task_table.setBorderRadius(12)

        # 设置表格不可编辑
        self.task_table.setEditTriggers(QTableWidget.NoEditTriggers)

        # 设置表格大小策略
        self.task_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.task_table.setMinimumHeight(300)  # 设置最小高度

        # 连接双击信号
        self.task_table.doubleClicked.connect(self.on_table_double_clicked)

        # 添加到主布局
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.task_table)
        self.customer_selector.note.hide()
        for i in range(self.customer_selector.mode_row.count()):
            widget = self.customer_selector.mode_row.itemAt(i).widget()
            if widget is not None:
                widget.hide()
                self.settings_btn.toggled.connect(widget.setVisible)
        for widget in (self.quality_label, self.quality_combo, self.task_type_combo):
            widget.hide()
            self.settings_btn.toggled.connect(widget.setVisible)
        self.output_hint = BodyLabel("成品保存至原视频目录 · 双击任务可预览与编辑 · 文件占用时自动排队", self)
        main_layout.addWidget(self.output_hint)

        # 连接信号
        self.add_file_btn.clicked.connect(self.on_add_file_clicked)
        self.history_btn.clicked.connect(self.load_saved_projects)
        self.start_all_btn.clicked.connect(self.start_all_tasks)
        self.clear_btn.clicked.connect(self.clear_tasks)
        self.task_type_combo.currentTextChanged.connect(self.on_task_type_changed)

    def open_selected_editor(self):
        row = self.task_table.currentRow()
        if row < 0:
            home = self.window().homeInterface
            self.window().switchTo(home)
            home.show_step(home.subtitle_optimization_interface, "SubtitleInterface")
            home.subtitle_optimization_interface.studio.refresh()
            return
        self.on_table_double_clicked(self.task_table.model().index(row, 0))

    def showEvent(self, event):
        super().showEvent(event)
        import time
        now=time.monotonic()
        if now-getattr(self,'_last_navigation_sync',-100)<3:return
        self._last_navigation_sync=now
        selected=self.customer_selector.combo.currentData()
        def apply(profile):
            if self.customer_selector.combo.currentData()!=selected or (profile.get('id') or '')!=(selected or ''):return
            self._apply_shared_defaults(profile)
        self._io.submit('navigation_defaults',lambda:copy.deepcopy(effective_profile()),apply,
            lambda error:self.route_summary.setToolTip('共享模板暂不可读，已保留当前显示；开始任务时会重新检查'))
        self.refresh_script_bindings()

    def refresh_script_bindings(self, only_row=None):
        rows=range(self.task_table.rowCount()) if only_row is None else [only_row]
        self._binding_pending.update(self.task_table.item(row,0).toolTip() for row in rows if self.task_table.item(row,0))
        self._start_binding_scan()

    def _start_binding_scan(self):
        if not self._binding_pending:return
        if 'bindings' in self._io.active:return
        paths=tuple(self._binding_pending);self._binding_pending.clear()
        def read():
            result={}
            for path in paths:
                try:result[path]=(bool(load_exact_video_script_reference(path)),Path(path).with_suffix('.ass').is_file(),None)
                except Exception as error:result[path]=(False,False,str(error))
            return result
        self._io.submit('bindings',read,self._apply_bindings)

    def _apply_bindings(self, results):
        for row in range(self.task_table.rowCount()):
            item=self.task_table.item(row,0)
            if not item or item.toolTip() not in results:continue
            bound,ass,error=results[item.toolTip()]
            self._ass_available[item.toolTip()]=ass
            text='读取失败，点击开始时重新检查' if error else ('已绑定' if bound else '未绑定（台词替换时需要）')
            previous=self.task_table.item(row,3)
            if previous is None or previous.text()!=text:
                previous=QTableWidgetItem(text);self.task_table.setItem(row,3,previous)
            previous.setToolTip(error or text)
        if self._binding_pending:self._binding_timer.start()

    def _drain_rows(self):
        self.task_table.setUpdatesEnabled(False)
        try:
            for _ in range(min(6,len(self._row_queue))):
                path,status,manifest=self._row_queue.popleft()
                if any(self.task_table.item(row,0).toolTip()==path for row in range(self.task_table.rowCount())):continue
                self.add_task_to_table(path,status_value=status,project_manifest=manifest)
        finally:self.task_table.setUpdatesEnabled(True)
        if not self._row_queue:
            self._row_timer.stop();self.add_file_btn.setEnabled(True)
            self.add_file_btn.setText('添加素材')
            self.start_all_btn.setEnabled(True)

    def mark_run_override(self, name):
        if not hasattr(self, '_run_overrides'): self._run_overrides = set()
        self._run_overrides.add(name)
        self.update_backend_hint()

    def reload_backend(self):
        self._run_overrides = set()
        profile = effective_profile()
        self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(
            profile['settings'].get('faster_whisper_device', cfg.faster_whisper_device.serialize()))))
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(profile['settings']['transcribe_model'])))
        if hasattr(self, 'quality_combo'):
            self.quality_combo.setCurrentIndex(max(0, self.quality_combo.findData(profile['settings']['video_quality'])))
        self.update_backend_hint()

    def update_backend_hint(self):
        cloud = self.backend_combo.currentData() == TranscribeModelEnum.MULTILINGUAL_ASR.value
        local = self.backend_combo.currentData() == TranscribeModelEnum.FASTER_WHISPER.value
        self.device_panel.setVisible(local and self.settings_btn.isChecked())
        self.route_summary.setText("本批转录：" + self.backend_combo.currentText() + (" · 并发上限 " + str(self.concurrency_spin.value()) + " 条" if cloud else " · 串行处理"))
        if getattr(self, '_run_overrides', None):
            self.route_summary.setText(self.route_summary.text() + ' · 本次临时调整')
        if local:
            self.route_summary.setText(self.route_summary.text() + ' · 运行设备：' +
                ('CPU' if self.device_combo.currentData() == 'cpu' else 'GPU（CUDA）'))
        self.concurrency_spin.setEnabled(cloud)
        self.route_hint.setText('云端失败直接报错，不切换本地；并发提交后由服务端调度，实际同时推理数取决于云端配置。'
            if cloud else '当前不是自建云端转录，任务按 1 条运行；切换到“多语言 ASR [云端]”可设置并发提交数。')

        if hasattr(self, 'execution_combo'):
            full = not hasattr(self, 'task_type_combo') or self.task_type_combo.currentText() in (str(BatchTaskType.FULL_PROCESS), str(BatchTaskType.RECOMPOSE))
            self.execution_combo.setEnabled(cloud and full)
            if not cloud:
                self.execution_combo.blockSignals(True)
                self.execution_combo.setCurrentIndex(max(0,self.execution_combo.findData('local')))
                self.execution_combo.blockSignals(False)
            self.execution_combo.setToolTip('可选择云端全流程或本机处理与合成。' if cloud and full else '当前接口在本机处理与合成；云端全流程需选择多语言 ASR [云端]。' if not cloud else '当前任务类型不使用全流程执行位置。')
            if cloud and full and self.execution_combo.currentData() == 'cloud':
                self.route_summary.setText('执行位置：云端全流程 · 上传及下载上限 ' + str(self.concurrency_spin.value()) + ' 条；云端处理与合成分别排队')
                self.route_hint.setText('本地只上传下载；转录、字幕处理和合成均在云端。失败不回退本地，完成后结果下载到原视频旁。')

        if hasattr(self,'task_type_combo') and self.task_type_combo.currentText()==str(BatchTaskType.RECOMPOSE):
            remote=cloud and hasattr(self,'execution_combo') and self.execution_combo.currentData()=='cloud'
            self.route_summary.setText('执行位置：'+('云端' if remote else '本机')+'仅重新合成 · 使用同名ASS，不转录、不校正文字')
            self.route_hint.setText('复用现有ASS字幕，完成后另存【重合成】成片。')

    def sync_shared_defaults(self):
        return self._apply_shared_defaults(copy.deepcopy(effective_profile()))

    def _apply_shared_defaults(self,profile):
        from app.core.template_permissions import is_protected
        fixed=is_protected(profile)
        backend_allowed=not is_protected(profile)
        overrides=getattr(self,'_run_overrides',set())
        for name, control in (('transcribe_model', self.backend_combo), ('video_quality', self.quality_combo),
                              ('faster_whisper_device', self.device_combo)):
            allowed=not fixed
            if allowed and name in overrides:
                profile['settings'][name]=control.currentData()
            else:
                control.blockSignals(True)
                control.setCurrentIndex(max(0,control.findData(profile['settings'].get(name,getattr(cfg,name).serialize()))))
                control.blockSignals(False)
            control.setEnabled(allowed and (name!='transcribe_model' or backend_allowed))
            control.setToolTip(('本次任务使用此转录接口；不修改客户模板，文字和外观仍按模板执行' if name=='transcribe_model' and backend_allowed else
                '阿拉伯模板受保护，转录接口按模板执行' if name=='transcribe_model' and not backend_allowed else
                '阿拉伯模板受保护，此项按模板执行' if fixed else '仅用于本次任务，不修改客户模板'))
        mode_control=self.customer_selector.mode_combo
        if not fixed and backend_allowed and 'text_source_mode' in overrides:
            profile['text_source_mode']=mode_control.currentData()
        else:
            mode_control.blockSignals(True)
            mode_control.setCurrentIndex(max(0,mode_control.findData(profile.get('text_source_mode'))))
            mode_control.blockSignals(False)
        mode_control.setEnabled(backend_allowed and not fixed)
        mode_control.setToolTip('阿拉伯模板受保护，文字处理方式按模板执行。' if fixed else '本次任务按此方式处理文字，不修改客户模板；客户外观与其他规则继续保留。')
        if fixed:self._run_overrides=set()
        self.update_backend_hint()
        return profile

    def batch_profile(self):
        profile = self.sync_shared_defaults()
        self.batch_thread.max_concurrent_tasks = self.concurrency_spin.value() if self.backend_combo.currentData() == TranscribeModelEnum.MULTILINGUAL_ASR.value else 1
        profile['cloud_full_process'] = bool(
            self.backend_combo.currentData() == TranscribeModelEnum.MULTILINGUAL_ASR.value
            and self.task_type_combo.currentText() in (str(BatchTaskType.FULL_PROCESS), str(BatchTaskType.RECOMPOSE))
            and self.execution_combo.currentData() == 'cloud')
        return profile

    def setup_connections(self):
        # 批处理线程信号连接
        self.batch_thread.task_progress.connect(self.update_task_progress)
        self.batch_thread.task_error.connect(self.on_task_error)
        self.batch_thread.task_completed.connect(self.on_task_completed)
        self.batch_thread.task_cancelled.connect(self.on_task_cancelled)

        # 表格右键菜单
        self.task_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self.show_context_menu)

    def on_add_file_clicked(self):
        task_type = self.task_type_combo.currentText()
        file_filter = ""
        if task_type in [
            BatchTaskType.TRANSCRIBE,
            BatchTaskType.TRANS_SUB,
            BatchTaskType.FULL_PROCESS,
            BatchTaskType.RECOMPOSE,
        ]:
            # 获取所有支持的音视频格式
            audio_formats = [f"*.{fmt.value}" for fmt in SupportedAudioFormats]
            video_formats = [f"*.{fmt.value}" for fmt in SupportedVideoFormats]
            formats = audio_formats + video_formats
            file_filter = f"音视频文件 ({' '.join(formats)})"
        elif task_type == BatchTaskType.SUBTITLE:
            # 获取所有支持的字幕格式
            subtitle_formats = [f"*.{fmt.value}" for fmt in SupportedSubtitleFormats]
            file_filter = f"字幕文件 ({' '.join(subtitle_formats)})"

        files, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", file_filter)
        if files:
            self.add_files(files)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = [url.toLocalFile() for url in event.mimeData().urls()]
        self.add_files(files)

    def add_files(self, file_paths):
        self._import_pending.update(str(path) for path in file_paths)
        self._start_import_scan()

    def _start_import_scan(self):
        if not self._import_pending or 'imports' in self._io.active:return
        paths=tuple(self._import_pending);self._import_pending.clear()
        self.add_file_btn.setEnabled(False);self.add_file_btn.setText('正在读取素材…')
        self.start_all_btn.setEnabled(False)
        def scan():
            valid=[];missing=[]
            for path in paths:
                (valid if os.path.isfile(path) else missing).append(path)
            return valid,[os.path.basename(path) for path in missing]
        self._io.submit('imports',scan,self._import_scanned,self._import_failed)

    def _import_failed(self, error):
        self.add_file_btn.setEnabled(True);self.add_file_btn.setText('添加素材')
        self.start_all_btn.setEnabled(True)
        InfoBar.warning('读取素材失败',str(error),parent=self)

    def _import_scanned(self, result):
        valid_files,non_existent_files=result
        self._apply_import(valid_files,non_existent_files)
        if self._row_queue:self._row_timer.start()
        else:self._drain_rows()
        if self._import_pending:QTimer.singleShot(0,self._start_import_scan)

    def _apply_import(self, valid_files, non_existent_files):
        task_type = BatchTaskType(self.task_type_combo.currentText())
        # 如果有不存在的文件，显示警告
        if non_existent_files:
            InfoBar.warning(
                title="文件不存在",
                content=f"以下文件不存在：\n{', '.join(non_existent_files)}",
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self,
            )

        # 如果没有有效文件，直接返回
        if not valid_files:
            return

        # 对有效文件按文件名排序
        valid_files.sort(key=lambda x: os.path.basename(x).lower())

        # 如果表格为空，自动检测文件类型并设置任务类型
        if self.task_table.rowCount() == 0 and self.task_type_combo.currentIndex() == 0:
            first_file = valid_files[0].lower()
            is_subtitle = any(
                first_file.endswith(f".{fmt.value}") for fmt in SupportedSubtitleFormats
            )
            is_media = any(
                first_file.endswith(f".{fmt.value}") for fmt in SupportedAudioFormats
            ) or any(
                first_file.endswith(f".{fmt.value}") for fmt in SupportedVideoFormats
            )
            if is_subtitle:
                self.task_type_combo.setCurrentText(str(BatchTaskType.SUBTITLE))
                task_type = BatchTaskType.SUBTITLE
            # elif is_media:
            #     self.task_type_combo.setCurrentText(str(BatchTaskType.FULL_PROCESS))
            #     task_type = BatchTaskType.FULL_PROCESS

        # 过滤文件类型
        valid_files = self.filter_files(valid_files, task_type)

        if not valid_files:
            InfoBar.warning(
                title="无效文件",
                content=f"请选择正确的文件类型",
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return

        for file_path in valid_files:
            # 检查是否已存在相同任务
            exists = False
            for row in range(self.task_table.rowCount()):
                if self.task_table.item(row, 0).toolTip() == file_path:
                    old_status=self.task_table.item(row,2).text()
                    if old_status in (str(BatchTaskStatus.FAILED),'已取消'):
                        self.retry_task(file_path)
                        exists=True
                        break
                    exists = True
                    InfoBar.warning(
                        title="任务已存在",
                        content=f"任务已存在",
                        duration=2000,
                        position=InfoBarPosition.TOP_RIGHT,
                        parent=self,
                    )
                    break

            if not exists:
                self._row_queue.append((file_path,None,None))

    def filter_files(self, file_paths, task_type: BatchTaskType):
        valid_extensions = {}

        # 根据任务类型设置有效的扩展名
        if task_type in [
            BatchTaskType.TRANSCRIBE,
            BatchTaskType.TRANS_SUB,
            BatchTaskType.FULL_PROCESS,
            BatchTaskType.RECOMPOSE,
        ]:
            valid_extensions = {f".{fmt.value}" for fmt in SupportedAudioFormats} | {
                f".{fmt.value}" for fmt in SupportedVideoFormats
            }
        elif task_type == BatchTaskType.SUBTITLE:
            valid_extensions = {f".{fmt.value}" for fmt in SupportedSubtitleFormats}

        return [
            f
            for f in file_paths
            if any(f.lower().endswith(ext) for ext in valid_extensions)
        ]

    def add_task_to_table(
        self,
        file_path,
        status_value=None,
        project_manifest=None,
    ):
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)

        # 文件名
        file_name = QTableWidgetItem(os.path.basename(file_path))
        file_name.setToolTip(file_path)
        file_name.setData(Qt.UserRole, project_manifest)
        self.task_table.setItem(row, 0, file_name)

        self.task_table.setItem(row,3,QTableWidgetItem("正在检查绑定…"))
        self._binding_pending.add(file_path)
        self._binding_timer.start()

        # 进度条
        progress_bar = ProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        progress_bar.setFixedHeight(18)
        self.task_table.setCellWidget(row, 1, progress_bar)

        # 状态
        status_text = status_value or str(BatchTaskStatus.WAITING)
        status = QTableWidgetItem(status_text)
        status.setTextAlignment(Qt.AlignCenter)
        status.setForeground(Qt.gray)  # 设置字体颜色为灰色
        font = QFont()
        font.setBold(True)
        status.setFont(font)
        if status_text == str(BatchTaskStatus.COMPLETED):
            status.setForeground(QColor("#13A10E"))
            status.setData(Qt.UserRole+61,'已生成 · 未检查')
            progress_bar.setValue(100)
        self.task_table.setItem(row, 2, status)
        if project_manifest:self._load_quality(file_path,project_manifest)

    def load_saved_projects(self):
        if 'history' in self._io.active:return
        self.history_btn.setEnabled(False);self.history_btn.setText('正在读取历史…')
        self._io.submit('history',list_batch_projects,self._history_loaded,self._history_failed)

    def _history_failed(self, error):
        self.history_btn.setEnabled(True);self.history_btn.setText('历史任务')
        InfoBar.warning('读取历史失败',str(error),parent=self)

    def _history_loaded(self, projects):
        self.history_btn.setEnabled(True);self.history_btn.setText('历史任务')
        from app.view.history_browser import choose_history
        selected=choose_history(projects,self,multiple=True)
        if not selected:return
        existing={os.path.normcase(self.task_table.item(row,0).toolTip()) for row in range(self.task_table.rowCount())}
        for project in selected:
            path=project.get('source_video','')
            if path and os.path.normcase(path) not in existing:
                self._row_queue.append((path,str(BatchTaskStatus.COMPLETED),project.get('manifest_path')))
                existing.add(os.path.normcase(path))
        if self._row_queue:self.start_all_btn.setEnabled(False);self._row_timer.start()
        InfoBar.info('历史任务',f'选中 {len(selected)} 集，已加入列表；已有任务不会重复加入',parent=self)

    def show_context_menu(self, pos):
        row = self.task_table.rowAt(pos.y())
        if row < 0:
            return

        menu = RoundMenu(parent=self)
        file_path = self.task_table.item(row, 0).toolTip()
        project_manifest = self.task_table.item(row, 0).data(Qt.UserRole)
        status = self.task_table.item(row, 2).text()

        start_action = Action(FIF.PLAY, "开始")
        start_action.triggered.connect(lambda: self.start_task(file_path))
        menu.addAction(start_action)

        cancel_action = Action(FIF.CLOSE, "取消")
        cancel_action.triggered.connect(lambda: self.cancel_task(file_path))
        menu.addAction(cancel_action)

        menu.addSeparator()
        edit_action = Action(FIF.EDIT, "编辑本集字幕")
        edit_action.triggered.connect(
            lambda: self.open_project_editor(file_path, project_manifest)
        )
        menu.addAction(edit_action)

        open_folder_action = Action(FIF.FOLDER, "打开输出文件夹")
        open_folder_action.triggered.connect(lambda: self.open_output_folder(file_path))
        menu.addAction(open_folder_action)

        if status != str(BatchTaskStatus.WAITING):
            start_action.setEnabled(False)
        if not project_manifest and status not in (str(BatchTaskStatus.COMPLETED),str(BatchTaskStatus.FAILED)):
            edit_action.setEnabled(False)

        menu.exec_(self.task_table.viewport().mapToGlobal(pos))

    def open_output_folder(self, file_path: str):
        # 根据任务类型和文件路径确定输出文件夹
        task_type = BatchTaskType(self.task_type_combo.currentText())
        file_dir = os.path.dirname(file_path)

        if task_type == BatchTaskType.FULL_PROCESS:
            # 对于全流程任务，输出在视频同目录下
            output_dir = file_dir
        else:
            # 其他任务输出在文件同目录下
            output_dir = file_dir

        # 打开文件夹
        QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def open_project_editor(self,file_path: str,project_manifest=None,review=False):
        self._editor_open_generation=getattr(self,'_editor_open_generation',0)+1
        generation=self._editor_open_generation
        def open_manifest(manifest):
            if generation!=self._editor_open_generation:return
            if not manifest:
                InfoBar.warning('没有可编辑工程','旧任务未保存工程索引；可直接导入现有字幕',duration=5000,parent=self);return
            try:
                main=self.window();home=main.homeInterface;editor=home.subtitle_optimization_interface
                def loaded():
                    if generation!=self._editor_open_generation:return
                    if review and editor.isVisible() and hasattr(editor.studio,'quality_review'):editor.studio.quality_review.open_when_ready=True
                if not editor.open_batch_project_async(manifest,callback=loaded):return
                main.switchTo(home);home.stackedWidget.setCurrentWidget(editor);home.pivot.setCurrentItem('SubtitleInterface')
            except Exception as error:InfoBar.error('打开失败',str(error),duration=5000,parent=self)
        if project_manifest:open_manifest(project_manifest)
        else:
            InfoBar.info('查找工程','正在后台查找本集工程，请稍候…',duration=2000,parent=self)
            self._io.submit('editor_lookup',lambda:find_batch_project(file_path),open_manifest,lambda error:InfoBar.warning('工程暂不可读','请检查共享盘连接后重试',parent=self))

    def update_task_progress(self, file_path: str, progress: int, status: str):
        current=self.batch_thread.current_tasks.get(file_path)
        if current and (current.cancel_requested or current.status in (BatchTaskStatus.FAILED,BatchTaskStatus.COMPLETED)):
            return
        self._pending_progress[file_path] = (progress, status)
        if not self._progress_timer.isActive():
            self._progress_timer.start()

    def flush_task_progress(self):
        pending, self._pending_progress = self._pending_progress, {}
        if not pending:return
        self.task_table.setUpdatesEnabled(False)
        try:
            for row in range(self.task_table.rowCount()):
                item = self.task_table.item(row, 0)
                if item is None:continue
                file_path = item.toolTip()
                task = self.batch_thread.current_tasks.get(file_path)
                if task and (task.cancel_requested or task.status in (BatchTaskStatus.FAILED,BatchTaskStatus.COMPLETED)):continue
                values = pending.get(file_path)
                if values is None:continue
                progress, status = values
                bar = self.task_table.cellWidget(row, 1)
                if bar is not None and bar.value() != progress:bar.setValue(progress)
                cell = self.task_table.item(row, 2)
                if cell is not None and cell.text() != status:cell.setText(status)
                if cell is not None:
                    if task and getattr(task,'failure_info',{}) and '重试' in status:
                        from app.core.task_retry import error_detail
                        cell.setToolTip(status+'\n'+error_detail(task))
                    else:cell.setToolTip(status)
        finally:
            self.task_table.setUpdatesEnabled(True)

    def on_task_error(self, file_path: str, error: str):
        self._pending_progress.pop(file_path, None)
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row, 0).toolTip() == file_path:
                status_item = self.task_table.item(row, 2)
                status_item.setText(str(BatchTaskStatus.FAILED))
                task=self.batch_thread.current_tasks.get(file_path)
                stage=task.stage if task else "未知步骤"
                from app.core.task_retry import error_detail
                status_item.setToolTip(error_detail(task) if task else "失败步骤："+stage+"\n"+error)
                break

    def on_task_completed(self, file_path: str):
        self._pending_progress.pop(file_path, None)
        for row in range(self.task_table.rowCount()):
            item=self.task_table.item(row,0)
            if item is not None and item.toolTip()==file_path:
                bar=self.task_table.cellWidget(row,1)
                if bar is not None:bar.setValue(100)
                break
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row, 0).toolTip() == file_path:
                self.task_table.item(row, 2).setText(str(BatchTaskStatus.COMPLETED))
                self.task_table.item(row, 2).setData(Qt.UserRole+61,'已生成 · 未检查')
                self._quality_cache.pop(file_path,None)
                self.task_table.item(row, 2).setForeground(QColor("#13A10E"))
                self._ass_available[file_path]=True
                self._io.submit("project:"+file_path,lambda:find_batch_project(file_path),lambda manifest,path=file_path:self._apply_project(path,manifest))
                break

    def _apply_project(self, path, manifest):
        if not manifest:return
        self._load_quality(path,manifest)
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row,0).toolTip()==path:
                self.task_table.item(row,0).setData(Qt.UserRole,manifest)
                break

    def _load_quality(self,path,manifest):
        from app.core.quality_review import collect_review
        self._io.submit('quality:'+path,lambda:collect_review(manifest),lambda result:self._apply_quality(path,manifest,result))

    def _apply_quality(self,path,manifest,result):
        from app.core.quality_review import summary
        for row in range(self.task_table.rowCount()):
            source=self.task_table.item(row,0)
            if source.toolTip()==path and source.data(Qt.UserRole)==manifest:
                self._quality_cache[path]=result
                status=self.task_table.item(row,2)
                status.setData(Qt.UserRole+61,summary(result));status.setToolTip(summary(result))
                self.task_table.viewport().update()
                break

    def start_all_tasks(self):
        from app.core.task_preflight import BLOCKED
        if self._row_queue or 'imports' in self._io.active:
            InfoBar.info('正在添加素材','请等素材列表加载完成后开始',parent=self);return
        waiting=[self.task_table.item(row,0).toolTip() for row in range(self.task_table.rowCount()) if self.task_table.item(row,2).text() in (str(BatchTaskStatus.WAITING),BLOCKED)]
        if not waiting:
            InfoBar.info('无待处理任务','请先添加素材；已有任务不会重复开始',parent=self);return
        try:profile=self.batch_profile()
        except Exception as error:
            InfoBar.error('模板无法读取',str(error),parent=self);return
        tasks=[]
        for path in waiting:
            task=BatchTask(path,BatchTaskType(self.task_type_combo.currentText()));task.customer_profile=copy.deepcopy(profile);tasks.append(task)
        self._begin_preflight(tasks)

    def _begin_preflight(self,tasks):
        from app.core.task_preflight import check_batch,CHECKING
        if 'preflight' in self._io.active:
            InfoBar.info('正在检查','请等本批检查完成，避免重复入队',parent=self);return
        accepted=[]
        for task in tasks:
            current=self.batch_thread.current_tasks.get(task.file_path)
            if current and ((current.current_thread and current.current_thread.isRunning()) or (current.status in (BatchTaskStatus.WAITING,BatchTaskStatus.RUNNING) and not current.cancel_requested)):continue
            accepted.append(task)
        if not accepted:return
        self._preflight_generation+=1;generation=self._preflight_generation
        paths={task.file_path for task in accepted}
        for row in range(self.task_table.rowCount()):
            source=self.task_table.item(row,0)
            if source.toolTip() in paths:
                source.setData(Qt.UserRole+62,generation)
                cell=self.task_table.item(row,2);cell.setText(CHECKING);cell.setData(Qt.UserRole+61,None);cell.setToolTip('后台核对素材、模板、剧本与服务能力；尚未上传或转录')
        self.start_all_btn.setEnabled(False);self.start_all_btn.setText('开始前检查…')
        self._io.submit('preflight',lambda:check_batch(accepted),lambda results:self._apply_preflight(generation,results),lambda error:self._preflight_failed(generation,accepted))

    def _preflight_failed(self,generation,tasks):
        results=[(task,{'path':task.file_path,'ok':False,'errors':[{'section':'检查','reason':'检查过程中读取失败','fix':'检查共享盘与服务连接后重新检查'}],'warnings':[],'checks':[],'script_source':'','script_preview':''}) for task in tasks]
        self._apply_preflight(generation,results)

    def _apply_preflight(self,generation,results):
        from app.core.task_preflight import CHECKING,BLOCKED,detail
        if generation!=self._preflight_generation:return
        self.start_all_btn.setEnabled(True);self.start_all_btn.setText('开始处理')
        passed=blocked=0
        for task,result in results:
            for row in range(self.task_table.rowCount()):
                source=self.task_table.item(row,0);cell=self.task_table.item(row,2)
                if source.toolTip()!=task.file_path or source.data(Qt.UserRole+62)!=generation or cell.text()!=CHECKING:continue
                self._preflight_results[task.file_path]=result
                cell.setToolTip(detail(result))
                if result['ok']:
                    task.customer_profile=result['profile'];task.preflight_stamp=result['stamp']
                    cell.setText(str(BatchTaskStatus.WAITING));self.batch_thread.add_task(task);passed+=1
                else:cell.setText(BLOCKED);cell.setForeground(QColor('#c34848'));blocked+=1
                break
        if hasattr(self,'preflight_btn'):self.preflight_btn.setText('检查结果'+('（'+str(blocked)+'集需处理）' if blocked else ''))
        InfoBar.info('开始前检查完成',f'{passed} 集已入队，{blocked} 集未通过；可点“检查结果”查看处理办法',duration=5000,parent=self)

    def show_preflight_results(self):
        from app.view.preflight_results_ui import show_preflight_results
        paths={self.task_table.item(row,0).toolTip() for row in range(self.task_table.rowCount())}
        show_preflight_results({path:result for path,result in self._preflight_results.items() if path in paths},self)

    def recompose_task(self, file_path: str):
        existing=self.batch_thread.current_tasks.get(file_path)
        if existing and existing.status in (BatchTaskStatus.WAITING,BatchTaskStatus.RUNNING):
            InfoBar.warning('任务正在运行','请等当前任务结束后再重新合成',parent=self)
            return
        if not Path(file_path).with_suffix('.ass').is_file():
            InfoBar.warning('缺少ASS字幕','请先保存同名ASS字幕；重新合成不会生成字幕',parent=self)
            return
        profile=copy.deepcopy(existing.customer_profile) if existing and existing.customer_profile else self.batch_profile()
        profile['cloud_synthesis_only']=bool(profile.get('cloud_full_process'))
        task=BatchTask(file_path,BatchTaskType.RECOMPOSE)
        task.project_manifest=find_batch_project(file_path)
        task.customer_profile=profile
        self.update_task_progress(file_path,0,'等待云端重新合成' if profile.get('cloud_full_process') else '等待本机重新合成')
        self._begin_preflight([task])

    def start_task(self, file_path: str):
        task=BatchTask(file_path,BatchTaskType(self.task_type_combo.currentText()))
        try:task.customer_profile=self.batch_profile()
        except Exception as error:
            InfoBar.error('模板无法读取',str(error),parent=self);return
        self._begin_preflight([task])

    def configure_auto_retry(self):
        from app.core.task_retry import save_limit
        value,ok=QInputDialog.getInt(self,'自动重试设置','首次失败后最多自动重试几次？（0为关闭，最多5次）\n仅恢复临时连接异常等可安全继续的任务；缺素材、配置或规则错误会停止。\n已开始的任务保持原上限，新设置用于之后开始或手动恢复的任务。',self.batch_thread.auto_retry_limit,0,5)
        if not ok:return
        try:save_limit(value)
        except OSError as error:
            InfoBar.error('保存失败',str(error),parent=self);return
        self.batch_thread.auto_retry_limit=value
        if hasattr(self,'retry_settings_btn'):self.retry_settings_btn.setText('自动重试：最多'+str(value)+'次' if value else '自动重试：关闭')

    def show_task_error(self, file_path):
        from app.core.task_preflight import detail
        result=self._preflight_results.get(file_path)
        if result and not result['ok']:
            QMessageBox.information(self,'开始前检查未通过',detail(result));return
        task=self.batch_thread.current_tasks.get(file_path)
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row,0).toolTip()==file_path:
                from app.core.task_retry import error_detail
                detail=error_detail(task) if task else self.task_table.item(row,2).toolTip()
                QMessageBox.information(self,'任务详情',detail or '暂无错误信息')
                return

    def cancel_task(self, file_path: str):
        self._pending_progress.pop(file_path,None)
        task=self.batch_thread.current_tasks.get(file_path)
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row,0).toolTip()!=file_path:continue
            status=self.task_table.item(row,2).text()
            if status in (str(BatchTaskStatus.COMPLETED),'已取消',str(BatchTaskStatus.FAILED),'检查未通过') and not (task and task.current_thread and task.current_thread.isRunning()) and not (task and (task.retry_pending or (task.status in (BatchTaskStatus.WAITING,BatchTaskStatus.RUNNING) and not task.cancel_requested))):
                self.task_table.removeRow(row)
                return
            if task is not None:
                self.task_table.item(row,2).setText('正在取消')
            self.batch_thread.stop_task(file_path)
            return

    def retry_task(self, file_path):
        from app.core.task_preflight import BLOCKED
        if any(self.task_table.item(row,0).toolTip()==file_path and self.task_table.item(row,2).text()==BLOCKED for row in range(self.task_table.rowCount())):
            self.start_task(file_path);return
        previous=self.batch_thread.current_tasks.get(file_path)
        if previous and ((previous.current_thread and previous.current_thread.isRunning()) or (previous.status in (BatchTaskStatus.WAITING,BatchTaskStatus.RUNNING) and not previous.cancel_requested)):
            InfoBar.warning('任务仍在运行','请勿重复提交正在运行的任务',parent=self)
            return
        task=BatchTask(file_path,previous.task_type if previous else BatchTaskType(self.task_type_combo.currentText()))
        task.customer_profile=copy.deepcopy(previous.customer_profile) if previous and previous.customer_profile else self.batch_profile()
        task.project_manifest=previous.project_manifest if previous else find_batch_project(file_path)
        if task.customer_profile and not task.customer_profile.get("_script_reference"):
            task.customer_profile.pop("_script_reference",None)
        task.checkpoint=previous.checkpoint if previous else None
        task.preflight_stamp=getattr(previous,"preflight_stamp",None) if previous else None
        self._pending_progress.pop(file_path,None)
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row,0).toolTip()==file_path:
                self.task_table.item(row,2).setText('等待恢复')
                self.task_table.item(row,2).setToolTip('保留配置和已有成果；上传及下载恢复原任务，本地处理从最近完成步骤继续')
                self.task_table.item(row,2).setForeground(QColor('#168779'))
        # A retry is not cancellation: preserve journal ID and acknowledged chunks.
        self._begin_preflight([task])

    def cancel_all_tasks(self):
        paths=[self.task_table.item(row,0).toolTip() for row in range(self.task_table.rowCount())
               if self.task_table.item(row,2).text() not in (str(BatchTaskStatus.COMPLETED),str(BatchTaskStatus.FAILED),'已取消','正在取消','检查未通过')
               or getattr(self.batch_thread.current_tasks.get(self.task_table.item(row,0).toolTip()),'retry_pending',False)
               or getattr(self.batch_thread.current_tasks.get(self.task_table.item(row,0).toolTip()),'status',None) in (BatchTaskStatus.WAITING,BatchTaskStatus.RUNNING)]
        for path in paths:
            self.cancel_task(path)

    def on_task_cancelled(self, file_path, message):
        self._pending_progress.pop(file_path,None)
        for row in range(self.task_table.rowCount()):
            if self.task_table.item(row,0).toolTip()==file_path:
                cell=self.task_table.item(row,2)
                cell.setText('已取消');cell.setToolTip(message)
                cell.setForeground(QColor('#777777'))
                break

    def clear_tasks(self):
        # Clear terminal rows only. Running tasks require explicit cancellation.
        for row in range(self.task_table.rowCount()-1,-1,-1):
            status=self.task_table.item(row,2).text()
            path=self.task_table.item(row,0).toolTip()
            task=self.batch_thread.current_tasks.get(path)
            if status in (str(BatchTaskStatus.COMPLETED),str(BatchTaskStatus.FAILED),'已取消','检查未通过') and not (task and task.current_thread and task.current_thread.isRunning()) and not (task and (task.retry_pending or (task.status in (BatchTaskStatus.WAITING,BatchTaskStatus.RUNNING) and not task.cancel_requested))):
                self._pending_progress.pop(path,None)
                self.task_table.removeRow(row)

    def on_task_type_changed(self, task_type):
        # Existing jobs retain the type/configuration captured at submission.
        pass

    def closeEvent(self, event):
        self._io.close()
        self._row_timer.stop()
        self.batch_thread.stop_all()
        super().closeEvent(event)

    def on_table_double_clicked(self, index):
        """处理表格双击事件"""
        row = index.row()
        file_path = self.task_table.item(row, 0).toolTip()
        project_manifest = self.task_table.item(row, 0).data(Qt.UserRole)
        if project_manifest or self.task_table.item(row,2).text() in (str(BatchTaskStatus.COMPLETED),str(BatchTaskStatus.FAILED)):
            self.open_project_editor(file_path, project_manifest)
        else:
            self.open_output_folder(file_path)
