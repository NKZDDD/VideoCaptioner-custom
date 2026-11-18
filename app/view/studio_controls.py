"""Shared polished controls, with QComboBox-compatible item data semantics."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QSpinBox,QPushButton
from qfluentwidgets import ComboBox


class StyledCombo(ComboBox):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.before_open=None
        self.setMinimumHeight(38);self.setMaxVisibleItems(9)
        self.setStyleSheet('QPushButton {background:#ffffff;color:#304651;border:1px solid #dce5e9;border-radius:8px;text-align:left;padding:8px 34px 8px 12px;font-size:13px;} QPushButton:hover {background:#f9fcfb;border-color:#8dc6b8;} QPushButton:focus {border:1px solid #168779;} QPushButton:disabled {background:#f1f4f6;color:#9aa7b1;}')

    def addItem(self,text,userData=None,**kwargs):
        # Native QComboBox's second argument is userData. Fluent's is icon.
        # Retain native call sites so template IDs never become icon paths.
        super().addItem(text,icon=kwargs.get('icon'),userData=userData)

    def setEditable(self,editable):
        if editable:raise ValueError('此选项只允许从已保存列表中选择')

    def _showComboMenu(self):
        if self.before_open:self.before_open()
        super()._showComboMenu()

    def _createComboMenu(self):
        menu=super()._createComboMenu()
        menu.setItemHeight(38)
        menu.view.setMinimumWidth(max(240,self.width()))
        menu.view.setStyleSheet('QListWidget {background:#ffffff;border:0;padding:5px;color:#304651;font-size:13px;} QListWidget::item {border:0;border-radius:6px;padding-left:12px;} QListWidget::item:hover {background:#eef7f3;} QListWidget::item:selected {background:#e5f3ed;color:#137967;}')
        return menu


class TaskConcurrency(QSpinBox):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QSpinBox.NoButtons)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(118,38)
        self.setStyleSheet('QSpinBox {background:#ffffff;color:#27463f;border:1px solid #dce6e9;border-radius:8px;padding:0 34px;font-size:14px;font-weight:500;} QSpinBox:focus {border-color:#168779;} QSpinBox:disabled {color:#9aa7b1;background:#f3f5f6;}')
        self.minus=QPushButton('−',self);self.plus=QPushButton('+',self)
        for b in (self.minus,self.plus):
            b.setFixedSize(26,26)
            b.setStyleSheet('QPushButton {background:#edf6f2;color:#148171;border:0;border-radius:6px;font-size:18px;padding:0;} QPushButton:hover {background:#dbefe6;} QPushButton:disabled {background:#f3f5f5;color:#b8c3c8;}')
        self.minus.setToolTip('减少同时处理数');self.plus.setToolTip('增加同时处理数')
        self.minus.clicked.connect(self.stepDown);self.plus.clicked.connect(self.stepUp)
        self.valueChanged.connect(self.sync_buttons);self.sync_buttons()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'minus'):
            self.minus.move(6,(self.height()-26)//2)
            self.plus.move(self.width()-32,(self.height()-26)//2)

    def sync_buttons(self,*args):
        self.minus.setEnabled(self.value()>self.minimum())
        self.plus.setEnabled(self.value()<self.maximum())

    def wheelEvent(self,event):
        if self.hasFocus():super().wheelEvent(event)
        else:event.ignore()
