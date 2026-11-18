"""Global window sizing only; no task, customer, style or service configuration."""
from PyQt5.QtCore import QObject,QEvent,Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QShortcut


class WindowWorkspace(QObject):
    def __init__(self,window):
        super().__init__(window);self.window=window;self.fullscreen_state=None
        self.f11=QShortcut(QKeySequence('F11'),window);self.f11.activated.connect(self.toggle_fullscreen)
        self.escape=QShortcut(QKeySequence(Qt.Key_Escape),window);self.escape.activated.connect(self.exit_fullscreen);self.escape.setEnabled(False)
        window.installEventFilter(self);self.sync()

    def toggle_maximize(self):
        if self.fullscreen_state is not None:self.exit_fullscreen()
        if self.window.isMaximized():self.window.showNormal()
        else:self.window.showMaximized()
        self.sync()

    def toggle_fullscreen(self):
        if self.fullscreen_state is not None:self.exit_fullscreen();return
        # The subtitle editor has its own focus view. Do not take ownership of it.
        if self.window.isFullScreen():return
        self.fullscreen_state=(self.window.windowState(),self.window.saveGeometry())
        self.escape.setEnabled(True);self.window.showFullScreen();self.sync()

    def exit_fullscreen(self):
        if self.fullscreen_state is None:return
        state,geometry=self.fullscreen_state;self.fullscreen_state=None;self.escape.setEnabled(False)
        self.window.setWindowState(state);self.window.restoreGeometry(geometry);self.sync()

    def sync(self):
        pass  # Native title-bar controls remain authoritative.

    def eventFilter(self,obj,event):
        if obj is self.window and event.type()==QEvent.WindowStateChange:self.sync()
        return super().eventFilter(obj,event)
