"""Requirements text/document -> constrained editable review -> new template."""
import copy
from pathlib import Path
from PyQt5.QtCore import Qt,QBuffer,QIODevice
from PyQt5.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
 QPlainTextEdit,QTableWidget,QTableWidgetItem,QHeaderView,QComboBox,QSpinBox,QDoubleSpinBox,
 QDialogButtonBox,QFileDialog,QTabWidget,QCheckBox,QWidget,QToolButton)
from app.view.background_jobs import BackgroundJobs
from app.core.requirements_import import (read_document,extract_requirements,make_draft,
 field_schema,LABELS,combine_requirements,validate_extraction)
from app.core.template_permissions import assert_assistant_base

GROUPS=[('交付限制',('delivery_max_line_chars','delivery_max_cue_chars','delivery_max_lines','delivery_pause_ms')),('字幕来源',('text_source_mode','transcribe_language','need_split','need_optimize','split_type','need_translate','target_language')),
 ('文字要求',('capitalization','needs_remove_punctuation','max_word_count_cjk','max_word_count_english','english_line_words')),
 ('字幕外观',('font_name','font_size_ass','font_color','alignment','subtitle_layout')),
 ('输出方式',('need_video','soft_subtitle'))]
MODES={None:'沿用基础流程','voice':'按原声出字幕','script_correct':'原声＋剧本校正','strict':'剧本完全替换','hybrid':'旧版兼容流程'}

