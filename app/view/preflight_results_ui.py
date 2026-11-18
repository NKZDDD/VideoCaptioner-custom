from PyQt5.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QTextBrowser,QPushButton
from app.core.task_preflight import detail
from pathlib import Path

def show_preflight_results(results,parent):
    dialog=QDialog(parent);dialog.setWindowTitle('开始前检查结果');dialog.resize(860,540)
    dialog.setStyleSheet('QDialog {background:#f7f9fa;} QLabel {color:#263440;font-size:14px;} QListWidget,QTextBrowser {background:white;border:1px solid #dbe5e8;border-radius:6px;font-size:13px;padding:8px;} QListWidget::item {padding:9px;} QListWidget::item:selected {background:#e3f2ee;color:#126c62;} QPushButton {padding:7px 18px;}')
    outer=QVBoxLayout(dialog)
    values=list(results.values());passed=sum(bool(result['ok']) for result in values)
    outer.addWidget(QLabel(f'通过 {passed} 集 · 未通过 {len(values)-passed} 集；通过的任务正常入队',dialog))
    row=QHBoxLayout();listing=QListWidget(dialog);listing.setMaximumWidth(280);body=QTextBrowser(dialog);row.addWidget(listing);row.addWidget(body,1);outer.addLayout(row,1)
    for result in values:listing.addItem(('✓ ' if result['ok'] else '需处理 · ')+Path(result['path']).name)
    listing.currentRowChanged.connect(lambda index:body.setPlainText(detail(values[index])) if 0<=index<len(values) else None)
    if values:listing.setCurrentRow(next((i for i,r in enumerate(values) if not r['ok']),0))
    else:body.setPlainText('点击开始处理时会自动检查。当前尚无检查记录。')
    close=QPushButton('关闭',dialog);close.clicked.connect(dialog.accept);outer.addWidget(close)
    dialog.exec_()
