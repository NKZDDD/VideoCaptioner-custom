"""Shared light form presentation. Does not read/write task or template settings."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget,QVBoxLayout,QLabel,QScrollArea,QStackedWidget,
    QGroupBox,QFormLayout,QPushButton,QLineEdit,QCheckBox)
from qfluentwidgets import PushButton,PrimaryPushButton,SegmentedWidget,LineEdit

FORM_STYLE='''
QWidget#TaskCreationInterface,QWidget#CustomerTemplateInterface,
QWidget#studioFormContent,QWidget#studioTemplateEditor {background:#f4f6f8;color:#304552;}
QLabel {color:#304552;background:transparent;font-family:"Microsoft YaHei UI";font-size:13px;}
QGroupBox {background:#ffffff;color:#304552;border:1px solid #dfe7ed;border-radius:10px;
    margin-top:18px;padding:20px 16px 14px;font-size:14px;font-weight:600;}
QGroupBox::title {subcontrol-origin:margin;left:17px;top:2px;padding:0 5px;color:#354f5d;}
QCheckBox {color:#304552;background:transparent;font-size:13px;spacing:9px;min-height:27px;}
QLineEdit,QPlainTextEdit,QTextEdit {background:#ffffff;color:#304552;border:1px solid #dce5eb;border-radius:7px;padding:7px;selection-background-color:#d8eee8;selection-color:#234e47;}
QLineEdit:focus,QPlainTextEdit:focus,QTextEdit:focus {border:1px solid #168779;}
QLineEdit:disabled,QPlainTextEdit:disabled {background:#edf1f4;color:#86939d;border-color:#e4eaee;}
QSpinBox {background:#ffffff;color:#304552;border:1px solid #dce5eb;border-radius:7px;min-height:34px;padding:0 8px;}
QScrollArea {background:#f4f6f8;border:0;}
QScrollArea > QWidget {background:#f4f6f8;}
QScrollArea > QWidget > QWidget {background:#f4f6f8;}
QListWidget {background:#ffffff;color:#344b59;border:1px solid #e0e7ed;border-radius:9px;outline:0;}
QListWidget::item {padding:13px 11px;min-height:20px;border:0;border-radius:6px;margin:3px 5px;}
QListWidget::item:selected {background:#e4f3ef;color:#147e72;}
QListWidget::item:hover:!selected {background:#f1f6f7;}
QScrollBar:vertical {background:transparent;width:9px;margin:0;}
QScrollBar::handle:vertical {background:#d7e1e7;min-height:28px;border-radius:4px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
'''

BUTTON_STYLE='''QPushButton {background:#ffffff;color:#354b59;border:1px solid #dce5eb;border-radius:7px;padding:7px 12px;font-size:13px;min-height:20px;}
QPushButton:hover {background:#eff6f4;border-color:#a9cfc5;}
QPushButton:pressed {background:#e2efeb;}
QPushButton:disabled {color:#98a6af;background:#f2f5f7;border-color:#e5ebef;}'''
PRIMARY_STYLE='''QPushButton {background:#168779;color:white;border:1px solid #168779;border-radius:7px;padding:8px 16px;font-size:13px;min-height:20px;}
QPushButton:hover {background:#10776b;}
QPushButton:disabled {background:#a7cbc4;border-color:#a7cbc4;color:white;}'''


def apply_form_theme(page):
    page.setStyleSheet(FORM_STYLE)
    for button in page.findChildren(PushButton):
        # StyledCombo owns its arrow/menu geometry, not ordinary button padding.
        if hasattr(button,'currentData'):continue
        button.setStyleSheet(PRIMARY_STYLE if isinstance(button,PrimaryPushButton) else BUTTON_STYLE)
    for form in page.findChildren(QFormLayout):
        form.setHorizontalSpacing(22);form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    for box in page.findChildren(QGroupBox):
        if box.layout():box.layout().setSpacing(12)


def finish_workbench(page,scroll,content,title,hint):
    content.setObjectName('studioFormContent')
    scroll.setObjectName('studioWorkbenchScroll')
    title.setText('单条处理');title.setStyleSheet('font-size:26px;font-weight:600;color:#253846;background:transparent;')
    hint.setText('选择客户和素材，按需转录、替换台词、翻译或合成。')
    hint.setStyleSheet('font-size:12px;color:#788795;background:transparent;')
    page.customer_selector.mode_combo.setStyleSheet('')
    page.customer_selector.apply_studio_theme()
    for control in (page.search_input,page.subtitle_input):control.setMinimumHeight(38)
    page.customer_selector.combo.setMaximumWidth(16777215)
    for control in (page.style_hint,page.script_hint,page.mode_hint):
        control.setStyleSheet('color:#728592;font-size:12px;background:transparent;')
    page.flow_hint.setStyleSheet('color:#197e71;background:#e9f4f0;border-radius:7px;padding:10px;font-size:12px;')
    page.status_label.setStyleSheet('color:#6d8290;font-size:12px;')
    page.layout().setSpacing(12)
    apply_form_theme(page)


def finish_customers(page,body,scroll,editor,editor_layout,basic):
    editor.setObjectName('studioTemplateEditor')
    page.setWindowTitle('客户模板')
    page.layout().setSpacing(12)
    page.layout().itemAt(0).widget().setText('客户模板')
    page.layout().itemAt(0).widget().setStyleSheet('font-size:26px;font-weight:600;color:#253846;')
    page.layout().itemAt(1).widget().setText('共享客户要求与字幕样式，每台电脑都使用同一份模板。')
    page.layout().itemAt(1).widget().setStyleSheet('color:#788795;font-size:12px;')
    # Group existing live fields; no reconstruction of values, IDs or snapshots.
    groups=[editor_layout.itemAt(i).widget() for i in range(editor_layout.count())
        if editor_layout.itemAt(i).widget() is not None and isinstance(editor_layout.itemAt(i).widget(),QGroupBox)]
    while editor_layout.count():editor_layout.takeAt(0)
    editor_layout.setContentsMargins(0,0,0,0);editor_layout.setSpacing(12)
    basic.setTitle('模板信息');editor_layout.addWidget(basic)
    tabs=SegmentedWidget(editor);stack=QStackedWidget(editor)
    tabs.setFixedHeight(34)
    for key,text,group in zip(('flow','output','text'),('处理流程','字幕与输出','文字要求'),groups[1:]):
        wrapper=QScrollArea(editor);wrapper.setWidgetResizable(True);wrapper.setFrameShape(QScrollArea.NoFrame)
        content=QWidget();layout=QVBoxLayout(content);layout.setContentsMargins(0,0,8,0);layout.setSpacing(8)
        layout.addWidget(group);layout.addStretch(1);wrapper.setWidget(content);stack.addWidget(wrapper)
        tabs.addItem(routeKey=key,text=text,onClick=lambda checked=False,w=wrapper:stack.setCurrentWidget(w))
    tabs.setCurrentItem('flow');stack.setCurrentIndex(0)
    editor_layout.addWidget(tabs);editor_layout.addWidget(stack,1)
    page.status.setStyleSheet('color:#6e8390;background:#eaf3f0;border-radius:7px;padding:10px;font-size:12px;')
    editor_layout.addWidget(page.status)
    page.template_tabs=tabs;page.template_sections=stack
    # A compact library with search, using the original list and selection handler.
    body.removeWidget(page.list)
    library=QWidget(page);library.setFixedWidth(200)
    library_layout=QVBoxLayout(library);library_layout.setContentsMargins(0,0,0,0);library_layout.setSpacing(9)
    page.template_search=LineEdit(library);page.template_search.setPlaceholderText('搜索客户模板');page.template_search.setClearButtonEnabled(True)
    page.list.setMinimumWidth(0);page.list.setMaximumWidth(16777215)
    library_layout.addWidget(page.template_search);library_layout.addWidget(page.list,1)
    body.insertWidget(0,library);body.setSpacing(16)
    page.template_search.textChanged.connect(lambda text:filter_templates(page,text))
    for button in page.findChildren(PushButton):
        if button.text()=='新建（读取当前设置）':button.setText('新建模板');button.setToolTip('以当前处理设置创建新模板')
        elif button.text()=='复制':button.setText('复制模板')
        elif button.text()=='刷新列表':button.setText('刷新')
    apply_form_theme(page)


def filter_templates(page,text):
    for i in range(page.list.count()):
        item=page.list.item(i);item.setHidden(text.casefold() not in item.text().casefold())
