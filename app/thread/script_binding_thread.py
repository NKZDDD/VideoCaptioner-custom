"""Background binding I/O; signals are the only bridge back to widgets."""
from PyQt5.QtCore import QThread,pyqtSignal
_active=set()
class ScriptBindingWorker(QThread):
    progress=pyqtSignal(str)
    succeeded=pyqtSignal(object)
    failed=pyqtSignal(str)
    def __init__(self,operation):
        super().__init__();self.operation=operation
        _active.add(self)
        self.finished.connect(self._release)
    def _release(self):
        _active.discard(self);self.deleteLater()
    def run(self):
        try:self.succeeded.emit(self.operation(self.progress.emit))
        except Exception as error:self.failed.emit(str(error))
