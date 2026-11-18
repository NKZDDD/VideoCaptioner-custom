# -*- coding: utf-8 -*-
import os
import subprocess
import sys
import tempfile
import uuid
import json
import hashlib
from app.core.concurrency import guarded_editor
import copy
import re
from difflib import SequenceMatcher
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, QAbstractTableModel, pyqtSignal, QEvent
from PyQt5.QtGui import QColor, QDragEnterEvent, QDropEvent
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QPlainTextEdit,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import Action, BodyLabel, CommandBar
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    TableView,
    TextEdit,
    TransparentDropDownPushButton,
)

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.components.SubtitleSettingDialog import SubtitleSettingDialog
from app.config import SUBTITLE_STYLE_PATH
from app.core.bk_asr.asr_data import ASRData
from app.core.entities import (
    OutputSubtitleFormatEnum,
    SubtitleTask,
    SupportedSubtitleFormats,
    TargetLanguageEnum,
)
from app.core.task_factory import TaskFactory
from app.core.batch_project_store import (
    load_batch_project as read_batch_project,
    update_batch_project,
)
from app.core.utils.get_subtitle_style import get_subtitle_style
from app.thread.subtitle_thread import SubtitleThread


class CaptionSplitDelegate(QStyledItemDelegate):
    splitRequested = pyqtSignal(int, int, str, int)

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setProperty("captionRow", index.row())
        editor.setProperty("captionColumn", index.column())
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(str(index.data(Qt.EditRole) or ""))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.EditRole)

    def eventFilter(self, editor, event):
        if isinstance(editor, QPlainTextEdit) and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    editor.insertPlainText("\n")
                    return True
                text = editor.toPlainText()
                # QTextCursor positions count UTF-16 units; convert for Python slicing.
                position = len(text.encode("utf-16-le")[:editor.textCursor().position() * 2].decode("utf-16-le"))
                if not text[:position].strip() or not text[position:].strip():
                    return True
                row = editor.property("captionRow")
                column = editor.property("captionColumn")
                self.closeEditor.emit(editor, QStyledItemDelegate.RevertModelCache)
                self.splitRequested.emit(row, column, text, position)
                return True
        return super().eventFilter(editor, event)


