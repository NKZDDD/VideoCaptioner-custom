"""Readable template dashboard over existing controls; no setting migration."""
import re
from PyQt5.QtCore import Qt,QTimer
from PyQt5.QtGui import QColor,QFont,QPainter,QPainterPath,QPen
from PyQt5.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,
 QFrame,QPushButton,QToolButton,QButtonGroup,QComboBox,QDialog,QDialogButtonBox,
 QFormLayout,QScrollArea,QMenu,QSizePolicy)

STYLE='''QWidget {font-family:"Microsoft YaHei";font-size:13px;}
QWidget#CustomerTemplateInterface {background:#f5f7f9;color:#263c48;}
QWidget#templateDashboard,QWidget#templateBody {background:#f5f7f9;}
QFrame#templateCard,QFrame#templateLibrary {background:white;border:1px solid #dde5e9;border-radius:12px;}
QLabel {color:#304552;background:transparent;font-size:13px;}
QLabel#dashboardTitle {font-size:24px;font-weight:600;color:#1f3440;}
QLabel#cardTitle {font-size:16px;font-weight:600;color:#233c46;}
QLabel#quiet {color:#788993;font-size:12px;}
QLabel#summaryChip {color:#176f66;background:#eaf5f1;border-radius:6px;padding:5px 10px;}
QLineEdit,QPlainTextEdit,QComboBox,QSpinBox {background:white;color:#304552;border:1px solid #dce5eb;border-radius:6px;padding:5px;min-height:24px;}
QPlainTextEdit {padding:8px;}
QListWidget {background:white;color:#304552;border:0;outline:0;font-size:13px;}
QListWidget::item {padding:12px 8px;border-radius:7px;}
QListWidget::item:selected {background:#e4f3ee;color:#08796f;}
QPushButton,QToolButton {background:white;color:#304552;border:1px solid #dce5eb;border-radius:7px;padding:7px 10px;font-size:13px;}
QPushButton:hover,QToolButton:hover {background:#f0f8f5;border-color:#168779;}
QPushButton#primary {background:#168779;color:white;border:1px solid #168779;font-weight:600;padding:9px 20px;}
QToolButton#modeTile {background:white;color:#304552;border:1px solid #dce5eb;border-radius:9px;padding:12px 6px;font-size:13px;}
QToolButton#modeTile:checked {background:#e7f5f0;color:#08796f;border:2px solid #168779;font-weight:600;}
QCheckBox {color:#304552;font-size:13px;background:transparent;spacing:7px;}
QScrollArea {border:0;background:transparent;}
QScrollArea>QWidget>QWidget {background:transparent;}
QPushButton#link {border:0;background:transparent;color:#168779;text-align:left;padding:5px 0;}
'''

def label(text,parent=None,name=None):
 widget=QLabel(text,parent);widget.setWordWrap(True)
 if name:widget.setObjectName(name)
 return widget

def take(layout,storage):
 while layout.count():
  item=layout.takeAt(0)
  if item.widget():item.widget().setParent(storage)
  elif item.layout():take(item.layout(),storage)

