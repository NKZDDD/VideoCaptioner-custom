"""Editor review entry; background report IO and explicit, evidence-only navigation."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QPushButton
from qfluentwidgets import PushButton
from app.view.background_jobs import BackgroundJobs
from app.core.quality_review import collect_review,summary,matching_row

class QualityReviewController:
    def __init__(self,studio,header):
        self.studio=studio;self.result={'count':0,'state':'uninspected','items':[],'errors':[]}
        self.io=BackgroundJobs(studio.editor)
        self.button=PushButton('核对字幕 · 未检查',studio.editor)
        self.button.setMinimumWidth(140)
        self.button.setToolTip('查看时间定位、剧本匹配等已有报告；不重新识别、不调用模型')
        self.button.clicked.connect(self.open);header.addWidget(self.button)
        self.manifest=None
    def load(self):
        manifest=self.studio.editor.batch_project_manifest
        self.manifest=manifest;self.open_when_ready=False;self.result={'count':0,'state':'uninspected','items':[],'errors':[]}
        if not manifest:self.button.setText('核对字幕 · 未检查');return
        self.button.setText('核对字幕 · 读取中');self.button.setEnabled(False)
        self.io.submit('quality',lambda:collect_review(manifest),lambda result:self.ready(manifest,result),lambda error:self.ready(manifest,{'count':0,'state':'unavailable','items':[],'errors':['报告暂不可读，可重新打开重试']}))
    def ready(self,manifest,result):
        if manifest!=self.studio.editor.batch_project_manifest:return
        self.result=result;self.button.setEnabled(True)
        self.button.setText('核对字幕 · '+(str(result['count'])+' 项' if result['count'] else '无待核对项' if result['state']=='no_flags' else '未检查' if result['state']=='uninspected' else '读取失败'))
        self.button.setToolTip(summary(result))
        if getattr(self,'open_when_ready',False):
            self.open_when_ready=False;self.open()
    def open(self):
        dialog=QDialog(self.studio.editor);dialog.setWindowTitle('核对字幕');dialog.resize(760,440)
        dialog.setStyleSheet('QDialog {background:#f7f9fa;} QLabel {color:#263440;font-size:13px;} QListWidget {background:white;border:1px solid #dbe5e8;border-radius:6px;font-size:13px;padding:6px;} QListWidget::item {padding:10px;border-bottom:1px solid #edf1f3;} QListWidget::item:selected {background:#e3f2ee;color:#126c62;} QPushButton {padding:7px 14px;}')
        layout=QVBoxLayout(dialog);layout.setSpacing(12);note=QLabel(summary(self.result),dialog);note.setWordWrap(True);layout.addWidget(note)
        listing=QListWidget(dialog);layout.addWidget(listing,1)
        for item in self.result['items']:
            a=item['start_ms'];position='未定位' if a is None else f'{a//60000:02d}:{a//1000%60:02d}.{a%1000:03d}'
            listing.addItem(position+'  '+item['kind']+'\n'+item['text']+'\n'+item['reason'])
        hint=QLabel('定位后可修改文字、拖动字幕两端调整时间；保存并重新合成。保存不会自动清除原报告的待核对标记。',dialog)
        if not self.result['items']:hint.setText('\n'.join(self.result['errors']) or '当前没有可显示的核对项。未检查不代表质量通过。')
        hint.setWordWrap(True);layout.addWidget(hint)
        row=QHBoxLayout();jump=QPushButton('定位并编辑',dialog);close=QPushButton('关闭',dialog);row.addStretch();row.addWidget(jump);row.addWidget(close);layout.addLayout(row)
        jump.setEnabled(False)
        listing.currentRowChanged.connect(lambda index:jump.setEnabled(0<=index<len(self.result['items']) and self.result['items'][index]['start_ms'] is not None))
        def locate():
            index=listing.currentRow()
            if not 0<=index<len(self.result['items']):return
            item=self.result['items'][index]
            if item['start_ms'] is None:return
            items=list(self.studio.editor.model._data.values());row=matching_row(items,item)
            if row is not None:
                table=self.studio.editor.subtitle_table;table.selectRow(row);table.scrollTo(table.model().index(row,0))
            self.studio.seek(item['start_ms']);self.studio.preview_note.setText(item['reason']);dialog.accept()
        jump.clicked.connect(locate);listing.itemDoubleClicked.connect(lambda _:locate());close.clicked.connect(dialog.reject)
        if listing.count():listing.setCurrentRow(0)
        dialog.exec_()
