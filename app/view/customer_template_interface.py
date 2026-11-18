"""Shared customer-template editor and reusable task selector."""
import copy
import json
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QScrollArea, QGroupBox, QLineEdit, QPlainTextEdit, QComboBox, QSpinBox,
    QCheckBox, QLabel, QListWidget, QListWidgetItem, QMessageBox)
from qfluentwidgets import PushButton, PrimaryPushButton, InfoBar, isDarkTheme, qconfig
from qfluentwidgets import CheckBox as QCheckBox, SpinBox as QSpinBox
from app.view.studio_controls import StyledCombo
from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.config import SUBTITLE_STYLE_PATH
from app.core.utils.text_source_mode import OPTIONS, selected_mode, select_mode
from app.core.customer_templates import (capture_settings, list_profiles, save_profile,
    apply_profile, active_profile, clear_profile, summary, appearance_summary)


class CustomerTemplateSelector(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel('客户', self))
        self.combo = StyledCombo(self)
        self.combo.setMinimumWidth(240)
        row.addWidget(self.combo, 1)
        refresh = PushButton('刷新', self)
        manage = PushButton('管理模板', self)
        row.addWidget(refresh)
        row.addWidget(manage)
        outer.addLayout(row)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color:#aaaaaa;font-size:14px;')
        outer.addWidget(self.hint)
        mode_row = QHBoxLayout()
        self.mode_row = mode_row
        mode_row.addWidget(QLabel('文字处理方式', self))
        self.mode_combo = StyledCombo(self)
        for label, value in OPTIONS:
            self.mode_combo.addItem(label, value)
        mode_row.addWidget(self.mode_combo, 1)
        outer.addLayout(mode_row)
        note = QLabel('客户模板提供默认要求；每集剧本单独绑定，服务地址与密钥在通用设置中管理。', self)
        self.note = note
        note.setWordWrap(True)
        outer.addWidget(note)
        self.mode_combo.activated.connect(self.change_mode)
        self.profiles = []
        self.apply_studio_theme()
        qconfig.themeChanged.connect(self.apply_studio_theme)
        refresh.clicked.connect(self.reload)
        manage.clicked.connect(self.open_manager)
        self.combo.activated.connect(self.choose)
        signalBus.customer_templates_changed.connect(self.reload)
        signalBus.customer_template_applied.connect(self.reload)
        self.reload()
        self.combo.before_open = self.reload

    def apply_studio_theme(self, *args):
        dark = isDarkTheme()
        text, background, border = ("#e4eaf0", "#292f36", "#414b55") if dark else ("#273540", "#ffffff", "#dce3e9")
        self.setStyleSheet(f"QLabel {{color:{text};}} QComboBox {{background:{background};color:{text};padding:7px;border:1px solid {border};border-radius:6px;}}")
        self.hint.setStyleSheet("font-size:12px;color:" + ("#a4b2bf" if dark else "#657687") + ";")

    def reload(self):
        current = active_profile()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(selected_mode(current))))
        self.mode_combo.blockSignals(False)
        from app.core.template_permissions import is_protected
        protected=is_protected(current)
        self.mode_combo.setEnabled(not protected)
        self.mode_combo.setToolTip('阿拉伯模板受保护，文字处理方式按模板执行。' if protected else '可在任务选项中临时切换，不修改客户模板。')
        try:
            self.profiles = list_profiles()
        except OSError as exc:
            self.hint.setText('模板库暂不可用：' + str(exc))
            return
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem('自定义本次任务', '')
        for profile in self.profiles:
            self.combo.addItem(profile['name'], profile['id'])
        index = self.combo.findData(current.get('id', ''))
        if index < 0 and current.get('id'):
            self.combo.addItem(current.get('name', '当前模板') + '（暂未同步）', current['id'])
            index = self.combo.findData(current['id'])
        self.combo.setCurrentIndex(max(0, index))
        self.combo.blockSignals(False)
        if current:
            self.hint.setText(f"共享模板 v{current.get('revision', 1)} · {current.get('settings', {}).get('transcribe_language', '自动')} · 样式 {current.get('settings', {}).get('subtitle_style_name', 'default')}")
            self.hint.setToolTip(appearance_summary(current))
        else:
            self.hint.setText('选择共享客户模板；本次调整单独保存，不改写模板。')
        if current.get('_shared_source_error'):
            self.hint.setText(current['_shared_source_error'])

    def change_mode(self):
        select_mode(active_profile().get('id'), self.mode_combo.currentData())
        signalBus.customer_template_applied.emit()

    def choose(self):
        identity = self.combo.currentData()
        if not identity:
            clear_profile()
        else:
            # Resolve again at selection time so a colleague's saved revision
            # is not silently replaced by an old in-memory list entry.
            profile = next((item for item in list_profiles() if item['id'] == identity), None)
            if not profile:
                return
            try:
                apply_profile(profile)
            except (OSError, ValueError, RuntimeError) as exc:
                InfoBar.error('模板加载失败', str(exc), parent=self)
                return
        signalBus.customer_template_applied.emit()

    def open_manager(self):
        window = self.window()
        if hasattr(window, 'customerTemplateInterface'):
            window.switchTo(window.customerTemplateInterface)

    def showEvent(self, event):
        super().showEvent(event)
        import time
        now=time.monotonic()
        if now-getattr(self,'_last_navigation_reload',-100)<3:return
        self._last_navigation_reload=now
        self.reload()


class CustomerTemplateInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('CustomerTemplateInterface')
        self.setWindowTitle('客户任务模板')
        self.current = {}
        self.controls = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel('客户', self)
        title.setStyleSheet('font-size:24px;font-weight:600;')
        layout.addWidget(title)
        hint = QLabel('共享给所有使用者 · 保存处理要求、词库、字幕规则和样式 · 不保存API密钥或每集剧本', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        toolbar = QHBoxLayout()
        for label, callback in [('新建（读取当前设置）', self.new), ('复制', self.duplicate), ('刷新列表', self.reload)]:
            button = PushButton(label, self)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        from app.view.requirements_import_ui import DocumentImportController
        self.import_document_btn=PushButton('导入客户文档（LLM）',self)
        self.document_import=DocumentImportController(self,self.import_document_btn)
        self.import_document_btn.clicked.connect(self.document_import.choose)
        toolbar.addWidget(self.import_document_btn)
        review=PushButton('核对导入要求',self)
        review.clicked.connect(self.document_import.review_current)
        toolbar.addWidget(review)
        self.setAcceptDrops(True)
        toolbar.addStretch()
        save = PrimaryPushButton('保存到共享模板库', self)
        apply = PushButton('应用已保存模板', self)
        save.clicked.connect(self.save)
        apply.clicked.connect(self.apply_current)
        toolbar.addWidget(save)
        toolbar.addWidget(apply)
        layout.addLayout(toolbar)
        body = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.setFixedWidth(235)
        self.list.currentItemChanged.connect(self.load_selected)
        body.addWidget(self.list)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        editor = QWidget(scroll)
        editor_layout = QVBoxLayout(editor)
        self.name = QLineEdit(editor)
        self.name.setPlaceholderText('例如：客户A · 英文原声口语版')
        self.enabled = QCheckBox('启用（停用后不在任务选择列表显示）', editor)
        self.enabled.setChecked(True)
        self.notes = QLineEdit(editor)
        basic = QGroupBox('客户信息', editor)
        form = QFormLayout(basic)
        form.addRow('模板名称', self.name)
        form.addRow('备注', self.notes)
        form.addRow(self.enabled)
        editor_layout.addWidget(basic)

        def group(title):
            box = QGroupBox(title, editor)
            editor_layout.addWidget(box)
            return QFormLayout(box)

        processing = group('处理方式')
        self.template_mode_combo = StyledCombo(editor)
        for label, value in OPTIONS:
            self.template_mode_combo.addItem('常规流程（按下方转录/校正/翻译设置）' if value is None else label, value)
        processing.addRow('文字处理方式', self.template_mode_combo)
        mode_help = QLabel('保存后单集和批量均固定使用此流程；JSON替换需为每集绑定剧本，并配置可用的LLM。', editor)
        mode_help.setWordWrap(True)
        self.mode_help=mode_help
        processing.addRow(mode_help)
        for key, label in [('transcribe_model', '转录方式'), ('transcribe_language', '音频语言'),
                ('target_language', '翻译目标语言'), ('translator_service', '翻译服务')]:
            self.add_option(processing, key, label)
        for key, label in [('need_split', '启用语义断句'), ('need_optimize', '启用字幕校正'),
                ('need_translate', '启用翻译（原声同语字幕请关闭）')]:
            self.add_check(processing, key, label)
        self.add_option(processing, 'split_type', '断句方式')
        rules = group('字幕长度与输出')
        self.add_number(rules, 'max_word_count_cjk', '中文每条最多字数', 8, 100)
        self.add_number(rules, 'max_word_count_english', '英文每条最多单词', 3, 100)
        self.line_words = QSpinBox(editor)
        self.line_words.setRange(3, 12)
        rules.addRow('英文每行最多单词（常规两行）', self.line_words)
        explanation = QLabel('客户模板包含文字规则和完整外观；单集、批量均按所选模板固定参数。要临时调参，请先切换“自定义”。', editor)
        explanation.setWordWrap(True)
        rules.addRow(explanation)
        self.add_check(rules, 'needs_remove_punctuation', '去除末尾标点（保留英文标点请关闭）')
        self.add_check(rules, 'need_video', '生成合成视频')
        self.add_check(rules, 'soft_subtitle', '使用可切换软字幕')
        self.add_option(rules, 'video_quality', '视频质量')
        self.style = StyledCombo(editor)
        self.style.setEditable(False)
        rules.addRow('字幕外观模板', self.style)
        reread = PushButton('重新读取外观模板（下次保存更新快照）', editor)
        reread.clicked.connect(self.reread_style)
        rules.addRow(reread)
        self.appearance_hint = QLabel(editor)
        self.appearance_hint.setWordWrap(True)
        rules.addRow('此客户实际保存的外观', self.appearance_hint)
        style_help = QLabel('字号、颜色、描边、阴影和位置均保存在外观快照中。先到“字幕样式”调整并保存，再点上方“重新读取外观模板”，最后保存客户模板。不会自动改动其他客户。', editor)
        style_help.setWordWrap(True)
        rules.addRow(style_help)
        self.layout_combo = StyledCombo(editor)
        self.layout_combo.addItems(['仅原文', '仅译文', '原文在上', '译文在上'])
        rules.addRow('字幕显示方式', self.layout_combo)
        language = group('客户文字要求')
        self.prompt = QPlainTextEdit(editor)
        self.prompt.setPlaceholderText('例如：保留自然口语和缩写；使用句首大写；不随意改写人物台词。')
        self.prompt.setMinimumHeight(140)
        language.addRow('校正 / 翻译要求', self.prompt)
        self.glossary = QPlainTextEdit(editor)
        self.glossary.setPlaceholderText('例如：人名 Alex、公司名 ACME；具体每集台词请到“剧本参考”绑定。')
        self.glossary.setMinimumHeight(95)
        language.addRow('客户专用词库', self.glossary)
        self.permission_hint=QLabel(editor);self.permission_hint.setWordWrap(True)
        processing.addRow('执行分工',self.permission_hint)
        self.template_mode_combo.currentIndexChanged.connect(self.update_permission_hint)
        for key in ('need_optimize','need_split','need_translate','needs_remove_punctuation'):
            self.controls[key].toggled.connect(self.update_permission_hint)
        self.status = QLabel(editor)
        self.status.setWordWrap(True)
        editor_layout.addWidget(self.status)
        editor_layout.addStretch()
        scroll.setWidget(editor)
        body.addWidget(scroll, 1)
        layout.addLayout(body, 1)
        self.setStyleSheet('''QWidget#CustomerTemplateInterface {background:#252525; color:#eeeeee; font-family:"Microsoft YaHei UI";} QLabel,QCheckBox,QGroupBox {color:#eeeeee;} QScrollArea,QScrollArea > QWidget > QWidget {background:#252525;} QGroupBox {font-weight:600; border:1px solid #484848; border-radius:7px; margin-top:12px; padding:14px 8px 8px;} QGroupBox::title {subcontrol-origin:margin; left:12px;} QLineEdit,QPlainTextEdit,QComboBox,QSpinBox,QListWidget {background:#303030; color:#eeeeee; border:1px solid #555; border-radius:4px; padding:5px;}''')
        from app.view.studio_form_theme import finish_customers
        finish_customers(self, body, scroll, editor, editor_layout, basic)
        from app.view.customer_appearance import install_appearance
        install_appearance(self)
        from app.view.customer_template_dashboard import install_dashboard
        install_dashboard(self)
        self.reload()
        if not self.list.count():
            self.new()
        else:
            self.list.setCurrentRow(0)

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and any(Path(url.toLocalFile()).suffix.lower() in ('.docx','.pdf','.txt','.md') for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:event.ignore()

    def dropEvent(self,event):
        paths=[url.toLocalFile() for url in event.mimeData().urls() if Path(url.toLocalFile()).suffix.lower() in ('.docx','.pdf','.txt','.md')]
        if len(paths)!=1:
            InfoBar.warning('一次导入一份文档','请将同一客户的要求合并到一份文档，避免相互覆盖',parent=self);return
        self.document_import.import_file(paths[0]);event.acceptProposedAction()

    def add_option(self, form, key, label):
        box = StyledCombo(self)
        item = getattr(cfg, key)
        for value in item.validator.options:
            option_label = str(getattr(value, 'value', value))
            if key == 'video_quality':
                option_label = {'source': '跟随原视频码率', '18': '高质量（18）', '20': '较高质量（20）', '23': '标准质量（23）', '28': '小体积（28）'}.get(option_label, option_label)
            box.addItem(option_label, item.serializer.serialize(value))
        self.controls[key] = box
        form.addRow(label, box)

    def add_check(self, form, key, label):
        box = QCheckBox(label, self)
        self.controls[key] = box
        form.addRow(box)

    def add_number(self, form, key, label, low, high):
        box = QSpinBox(self)
        box.setRange(low, high)
        self.controls[key] = box
        form.addRow(label, box)

    def reload(self):
        identity = self.current.get('id')
        try:
            profiles = list_profiles(include_disabled=True)
        except OSError as exc:
            self.status.setText(str(exc))
            return
        self.list.blockSignals(True)
        self.list.clear()
        selected = None
        for profile in profiles:
            item = QListWidgetItem(profile['name'] + ('' if profile.get('enabled', True) else '（停用）'))
            item.setData(Qt.UserRole, profile)
            self.list.addItem(item)
            if profile['id'] == identity:
                selected = item
        self.list.blockSignals(False)
        if hasattr(self, 'template_search'):
            from app.view.studio_form_theme import filter_templates
            filter_templates(self, self.template_search.text())
        if selected:
            self.list.setCurrentItem(selected)

    def new(self):
        self.populate(dict(name='', enabled=True, settings=capture_settings(), notes='', glossary='', english_line_words=6))

    def reread_style(self):
        path = SUBTITLE_STYLE_PATH / (self.style.currentText() + '.txt')
        try:
            style = path.read_text(encoding='utf-8')
            if 'Style: Default,' not in style:
                raise ValueError('外观文件缺少主字幕样式')
            self.current['style_template'] = style
            self.current.setdefault('settings', {})['subtitle_style_name'] = self.style.currentText()
            self.appearance_hint.setText(appearance_summary(self.current))
            self.appearance.load_draft()
            self.status.setText('外观已重新读取到草稿；保存并确认后生效。')
        except (OSError, ValueError) as exc:
            InfoBar.error('读取外观失败', str(exc), parent=self)

    def duplicate(self):
        from app.core.template_permissions import assert_template_write
        try:assert_template_write(self.current)
        except RuntimeError as error:InfoBar.warning('只读模板',str(error),parent=self);return
        data = self.collect()
        data.pop('id', None)
        data['revision'] = 0
        data['name'] += '（副本）'
        self.populate(data)

    def load_selected(self, item, previous=None):
        if item:
            self.populate(item.data(Qt.UserRole))

    def populate(self, profile):
        self.current = copy.deepcopy(profile)
        self._saved_profile = copy.deepcopy(profile)
        settings = profile.get('settings', {})
        self.name.setText(profile.get('name', ''))
        self.template_mode_combo.setCurrentIndex(max(0, self.template_mode_combo.findData(selected_mode(profile))))
        self.notes.setText(profile.get('notes', ''))
        self.enabled.setChecked(profile.get('enabled', True))
        for key, box in self.controls.items():
            value = settings.get(key, getattr(cfg, key).serialize())
            if isinstance(box, QCheckBox):
                box.setChecked(bool(value))
            elif isinstance(box, QSpinBox):
                box.setValue(int(value))
            else:
                box.setCurrentIndex(max(0, box.findData(value)))
        self.style.clear()
        self.style.addItems(sorted(path.stem for path in SUBTITLE_STYLE_PATH.glob('*.txt')))
        name = settings.get('subtitle_style_name', 'default')
        if self.style.findText(name) < 0:
            self.style.addItem(name)
        self.style.setCurrentText(name)
        self.layout_combo.setCurrentText(settings.get('subtitle_layout', '仅原文'))
        self.line_words.setValue(int(profile.get('english_line_words', 6)))
        self.prompt.setPlainText(settings.get('custom_prompt_text', ''))
        self.glossary.setPlainText(profile.get('glossary', ''))
        self.appearance_hint.setText(appearance_summary(profile))
        self.status.setText(f"版本：{profile.get('revision', '未保存')} · 保存时会冻结样式，不随原外观模板自动变化。")

        if hasattr(self, 'appearance'): self.appearance.load_draft()
        self._saved_profile = self.collect()
        self.update_permission_hint()
        if hasattr(self,"dashboard"):self.dashboard.refresh()
        self.refresh_edit_permission()

    def refresh_edit_permission(self):
        from app.core.template_permissions import can_edit,is_protected
        allowed=can_edit(self.current)
        if hasattr(self,'dashboard'):
            self.dashboard.scroll.setEnabled(allowed)
            self.dashboard.advanced_dialog.setEnabled(allowed)
            self.dashboard.save_button.setEnabled(allowed)
        if is_protected(self.current):self.status.setText('阿拉伯模板 · '+('本机所有者可修改；其他电脑只读使用。' if allowed else '只读使用；修改、改名、停用和覆盖均被禁止。'))

    def update_permission_hint(self,*args):
        from app.core.llm_permissions import permission_summary
        settings={key:self.controls[key].isChecked() for key in ('need_optimize','need_split','need_translate','needs_remove_punctuation')}
        mode=self.template_mode_combo.currentData()
        self.permission_hint.setText(permission_summary(settings,mode))
        self.mode_help.setText({'script_correct':'原声确定说话范围，本集剧本纠正错误发音和识别；没有可靠对应的内容保留并列入检查单。必须绑定本集剧本，不追加自由改写。','strict':'独立的剧本完全替换模式；保留原有替换与时间处理流程，不受普通校正模式影响。','voice':'按实际原声生成字幕；剧本仅用于局部识别核对，不整句替换实际说法。','hybrid':'兼容旧模板的混合改写流程；新模板建议选择原声定位＋剧本校正。'}.get(mode,'常规处理：按下方校正、断句和翻译设置执行。'))

    def collect(self):
        data = copy.deepcopy(self.current)
        data['text_source_mode'] = self.template_mode_combo.currentData()
        settings = dict(data.get('settings', {}))
        for key, box in self.controls.items():
            settings[key] = box.isChecked() if isinstance(box, QCheckBox) else box.value() if isinstance(box, QSpinBox) else box.currentData()
        settings.update(custom_prompt_text=self.prompt.toPlainText(),
            subtitle_style_name=self.style.currentText(), subtitle_layout=self.layout_combo.currentText())
        style_name = settings['subtitle_style_name']
        previous = self.current.get('settings', {}).get('subtitle_style_name')
        style = self.current.get('style_template', '') if previous == style_name else ''
        if not style:
            path = SUBTITLE_STYLE_PATH / (style_name + '.txt')
            if not path.exists():
                raise ValueError('找不到字幕外观模板，请先到字幕样式页创建或选择已有模板')
            style = path.read_text(encoding='utf-8')
        data.update(name=self.name.text(), notes=self.notes.text(), enabled=self.enabled.isChecked(),
            glossary=self.glossary.toPlainText(), settings=settings, style_template=style,
            english_line_words=self.line_words.value())
        return data

    def save(self):
        try:
            from app.core.template_permissions import assert_template_write
            assert_template_write(self.current)
            draft = self.collect()
            if QMessageBox.question(self, '确认保存客户模板',
                    '确定保存“' + draft.get('name', '') + '”的修改吗？\n保存后所有电脑的新任务将使用此版本；正在处理的任务不变。',
                    QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Save:
                return
            profile = save_profile(draft, int(self.current.get('revision', 0)))
            self.current = profile
            self.reload()
            if active_profile().get('id') == profile.get('id'):
                apply_profile(profile)
                signalBus.customer_template_applied.emit()
            signalBus.customer_templates_changed.emit()
            self.status.setText(f"已保存到共享盘 · 版本 {profile['revision']} · 所有人刷新模板列表即可看到。")
        except (OSError, ValueError, RuntimeError) as exc:
            InfoBar.error('保存失败', str(exc), duration=6000, parent=self)

    def apply_current(self):
        try:
            if self.collect() != self._saved_profile:
                raise ValueError('存在未保存的修改；请先点保存并确认后再应用。')
            profile = copy.deepcopy(self._saved_profile)
            if not profile.get('id'):
                raise ValueError('请先保存客户模板，再应用')
            apply_profile(profile)
            signalBus.customer_template_applied.emit()
            InfoBar.success('已应用', '已载入本机任务设置；已有任务不回写共享模板。', parent=self)
        except (OSError, ValueError, RuntimeError) as exc:
            InfoBar.error('应用失败', str(exc), duration=5000, parent=self)
