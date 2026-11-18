"""Document import UI; model results are drafts, never silently published."""
import copy
import json
from pathlib import Path
from PyQt5.QtWidgets import QFileDialog,QDialog,QVBoxLayout,QPlainTextEdit,QDialogButtonBox,QLabel,QMessageBox,QCheckBox
from qfluentwidgets import InfoBar
from app.view.background_jobs import BackgroundJobs
from app.core.requirements_import import read_document,extract_requirements,make_draft,LABELS


def describe(extracted,base=None):
    lines=['文档：'+extracted['source']['filename'],'','已识别的要求（请对照原文核对）：']
    for rule in extracted['rules']:
        value=display_value(rule['field'],rule['value'])
        lines.extend([LABELS.get(rule['field'],rule['field'])+'：'+value,'原文：'+rule['evidence'],''])
    lines+=['待确认 / 暂未支持：']+[str(p) for p in extracted['pending']]
    if not extracted['pending']:lines.append('未检测到待确认项；模型可能遗漏，仍请人工核对原文。')
    if base:
        lines+=['','文档未提及的项目继承以下基础配置；不会自动推断：',
            '基础模板：'+base.get('name','当前设置'),
            '\n'.join(LABELS[k]+'：'+display_value(k,v) for k,v in base.get('settings',{}).items() if k in LABELS),
            '英文每行词数：'+str(base.get('english_line_words',6)),
            '台词依据：'+str(base.get('text_source_mode'))]
    return '\n'.join(lines)


def display_value(key,value):
    if key=='text_source_mode':return {None:'常规流程','voice':'原声为主','strict':'剧本完全替换','hybrid':'旧版混合改写','script_correct':'原声定位＋剧本校正'}.get(value,str(value))
    if type(value) is bool:return '是' if value else '否'
    if value is None:return '未指定'
    return str(value)


def review_dialog(page,title,text,button_text='生成新模板草稿'):
    dialog=QDialog(page);dialog.setWindowTitle(title);dialog.resize(820,650)
    layout=QVBoxLayout(dialog)
    note=QLabel('只生成草稿，不会修改现有客户模板。未支持要求不能视为已实现。',dialog);note.setWordWrap(True);layout.addWidget(note)
    view=QPlainTextEdit(dialog);view.setReadOnly(True);view.setPlainText(text);layout.addWidget(view)
    buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel,dialog)
    buttons.button(QDialogButtonBox.Ok).setText(button_text)
    buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
    return dialog.exec_()==QDialog.Accepted