class SubtitleTableModel(QAbstractTableModel):
    timeEditRejected = pyqtSignal(str)
    timeEditAdjusted = pyqtSignal(str)
    timelineChanged = pyqtSignal()

    def __init__(self, data=""):
        super().__init__()
        self._data = {}
        self.split_history = []
        self.word_anchors = []
        if isinstance(data, str):
            self.load_data(data)
        else:
            self._data = data

    @staticmethod
    def format_time(milliseconds):
        """把毫秒显示为不受 QTime 24 小时回绕影响的时间文本。"""
        total_ms = max(0, int(milliseconds))
        hours, remainder = divmod(total_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"

    @staticmethod
    def parse_time(value):
        """支持 HH:MM:SS.mmm、MM:SS.mmm 或秒数，返回毫秒。"""
        text = str(value).strip().replace(",", ".")
        if not text:
            raise ValueError("时间不能为空")
        parts = text.split(":")
        if len(parts) > 3 or any(not part.strip() for part in parts):
            raise ValueError("时间格式应为 HH:MM:SS.毫秒")
        try:
            if len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
            elif len(parts) == 2:
                hours = 0
                minutes = int(parts[0])
                seconds = float(parts[1])
            else:
                hours = 0
                minutes = 0
                seconds = float(parts[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("时间格式应为 HH:MM:SS.毫秒") from exc
        if (
            hours < 0
            or minutes < 0
            or minutes >= 60
            or seconds < 0
            or (len(parts) > 1 and seconds >= 60)
        ):
            raise ValueError("时间数值超出范围")
        return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))

    def load_data(self, data: str):
        """加载字幕数据"""
        try:
            self._data = json.loads(data)
            self.layoutChanged.emit()
        except json.JSONDecodeError:
            pass

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not self._data:
            return None

        row = index.row()
        col = index.column()
        segment = self._data.get(str(row + 1))

        if not segment:
            return None

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if col == 0:
                return self.format_time(segment["start_time"])
            elif col == 1:
                return self.format_time(segment["end_time"])
            elif col == 2:
                return segment["original_subtitle"]
            elif col == 3:
                return segment["translated_subtitle"]
        elif role == Qt.TextAlignmentRole:
            if col in [0, 1]:
                return Qt.AlignCenter
        elif role == Qt.ToolTipRole and col in [0, 1]:
            return self.tr("双击修改时间，格式：00:00:01.250")
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or not self._data:
            return False

        if role == Qt.EditRole:
            row = index.row()
            col = index.column()
            segment = self._data.get(str(row + 1))

            if not segment:
                return False

            if col in [0, 1]:
                try:
                    new_time = self.parse_time(value)
                except ValueError as exc:
                    self.timeEditRejected.emit(str(exc))
                    return False

                data_list = list(self._data.values())
                min_duration = 10
                changed_first = row
                changed_last = row
                if col == 0:
                    if new_time > int(segment["end_time"]) - min_duration:
                        self.timeEditRejected.emit("开始时间必须早于结束时间")
                        return False
                    if row > 0:
                        previous = data_list[row - 1]
                        if int(previous["end_time"]) > new_time:
                            if new_time <= int(previous["start_time"]) + min_duration:
                                self.timeEditRejected.emit("该时间会让上一条字幕没有有效时长")
                                return False
                            previous["end_time"] = new_time
                            changed_first = row - 1
                            self.timeEditAdjusted.emit("已同步缩短上一条字幕，避免时间重叠")
                    segment["start_time"] = new_time
                else:
                    if new_time < int(segment["start_time"]) + min_duration:
                        self.timeEditRejected.emit("结束时间必须晚于开始时间")
                        return False
                    if row + 1 < len(data_list):
                        following = data_list[row + 1]
                        if int(following["start_time"]) < new_time:
                            if new_time >= int(following["end_time"]) - min_duration:
                                self.timeEditRejected.emit("该时间会让下一条字幕没有有效时长")
                                return False
                            following["start_time"] = new_time
                            changed_last = row + 1
                            self.timeEditAdjusted.emit("已同步推迟下一条字幕，避免时间重叠")
                    segment["end_time"] = new_time

                self.dataChanged.emit(
                    self.index(changed_first, 0),
                    self.index(changed_last, 1),
                    [Qt.DisplayRole, Qt.EditRole],
                )
                self.split_history.clear()
                self.timelineChanged.emit()
                return True
            elif col == 2:
                segment["original_subtitle"] = value
            elif col == 3:
                segment["translated_subtitle"] = value
            else:
                return False

            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
            self.split_history.clear()
            return True
        return False

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return [
                    self.tr("开始时间"),
                    self.tr("结束时间"),
                    self.tr("字幕内容"),
                    (
                        self.tr("翻译字幕")
                        if cfg.need_translate.value
                        else self.tr("优化字幕")
                    ),
                ][section]
            elif orientation == Qt.Vertical:
                return str(section + 1)  # 显示行号
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignCenter  # 居中对齐
        return None

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return 4

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        if index.column() in [0, 1, 2, 3]:
            return Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def shift_rows(self, rows, requested_delta_ms):
        """整体移动指定字幕，必要时限制偏移量以避免负时间和邻行重叠。"""
        data_list = list(self._data.values())
        selected = sorted({int(row) for row in rows if 0 <= int(row) < len(data_list)})
        if not selected or not requested_delta_ms:
            return 0

        selected_set = set(selected)
        minimum_delta = max(-int(data_list[row]["start_time"]) for row in selected)
        maximum_delta = 10**12
        for row in selected:
            current = data_list[row]
            if row > 0 and row - 1 not in selected_set:
                previous = data_list[row - 1]
                minimum_delta = max(
                    minimum_delta,
                    int(previous["end_time"]) - int(current["start_time"]),
                )
            if row + 1 < len(data_list) and row + 1 not in selected_set:
                following = data_list[row + 1]
                maximum_delta = min(
                    maximum_delta,
                    int(following["start_time"]) - int(current["end_time"]),
                )

        if minimum_delta > maximum_delta:
            self.timeEditRejected.emit("当前字幕已有时间重叠，请先手动修正边界")
            return 0

        applied_delta = max(min(int(requested_delta_ms), maximum_delta), minimum_delta)
        if applied_delta == 0:
            self.timeEditRejected.emit("已到达可调整边界，不能继续移动")
            return 0

        for row in selected:
            data_list[row]["start_time"] = int(data_list[row]["start_time"]) + applied_delta
            data_list[row]["end_time"] = int(data_list[row]["end_time"]) + applied_delta
        self.split_history.clear()

        self.dataChanged.emit(
            self.index(selected[0], 0),
            self.index(selected[-1], 1),
            [Qt.DisplayRole, Qt.EditRole],
        )
        self.timelineChanged.emit()
        return applied_delta

    def update_data(self, new_data):
        """更新字幕数据"""
        self.split_history.clear()
        updated_rows = set()

        # 更新内部数据
        for key, value in new_data.items():
            if key in self._data:
                if "||" in value:
                    original_subtitle, translated_subtitle = value.split("||", 1)
                    self._data[key]["original_subtitle"] = original_subtitle
                    self._data[key]["translated_subtitle"] = translated_subtitle
                else:
                    self._data[key]["translated_subtitle"] = value
                row = list(self._data.keys()).index(key)
                updated_rows.add(row)

        # 如果有更新，发出dataChanged信号
        if updated_rows:
            min_row = min(updated_rows)
            max_row = max(updated_rows)
            top_left = self.index(min_row, 2)
            bottom_right = self.index(max_row, 3)
            self.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole, Qt.EditRole])

    def update_all(self, data: dict):
        """更新所有数据"""
        self.split_history.clear()
        self._data = data
        self.layoutChanged.emit()

    @staticmethod
    def spoken_text(text):
        return "".join(re.findall(r"[\w\u3400-\u9fff]", text, re.UNICODE))

    def split_caption(self, row, column, text, position):
        if column not in (2, 3) or not 0 <= row < self.rowCount():
            return False, ""
        left, right = text[:position].strip(), text[position:].strip()
        if not left or not right:
            return False, "请在字幕中间拆分"
        entries = list(self._data.values())
        original = entries[row]
        start, end = int(original["start_time"]), int(original["end_time"])
        if end - start < 40:
            return False, "字幕时长太短，不能拆分"
        full = self.spoken_text(text)
        offset = len(self.spoken_text(text[:position]))
        ratio = offset / max(1, len(full))
        boundary = start + round((end - start) * ratio)
        source = "按文字比例估算，可手动微调"
        if column == 2 and full:
            anchors = [a for a in self.word_anchors if start <= a.start_time < end]
            anchor_text = ""
            times = []
            for anchor in anchors:
                chars = self.spoken_text(anchor.text)
                for i, char in enumerate(chars):
                    anchor_text += char
                    times.append(round(anchor.start_time + (anchor.end_time - anchor.start_time) * i / len(chars)))
            matcher = SequenceMatcher(None, full, anchor_text, autojunk=False)
            if matcher.ratio() >= 0.8:
                for block in matcher.get_matching_blocks():
                    if block.a <= offset < block.a + block.size:
                        boundary = times[block.b + offset - block.a]
                        source = "已匹配原转录时间点"
                        break
        boundary = max(start + 20, min(end - 20, boundary))
        first, second = copy.deepcopy(original), copy.deepcopy(original)
        field = "original_subtitle" if column == 2 else "translated_subtitle"
        other = "translated_subtitle" if column == 2 else "original_subtitle"
        first[field], second[field] = left, right
        other_text = str(original.get(other, ""))
        other_offset = round(len(other_text) * ratio)
        if other_text:
            first[other], second[other] = other_text[:other_offset].strip(), other_text[other_offset:].strip()
            source += "；另一列按比例拆分，请复核"
        first["end_time"], second["start_time"] = boundary, boundary
        self.split_history.append(copy.deepcopy(self._data))
        self.split_history = self.split_history[-30:]
        entries[row:row + 1] = [first, second]
        self.beginResetModel()
        self._data = {str(i + 1): value for i, value in enumerate(entries)}
        self.endResetModel()
        self.timelineChanged.emit()
        return True, source

    def undo_caption_split(self):
        if not self.split_history:
            return False
        self.beginResetModel()
        self._data = self.split_history.pop()
        self.endResetModel()
        self.timelineChanged.emit()
        return True