class SubtitleExample(QWidget):
 def __init__(self,page):
  super().__init__();self.page=page;self.setMinimumSize(150,210);self.setMaximumHeight(350)
  self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
 def paintEvent(self,event):
  painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
  rect=self.rect().adjusted(4,4,-4,-4);painter.setPen(Qt.NoPen);painter.setBrush(QColor('#e8e4df'));painter.drawRoundedRect(rect,10,10)
  # Neutral scene drawn by the app, not a customer video or a render promise.
  painter.setBrush(QColor('#ddd6ce'));painter.drawRoundedRect(rect.adjusted(15,25,-15,-75),8,8)
  painter.setBrush(QColor('#c9bfb3'));painter.drawRoundedRect(rect.left()+18,rect.bottom()-110,rect.width()-36,54,8,8)
  font_name,color,outline='Microsoft YaHei UI',QColor('white'),QColor('black')
  for line in self.page.current.get('style_template','').splitlines():
   if re.match(r'^Style:\s*Default,',line):
    fields=line.split(',')
    try:
     font_name=fields[1]
     def ass_color(value):
      value=value.replace('&H','').zfill(8);return QColor('#'+value[-2:]+value[-4:-2]+value[-6:-4])
     color,outline=ass_color(fields[3]),ass_color(fields[5])
    except (IndexError,ValueError):pass
    break
  painter.setPen(QColor('#8b8178'));font=QFont('Microsoft YaHei UI');font.setPixelSize(12);painter.setFont(font)
  painter.drawText(rect.adjusted(0,12,0,0),Qt.AlignHCenter|Qt.AlignTop,'示例画面')
  font=QFont(font_name);font.setPixelSize(17);painter.setFont(font)
  text='我会一直陪着你。';width=painter.fontMetrics().horizontalAdvance(text)
  path=QPainterPath();path.addText(rect.center().x()-width/2,rect.bottom()-28,font,text)
  painter.strokePath(path,QPen(outline,2.8,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));painter.fillPath(path,color)