class DocumentImportController:
    def __init__(self,page,button):
        self.page=page;self.button=button;self.jobs=BackgroundJobs(page)
        self.button.setToolTip('直接填写需求或添加文档，调用已配置的LLM提炼；确认后保存为新模板，不覆盖现有模板。')

    def choose(self):
        self.open_dialog()

    def import_file(self,path):
        self.open_dialog(path)

    def open_dialog(self,path=None):
        from app.view.template_import_dialog import TemplateImportDialog
        from app.core.customer_templates import list_profiles
        from app.core.template_permissions import is_protected
        try:
            current=copy.deepcopy(self.page.collect())
            bases=[p for p in list_profiles(include_disabled=True) if not is_protected(p)]
            if not bases:raise ValueError('请先建立一份普通基础模板；阿拉伯模板不能作为需求创建的基础')
            base=current if not is_protected(current) else copy.deepcopy(bases[0])
            if not any(p.get('id')==base.get('id') for p in bases):bases.insert(0,base)
        except Exception as error:InfoBar.error('基础模板不可用',str(error),parent=self.page);return
        dialog=TemplateImportDialog(self.page,base,bases=bases)
        if path:
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0,lambda:dialog.start(path))
        if dialog.exec_()!=QDialog.Accepted:return
        draft=copy.deepcopy(dialog.draft)
        draft.pop('id',None);draft.pop('revision',None)
        self.unsaved_assistant_draft=copy.deepcopy(draft)
        self.page.setEnabled(False)
        self.button.setEnabled(False);self.page.status.setText('正在保存已确认的新模板…')
        def save_new():
            from app.core.customer_templates import save_profile
            return save_profile(draft,0)
        def saved(profile):
            self.page.setEnabled(True)
            self.button.setEnabled(True)
            self.unsaved_assistant_draft=None
            # Do not overwrite a different edit opened while the shared write ran.
            try:
                if self.page.collect()==current:self.page.populate(profile)
                self.page.reload()
                from app.common.signal_bus import signalBus
                signalBus.customer_templates_changed.emit()
            except Exception:
                self.page.status.setText('新模板已保存：'+profile['name']+'；页面刷新失败，请稍后刷新模板库。');return
            self.page.status.setText('新模板已保存：'+profile['name']+'。'+('有待确认项，保持停用。' if not profile['enabled'] else '制作时可直接选择；建议先用一集核对效果。'))
            (InfoBar.success if profile['enabled'] else InfoBar.warning)('模板可用于任务' if profile['enabled'] else '已保存为待完善草稿',profile['name']+('；制作时可以选择' if profile['enabled'] else '；任务中暂不显示，请聊天补充待确认要求'),parent=self.page)
        self.jobs.submit('save-new-template',save_new,saved,self.save_failed)

    def save_failed(self,error):
        self.page.setEnabled(True)
        self.button.setEnabled(True)
        if getattr(self,'unsaved_assistant_draft',None):
            self.page.populate(self.unsaved_assistant_draft)
        self.page.status.setText('新模板未能保存，已保留为编辑草稿，可稍后点击保存模板；原模板未改动：'+str(error))
        InfoBar.error('保存失败',str(error),parent=self.page)

    def failed(self,error):
        self.button.setEnabled(True);self.button.setText('导入客户文档（LLM）')
        self.page.status.setText('文档导入失败，原模板未改动：'+str(error))
        InfoBar.error('文档导入失败',str(error),duration=8000,parent=self.page)

    def ready(self,base,result):
        self.button.setEnabled(True);self.button.setText('导入客户文档（LLM）')
        if self.page.collect()!=base:
            if QMessageBox.question(self.page,'保留当前修改',
                '解析期间当前模板已切换或修改。继续会将编辑区切换为新文档草稿，未保存修改将被替换。是否继续？',
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        if not review_dialog(self.page,'核对文档提取结果',describe(result,base)):return
        draft=make_draft(base,result)
        self.page.populate(draft)
        self.page.status.setText('文档草稿已生成，尚未保存。'+('有待确认要求，模板已停用；请点“核对导入要求”处理。' if result['pending'] else '核对外观和处理规则后保存，再试片。'))

    def edit_current(self):
        from app.view.template_import_dialog import TemplateImportDialog
        from app.core.template_permissions import assert_template_write,assert_assistant_base
        base=copy.deepcopy(self.page.collect())
        try:
            assert_template_write(base);assert_assistant_base(base)
            if not base.get('id'):raise ValueError('请先保存或选择要修改的模板')
        except (ValueError,RuntimeError) as error:
            InfoBar.warning('无法修改',str(error),parent=self.page);return
        dialog=TemplateImportDialog(self.page,base,copy.deepcopy(base.get('requirements_import')),edit_mode=True)
        if dialog.exec_()!=QDialog.Accepted:return
        draft=copy.deepcopy(dialog.draft)
        self.page.setEnabled(False)
        def save_edit():
            from app.core.customer_templates import save_profile
            return save_profile(draft,base.get('revision',0))
        def saved(profile):
            self.page.setEnabled(True);self.page.populate(profile);self.page.reload()
            from app.common.signal_bus import signalBus
            signalBus.customer_templates_changed.emit()
            message=('已保存修改；新任务可选择使用。' if profile['enabled'] else '已保存为待完善草稿；任务中暂不显示，请继续补充待确认要求。')
            self.page.status.setText(profile['name']+'：'+message)
            (InfoBar.success if profile['enabled'] else InfoBar.warning)('模板修改已保存',message,parent=self.page)
        def failed(error):
            self.page.setEnabled(True);self.page.populate(draft)
            self.page.status.setText('修改未保存，已保留草稿：'+str(error))
            InfoBar.error('保存失败',str(error),parent=self.page)
        self.jobs.submit('save-chat-template-edit',save_edit,saved,failed)

    def review_current(self):
        self.edit_current()
