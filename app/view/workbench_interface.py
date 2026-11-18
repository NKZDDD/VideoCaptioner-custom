"""A shared workflow with explicit, per-run customer overrides."""
import copy
import os
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QScrollArea, QGroupBox, QCheckBox
from qfluentwidgets import LineEdit, PushButton, PrimaryPushButton, ComboBox, ProgressBar, InfoBar, CheckBox as QCheckBox
from PyQt5.QtWidgets import QFileDialog

from app.view.studio_controls import StyledCombo as ComboBox
from app.common.config import cfg
from app.config import SUBTITLE_STYLE_PATH
from app.core.utils.task_style import freeze_task_style, appearance_details
from app.common.signal_bus import signalBus
from app.core.customer_templates import effective_profile, profile_value
from app.core.entities import SupportedVideoFormats, SupportedAudioFormats, TranscribeModelEnum
from app.core.script_reference_store import (load_script_file, save_video_script_reference,
    load_exact_video_script_reference, extract_episode_number)
from app.view.customer_template_interface import CustomerTemplateSelector
from app.view.task_creation_interface import TaskCreationInterface
from app.common.video_quality_ui import fill_video_quality_combo


class WorkbenchInterface(TaskCreationInterface):
    requestReady = pyqtSignal(dict)

    def setup_ui(self):
        self.pending_request = None
        self.busy = False
        self.result_path = ''
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(12)
        title = QLabel('字幕工作台', self)
        title.setStyleSheet('font-size:25px;font-weight:600;color:#f4f4f4;')
        outer.addWidget(title)
        hint = QLabel('选客户、导入素材、勾选步骤，在同一处开始。', self)
        hint.setStyleSheet('color:#b9c2cc;font-size:14px;')
        outer.addWidget(hint)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget(scroll)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 6, 8, 6)
        body.setSpacing(18)
        self.customer_selector = CustomerTemplateSelector(self)
        self.customer_selector.layout().removeItem(self.customer_selector.mode_row)
        self.customer_selector.note.hide()
        self.customer_selector.mode_combo.setStyleSheet('QComboBox {background:#30353c;color:#eeeeee;padding:5px;border:1px solid #555d68;border-radius:5px;font-size:14px;min-height:30px;} QComboBox QAbstractItemView {background:#30353c;color:#eeeeee;selection-background-color:#35654f;}')
        customer = QGroupBox('1  客户模板', content)
        box = QVBoxLayout(customer)
        box.addWidget(self.customer_selector)
        body.addWidget(customer)

        material = QGroupBox('2  导入素材', content)
        grid = QGridLayout(material)
        grid.setVerticalSpacing(12)
        self.search_input = LineEdit(self)
        self.search_input.setPlaceholderText('拖入视频 / 音频，或粘贴文件路径、视频网址')
        self.media_button = PushButton('选择视频 / 音频', self)
        self.subtitle_input = LineEdit(self)
        self.subtitle_input.setPlaceholderText('已有 SRT / ASS / VTT 可直接处理，跳过转录')
        self.subtitle_button = PushButton('选择已有字幕', self)
        self.script_button = PushButton('绑定剧本 JSON', self)
        self.script_hint = QLabel('剧本可选；仅在需要台词替换时绑定。', self)
        self.script_hint.setWordWrap(True)
        grid.addWidget(QLabel('视频 / 音频'), 0, 0)
        grid.addWidget(self.search_input, 0, 1)
        grid.addWidget(self.media_button, 0, 2)
        grid.addWidget(QLabel('已有字幕'), 1, 0)
        grid.addWidget(self.subtitle_input, 1, 1)
        grid.addWidget(self.subtitle_button, 1, 2)
        grid.addWidget(QLabel('本集剧本'), 2, 0)
        grid.addWidget(self.script_hint, 2, 1)
        grid.addWidget(self.script_button, 2, 2)
        grid.setColumnStretch(1, 1)
        body.addWidget(material)

        steps = QGroupBox('3  本次处理步骤', content)
        layout = QVBoxLayout(steps)
        layout.setSpacing(12)
        checks = QHBoxLayout()
        self.transcribe_check = QCheckBox('语音转录', self)
        self.split_check = QCheckBox('智能断句', self)
        self.optimize_check = QCheckBox('字幕校正', self)
        self.translate_check = QCheckBox('字幕翻译', self)
        self.synthesis_check = QCheckBox('合成视频', self)
        for control in self.checks:
            checks.addWidget(control)
        checks.addStretch()
        layout.addLayout(checks)
        layout.addLayout(self.customer_selector.mode_row)
        language_row = QGridLayout()
        self.engine_combo = self.option_combo('transcribe_model')
        self.language_combo = self.option_combo('transcribe_language')
        self.target_combo = self.option_combo('target_language')
        for col, (label, combo) in enumerate([('转录方式', self.engine_combo), ('原语言', self.language_combo), ('翻译为', self.target_combo)]):
            language_row.addWidget(QLabel(label), 0, col)
            language_row.addWidget(combo, 1, col)
            language_row.setColumnStretch(col, 1)
        layout.addLayout(language_row)
        self.device_panel = QWidget(self)
        device_row = QHBoxLayout(self.device_panel)
        device_row.setContentsMargins(0, 0, 0, 0)
        device_row.addWidget(QLabel('本地运行设备', self.device_panel))
        self.device_combo = ComboBox(self.device_panel)
        self.device_combo.addItem('CPU（已装 CPU 版请选择此项）', userData='cpu')
        self.device_combo.addItem('GPU（CUDA，需安装 GPU 版）', userData='cuda')
        self.device_combo.setToolTip('仅用于 FasterWhisper；本次选择会随任务固定，不修改共享客户模板。')
        device_row.addWidget(self.device_combo, 1)
        layout.addWidget(self.device_panel)
        quality = QHBoxLayout()
        quality.addWidget(QLabel('视频质量'))
        self.quality_combo = ComboBox(self)
        fill_video_quality_combo(self.quality_combo, self.tr)
        quality.addWidget(self.quality_combo, 1)
        self.soft_check = QCheckBox('可切换软字幕', self)
        quality.addWidget(self.soft_check)
        layout.addLayout(quality)
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel('本次字幕样式'))
        self.style_combo = ComboBox(self)
        self.style_combo.addItem('按客户模板', userData=None)
        for path in sorted(SUBTITLE_STYLE_PATH.glob('*.txt')):
            self.style_combo.addItem(path.stem, userData=path.stem)
        style_row.addWidget(self.style_combo, 1)
        self.style_edit_button = PushButton('调整样式', self)
        style_row.addWidget(self.style_edit_button)
        layout.addLayout(style_row)
        self.style_hint = QLabel(self)
        self.style_hint.setWordWrap(True)
        layout.addWidget(self.style_hint)
        self.flow_hint = QLabel(self)
        self.flow_hint.setWordWrap(True)
        layout.addWidget(self.flow_hint)
        self.mode_hint = QLabel('软件内部调用已配置的GPT/LLM，按上下文匹配JSON原台词；保留转录时间，无需外部复制。', self)
        self.mode_hint.setWordWrap(True)
        layout.addWidget(self.mode_hint)
        note = QLabel('以上调整只用于本次任务；客户模板需在“管理模板”中单独保存。', self)
        note.setWordWrap(True)
        layout.addWidget(note)
        body.addWidget(steps)
        body.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        footer = QHBoxLayout()
        self.status_label = QLabel('准备就绪', self)
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.result_button = PushButton('打开结果文件夹', self)
        self.result_button.setEnabled(False)
        self.start_button = PrimaryPushButton('开始处理', self)
        self.start_button.setMinimumWidth(140)
        footer.addWidget(self.result_button)
        footer.addWidget(self.start_button)
        outer.addLayout(footer)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.hide()
        outer.addWidget(self.progress_bar)
        tools = QHBoxLayout()
        for label, callback in [('批量处理', lambda: self.navigate('batchProcessInterface')),
                                ('字幕样式', lambda: self.navigate('subtitleStyleInterface')),
                                ('服务与高级设置', lambda: self.navigate('settingInterface')),
                                ('查看日志', self.show_log_window)]:
            button = PushButton(label, self)
            button.clicked.connect(callback)
            tools.addWidget(button)
        tools.addStretch()
        outer.addLayout(tools)
        self.setStyleSheet('''
            QWidget#TaskCreationInterface {background:#202328;}
            QGroupBox {color:#f0f2f5;font-size:16px;font-weight:600;border:1px solid #494d53;
                border-radius:10px;margin-top:12px;padding:16px 14px 12px;background:#292c31;}
            QGroupBox::title {subcontrol-origin:margin;left:16px;}
            QLabel,QCheckBox {color:#e0e4ea;font-size:14px;font-weight:400;}
            QCheckBox::indicator {width:18px;height:18px;}
            QLineEdit,QComboBox {font-size:14px;min-height:32px;}
            QPushButton {font-size:14px;min-height:32px;}
            QScrollArea,QScrollArea > QWidget > QWidget {background:transparent;}
        ''')
        for button in self.findChildren(PushButton):
            if not isinstance(button, PrimaryPushButton):
                button.setStyleSheet('QPushButton {background:#353a42;color:#f1f4f8;border:1px solid #505762;border-radius:6px;padding:5px 12px;font-size:14px;min-height:30px;} QPushButton:hover {background:#414954;} QPushButton:disabled {color:#8e98a5;background:#2c3036;}')

        from app.view.studio_form_theme import finish_workbench
        finish_workbench(self, scroll, content, title, hint)

    @property
    def checks(self):
        return (self.transcribe_check, self.split_check, self.optimize_check, self.translate_check, self.synthesis_check)

    def option_combo(self, name):
        combo = ComboBox(self)
        item = getattr(cfg, name)
        for value in item.validator.options:
            combo.addItem(str(getattr(value, 'value', value)), userData=item.serializer.serialize(value))
        return combo

    def setup_signals(self):
        self.start_button.clicked.connect(self.process)
        self.media_button.clicked.connect(self.choose_media)
        self.subtitle_button.clicked.connect(self.choose_subtitle)
        self.script_button.clicked.connect(self.bind_script)
        self.search_input.textChanged.connect(self.on_search_input_changed)
        self.result_button.clicked.connect(self.open_result)
        for control in self.checks:
            control.toggled.connect(self.refresh_flow)
        self.customer_selector.mode_combo.currentIndexChanged.connect(self.refresh_flow)
        self.engine_combo.currentIndexChanged.connect(self.refresh_flow)
        self.device_combo.currentIndexChanged.connect(self.refresh_flow)
        self.style_combo.currentIndexChanged.connect(self.refresh_style_hint)
        self.style_edit_button.clicked.connect(self.edit_task_style)
        signalBus.customer_template_applied.connect(self.setup_values)

    def setup_values(self):
        if self.busy:
            return
        profile = effective_profile()
        identity = profile.get('id', '')
        if identity != getattr(self, '_style_customer_id', None):
            self.style_combo.setCurrentIndex(0)
            self._style_customer_id = identity
        self.transcribe_check.setChecked(not bool(self.subtitle_input.text().strip()))
        for control, key in [(self.split_check, 'need_split'), (self.optimize_check, 'need_optimize'),
                             (self.translate_check, 'need_translate'), (self.synthesis_check, 'need_video'),
                             (self.soft_check, 'soft_subtitle')]:
            control.setChecked(bool(profile_value(profile, key)))
        for combo, key in [(self.engine_combo, 'transcribe_model'), (self.language_combo, 'transcribe_language'),
                           (self.target_combo, 'target_language'), (self.quality_combo, 'video_quality'),
                           (self.device_combo, 'faster_whisper_device')]:
            combo.setCurrentIndex(max(0, combo.findData(profile['settings'].get(key, getattr(cfg, key).serialize()))))
        self.refresh_flow()
        self.refresh_style_hint()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_style_hint()
        self.on_search_input_changed()

    def refresh_style_hint(self):
        try:
            profile=freeze_task_style(effective_profile(), self.style_combo.currentData())
            d=appearance_details(profile['style_template'])
            self.style_hint.setText(f"{profile['style_source']} · {d['font']} · 字号 {d['software_font_size']:g} · 距底部 {d['margin_bottom']/7.2:.1f}% · 阴影已按保存参数载入")
        except Exception as exc:
            self.style_hint.setText('样式未就绪：'+str(exc))

    def edit_task_style(self):
        window=self.window()
        if hasattr(window, 'subtitleStyleInterface'):
            name=self.style_combo.currentData() or effective_profile()['settings']['subtitle_style_name']
            window.subtitleStyleInterface.loadStyle(name)
            # Explicit appearance editing uses that saved preset for this run.
            self.style_combo.setCurrentIndex(self.style_combo.findData(name))
            window.switchTo(window.subtitleStyleInterface)

    def refresh_flow(self):
        strict = self.customer_selector.mode_combo.currentData() == 'strict'
        for control in (self.split_check, self.optimize_check, self.translate_check):
            control.setEnabled(True)
        self.mode_hint.setVisible(strict)
        self.engine_combo.setEnabled(self.transcribe_check.isChecked())
        self.language_combo.setEnabled(self.transcribe_check.isChecked())
        local = self.engine_combo.currentData() == TranscribeModelEnum.FASTER_WHISPER.value
        self.device_panel.setVisible(local)
        self.device_combo.setEnabled(self.transcribe_check.isChecked() and local)
        self.target_combo.setEnabled(self.translate_check.isChecked())
        self.quality_combo.setEnabled(self.synthesis_check.isChecked())
        self.soft_check.setEnabled(self.synthesis_check.isChecked())
        mode = self.customer_selector.mode_combo.currentData()
        steps = ['语音转录' if self.transcribe_check.isChecked() else '读取已有字幕']
        if mode in ('strict', 'hybrid', 'script_correct'):
            steps.append('原声定位＋剧本校正' if mode=='script_correct' else '剧本完全替换' if mode=='strict' else '旧版混合改写')
        elif mode == 'voice':
            steps.append('语音为主＋JSON局部辅助校正')
        elif self.optimize_check.isChecked():
            steps.append('字幕校正')
        if self.split_check.isChecked():
            steps.append('智能断句')
        if self.translate_check.isChecked():
            steps.append('字幕翻译')
        if self.synthesis_check.isChecked():
            steps.append('合成视频')
        route = '\n转录执行：' + self.engine_combo.currentText() + '（按本次选择执行）' if self.transcribe_check.isChecked() else ''
        if local and self.transcribe_check.isChecked():
            route += ' · 运行设备：' + ('CPU' if self.device_combo.currentData() == 'cpu' else 'GPU（CUDA）')
        self.flow_hint.setText('本次流程：' + ' → '.join(steps) + route)

    def choose_media(self):
        formats = ' '.join('*.' + fmt.value for fmt in list(SupportedVideoFormats) + list(SupportedAudioFormats))
        path, _ = QFileDialog.getOpenFileName(self, '选择视频 / 音频', '', '媒体文件 (' + formats + ')')
        if path:
            self.search_input.setText(path)

    def choose_subtitle(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择已有字幕', '', '字幕文件 (*.srt *.ass *.vtt)')
        if path:
            self.subtitle_input.setText(path)
            self.transcribe_check.setChecked(False)
            self.status_label.setText('已选择已有字幕，将跳过语音转录。')

    def on_search_input_changed(self):
        path = self.search_input.text().strip()
        if not os.path.isfile(path):
            self.script_hint.setText('选择原视频后显示绑定状态；已在“剧本绑定”保存的记录会自动读取。')
            return
        self.script_hint.setText('已绑定本集剧本（单集与批量共用，无需重复绑定）' if os.path.isfile(path) and load_exact_video_script_reference(path)
                                 else '尚未绑定；需要 JSON 台词替换时再绑定。')

    def bind_script(self):
        video = self.search_input.text().strip()
        if not os.path.isfile(video):
            InfoBar.warning('先选择原视频', '剧本需要与本集原视频绑定。', parent=self)
            return
        path, _ = QFileDialog.getOpenFileName(self, '绑定本集剧本', str(Path(video).parent), '剧本文件 (*.json *.txt *.docx)')
        if not path:
            return
        try:
            a, b = extract_episode_number(path), extract_episode_number(video)
            if a is not None and b is not None and a != b:
                raise ValueError(f'集号不一致：剧本第{a}集，视频第{b}集')
            save_video_script_reference(video, load_script_file(path), path)
            self.on_search_input_changed()
            InfoBar.success('绑定完成', '本集处理时会自动读取此剧本。', parent=self)
        except Exception as exc:
            InfoBar.error('绑定失败', str(exc), parent=self)

    def build_request(self):
        video = self.search_input.text().strip()
        subtitle = self.subtitle_input.text().strip()
        transcribe = self.transcribe_check.isChecked()
        mode = self.customer_selector.mode_combo.currentData()
        if transcribe and not (os.path.isfile(video) or self._is_valid_url(video)):
            raise ValueError('请选择视频 / 音频，或填写有效的视频网址。')
        if not transcribe and not os.path.isfile(subtitle):
            raise ValueError('已关闭转录，请选择已有字幕。')
        if (self.synthesis_check.isChecked() or mode in ('strict', 'hybrid', 'script_correct')) and not os.path.isfile(video):
            raise ValueError('合成视频或 JSON 台词替换需要选择对应的本地原视频。')
        if self.synthesis_check.isChecked() and Path(video).suffix.lower().lstrip('.') not in {fmt.value for fmt in SupportedVideoFormats}:
            raise ValueError('合成视频需要视频素材；音频可转录并导出字幕。')
        if mode in ('strict', 'hybrid', 'script_correct') and not load_exact_video_script_reference(video):
            raise ValueError('本集原视频尚未绑定剧本，请点击“绑定剧本 JSON”。')
        profile = freeze_task_style(effective_profile(), self.style_combo.currentData())
        profile['text_source_mode'] = mode
        settings = profile['settings']
        strict = mode == 'strict'
        for control, key in [(self.split_check, 'need_split'), (self.optimize_check, 'need_optimize'),
                             (self.translate_check, 'need_translate'), (self.synthesis_check, 'need_video'),
                             (self.soft_check, 'soft_subtitle')]:
            settings[key] = control.isChecked()
        for combo, key in [(self.engine_combo, 'transcribe_model'), (self.language_combo, 'transcribe_language'),
                           (self.target_combo, 'target_language'), (self.quality_combo, 'video_quality'),
                           (self.device_combo, 'faster_whisper_device')]:
            settings[key] = combo.currentData()
        if transcribe and settings['transcribe_model'] == TranscribeModelEnum.MULTILINGUAL_ASR.value:
            from urllib.parse import urlsplit
            endpoint = urlsplit(cfg.multilingual_asr_base.value.strip())
            if endpoint.scheme not in ('http', 'https') or not endpoint.hostname:
                raise ValueError('已选择云端转录，请先在“服务与高级设置”填写云端服务地址；不会改用本地。')
        process_subtitles = mode in ('strict', 'hybrid', 'voice', 'script_correct') or any(settings[key] for key in ('need_split', 'need_optimize', 'need_translate'))
        return dict(video=video, subtitle=subtitle, transcribe=transcribe, process_subtitles=process_subtitles,
                    synthesize=self.synthesis_check.isChecked(), profile=profile)

    def process(self):
        if self.busy:
            return
        try:
            request = self.build_request()
        except Exception as exc:
            InfoBar.warning('请完善任务', str(exc), duration=6000, parent=self)
            return
        self.pending_request = request
        self.set_busy(True)
        if request['transcribe'] and self._is_valid_url(request['video']):
            self._process_url(request['video'])
        else:
            self.requestReady.emit(request)

    def on_video_download_finished(self, video_file_path):
        if not video_file_path:
            self.on_create_task_error('视频下载失败')
            return
        self.pending_request['video'] = video_file_path
        self.search_input.setText(video_file_path)
        self.requestReady.emit(self.pending_request)

    def on_create_task_error(self, error):
        self.set_busy(False)
        super().on_create_task_error(error)

    def set_busy(self, busy):
        self.busy = busy
        self.customer_selector.setEnabled(not busy)
        self.start_button.setEnabled(not busy)
        self.start_button.setText('处理中…' if busy else '开始处理')
        self.status_label.setText('处理中，可切换到对应步骤查看进度。' if busy else '准备就绪')

    def complete(self, path='', error=False):
        self.set_busy(False)
        self.result_path = path or self.result_path
        self.result_button.setEnabled(bool(self.result_path))
        self.status_label.setText('处理失败，请查看当前步骤提示或日志。' if error else '处理完成，可查看字幕或打开结果文件夹。')

    def open_result(self):
        if self.result_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.result_path).parent)))

    def navigate(self, name):
        window = self.window()
        if hasattr(window, name):
            window.switchTo(getattr(window, name))