class SubtitleInterface(QWidget):
    finished = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.task = None
        self.subtitle_path = None
        self.batch_project_manifest = None
        self.batch_project_video = None
        self.custom_prompt_text = cfg.custom_prompt_text.value
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._init_ui()
        self._setup_signals()
        self._update_prompt_button_style()
        self.set_values()
        from app.view.studio_editor import StudioEditorController
        self.studio = StudioEditorController(self)

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setObjectName("main_layout")
        self.main_layout.setSpacing(20)

        self._setup_top_layout()
        self._setup_subtitle_table()
        self._setup_bottom_layout()

    def set_values(self):
        self.layout_button.setText(cfg.subtitle_layout.value)
        self.translate_button.setChecked(cfg.need_translate.value)
        self.optimize_button.setChecked(cfg.need_optimize.value)
        self.target_language_button.setText(cfg.target_language.value.value)
        self.target_language_button.setEnabled(cfg.need_translate.value)

    def _setup_top_layout(self):
        # 创建水平布局
        top_layout = QHBoxLayout()

        # 创建命令栏
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon
        )  # 设置图标和文字并排显示
        top_layout.addWidget(self.command_bar, 1)  # 设置stretch为1，使其尽可能占用空间

        # 创建保存按钮的下拉菜单
        save_menu = RoundMenu(parent=self)
        save_menu.view.setMaxVisibleItems(8)  # 设置菜单最大高度
        for format in OutputSubtitleFormatEnum:
            action = Action(text=format.value)
            action.triggered.connect(
                lambda checked, f=format.value: self.on_save_format_clicked(f)
            )
            save_menu.addAction(action)

        # 添加保存按钮(带下拉菜单)
        save_button = TransparentDropDownPushButton(self.tr("保存"), self, FIF.SAVE)
        save_button.setMenu(save_menu)
        save_button.setFixedHeight(34)
        self.command_bar.addWidget(save_button)

        # 批量留存工程可直接覆盖保存，不必每次另存为。
        self.save_current_action = Action(
            FIF.SAVE,
            self.tr("保存本集"),
            triggered=self.request_save_current,
        )
        self.command_bar.addAction(self.save_current_action)

        self.rerender_action = Action(
            FIF.VIDEO,
            self.tr("保存并合成当前视频"),
            triggered=self.prepare_batch_project_rerender,
        )
        self.rerender_action.setEnabled(False)
        self.command_bar.addAction(self.rerender_action)

        # 时间轴编辑：既支持表格双击精调，也支持选中字幕或整集批量偏移。
        timeline_menu = RoundMenu(parent=self)
        timeline_menu.addAction(Action(FIF.SYNC, self.tr("撤销字幕拆分"), triggered=self.undo_caption_split))
        timeline_menu.addAction(
            Action(
                FIF.UP,
                self.tr("所选提前 100ms"),
                triggered=lambda: self.shift_selected_timeline(-100),
            )
        )
        timeline_menu.addAction(
            Action(
                FIF.DOWN,
                self.tr("所选延后 100ms"),
                triggered=lambda: self.shift_selected_timeline(100),
            )
        )
        timeline_menu.addAction(
            Action(
                FIF.EDIT,
                self.tr("所选自定义偏移"),
                triggered=self.shift_selected_timeline_custom,
            )
        )
        timeline_menu.addSeparator()
        timeline_menu.addAction(
            Action(
                FIF.SYNC,
                self.tr("整集时间偏移"),
                triggered=self.shift_all_timeline_custom,
            )
        )
        self.timeline_button = TransparentDropDownPushButton(
            self.tr("时间轴"), self, FIF.SYNC
        )
        self.timeline_button.setMenu(timeline_menu)
        self.timeline_button.setFixedHeight(34)
        self.timeline_button.setMinimumWidth(105)
        self.command_bar.addWidget(self.timeline_button)

        # 添加字幕排布下拉按钮
        self.layout_button = TransparentDropDownPushButton(
            self.tr("字幕排布"), self, FIF.LAYOUT
        )
        self.layout_button.setFixedHeight(34)
        self.layout_button.setMinimumWidth(125)
        self.layout_menu = RoundMenu(parent=self)
        for layout in ["译文在上", "原文在上", "仅译文", "仅原文"]:
            action = Action(text=layout)
            action.triggered.connect(
                lambda checked, l=layout: signalBus.subtitle_layout_changed.emit(l)
            )
            self.layout_menu.addAction(action)
        self.layout_button.setMenu(self.layout_menu)
        self.command_bar.addWidget(self.layout_button)

        self.command_bar.addSeparator()

        # 添加字幕优化按钮
        self.optimize_button = Action(
            FIF.EDIT,
            self.tr("字幕校正"),
            triggered=self.on_subtitle_optimization_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.optimize_button)

        # 添加字幕翻译按钮
        self.translate_button = Action(
            FIF.LANGUAGE,
            self.tr("字幕翻译"),
            triggered=self.on_subtitle_translation_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.translate_button)

        # 添加翻译语言选择
        self.target_language_button = TransparentDropDownPushButton(
            self.tr("翻译语言"), self, FIF.LANGUAGE
        )
        self.target_language_button.setFixedHeight(34)
        self.target_language_button.setMinimumWidth(125)
        self.target_language_menu = RoundMenu(parent=self)
        self.target_language_menu.setMaxVisibleItems(10)
        for lang in TargetLanguageEnum:
            action = Action(text=lang.value)
            action.triggered.connect(
                lambda checked, l=lang.value: signalBus.target_language_changed.emit(l)
            )
            self.target_language_menu.addAction(action)
        self.target_language_button.setMenu(self.target_language_menu)

        self.command_bar.addWidget(self.target_language_button)

        self.command_bar.addSeparator()

        # 添加文稿提示按钮
        self.prompt_button = Action(
            FIF.DOCUMENT, self.tr("文稿提示"), triggered=self.show_prompt_dialog
        )
        self.command_bar.addAction(self.prompt_button)

        # 添加设置按钮
        self.command_bar.addAction(
            Action(FIF.SETTING, "", triggered=self.show_subtitle_settings)
        )

        # 添加视频播放按钮
        # self.command_bar.addAction(Action(FIF.VIDEO, "", triggered=self.show_video_player))

        # 添加打开文件夹按钮
        self.command_bar.addAction(
            Action(FIF.FOLDER, "", triggered=self.on_open_folder_clicked)
        )

        self.command_bar.addSeparator()

        # 添加文件选择按钮
        self.command_bar.addAction(
            Action(FIF.FOLDER_ADD, "", triggered=self.on_file_select)
        )

        # 添加开始按钮到水平布局
        self.start_button = PrimaryPushButton(self.tr("开始"), self, icon=FIF.PLAY)
        self.start_button.clicked.connect(
            lambda: self.start_subtitle_optimization(need_create_task=True)
        )
        self.start_button.setFixedHeight(34)
        top_layout.addWidget(self.start_button)

        self.main_layout.addLayout(top_layout)

    def _setup_subtitle_table(self):
        self.subtitle_table = TableView(self)
        self.model = SubtitleTableModel("")
        self.subtitle_table.setModel(self.model)
        self.caption_delegate = CaptionSplitDelegate(self.subtitle_table)
        self.caption_delegate.splitRequested.connect(self.split_caption_at_cursor)
        self.subtitle_table.setItemDelegateForColumn(2, self.caption_delegate)
        self.subtitle_table.setItemDelegateForColumn(3, self.caption_delegate)
        self.subtitle_table.setToolTip("双击文字编辑；Enter 拆为两条，Shift+Enter 同条换行；时间轴菜单可撤销拆分")
        self.subtitle_table.setBorderVisible(True)
        self.subtitle_table.setBorderRadius(8)
        self.subtitle_table.setWordWrap(True)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Fixed
        )
        self.subtitle_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Fixed
        )
        self.subtitle_table.setColumnWidth(0, 120)
        self.subtitle_table.setColumnWidth(1, 120)

        # 配置垂直表头
        self.subtitle_table.verticalHeader().setVisible(True)  # 显示垂直表头
        self.subtitle_table.verticalHeader().setDefaultAlignment(
            Qt.AlignCenter
        )  # 居中对齐
        self.subtitle_table.verticalHeader().setDefaultSectionSize(50)  # 行高
        self.subtitle_table.verticalHeader().setMinimumWidth(20)  # 设置最小宽度

        self.subtitle_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        self.subtitle_table.clicked.connect(self.on_subtitle_clicked)
        # 添加右键菜单支持
        self.subtitle_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.subtitle_table.customContextMenuRequested.connect(self.show_context_menu)
        self.main_layout.addWidget(self.subtitle_table)

    def _setup_bottom_layout(self):
        self.bottom_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("请拖入字幕文件"), self)
        self.status_label.setMinimumWidth(100)
        self.status_label.setAlignment(Qt.AlignCenter)

        # 添加取消按钮
        self.cancel_button = PushButton(self.tr("取消"), self, icon=FIF.CANCEL)
        self.cancel_button.hide()  # 初始隐藏
        self.cancel_button.clicked.connect(self.cancel_optimization)

        self.bottom_layout.addWidget(self.progress_bar, 1)
        self.bottom_layout.addWidget(self.status_label)
        self.bottom_layout.addWidget(self.cancel_button)
        self.main_layout.addLayout(self.bottom_layout)

    def _setup_signals(self):
        signalBus.subtitle_layout_changed.connect(self.on_subtitle_layout_changed)
        signalBus.target_language_changed.connect(self.on_target_language_changed)
        signalBus.subtitle_optimization_changed.connect(
            self.on_subtitle_optimization_changed
        )
        signalBus.subtitle_translation_changed.connect(
            self.on_subtitle_translation_changed
        )
        self.model.timeEditRejected.connect(self.on_time_edit_rejected)
        self.model.timeEditAdjusted.connect(self.on_time_edit_adjusted)
        self.model.timelineChanged.connect(self.on_timeline_changed)
        # self.subtitle_setting_button.clicked.connect(self.show_subtitle_settings)
        # self.video_player_button.clicked.connect(self.show_video_player)

    def selected_subtitle_rows(self):
        return sorted(
            {index.row() for index in self.subtitle_table.selectedIndexes()}
        )

    def on_timeline_changed(self):
        self.status_label.setText(self.tr("时间轴已修改，请保存后重新生成"))

    def split_caption_at_cursor(self, row, column, text, position):
        if hasattr(self, "subtitle_optimization_thread") and self.subtitle_optimization_thread.isRunning():
            self.on_time_edit_rejected("请先等待字幕校正结束再拆分")
            return
        success, message = self.model.split_caption(row, column, text, position)
        if success:
            self.subtitle_table.selectRow(row + 1)
            InfoBar.success("字幕已拆分", message + "；请保存", duration=5000, parent=self)
        elif message:
            self.on_time_edit_rejected(message)

    def undo_caption_split(self):
        if self.model.undo_caption_split():
            InfoBar.success("已撤销拆分", "已恢复拆分前的字幕与时间", duration=2500, parent=self)

    def load_split_time_anchors(self, subtitle_path, project=None):
        self.model.word_anchors = []
        self.model.split_history.clear()
        directories = [Path(subtitle_path).parent]
        if project:
            manifest = project.get("work_manifest")
            if manifest:
                directories.insert(0, Path(manifest).parent / "subtitle")
        for directory in directories:
            try:
                candidates = list(directory.glob("【原始字幕】*.srt"))
                # An ambiguous directory must never attach another episode's audio.
                if len(candidates) != 1:
                    continue
                data = ASRData.from_subtitle_file(str(candidates[0]))
                if data.is_word_timestamp():
                    self.model.word_anchors = data.segments
                    return
            except (OSError, ValueError):
                continue

    def on_time_edit_rejected(self, message):
        InfoBar.warning(
            self.tr("时间修改无效"),
            self.tr(message),
            duration=3500,
            parent=self,
        )

    def on_time_edit_adjusted(self, message):
        InfoBar.info(
            self.tr("已防止重叠"),
            self.tr(message),
            duration=2500,
            parent=self,
        )

    def shift_selected_timeline(self, delta_ms):
        rows = self.selected_subtitle_rows()
        if not rows:
            InfoBar.warning(
                self.tr("未选择字幕"),
                self.tr("请先在表格中选择一条或多条字幕"),
                duration=3000,
                parent=self,
            )
            return
        self.apply_timeline_shift(rows, delta_ms, self.tr("所选字幕"))

    def shift_selected_timeline_custom(self):
        rows = self.selected_subtitle_rows()
        if not rows:
            InfoBar.warning(
                self.tr("未选择字幕"),
                self.tr("请先在表格中选择一条或多条字幕"),
                duration=3000,
                parent=self,
            )
            return
        delta_ms, accepted = QInputDialog.getInt(
            self,
            self.tr("所选字幕时间偏移"),
            self.tr("输入毫秒数：负数提前，正数延后"),
            0,
            -3_600_000,
            3_600_000,
            10,
        )
        if accepted and delta_ms:
            self.apply_timeline_shift(rows, delta_ms, self.tr("所选字幕"))

    def shift_all_timeline_custom(self):
        if not self.model._data:
            InfoBar.warning(
                self.tr("没有字幕"),
                self.tr("请先加载字幕文件"),
                duration=3000,
                parent=self,
            )
            return
        delta_ms, accepted = QInputDialog.getInt(
            self,
            self.tr("整集时间偏移"),
            self.tr("输入毫秒数：负数整集提前，正数整集延后"),
            0,
            -3_600_000,
            3_600_000,
            10,
        )
        if accepted and delta_ms:
            self.apply_timeline_shift(
                range(self.model.rowCount()), delta_ms, self.tr("整集字幕")
            )

    def apply_timeline_shift(self, rows, requested_delta_ms, scope_name):
        applied_delta = self.model.shift_rows(rows, requested_delta_ms)
        if not applied_delta:
            return
        direction = self.tr("延后") if applied_delta > 0 else self.tr("提前")
        limited = ""
        if applied_delta != requested_delta_ms:
            limited = self.tr("（为避免负时间或重叠，已自动限制）")
        InfoBar.success(
            self.tr("时间轴已调整"),
            self.tr(f"{scope_name}{direction} {abs(applied_delta)}ms{limited}"),
            duration=3500,
            parent=self,
        )

    def show_prompt_dialog(self):
        dialog = PromptDialog(self)
        if dialog.exec_():
            self.custom_prompt_text = cfg.custom_prompt_text.value
            self._update_prompt_button_style()

    def _update_prompt_button_style(self):
        if self.custom_prompt_text.strip():
            green_icon = FIF.DOCUMENT.colored(
                QColor(76, 255, 165), QColor(76, 255, 165)
            )
            self.prompt_button.setIcon(green_icon)
        else:
            self.prompt_button.setIcon(FIF.DOCUMENT)

    def set_task(self, task: SubtitleTask):
        """设置任务并更新UI"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()
        self.start_button.setEnabled(True)
        self.task = task
        self.subtitle_path = task.subtitle_path
        self.update_info(task)

    def update_info(self, task: SubtitleTask):
        """更新页面信息"""
        original_subtitle_save_path = Path(self.task.subtitle_path)
        asr_data = ASRData.from_subtitle_file(original_subtitle_save_path)
        self._loaded_subtitle_hash = hashlib.sha256(original_subtitle_save_path.read_bytes()).hexdigest()
        self.model._data = asr_data.to_json()
        self.load_split_time_anchors(str(original_subtitle_save_path))
        self.model.layoutChanged.emit()
        self.status_label.setText(self.tr("已加载文件"))
        if hasattr(self, "studio"):
            self.studio.loaded()
        return True

    def start_subtitle_optimization(self, need_create_task=True):
        # 检查是否有任务
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"), self.tr("请先加载字幕文件"), duration=3000, parent=self
            )
            return
        self.start_button.setEnabled(False)
        self.progress_bar.reset()
        self.cancel_button.show()

        if need_create_task:
            previous = getattr(self, 'task', None)
            self.task = TaskFactory.create_subtitle_task(file_path=self.subtitle_path,
                video_path=getattr(previous, 'video_path', None),
                need_next_task=getattr(previous, 'need_next_task', False))
        if need_create_task and getattr(self.task.subtitle_config, 'text_source_mode', None) in ('strict', 'hybrid', 'script_correct'):
            # Explicitly bind manually loaded subtitles to their original video.
            video_path, _ = QFileDialog.getOpenFileName(
                self, self.tr("选择这份字幕对应的原视频（用于读取已绑定剧本）"),
                str(Path(getattr(self.task, 'video_path', '') or self.subtitle_path).parent),
                self.tr("视频文件 (*.mp4 *.mkv *.mov *.avi *.webm);;所有文件 (*.*)"),
            )
            if not video_path:
                self.start_button.setEnabled(True)
                self.cancel_button.hide()
                return
            from app.core.script_reference_store import load_exact_video_script_reference
            if not load_exact_video_script_reference(video_path):
                InfoBar.warning(self.tr("未找到对应剧本"),
                    self.tr("所选原视频未绑定剧本，请先在剧本台词参考中绑定对应 JSON。"),
                    duration=6000, parent=self)
                self.start_button.setEnabled(True)
                self.cancel_button.hide()
                return
            self.task = TaskFactory.create_subtitle_task(
                file_path=self.subtitle_path, video_path=video_path, need_next_task=False)
        self.subtitle_optimization_thread = SubtitleThread(self.task)
        self.subtitle_optimization_thread.finished.connect(
            self.on_subtitle_optimization_finished
        )
        self.subtitle_optimization_thread.progress.connect(
            self.on_subtitle_optimization_progress
        )
        self.subtitle_optimization_thread.update.connect(self.update_data)
        self.subtitle_optimization_thread.update_all.connect(self.update_all)
        self.subtitle_optimization_thread.error.connect(
            self.on_subtitle_optimization_error
        )
        self.subtitle_optimization_thread.set_custom_prompt_text(
            self.custom_prompt_text
        )
        self.subtitle_optimization_thread.start()
        InfoBar.info(
            self.tr("开始优化"), self.tr("开始优化字幕"), duration=3000, parent=self
        )

    def process(self):
        """主处理函数"""
        # 检查是否有任务
        self.start_subtitle_optimization(need_create_task=False)

    def on_subtitle_optimization_finished(self, video_path, output_path):
        self.start_button.setEnabled(True)
        self.cancel_button.hide()  # 隐藏取消按钮
        if self.task.need_next_task:
            self.finished.emit(video_path, output_path)
        InfoBar.success(
            self.tr("优化完成"),
            self.tr("优化完成字幕..."),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
            parent=self.parent(),
        )

    def on_subtitle_optimization_error(self, error):
        self.start_button.setEnabled(True)
        self.cancel_button.hide()  # 隐藏取消按钮
        self.progress_bar.error()
        InfoBar.error(self.tr("优化失败"), self.tr(error), duration=20000, parent=self)

    def on_subtitle_optimization_progress(self, value, status):
        self.progress_bar.setValue(value)
        self.status_label.setText(status)

    def update_data(self, data):
        self.model.update_data(data)

    def update_all(self, data):
        self.model.update_all(data)

    def remove_widget(self):
        """隐藏顶部开始按钮和底部进度条"""
        self.start_button.hide()
        for i in range(self.bottom_layout.count()):
            widget = self.bottom_layout.itemAt(i).widget()
            if widget:
                widget.hide()

    def on_file_select(self):
        # 构建文件过滤器
        subtitle_formats = " ".join(
            f"*.{fmt.value}" for fmt in SupportedSubtitleFormats
        )
        filter_str = f"{self.tr('字幕文件')} ({subtitle_formats})"

        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择字幕文件"), "", filter_str
        )
        if file_path:
            self.subtitle_path = file_path
            self.load_subtitle_file(file_path)

    def on_save_format_clicked(self, format: str):
        """处理保存格式的选择"""
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"), self.tr("请先加载字幕文件"), duration=3000, parent=self
            )
            return

        # 获取保存路径
        default_name = Path(self.subtitle_path).stem
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("保存字幕文件"),
            default_name,  # 使用原文件名作为默认名
            f"{self.tr('字幕文件')} (*.{format})",
        )
        if not file_path:
            return

        try:
            # 转换并保存字幕
            if hasattr(self, "studio"):
                self.studio.archive_before_save(file_path)
            asr_data = ASRData.from_json(self.model._data)
            layout = cfg.subtitle_layout.value

            if file_path.endswith(".ass"):
                style_str = get_subtitle_style(cfg.subtitle_style_name.value)
                asr_data.to_ass(style_str, layout, file_path)
            else:
                asr_data.save(file_path, layout=layout)
            InfoBar.success(
                self.tr("保存成功"),
                self.tr(f"字幕已保存至:") + file_path,
                duration=3000,
                parent=self,
            )
        except Exception as e:
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("保存字幕文件失败: ") + str(e),
                duration=5000,
                parent=self,
            )

    def save_controller(self):
        if not hasattr(self,'_save_flow'):
            from app.view.editor_save_ui import EditorSaveController
            self._save_flow=EditorSaveController(self)
        return self._save_flow

    def request_save_current(self,checked=False):
        return self.save_controller().request()

    def save_current_in_place(self,show_success=True):
        # Synchronous compatibility for save-before-close confirmations only.
        if not self.subtitle_path:return False
        flow=self.save_controller()
        if flow.busy:return False
        from app.core.editor_save import save_snapshot
        try:
            result=save_snapshot(flow.snapshot())
            self._loaded_subtitle_hash=result['hash'];self.studio.saved()
            self.status_label.setText(result['warning'] or '字幕已保存：'+Path(result['path']).name)
            if result['warning']:InfoBar.warning('字幕已保存，索引未更新',result['warning'],parent=self)
            elif show_success:InfoBar.success('保存成功','本集字幕已保存；成片需重新合成才会更新',parent=self)
            flow.refresh_latest();return True
        except Exception as error:
            self.status_label.setText('保存失败，当前修改保留：'+str(error));InfoBar.error('保存失败',str(error),parent=self);return False

    def open_batch_project_async(self,manifest_path,callback=None):
        if not hasattr(self,'_project_open'):
            from app.view.project_open_ui import ProjectOpenController
            self._project_open=ProjectOpenController(self)
        return self._project_open.open(manifest_path,callback)

    def load_batch_project(self, manifest_path):
        """载入批量留存工程，并关联原视频与当时使用的字幕模板。"""
        if hasattr(self, "studio") and not self.studio.confirm_switch():
            return False
        if hasattr(self,'_project_open') and self._project_open.busy:self._project_open.cancel()
        project = read_batch_project(manifest_path)
        video_path = project.get("source_video", "")
        sidecar_ass = str(Path(video_path).with_suffix(".ass")) if video_path else ""
        candidates = (
            project.get("editable_subtitle", ""),
            project.get("styled_subtitle", ""),
            sidecar_ass,
        )
        subtitle_path = next(
            (path for path in candidates if path and Path(path).exists()), ""
        )
        if not subtitle_path:
            raise FileNotFoundError("可编辑 ASS/SRT 已被移动或删除")

        self._studio_project_loading = True
        try:
            self.load_subtitle_file(subtitle_path)
        finally:
            self._studio_project_loading = False
        self.load_split_time_anchors(subtitle_path, project)
        self.batch_project_manifest = str(manifest_path)
        self.batch_project_video = video_path
        self.task = TaskFactory.create_subtitle_task(subtitle_path, video_path, customer_profile=project.get("customer_template_snapshot") or None)
        self.rerender_action.setEnabled(bool(video_path and Path(video_path).exists()))
        project_values = {
            "editable_subtitle": str(Path(subtitle_path).resolve()),
        }
        if subtitle_path.lower().endswith(".ass"):
            project_values["styled_subtitle"] = str(Path(subtitle_path).resolve())
        update_batch_project(str(manifest_path), **project_values)
        self.status_label.setText(self.tr(f"已载入批量工程：{project.get('title', '')}"))
        if hasattr(self, "studio"):
            self.studio.loaded()
        return True

    def prepare_batch_project_rerender(self,checked=False):
        if not self.batch_project_manifest:
            InfoBar.warning('没有批量工程','请先打开本集的可编辑工程',parent=self);return False
        home=self.window().homeInterface
        active=getattr(home.video_synthesis_interface,'video_synthesis_thread',None)
        if getattr(home,'_workflow',None) is not None or (active is not None and active.isRunning()):
            InfoBar.warning('正在处理','请等当前单条任务完成；批量任务不受影响',parent=self);return False
        return self.save_controller().request(render=True)

    def on_open_folder_clicked(self):
        """打开文件夹按钮点击事件"""
        if not self.task:
            InfoBar.warning(
                self.tr("警告"), self.tr("请先加载字幕文件"), duration=3000, parent=self
            )
            return
        output_path = Path(self.task.output_path)
        target_dir = str(
            output_path.parent
            if output_path.exists()
            else Path(self.task.subtitle_path).parent
        )
        if sys.platform == "win32":
            os.startfile(target_dir)
        elif sys.platform == "darwin":  # macOS
            subprocess.run(["open", target_dir])
        else:  # Linux
            subprocess.run(["xdg-open", target_dir])

    def load_subtitle_file(self, file_path):
        if hasattr(self,'_project_open') and self._project_open.busy:self._project_open.cancel()
        if hasattr(self, "studio") and not getattr(self, "_studio_project_loading", False):
            if not self.studio.confirm_switch():
                return False
        if hasattr(self, "studio"):
            self.studio.loading = True
        # 普通文件导入不应误用上一次批量工程的原视频关联。
        if str(file_path) != str(self.subtitle_path):
            self.batch_project_manifest = None
            self.batch_project_video = None
            if hasattr(self, "rerender_action"):
                self.rerender_action.setEnabled(False)
        self.subtitle_path = file_path
        asr_data = ASRData.from_subtitle_file(file_path)
        self._loaded_subtitle_hash = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
        self.model._data = asr_data.to_json()
        self.load_split_time_anchors(file_path)
        self.model.layoutChanged.emit()
        self.status_label.setText(self.tr("已加载文件"))

    def dragEnterEvent(self, event: QDragEnterEvent):
        event.accept() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event: QDropEvent):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file_path in files:
            if not os.path.isfile(file_path):
                continue

            file_ext = os.path.splitext(file_path)[1][1:].lower()

            # 检查文件格式是否支持
            supported_formats = {fmt.value for fmt in SupportedSubtitleFormats}
            is_supported = file_ext in supported_formats

            if is_supported:
                self.load_subtitle_file(file_path)
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr(f"成功导入") + os.path.basename(file_path),
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                    parent=self,
                )
                break
            else:
                InfoBar.error(
                    self.tr(f"格式错误") + file_ext,
                    self.tr(f"支持的字幕格式:") + str(supported_formats),
                    duration=3000,
                    parent=self,
                )
        event.accept()

    def closeEvent(self, event):
        if hasattr(self, "studio"):
            if not self.studio.confirm_switch():
                event.ignore()
                return
            if hasattr(self,'_project_open'):self._project_open.close()
            self.studio.close()
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()
        super().closeEvent(event)

    def show_subtitle_settings(self):
        """显示字幕设置对话框"""
        dialog = SubtitleSettingDialog(self.window())
        dialog.exec_()

    def show_video_player(self):
        """显示视频播放器窗口"""
        # 创建视频播放器窗口
        from ..components.MyVideoWidget import MyVideoWidget

        self.video_player = MyVideoWidget()
        self.video_player.resize(800, 600)

        def signal_update():
            if not self.model._data:
                return
            ass_style_name = cfg.subtitle_style_name.value
            ass_style_path = SUBTITLE_STYLE_PATH / f"{ass_style_name}.txt"
            if ass_style_path.exists():
                subtitle_style_srt = ass_style_path.read_text(encoding="utf-8")
            else:
                subtitle_style_srt = None
            if not hasattr(self, '_isolated_preview_ass'):
                self._isolated_preview_ass = os.path.join(tempfile.gettempdir(), f"caption_preview_{uuid.uuid4().hex}.ass")
            temp_srt_path = self._isolated_preview_ass
            asr_data = ASRData.from_json(self.model._data)
            asr_data.save(
                temp_srt_path,
                layout=cfg.subtitle_layout.value,
                ass_style=subtitle_style_srt,
            )
            signalBus.add_subtitle(temp_srt_path)

        # 如果有字幕文件,则添加字幕
        signal_update()

        signalBus.subtitle_layout_changed.connect(signal_update)
        self.model.dataChanged.connect(signal_update)
        self.model.layoutChanged.connect(signal_update)

        # 如果有关联的视频文件,则自动加载
        if self.task and hasattr(self.task, "file_path") and self.task.file_path:
            self.video_player.setVideo(QUrl.fromLocalFile(self.task.file_path))

        self.video_player.show()
        self.video_player.play()

    def on_subtitle_clicked(self, index):
        row = index.row()
        item = list(self.model._data.values())[row]
        start_time = item["start_time"]  # 毫秒
        end_time = (
            item["end_time"] - 50
            if item["end_time"] - 50 > start_time
            else item["end_time"]
        )
        if hasattr(self, "studio"):
            self.studio.seek(start_time)
        else:
            signalBus.play_video_segment(start_time, end_time)

    def show_context_menu(self, pos):
        """显示右键菜单"""
        menu = RoundMenu(parent=self)

        # 获取选中的行
        indexes = self.subtitle_table.selectedIndexes()
        if not indexes:
            return

        # 获取唯一的行号
        rows = sorted(set(index.row() for index in indexes))
        if not rows:
            return

        # 添加菜单项
        # retranslate_action = Action(FIF.SYNC, self.tr("重新翻译"))
        merge_action = Action(FIF.LINK, self.tr("合并"))  # 添加快捷键提示
        # menu.addAction(retranslate_action)
        menu.addAction(merge_action)
        merge_action.setShortcut("Ctrl+M")  # 设置快捷键
        menu.addSeparator()
        advance_action = Action(FIF.UP, self.tr("所选字幕提前 100ms"))
        delay_action = Action(FIF.DOWN, self.tr("所选字幕延后 100ms"))
        custom_shift_action = Action(FIF.EDIT, self.tr("所选字幕自定义偏移"))
        menu.addAction(advance_action)
        menu.addAction(delay_action)
        menu.addAction(custom_shift_action)

        # 设置动作状态
        # retranslate_action.setEnabled(cfg.need_translate.value)
        merge_action.setEnabled(len(rows) > 1)

        # 连接动作信号
        # retranslate_action.triggered.connect(lambda: self.retranslate_selected_rows(rows))
        merge_action.triggered.connect(lambda: self.merge_selected_rows(rows))
        advance_action.triggered.connect(lambda: self.shift_selected_timeline(-100))
        delay_action.triggered.connect(lambda: self.shift_selected_timeline(100))
        custom_shift_action.triggered.connect(self.shift_selected_timeline_custom)

        # 显示菜单
        menu.exec(self.subtitle_table.viewport().mapToGlobal(pos))

    def merge_selected_rows(self, rows):
        """合并选中的字幕行"""
        if not rows or len(rows) < 2:
            return

        # 获取选中行的数据
        data = self.model._data
        data_list = list(data.values())

        # 获取第一行和最后一行的时间戳
        first_row = data_list[rows[0]]
        last_row = data_list[rows[-1]]
        start_time = first_row["start_time"]
        end_time = last_row["end_time"]

        # 合并字幕内容
        original_subtitles = []
        translated_subtitles = []
        for row in rows:
            item = data_list[row]
            original_subtitles.append(item["original_subtitle"])
            translated_subtitles.append(item["translated_subtitle"])

        merged_original = " ".join(original_subtitles)
        merged_translated = " ".join(translated_subtitles)

        # 创建新的合并后的字幕项
        merged_item = {
            "start_time": start_time,
            "end_time": end_time,
            "original_subtitle": merged_original,
            "translated_subtitle": merged_translated,
        }

        # 获取所有需要保留的键
        keys = list(data.keys())
        preserved_keys = keys[: rows[0]] + keys[rows[-1] + 1 :]

        # 创建新的数据字典
        new_data = {}
        for i, key in enumerate(preserved_keys):
            if i == rows[0]:
                new_key = f"{len(new_data)+1}"
                new_data[new_key] = merged_item
            new_key = f"{len(new_data)+1}"
            new_data[new_key] = data[key]

        # 如果合并的是最后几行，需要确保合并项被添加
        if rows[0] >= len(preserved_keys):
            new_key = f"{len(new_data)+1}"
            new_data[new_key] = merged_item

        # 更新模型数据
        self.model.update_all(new_data)

        # 显示成功提示
        InfoBar.success(
            self.tr("合并成功"),
            self.tr("已成功合并选中的字幕行"),
            duration=1000,
            parent=self,
        )

    def keyPressEvent(self, event):
        """处理键盘事件"""
        # 处理 Ctrl+M 快捷键
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_M:
            indexes = self.subtitle_table.selectedIndexes()
            if indexes:
                rows = sorted(set(index.row() for index in indexes))
                if len(rows) > 1:
                    self.merge_selected_rows(rows)
            event.accept()
        else:
            super().keyPressEvent(event)

    def cancel_optimization(self):
        """取消字幕校正"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()
            self.start_button.setEnabled(True)
            self.cancel_button.hide()
            self.progress_bar.setValue(0)
            self.status_label.setText(self.tr("已取消校正"))
            InfoBar.warning(
                self.tr("已取消"), self.tr("字幕校正已取消"), duration=3000, parent=self
            )

    def on_target_language_changed(self, language: str):
        """处理翻译语言变更"""
        for lang in TargetLanguageEnum:
            if lang.value == language:
                self.target_language_button.setText(lang.value)
                cfg.set(cfg.target_language, lang)
                break

    def on_subtitle_optimization_changed(self, checked: bool):
        """处理字幕优化开关变更"""
        cfg.set(cfg.need_optimize, checked)
        self.optimize_button.setChecked(checked)

    def on_subtitle_translation_changed(self, checked: bool):
        """处理字幕翻译开关变更"""
        cfg.set(cfg.need_translate, checked)
        self.translate_button.setChecked(checked)
        # 控制翻译语言选择按钮的启用状态
        self.target_language_button.setEnabled(checked)

    def on_subtitle_layout_changed(self, layout: str):
        """处理字幕排布变更"""
        cfg.set(cfg.subtitle_layout, layout)
        self.layout_button.setText(layout)


class PromptDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.setWindowTitle(self.tr("文稿提示"))
        # 连接按钮点击事件
        self.yesButton.clicked.connect(self.save_prompt)

    def setup_ui(self):
        self.titleLabel = BodyLabel(self.tr("文稿提示"), self)

        # 添加文本编辑框
        self.text_edit = TextEdit(self)
        self.text_edit.setPlaceholderText(
            self.tr(
                "请输入文稿提示（辅助校正字幕和翻译）\n\n"
                "支持以下内容:\n"
                "1. 术语表 - 专业术语、人名、特定词语的修正对照表\n"
                "示例:\n机器学习->Machine Learning\n马斯克->Elon Musk\n打call->应援\n\n"
                "2. 原字幕文稿 - 视频的原有文稿或相关内容\n"
                "示例: 完整的演讲稿、课程讲义等\n\n"
                "3. 修正要求 - 内容相关的具体修正要求\n"
                "示例: 统一人称代词、规范专业术语等\n\n"
                "注意: 使用小型LLM模型时建议控制文稿在1千字内。对于不同字幕文件,请使用与该字幕相关的文稿提示。"
            )
        )
        self.text_edit.setText(cfg.custom_prompt_text.value)

        self.text_edit.setMinimumWidth(420)
        self.text_edit.setMinimumHeight(380)

        # 添加到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.text_edit)
        self.viewLayout.setSpacing(10)

        # 设置按钮文本
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))

    def get_prompt(self):
        return self.text_edit.toPlainText()

    def save_prompt(self):
        # 在点击确定按钮时保存提示文本到配置
        prompt_text = self.text_edit.toPlainText()
        cfg.set(cfg.custom_prompt_text, prompt_text)


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    window = SubtitleInterface()
    window.show()
    sys.exit(app.exec_())

