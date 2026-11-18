"""Bounded background I/O with GUI-thread delivery and keyed coalescing."""
import logging
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, Qt, pyqtSlot

_POOL = QThreadPool()
_POOL.setMaxThreadCount(3)


class _Signals(QObject):
    finished = pyqtSignal(str, bool, object)


class _Job(QRunnable):
    def __init__(self, key, function):
        super().__init__()
        self.key, self.function, self.signals = key, function, _Signals()

    def run(self):
        try:
            value = self.function()
        except Exception as error:
            self.signals.finished.emit(self.key, False, str(error))
        else:
            self.signals.finished.emit(self.key, True, value)


class BackgroundJobs(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = _POOL
        self.active, self.pending = {}, {}
        self.closed = False

    def submit(self, key, function, callback, failed=None):
        if self.closed:
            return
        if key in self.active:
            self.pending[key] = (function, callback, failed)
            return
        job = _Job(key, function)
        self.active[key] = (job, callback, failed)
        job.signals.finished.connect(self._finished, Qt.QueuedConnection)
        self.pool.start(job)

    @pyqtSlot(str, bool, object)
    def _finished(self, key, success, value):
        entry = self.active.pop(key, None)
        if self.closed or entry is None:
            return
        try:
            callback = entry[1] if success else entry[2]
            if callback:
                callback(value)
        except Exception as error:
            if success and entry[2]:
                try:
                    entry[2](str(error))
                except Exception:
                    logging.getLogger(__name__).exception('Background result handler failed')
            else:
                logging.getLogger(__name__).exception('Background result handler failed')
        finally:
            pending = self.pending.pop(key, None)
            if pending:
                self.submit(key, *pending)

    def close(self):
        self.closed = True
        self.pending.clear()