class TemplateDashboard:
 def __init__(self,page):
  self.page=page;self.loading=False;self.mode_buttons={}
  self.storage=QWidget(page);self.storage.hide();take(page.layout(),self.storage)
  outer=page.layout();outer.setContentsMargins(22,18,22,14);outer.setSpacing(12)
  head=QHBoxLayout();titles=QVBoxLayout();titles.addWidget(label('客户模板',page,'dashboardTitle'));titles.addWidget(label('上传文档或截图，说清要求，助手生成可核对的模板。',page,'quiet'));head.addLayout(titles,1)
  self.reparent(page.import_document_btn);page.import_document_btn.setText('＋ 用需求新建模板');page.import_document_btn.setObjectName('primary');head.addWidget(page.import_document_btn)
  save=QPushButton('保存模板',page);save.setObjectName('primary');save.clicked.connect(page.save);head.addWidget(save)
  self.save_button=save
  more=QToolButton(page);more.setText('⋯');menu=QMenu(more)
  for text,callback in [('手动新建（高级）',page.new),('复制当前模板',page.duplicate),('刷新模板库',page.reload),('应用已保存模板',page.apply_current)]:menu.addAction(text,callback)
  more.setMenu(menu);more.setPopupMode(QToolButton.InstantPopup);head.addWidget(more);outer.addLayout(head)
  body=QHBoxLayout();body.setSpacing(16);library=QFrame(page);library.setObjectName('templateLibrary');library.setFixedWidth(195)
  lib=QVBoxLayout(library);lib.setContentsMargins(12,14,12,10);libhead=QHBoxLayout();libhead.addWidget(label('模板库',library,'cardTitle'),1)
  plus=QPushButton('＋',library);plus.setFixedWidth(30);plus.clicked.connect(page.document_import.choose);libhead.addWidget(plus);lib.addLayout(libhead)
  for w in (page.template_search,page.list):self.reparent(w);lib.addWidget(w,1 if w is page.list else 0)
  page.list.setMinimumWidth(0);page.list.setMaximumWidth(16777215);body.addWidget(library)
  main=QVBoxLayout();main.setSpacing(12)
  header,header_layout=self.card();name_row=QHBoxLayout();self.reparent(page.name);page.name.setPlaceholderText('填写客户模板名称')
  page.name.setStyleSheet('font-size:19px;font-weight:600;border:0;padding:2px;');name_row.addWidget(page.name,1)
  self.reparent(page.enabled);page.enabled.setText('启用模板');name_row.addWidget(page.enabled);header_layout.addLayout(name_row)
  self.chips=label('',header,'summaryChip');header_layout.addWidget(self.chips);main.addWidget(header)
  columns=QHBoxLayout();columns.setSpacing(14);left=QVBoxLayout();left.setSpacing(12)
  card,layout=self.card('1  字幕以什么为准');self.button_group=QButtonGroup(page);self.button_group.setExclusive(True)
  row=QHBoxLayout()
  for mode,text in [('voice','按原声出字幕'),('script_correct','原声＋剧本校正'),('strict','剧本完全替换')]:
   b=QToolButton(page);b.setObjectName('modeTile');b.setText(text);b.setCheckable(True);b.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
   b.setMinimumHeight(48);self.button_group.addButton(b);self.mode_buttons[mode]=b;row.addWidget(b)
   b.clicked.connect(lambda checked=False,m=mode:self.select_mode(m))
  layout.addLayout(row);self.mode_description=label('',card,'quiet');layout.addWidget(self.mode_description)
  row=QHBoxLayout();row.addWidget(label('音频语言'));self.reparent(page.controls['transcribe_language']);row.addWidget(page.controls['transcribe_language'],1)
  self.reparent(page.controls['need_translate']);page.controls['need_translate'].setText('翻译字幕');row.addWidget(page.controls['need_translate']);layout.addLayout(row)
  self.target_row=QWidget();target=QHBoxLayout(self.target_row);target.setContentsMargins(0,0,0,0);target.addWidget(label('翻译成'));self.reparent(page.controls['target_language']);target.addWidget(page.controls['target_language'],1);layout.addWidget(self.target_row)
  left.addWidget(card)
  card,layout=self.card('2  客户有哪些要求');row=QHBoxLayout();row.addStretch();extract=QPushButton('聊天调整 / 添加附件',card);extract.setObjectName('link');extract.clicked.connect(page.document_import.edit_current);row.addWidget(extract);layout.addLayout(row)
  self.reparent(page.prompt);page.prompt.setMinimumHeight(90);page.prompt.setMaximumHeight(120);page.prompt.setPlaceholderText('例如：保留标点；英文句首大写；人名按剧本核对。');layout.addWidget(page.prompt)
  row=QHBoxLayout();row.addWidget(label('客户词库'));self.reparent(page.glossary);page.glossary.setMinimumHeight(34);page.glossary.setMaximumHeight(48);page.glossary.setPlaceholderText('填写人名、公司名和专用词');row.addWidget(page.glossary,1);layout.addLayout(row)
  self.review=QPushButton('',card);self.review.setObjectName('link');self.review.clicked.connect(page.document_import.edit_current);layout.addWidget(self.review);left.addWidget(card)
  card,layout=self.card('3  字幕怎么输出');grid=QGridLayout();grid.setHorizontalSpacing(12)
  self.punctuation=QComboBox();self.punctuation.addItem('保留标点',False);self.punctuation.addItem('去除句末标点',True)
  self.punctuation.activated.connect(lambda:self.change_punctuation());grid.addWidget(label('标点'),0,0);grid.addWidget(self.punctuation,1,0)
  length=QWidget();length_row=QHBoxLayout(length);length_row.setContentsMargins(0,0,0,0)
  for key,text in [('max_word_count_cjk','中文字'),('max_word_count_english','英文词')]:
   self.reparent(page.controls[key]);page.controls[key].setFixedWidth(62)
   if hasattr(page.controls[key],'setSymbolVisible'):page.controls[key].setSymbolVisible(False)
   page.controls[key].setAlignment(Qt.AlignCenter);length_row.addWidget(page.controls[key]);length_row.addWidget(label(text))
  grid.addWidget(label('每条最多'),0,1);grid.addWidget(length,1,1)
  self.output=QComboBox();self.output.addItem('烧录字幕到视频','burn');self.output.addItem('视频＋可切换字幕','soft');self.output.addItem('仅导出字幕文件','subtitle')
  self.output.activated.connect(self.change_output);grid.addWidget(label('导出方式'),0,2);grid.addWidget(self.output,1,2);layout.addLayout(grid);left.addWidget(card)
  advanced=QPushButton('⚙  更多设置',page);advanced.setObjectName('link');advanced.clicked.connect(self.open_advanced);left.addWidget(advanced)
  left.addStretch(1);columns.addLayout(left,3)
  right=QVBoxLayout();card,layout=self.card('字幕效果');self.example=SubtitleExample(page);layout.addWidget(self.example,1)
  self.style_summary=label('',card,'quiet');layout.addWidget(self.style_summary)
  layout.addWidget(label('外观示意；字号、位置以实际素材预览为准。',card,'quiet'))
  appearance=QPushButton('调整字幕外观 / 实际预览',card);appearance.clicked.connect(self.open_appearance);layout.addWidget(appearance);right.addWidget(card,1)
  card,layout=self.card('执行摘要');self.flow=label('',card);self.flow.setStyleSheet('background:#eaf5f1;border-radius:7px;padding:12px;color:#116c61;');layout.addWidget(self.flow)
  layout.addWidget(label('保存后，新任务按这份模板执行。',card,'quiet'));right.addWidget(card);right.addStretch();columns.addLayout(right,1)
  main.addLayout(columns,1);container=QWidget(page);container.setObjectName('templateBody');container.setLayout(main)
  self.scroll=QScrollArea(page);self.scroll.setWidgetResizable(True);self.scroll.setFrameShape(QScrollArea.NoFrame);self.scroll.setWidget(container);body.addWidget(self.scroll,1);outer.addLayout(body,1)
  self.reparent(page.status);page.status.setObjectName('quiet');page.status.setMaximumHeight(45);outer.addWidget(page.status)
  self.advanced_dialog=QDialog(page);self.advanced_dialog.setWindowTitle('更多模板设置');self.advanced_dialog.resize(650,620)
  adv=QVBoxLayout(self.advanced_dialog);form=QFormLayout();form.setSpacing(10)
  self.reparent(page.template_mode_combo);form.addRow('处理模式（含兼容模式）',page.template_mode_combo)
  self.reparent(page.notes);form.addRow('模板备注',page.notes)
  for key,title in [('transcribe_model','转录服务'),('translator_service','翻译服务'),('need_split','按语义断句'),('need_optimize','普通文字校正'),('split_type','断句方式'),('video_quality','视频质量')]:
   w=page.controls[key];self.reparent(w)
   if hasattr(w,'setChecked'):w.setText(title);form.addRow(w)
   else:form.addRow(title,w)
  self.reparent(page.line_words);form.addRow('英文每行词数',page.line_words)
  self.reparent(page.layout_combo);form.addRow('原文 / 译文排列',page.layout_combo)
  self.reparent(page.style);form.addRow('外观预设',page.style)
  reread=QPushButton('重新读取外观预设到草稿');reread.clicked.connect(page.reread_style);form.addRow(reread)
  self.reparent(page.permission_hint);form.addRow('执行分工',page.permission_hint);adv.addLayout(form)
  close=QDialogButtonBox(QDialogButtonBox.Close);close.rejected.connect(self.advanced_dialog.accept);adv.addWidget(close)
  self.advanced_dialog.setStyleSheet(STYLE.replace('QWidget#CustomerTemplateInterface','QDialog'))
  self.storage.hide();page.setAttribute(Qt.WA_StyledBackground,True);page.setStyleSheet(STYLE)
  for widget in page.findChildren(QWidget):
   widget.style().unpolish(widget);widget.style().polish(widget);widget.update()
  for w in (page.controls['need_video'],page.controls['soft_subtitle'],page.controls['needs_remove_punctuation']):w.hide()
  page.template_mode_combo.currentIndexChanged.connect(self.refresh)
  for key in ('transcribe_language','need_translate','needs_remove_punctuation','need_video','soft_subtitle','need_optimize','need_split'):
   w=page.controls[key]
   (w.toggled if hasattr(w,'toggled') else w.currentIndexChanged).connect(self.refresh)
  self.refresh()
  page.refresh_edit_permission()
  QTimer.singleShot(0,self.apply_theme)
 def apply_theme(self):
  # Rebuild after old widgets have completed reparenting and initial populate.
  self.page.setStyleSheet('');self.page.setStyleSheet(STYLE);self.page.update()
 def reparent(self,w):w.setParent(self.page);w.show()
 def card(self,title=None):
  card=QFrame();card.setObjectName('templateCard');layout=QVBoxLayout(card);layout.setContentsMargins(16,13,16,13);layout.setSpacing(9)
  if title:layout.addWidget(label(title,card,'cardTitle'))
  return card,layout
 def select_mode(self,mode):
  combo=self.page.template_mode_combo;combo.setCurrentIndex(combo.findData(mode));self.refresh()
 def change_punctuation(self):
  if not self.loading:self.page.controls['needs_remove_punctuation'].setChecked(self.punctuation.currentData())
 def change_output(self,*args):
  if self.loading:return
  mode=self.output.currentData();self.page.controls['need_video'].setChecked(mode!='subtitle');self.page.controls['soft_subtitle'].setChecked(mode=='soft')
 def refresh(self,*args):
  if self.loading:return
  self.loading=True
  try:
   p=self.page;c=p.controls;mode=p.template_mode_combo.currentData()
   self.button_group.setExclusive(False)
   for key,b in self.mode_buttons.items():b.setChecked(key==mode)
   self.button_group.setExclusive(True)
   names={'voice':'按原声出字幕','script_correct':'原声定位＋剧本校正','strict':'剧本完全替换','hybrid':'旧版混合改写',None:'常规处理（兼容原模板）'}
   self.mode_description.setText({'voice':'字幕按实际说话生成；剧本仅辅助局部识别核对。','script_correct':'原声定位说话范围，剧本纠正错词；未匹配内容保留待核对。','strict':'独立的完全替换模式，保持原有替换和时间处理逻辑。','hybrid':'当前使用旧版混合改写；可在更多设置中查看。',None:'当前沿用原模板流程；选择上方模式后保存才会切换。'}.get(mode,''))
   remove=c['needs_remove_punctuation'].isChecked();self.punctuation.setCurrentIndex(self.punctuation.findData(remove))
   output='subtitle' if not c['need_video'].isChecked() else 'soft' if c['soft_subtitle'].isChecked() else 'burn';self.output.setCurrentIndex(self.output.findData(output))
   self.target_row.setVisible(c['need_translate'].isChecked())
   self.chips.setText('   ·   '.join([c['transcribe_language'].currentText(),names.get(mode,''),'去句末标点' if remove else '保留标点',self.output.currentText()]))
   record=p.current.get('requirements_import',{});pending=len(record.get('pending',[]));count=len(record.get('rules',[]))
   self.review.setText(f'已识别 {count} 项要求 · 待确认 {pending} 项  ›' if record else '聊天补充当前模板要求 / 添加附件  ›')
   self.review.setEnabled(True)
   steps=['原声识别']
   if mode=='script_correct':steps+=['剧本校正']
   elif mode=='strict':steps+=['完全替换']
   elif mode=='hybrid':steps+=['旧版混合改写']
   elif c['need_optimize'].isChecked():steps+=['文字校正']
   if c['need_translate'].isChecked():steps+=['翻译']
   steps+=['视频合成' if c['need_video'].isChecked() else '字幕导出'];self.flow.setText('  →  '.join(steps))
   self.style_summary.setText('当前客户保存的字体、颜色和描边');self.example.update()
  finally:self.loading=False
 def open_advanced(self):self.advanced_dialog.exec_();self.refresh()
 def open_appearance(self):
  p=self.page;dialog=QDialog(p);dialog.setWindowTitle('字幕外观与实际素材预览');dialog.resize(1120,760);layout=QVBoxLayout(dialog)
  p.template_sections.removeWidget(p.appearance);layout.addWidget(p.appearance);p.appearance.show()
  buttons=QDialogButtonBox(QDialogButtonBox.Close);buttons.rejected.connect(dialog.accept);layout.addWidget(buttons)
  dialog.exec_();layout.removeWidget(p.appearance);p.appearance.setParent(self.storage);p.appearance.hide();dialog.deleteLater();self.refresh()

def install_dashboard(page):page.dashboard=TemplateDashboard(page)