class DropArea(QLabel):
 def __init__(self,owner):
  super().__init__('拖入文档或截图，或者直接在下方说需求\nTXT · Word · 文字版PDF · PNG/JPG/WEBP｜最多6份附件',owner)
  self.owner=owner;self.setAcceptDrops(True);self.setAlignment(Qt.AlignCenter);self.setMinimumHeight(60)
  self.setStyleSheet('border:1px dashed #168779;border-radius:8px;padding:8px;color:#168779;')
 def dragEnterEvent(self,event):
  if event.mimeData().hasUrls() and not self.owner.busy:event.acceptProposedAction()
 def dropEvent(self,event):
  paths=[url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
  self.owner.start_many(paths);event.acceptProposedAction()

class TemplateImportDialog(QDialog):
 def __init__(self,parent,base,extracted=None,bases=None,edit_mode=False):
  super().__init__(parent);self.setWindowTitle('和助手一起创建客户模板');self.resize(900,700)
  self.setStyleSheet('''QDialog{background:#f5f7f9;} QLabel{color:#304552;background:transparent;}
   QLineEdit,QPlainTextEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTableWidget{background:white;color:#304552;border:1px solid #dce5eb;border-radius:5px;padding:5px;}
   QPushButton{background:white;color:#304552;border:1px solid #dce5eb;border-radius:6px;padding:7px 12px;}
   QPushButton:disabled{color:#97a3ab;} QHeaderView::section{background:#edf4f2;color:#304552;padding:6px;border:0;}
   QTabBar::tab{padding:9px 24px;} QTabBar::tab:selected{color:#168779;background:white;}''')
  self.base=copy.deepcopy(base);self.bases=copy.deepcopy(bases or [base]);self.review_mode=bool(extracted) or edit_mode;self.edit_mode=edit_mode
  self.busy=False;self.result=None;self.draft=None;self.document=None;self.documents=[];self.pending_paths=[];self.messages=[];self.editors=[];self.jobs=BackgroundJobs(self)
  outer=QVBoxLayout(self);outer.setContentsMargins(24,20,24,20);outer.setSpacing(12)
  title=QLabel('聊天修改：'+base.get('name','当前模板') if edit_mode else '你说需求，助手生成模板',self);title.setStyleSheet('font-size:23px;font-weight:600;');outer.addWidget(title)
  self.drop_area=DropArea(self);outer.addWidget(self.drop_area)
  row=QHBoxLayout();self.choose=QPushButton('＋ 添加文档 / 图片',self);self.choose.clicked.connect(self.pick);row.addWidget(self.choose)
  self.file_label=QLabel('未添加文档 · 可直接输入需求',self);row.addWidget(self.file_label,1)
  self.remove=QPushButton('清空附件',self);self.remove.clicked.connect(self.remove_document);row.addWidget(self.remove);outer.addLayout(row)
  self.document_preview=QPlainTextEdit(self);self.document_preview.setReadOnly(True);self.document_preview.setMaximumHeight(72);self.document_preview.hide();outer.addWidget(self.document_preview)
  self.conversation=QPlainTextEdit(self);self.conversation.setReadOnly(True);self.conversation.setMaximumHeight(160);self.conversation.hide();outer.addWidget(self.conversation)
  self.instructions=ChatInput(self);self.instructions.setPlaceholderText('例如：按这份文档做一个中文竖版模板，用原声＋剧本校正，每条最多11字。\n可以继续补充，也可以直接粘贴截图（Ctrl+V）。');self.instructions.setMinimumHeight(120);self.instructions.setMaximumHeight(180);outer.addWidget(self.instructions)
  row=QHBoxLayout();row.addWidget(QLabel('未提及的设置沿用',self));self.base_box=QComboBox(self)
  for item in self.bases:self.base_box.addItem(item.get('name','当前配置'),item)
  index=next((i for i,p in enumerate(self.bases) if p.get('id')==base.get('id')),0);self.base_box.setCurrentIndex(index)
  self.base_box.currentIndexChanged.connect(self.change_base);row.addWidget(self.base_box,1)
  self.analyze=QPushButton('发送需求，生成模板',self);self.analyze.setStyleSheet('background:#168779;color:white;padding:9px 20px;');self.analyze.clicked.connect(self.analyze_requirements);row.addWidget(self.analyze);outer.addLayout(row)
  note=QLabel('点击发送后，附件文字、图片及你的说明会交给当前配置的LLM。生成后可继续补充；不会改动已有模板。',self);note.setWordWrap(True);outer.addWidget(note)
  self.name_row=QWidget(self);row=QHBoxLayout(self.name_row);row.setContentsMargins(0,0,0,0);row.addWidget(QLabel('生成的模板名称',self));self.name=QLineEdit(self);row.addWidget(self.name,1);outer.addWidget(self.name_row)
  self.tabs=QTabWidget(self);self.tables={}
  for title,keys in GROUPS:
   table=QTableWidget(0,3,self);table.setHorizontalHeaderLabels(['项目','采用值 · 可修改','依据 / 继承来源'])
   table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents);table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents);table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
   self.tables[title]=table;self.tabs.addTab(table,title)
  self.details=QPushButton('查看 / 调整全部配置',self);self.details.clicked.connect(lambda:self.tabs.setVisible(not self.tabs.isVisible()));outer.addWidget(self.details)
  outer.addWidget(self.tabs,1)
  self.inherited=QLabel('',self);self.inherited.setWordWrap(True);outer.addWidget(self.inherited)
  self.pending_label=QLabel('尚未配置的要求（有未解决事项时只保存停用草稿）',self);outer.addWidget(self.pending_label)
  self.pending=QPlainTextEdit(self);self.pending.setMaximumHeight(65);outer.addWidget(self.pending)
  self.ack=QCheckBox('我已人工核对并处理移除的待确认事项；不将尚未实现的功能当成已支持',self);outer.addWidget(self.ack)
  self.notice=QLabel('先输入需求或添加附件，再点击“发送需求，生成模板”。',self);self.notice.setWordWrap(True);outer.addWidget(self.notice)
  buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel,self);self.confirm=buttons.button(QDialogButtonBox.Ok)
  self.confirm.setText('确认并保存为新模板');self.confirm.setEnabled(False);self.confirm.hide();buttons.button(QDialogButtonBox.Cancel).setText('取消')
  buttons.accepted.connect(self.finish);buttons.rejected.connect(self.reject);outer.addWidget(buttons)
  self.instructions.textChanged.connect(self.invalidate);self.name.textChanged.connect(lambda:self.confirm.setEnabled(bool(self.result) and not self.busy))
  self.review_widgets=(self.name_row,self.details,self.tabs,self.inherited,self.pending_label,self.pending,self.ack)
  for w in self.review_widgets:w.hide()
  if edit_mode:
   self.setWindowTitle('聊天修改已有模板');self.base_box.setEnabled(False)
   note.setText('补充或上传新要求，未修改的配置保留。生成后核对并保存到当前模板；受保护模板不能修改。')
   self.confirm.setText('确认并保存修改')
   self.messages=copy.deepcopy((extracted or {}).get('source',{}).get('chat_messages',[]))
   self.context_text='当前模板已有要求（保留，除非后续说明明确修改）：\n'+'\n'.join(r.get('evidence','') for r in (extracted or {}).get('rules',[]))
   if (extracted or {}).get('pending'):self.context_text+='\n尚待解决：\n'+'\n'.join(extracted['pending'])
   self.instructions.setPlaceholderText('直接说要增加或修改什么，例如：改成英文在上、中文在下。也可以添加新文档或截图。')
  else:self.context_text=''
  if extracted:self.show_result(extracted)

 def invalidate(self):
  self.result=None;self.confirm.setEnabled(False)
  self.notice.setText('补充完成后点击发送，助手会重新生成模板；已有模板保持不变。')
 def change_base(self):
  self.base=copy.deepcopy(self.base_box.currentData());self.invalidate()
 def pick(self):
  paths,_=QFileDialog.getOpenFileNames(self,'添加需求文档或截图','','需求附件 (*.docx *.pdf *.txt *.md *.png *.jpg *.jpeg *.webp)')
  self.start_many(paths)
 def set_busy(self,value):
  self.busy=value
  for widget in (self.choose,self.remove,self.analyze,self.instructions,self.base_box,self.name,self.tabs,self.pending,self.ack):widget.setEnabled(not value and not (widget is self.base_box and self.edit_mode))
  self.confirm.setEnabled(not value and bool(self.result))
 def start_many(self,paths):
  if len(self.documents)+len(self.pending_paths)+len(paths)>6:self.notice.setText('一次最多添加6份附件，请清空后重新选择。');return
  self.pending_paths.extend(paths)
  if not self.busy and self.pending_paths:self.start(self.pending_paths.pop(0))
 def start(self,path):
  if self.busy:self.pending_paths.append(path);return
  if len(self.documents)>=6:self.notice.setText('一次最多添加6份附件。');return
  self.invalidate();self.set_busy(True);self.notice.setText('正在读取附件，仅在本机读取，尚未发送…')
  self.jobs.submit('read',lambda:read_document(path),self.document_ready,self.failed)
 def document_ready(self,document):
  self.documents.append(document);self.rebuild_document();self.set_busy(False)
  if self.pending_paths:self.start(self.pending_paths.pop(0));return
  self.notice.setText('附件已就绪。补充你的要求后，点击发送生成模板。')
 def rebuild_document(self):
  import hashlib
  if not self.documents:self.document=None;self.file_label.setText('未添加附件 · 可以直接输入需求');return
  text='\n\n'.join('【'+d['filename']+'】\n'+d['text'] for d in self.documents if d['text'])
  identity='|'.join(d['sha256'] for d in self.documents)
  self.document=dict(filename='、'.join(d['filename'] for d in self.documents),text=text,sha256=hashlib.sha256(identity.encode()).hexdigest(),
    warnings=[w for d in self.documents for w in d.get('warnings',[])],images=[i for d in self.documents for i in d.get('images',[])])
  self.file_label.setText('已添加 '+str(len(self.documents))+' 份：'+self.document['filename']);self.file_label.setWordWrap(True)
  self.document_preview.setPlainText(text)
 def add_pasted_image(self,image):
  if self.busy:return
  if len(self.documents)>=6:self.notice.setText('一次最多添加6份附件。');return
  from app.core.requirements_images import read_image
  buffer=QBuffer();buffer.open(QIODevice.WriteOnly)
  try:
   if not image.save(buffer,'PNG'):raise ValueError('无法读取剪贴板图片')
   self.document_ready(read_image(bytes(buffer.data()),'粘贴截图'+str(len(self.documents)+1)+'.png'));self.invalidate()
  except Exception as error:self.notice.setText(str(error))
 def remove_document(self):
  if self.busy:return
  self.documents=[];self.document=None;self.pending_paths=[];self.document_preview.clear();self.document_preview.hide();self.file_label.setText('未添加附件 · 可以直接输入需求');self.invalidate()
 def analyze_requirements(self):
  if self.busy:return
  message=self.instructions.toPlainText().strip()
  if message and (not self.messages or self.messages[-1]!=message):self.messages.append(message)
  history='\n\n'.join('【第'+str(i+1)+'次操作者说明】\n'+m for i,m in enumerate(self.messages))
  try:assert_assistant_base(self.base);document=combine_requirements(self.document,self.context_text+'\n'+history)
  except (ValueError,RuntimeError) as error:self.notice.setText(str(error));return
  self.result=None;self.set_busy(True);self.notice.setText('助手正在读取需求并生成可配置规则…图片会先识别文字，已有模板不变')
  self.jobs.submit('extract',lambda:extract_requirements(document),self.show_result,self.failed)
 def failed(self,error):
  self.result=None;self.pending_paths=[];self.set_busy(False);self.notice.setText('未能提炼：'+str(error)+'。当前输入保留，可修改后重试。')
 def base_values(self):
  values={k:v for k,v in self.base.get('settings',{}).items() if k in LABELS}
  values.update(text_source_mode=self.base.get('text_source_mode'),english_line_words=self.base.get('english_line_words',6))
  for line in self.base.get('style_template','').splitlines():
   if line.startswith('Style: Default,') or line.startswith('Style:Default,'):
    fields=line.split(',')
    if len(fields)>18:
     try:
      color=fields[3].replace('&H','').zfill(8);values.update(font_name=fields[1],font_size_ass=float(fields[2]),font_color='#'+color[-2:]+color[-4:-2]+color[-6:-4],alignment=int(fields[18]))
     except (ValueError,IndexError):pass
    break
  from app.core.utils.delivery_policy import read_policy
  values.update(read_policy(self.base.get('style_template','')) or {})
  return values
 def show_result(self,result):
  self.result=copy.deepcopy(result);self.set_busy(False)
  for w in self.review_widgets:w.show()
  self.confirm.show();self.tabs.hide();self.drop_area.hide();self.instructions.setMinimumHeight(60);self.instructions.setMaximumHeight(80)
  self.conversation.show();self.conversation.setPlainText('你：'+('\n你：'.join(self.messages) if self.messages else '根据附件生成模板')+'\n\n助手：已提炼'+str(len(result['rules']))+'项配置，'+str(len(result['pending']))+'项需要补充或暂未支持。可以继续补充说明。')
  from app.view.requirements_import_ui import display_value
  summary='；'.join(LABELS.get(r['field'],r['field'])+'：'+display_value(r['field'],r['value']) for r in result['rules'][:8])
  self.conversation.appendPlainText('\n模板配置：'+summary)
  self.analyze.setText('发送补充，重新生成');self.resize(960,860)
  self.name.setText(result['name']);self.pending.setPlainText('\n'.join(result['pending']));self.ack.setChecked(False)
  self.editors=[];schema=field_schema();values=self.base_values();rules={r['field']:r for r in result['rules']}
  for title,keys in GROUPS:
   table=self.tables[title];selected=[key for key in keys if key in values or key in rules];table.setRowCount(len(selected))
   for row,key in enumerate(selected):
    original=copy.deepcopy(rules.get(key));value=original['value'] if original else values[key];options=schema[key]
    table.setItem(row,0,QTableWidgetItem(LABELS[key]));table.item(row,0).setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable)
    if type(value) is bool or isinstance(options,list):
     box=QComboBox(self)
     for choice in ([True,False] if type(value) is bool else options):box.addItem(MODES.get(choice,str(choice)) if key=='text_source_mode' else '句首大写（英文 I 大写）' if key=='capitalization' else '是' if choice is True else '否' if choice is False else str(choice),choice)
     box.setCurrentIndex(max(0,box.findData(value)))
    elif type(value) in (int,float):
     box=QDoubleSpinBox(self) if type(value) is float else QSpinBox(self)
     low,high={'delivery_max_line_chars':(8,200),'delivery_max_cue_chars':(8,400),'delivery_max_lines':(1,4),'delivery_pause_ms':(100,3000),'english_line_words':(3,12),'alignment':(1,9),'font_size_ass':(1,400),'max_word_count_cjk':(8,100),'max_word_count_english':(3,100)}.get(key,(1,1000));box.setRange(low,high);box.setValue(value)
    else:box=QLineEdit(str(value),self)
    box.setMinimumWidth(220);table.setCellWidget(row,1,box);self.editors.append((key,value,original,box))
    table.setItem(row,2,QTableWidgetItem(original['evidence'] if original else '沿用基础模板：'+self.base.get('name','当前配置')));table.item(row,2).setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable)
   table.resizeRowsToContents()
  self.inherited.setText('基础模板：'+self.base.get('name','当前配置')+'。列表同时展示提炼结果与继承项；未列出的转录服务、视频质量及描边等外观参数沿用基础模板。')
  self.pending_label.setVisible(bool(result['pending']));self.pending.setVisible(bool(result['pending']));self.ack.setVisible(bool(result['pending']))
  self.confirm.setText('保存为待完善草稿（任务中暂不可选）' if result['pending'] else '确认并保存修改' if self.edit_mode else '确认并保存为新模板')
  self.notice.setText('还有待确认要求；保存后仅在客户模板库显示，任务中暂不可选。可继续聊天补充解决。' if result['pending'] else '已生成可保存配置；请核对后保存。');self.confirm.setEnabled(True)
 def reviewed_result(self):
  if not self.result:raise ValueError('请先完成需求提炼')
  name=self.name.text().strip()
  if not name:raise ValueError('请填写新模板名称')
  raw=[];edits=[]
  for key,previous,original,box in self.editors:
   value=box.currentData() if isinstance(box,QComboBox) else box.value() if isinstance(box,(QSpinBox,QDoubleSpinBox)) else box.text().strip()
   if original:raw.append(dict(original,value=value))
   elif value!=previous:raw.append({'field':key,'value':value,'evidence':f'人工设置 {key} ASS：{value}'})
   if value!=previous:edits.append({'field':key,'before':previous,'after':value})
  pending=[line.strip() for line in self.pending.toPlainText().splitlines() if line.strip()]
  if set(self.result['pending'])-set(pending) and not self.ack.isChecked():raise ValueError('移除了待确认事项，请先勾选已人工处理，或保留这些事项以停用草稿保存')
  checked=validate_extraction({'name':name,'rules':raw,'pending':pending},{'filename':self.result['source']['filename'],'sha256':self.result['source']['sha256'],'text':'\n'.join(r['evidence'] for r in raw),'warnings':[]})
  if len(checked['rules'])!=len(raw):raise ValueError('部分修改值不受支持，请核对字段值或字号单位')
  checked['source']['chat_messages']=copy.deepcopy(self.messages)
  checked.update(operator_edits=edits,original_pending=self.result.get('original_pending',self.result['pending']),base_template={'id':self.base.get('id'),'revision':self.base.get('revision'),'name':self.base.get('name')})
  return checked
 def finish(self):
  if self.busy:return
  try:
   assert_assistant_base(self.base);self.result=self.reviewed_result();self.draft=make_draft(self.base,self.result,clean_text=not self.review_mode)
  except Exception as error:self.notice.setText(str(error));return
  if self.edit_mode:
   for key in ('id','revision'):
    if key in self.base:self.draft[key]=self.base[key]
  self.jobs.close();self.accept()
 def reject(self):self.jobs.close();super().reject()


class ChatInput(QPlainTextEdit):
 def __init__(self,owner):super().__init__(owner);self.owner=owner
 def insertFromMimeData(self,source):
  if source.hasImage():
   from PyQt5.QtGui import QImage
   self.owner.add_pasted_image(QImage(source.imageData()));return
  super().insertFromMimeData(source)
