"""Nonblocking, per-editor history cache. No persistent configuration writes."""
import time
from PyQt5.QtCore import QObject,QRunnable,QThreadPool,pyqtSignal

class HistoryJob(QRunnable):
    def __init__(self,loader,read):super().__init__();self.loader=loader;self.read=read
    def run(self):
        try:result=(list(self.read()),'')
        except Exception as exc:result=(None,str(exc))
        try:self.loader.result.emit(*result)
        except RuntimeError:pass  # Editor was destroyed while an IO read finished.

class HistoryLoader(QObject):
    result=pyqtSignal(object,str)
    ready=pyqtSignal(object)
    failed=pyqtSignal(str)
    def __init__(self,parent,read):
        super().__init__(parent);self.read=read;self.cache=None;self.at=0;self.busy=False;self.closed=False;self.rerun=False
        self.result.connect(self.complete)
    def refresh(self,force=False):
        if self.closed:return
        if self.busy:self.rerun=self.rerun or force;return
        if not force and self.cache is not None and time.monotonic()-self.at<10:
            self.ready.emit(self.cache);return
        self.busy=True;QThreadPool.globalInstance().start(HistoryJob(self,self.read))
    def complete(self,items,error):
        self.busy=False
        if self.closed:return
        if error:self.failed.emit(error)
        else:self.cache=items;self.at=time.monotonic();self.ready.emit(items)
        if self.rerun:self.rerun=False;self.refresh(True)
    def close(self):self.closed=True;self.rerun=False
