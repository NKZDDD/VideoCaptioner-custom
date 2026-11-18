"""Present existing controls together without changing configuration semantics."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget,QVBoxLayout,QScrollArea,QGroupBox,QLabel,QPushButton,QSizePolicy,QFormLayout

def simplify_template_layout(page):
 stack=page.template_sections;tabs=page.template_tabs
 groups=[];old=[]
 for i in range(3):
  wrapper=stack.widget(i);old.append(wrapper)
  groups.append(wrapper.widget().findChild(QGroupBox))
 content=QWidget();layout=QVBoxLayout(content);layout.setContentsMargins(0,0,8,0)
 summary=QLabel('选择处理方式 → 填写文字要求 → 确认字幕输出。保存后按此模板固定执行。',content)
 summary.setWordWrap(True);layout.addWidget(summary)
 # Reuse the original widgets and bindings. Every control remains collectable.
 for group,title in zip((groups[0],groups[2],groups[1]),('处理方式','文字要求','字幕与输出')):
  group.setTitle(title);group.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
  group.layout().setFormAlignment(Qt.AlignTop);group.layout().setVerticalSpacing(8);layout.addWidget(group)
 page.prompt.setMinimumHeight(75);page.prompt.setMaximumHeight(100)
 page.glossary.setMinimumHeight(55);page.glossary.setMaximumHeight(80)
 for old_wrapper in old:stack.removeWidget(old_wrapper);old_wrapper.hide()
 from qfluentwidgets import SegmentedWidget
 old_tabs=tabs;tabs=SegmentedWidget(old_tabs.parentWidget());tabs.setFixedHeight(34)
 old_tabs.parentWidget().layout().replaceWidget(old_tabs,tabs)
 old_tabs.hide();old_tabs.deleteLater();page.template_tabs=tabs
 wrapper=QScrollArea();wrapper.setWidgetResizable(True);wrapper.setFrameShape(QScrollArea.NoFrame)
 layout.addStretch();wrapper.setWidget(content);stack.addWidget(wrapper)
 tabs.addItem(routeKey='requirements',text='模板要求',onClick=lambda:stack.setCurrentWidget(wrapper))
 tabs.setCurrentItem('requirements');stack.setCurrentWidget(wrapper)
 page.requirements_page=wrapper;page.advanced_rows=[]
 advanced_box=QGroupBox('高级设置',content);advanced_form=QFormLayout(advanced_box)
 advanced_box.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
 for key in ('transcribe_model','translator_service','split_type','soft_subtitle','video_quality'):
  widget=page.controls[key]
  form=next((g.layout() for g in groups if g.layout().indexOf(widget)>=0),None)
  if form:
   label=form.labelForField(widget)
   form.takeRow(widget)
   if label:advanced_form.addRow(label,widget)
   else:advanced_form.addRow(widget)
   page.advanced_rows.append((widget,label))
 layout.insertWidget(layout.count()-1,advanced_box)
 toggle=QPushButton('展开高级设置（转录服务、断句方式、视频质量等）',content)
 toggle.setCheckable(True);layout.insertWidget(1,toggle);page.advanced_toggle=toggle
 def advanced(checked):
  advanced_box.setVisible(checked)
  toggle.setText('收起高级设置' if checked else '展开高级设置（转录服务、断句方式、视频质量等）')
 toggle.toggled.connect(advanced);advanced(False)
 # Translation-only details are irrelevant when translation is disabled.
 def translation(checked):
  widget=page.controls['target_language'];form=groups[0].layout()
  widget.setVisible(checked);label=form.labelForField(widget)
  if label:label.setVisible(checked)
 page.controls['need_translate'].toggled.connect(translation)
 translation(page.controls['need_translate'].isChecked())
