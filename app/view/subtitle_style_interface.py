import os
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFontDatabase
from PyQt5.QtWidgets import (QFileDialog, QHBoxLayout, QVBoxLayout, QWidget,
    QDialog, QFormLayout, QLabel, QDoubleSpinBox, QDialogButtonBox, QComboBox, QSpinBox)
from qfluentwidgets import BodyLabel, CardWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    ImageLabel,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    PushSettingCard,
    ScrollArea,
    SettingCardGroup,
)

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.components.MySettingCard import (
    ColorSettingCard,
    ComboBoxSettingCard,
    DoubleSpinBoxSettingCard,
    FontComboBoxSettingCard,
    SpinBoxSettingCard,
)
from app.config import SUBTITLE_STYLE_PATH, ASSETS_PATH, RESOURCE_PATH
from app.core.utils.ass_effects import parse_effects
from app.core.concurrency import atomic_text, shared_lock
from app.core.utils.subtitle_preview import generate_preview

PERVIEW_TEXTS = {
    "长文本": (
        "This is a long text used for testing subtitle preview and style settings.",
        "这是一段用于测试字幕预览和样式设置的长文本内容",
    ),
    "中文本": (
        "Welcome to apply for the prestigious South China Normal University!",
        "欢迎报考百年名校华南师范大学",
    ),
    "短文本": ("Elementary school students know this", "小学二年级的都知道"),
}

DEFAULT_BG_LANDSCAPE = {
    "path": ASSETS_PATH / "default_bg_landscape.png",
    "width": 1280,
    "height": 720,
}
DEFAULT_BG_PORTRAIT = {
    "path": ASSETS_PATH / "default_bg_portrait.png",
    "width": 480,
    "height": 852,
}

# Pixel-calibrated against a local Jianying/CapCut 1080x1920 draft using the
# bundled Source Han Sans CN Medium font.  Jianying size 14 matches ASS size 46
# when VideoCaptioner's exported ASS uses its legacy 1280x720 script canvas.
CAPCUT_FONT_TO_ASS = 46.0 / 14.0

# Calibrated from the exported V6 frame against Jianying's native text at the
# same displayed size.  Jianying width 3 has roughly one soft display pixel of
# dark edge, which matches ASS Outline 0.6 on the 1280x720 script canvas.
CAPCUT_OUTLINE_TO_ASS = 0.2


def calibrated_margin(y, height=1920):
    # Empirical anchor, then linear movement on the legacy 720-high ASS canvas.
    return round(217 + (float(y) / float(height) + 645 / 1920) * 720)


CANVAS_PRESETS = [
    ("9:16 竖屏", 1080, 1920), ("16:9 横屏", 1920, 1080),
    ("1:1 方形", 1080, 1080), ("4:3 横屏", 1440, 1080),
    ("3:4 竖屏", 1080, 1440), ("4:5 竖屏", 1080, 1350),
    ("2:3 竖屏", 1080, 1620), ("3:2 横屏", 1620, 1080),
    ("2:1 横屏", 2160, 1080), ("1:2 竖屏", 1080, 2160),
    ("21:9 宽屏", 2520, 1080), ("自定义 / 原视频尺寸", 0, 0),
]


class CapcutCalibrationDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("剪映参数换算 · 测试卷自己校准")
        self.resize(540, 470)
        form = QFormLayout(self)
        note = QLabel("原始校准画布：1080×1920；字体：剪映系统字体\n"
            "基准：字号14，Y=-645，描边3，阴影80%\n"
            "沿用已调好的柔化效果；其他数值按比例近似换算。\n"
            "应用后另存为新模板，不覆盖当前模板。")
        note.setWordWrap(True)
        form.addRow(note)
        self.canvasPreset = QComboBox(self)
        for label, width, height in CANVAS_PRESETS:
            self.canvasPreset.addItem(label, (width, height))
        form.addRow("画布比例", self.canvasPreset)
        self.canvasWidth, self.canvasHeight = QSpinBox(self), QSpinBox(self)
        for box, value in ((self.canvasWidth, 1080), (self.canvasHeight, 1920)):
            box.setRange(16, 32768)
            box.setValue(value)
        form.addRow("画布宽度（像素）", self.canvasWidth)
        form.addRow("画布高度（像素）", self.canvasHeight)
        self.inputs = {}
        specs = [
            ("size", "字号（剪映）", 14, 1, 300),
            ("y", "上下位置 Y（负数向下）", -645, -32768, 32768),
            ("outline", "描边粗细（剪映）", 3, 0, 100 / 4.8),
            ("outline_opacity", "描边不透明度 %", 88, 0, 100),
            ("shadow_opacity", "阴影不透明度 %", 80, 0, 100),
            ("blur", "阴影模糊（剪映，基准15）", 15, 0, 100 * 15 / 82),
            ("distance", "阴影距离（剪映，基准5）", 5, 0, 100 * 5 / 6.2),
            ("angle", "阴影角度", -45, -180, 180),
        ]
        for key, label, value, low, high in specs:
            box = QDoubleSpinBox(self)
            box.setDecimals(2)
            box.setRange(low, high)
            box.setValue(value)
            self.inputs[key] = box
            form.addRow(label, box)
        self.result = QLabel(self)
        self.result.setWordWrap(True)
        form.addRow(self.result)
        self.canvasPreset.currentIndexChanged.connect(self.change_canvas)
        self.canvasWidth.valueChanged.connect(self.update_result)
        self.canvasHeight.valueChanged.connect(self.update_result)
        for box in self.inputs.values():
            box.valueChanged.connect(self.update_result)
        self.update_result()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        self.applyButton = buttons.button(QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("另存并应用")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        values = {key: box.value() for key, box in self.inputs.items()}
        values.update(width=self.canvasWidth.value(), height=self.canvasHeight.value())
        return values

    def change_canvas(self):
        width, height = self.canvasPreset.currentData()
        old_height = self.canvasHeight.value()
        if width and height:
            self.canvasWidth.setValue(width)
            self.canvasHeight.setValue(height)
            # Keep relative height when switching presets; user may then enter the draft's exact Y.
            self.inputs['y'].setValue(self.inputs['y'].value() * height / old_height)
        self.update_result()

    def update_result(self):
        v = self.values()
        margin = calibrated_margin(v['y'], v['height'])
        if hasattr(self, 'applyButton'):
            self.applyButton.setEnabled(0 <= margin <= 10000)
        self.result.setText(
            f"画布 {v['width']}×{v['height']}；换算后距底部 {calibrated_margin(v['y'], v['height'])}，"
            f"ASS字号 {v['size'] * CAPCUT_FONT_TO_ASS:.2f}，"
            f"软件描边 {v['outline'] * 4.8:.2f}，"
            f"阴影柔和度 {v['blur'] * 82 / 15:.1f}，"
            f"偏移距离 {v['distance'] * 6.2 / 5:.2f}\n" +
            ("位置超出当前字幕边距范围，请调整Y。\n" if not 0 <= margin <= 10000 else "") +
            "位置按画布高度等比例推算；字号/阴影沿用竖屏近似校准。\n"
            "选择尺寸只用于换算与预览，合成视频尺寸仍由原视频决定。")
OUTLINE_RE = re.compile(
    r"^;\s*VideoCaptionerOutline:\s*([^,]+),\s*width=([-+]?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


class PreviewThread(QThread):
    previewReady = pyqtSignal(str)

    def __init__(
        self,
        style_str: str,
        preview_text: Tuple[str, Optional[str]],
        bg_path: str,
        width: int,
        height: int,
    ):
        """
        Args:
            style_str: ASS 样式字符串
            preview_text: 预览文本元组 (主字幕, 副字幕), 副字幕可选
        """
        super().__init__()
        self.style_str = style_str
        self.preview_text = preview_text
        self.bg_path = bg_path
        self.width = width
        self.height = height

    def run(self):
        preview_path = generate_preview(
            style_str=self.style_str,
            preview_text=self.preview_text,
            bg_path=self.bg_path,
            width=self.width,
            height=self.height,
        )
        self.previewReady.emit(preview_path)


class SubtitleStyleInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("SubtitleStyleInterface")
        self._style_versions = {}
        self.setWindowTitle(self.tr("字幕样式配置"))

        # 创建主布局
        self.hBoxLayout = QHBoxLayout(self)

        # 初始化界面组件
        self._initSettingsArea()
        self._initPreviewArea()
        self._loadApplicationFonts()
        self._initSettingCards()
        self._initLayout()
        self._initStyle()

        # 添加一个标志位来控制是否触发onSettingChanged
        self._loading_style = False
        self._conversion_canvas = None
        self.preview_thread = None
        self._preview_requested = False
        self.previewTimer = QTimer(self)
        self.previewTimer.setSingleShot(True)
        self.previewTimer.setInterval(180)
        self.previewTimer.timeout.connect(self._startPreview)

        # 设置初始值,加载样式
        self.__setValues()

        # 连接信号
        self.connectSignals()

    def _loadApplicationFonts(self):
        """加载应用内置字体，不需要将字体安装到 Windows。"""
        self._application_font_ids = []
        self.capcutSystemFontFamily = None
        for font_path in sorted((RESOURCE_PATH / 'fonts').iterdir()):
            if font_path.suffix.lower() not in ('.ttf', '.otf', '.ttc'):
                continue
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id < 0:
                continue
            self._application_font_ids.append(font_id)
            families = QFontDatabase.applicationFontFamilies(font_id)
            if font_path.name == 'capcut-system-zh-hans.ttf' and families:
                self.capcutSystemFontFamily = families[0]

    def _initSettingsArea(self):
        """初始化左侧设置区域"""
        self.settingsScrollArea = ScrollArea()
        self.settingsScrollArea.setFixedWidth(350)
        self.settingsWidget = QWidget()
        self.settingsLayout = QVBoxLayout(self.settingsWidget)
        self.settingsScrollArea.setWidget(self.settingsWidget)
        self.settingsScrollArea.setWidgetResizable(True)

        # 创建设置组
        self.layoutGroup = SettingCardGroup(self.tr("字幕排布"), self.settingsWidget)
        self.mainGroup = SettingCardGroup(self.tr("主字幕样式"), self.settingsWidget)
        self.subGroup = SettingCardGroup(self.tr("副字幕样式"), self.settingsWidget)
        self.previewGroup = SettingCardGroup(self.tr("预览设置"), self.settingsWidget)

    def _initPreviewArea(self):
        """初始化右侧预览区域"""
        self.previewCard = CardWidget()
        self.previewLayout = QVBoxLayout(self.previewCard)
        self.previewLayout.setSpacing(16)

        # 顶部预览区域
        self.previewTopWidget = QWidget()
        self.previewTopWidget.setFixedHeight(430)
        self.previewTopLayout = QVBoxLayout(self.previewTopWidget)

        self.previewLabel = BodyLabel(self.tr("预览效果"))
        self.previewImage = ImageLabel()
        self.previewImage.setAlignment(Qt.AlignCenter)
        self.previewTopLayout.addWidget(self.previewImage, 0, Qt.AlignCenter)
        self.previewTopLayout.setAlignment(Qt.AlignVCenter)

        # 底部控件区域
        self.previewBottomWidget = QWidget()
        self.previewBottomLayout = QVBoxLayout(self.previewBottomWidget)

        self.styleNameComboBox = ComboBoxSettingCard(
            FIF.VIEW, self.tr("选择样式"), self.tr("选择已保存的字幕样式"), texts=[]
        )

        self.newStyleButton = PushSettingCard(
            self.tr("新建样式"),
            FIF.ADD,
            self.tr("新建样式"),
            self.tr("基于当前样式新建预设"),
        )

        self.openStyleFolderButton = PushSettingCard(
            self.tr("打开样式文件夹"),
            FIF.FOLDER,
            self.tr("打开样式文件夹"),
            self.tr("在文件管理器中打开样式文件夹"),
        )

        self.previewBottomLayout.addWidget(self.styleNameComboBox)
        self.previewBottomLayout.addWidget(self.newStyleButton)
        self.previewBottomLayout.addWidget(self.openStyleFolderButton)

        self.previewLayout.addWidget(self.previewTopWidget)
        self.previewLayout.addWidget(self.previewBottomWidget)
        self.previewLayout.addStretch(1)

    def _initSettingCards(self):
        """初始化所有设置卡片"""
        # 字幕排布设置
        self.layoutCard = ComboBoxSettingCard(
            FIF.ALIGNMENT,
            self.tr("字幕排布"),
            self.tr("设置主字幕和副字幕的显示方式"),
            texts=["译文在上", "原文在上", "仅译文", "仅原文"],
        )

        # 垂直间距
        self.verticalSpacingCard = SpinBoxSettingCard(
            FIF.ALIGNMENT,
            self.tr("字幕距底部（越大越靠上）"),
            self.tr("旧称垂直间距；1080×1920 校准：217 对应剪映 Y=-645"),
            minimum=0,
            maximum=10000,
        )
        self.capcutConvertCard = PushSettingCard(
            "输入剪映参数", FIF.EDIT, "剪映参数换算（已校准）",
            "选择画布比例或自定义宽高，换算后另存新模板")
        self.capcutConvertCard.clicked.connect(self.openCapcutConversion)

        # 主字幕样式设置
        capcut_font_hint = (
            self.tr("剪映系统字体: ") + self.capcutSystemFontFamily
            if self.capcutSystemFontFamily
            else self.tr("从 Windows 系统已安装字体中选择")
        )
        self.mainFontCard = FontComboBoxSettingCard(
            FIF.FONT,
            self.tr("主字幕字体"),
            capcut_font_hint,
        )

        self.mainSizeCard = DoubleSpinBoxSettingCard(
            FIF.FONT_SIZE,
            self.tr("主字幕字号（剪映）"),
            self.tr("直接填写剪映字号；例如剪映 15（内部自动换算为 ASS 49.3）"),
            minimum=1.0,
            maximum=300.0,
            decimals=1,
        )

        self.mainSpacingCard = DoubleSpinBoxSettingCard(
            FIF.ALIGNMENT,
            self.tr("主字幕间距"),
            self.tr("设置主字幕的字符间距"),
            minimum=0.0,
            maximum=10.0,
            decimals=1,
        )

        self.mainColorCard = ColorSettingCard(
            QColor(255, 255, 255),
            FIF.PALETTE,
            self.tr("主字幕颜色"),
            self.tr("设置主字幕的颜色"),
        )

        self.mainOutlineColorCard = ColorSettingCard(
            QColor(0, 0, 0),
            FIF.PALETTE,
            self.tr("主字幕描边颜色"),
            self.tr("文字外侧一圈的颜色"),
        )

        self.mainOutlineSizeCard = DoubleSpinBoxSettingCard(
            FIF.ZOOM,
            self.tr("主字幕描边粗细（软件值）"),
            self.tr("越大越粗；本模板14.4≈剪映3，请用剪映参数换算入口"),
            minimum=0.0,
            maximum=100.0,
            decimals=1,
        )

        self.mainOutlineOpacityCard = SpinBoxSettingCard(
            FIF.PALETTE,
            self.tr("主字幕描边不透明度"),
            self.tr("0% 为全透明，100% 为完全不透明"),
            minimum=0,
            maximum=100,
        )
        self.mainOutlineOpacityCard.spinBox.setValue(100)

        self.mainShadowColorCard = ColorSettingCard(
            QColor(0, 0, 0),
            FIF.PALETTE,
            self.tr("主字幕阴影颜色"),
            self.tr("设置主字幕的阴影颜色"),
        )

        self.mainShadowOpacityCard = SpinBoxSettingCard(
            FIF.PALETTE,
            self.tr("主字幕阴影不透明度"),
            self.tr("0% 为全透明，100% 为完全不透明"),
            minimum=0,
            maximum=100,
        )
        self.mainShadowOpacityCard.spinBox.setValue(80)

        self.mainShadowBlurCard = SpinBoxSettingCard(
            FIF.ZOOM,
            self.tr("主字幕阴影柔和度（软件值）"),
            self.tr("越大越柔和；本模板82≈剪映模糊15，效果近似"),
            minimum=0,
            maximum=100,
        )
        self.mainShadowBlurCard.spinBox.setValue(15)

        self.mainShadowSizeCard = DoubleSpinBoxSettingCard(
            FIF.ZOOM,
            self.tr("主字幕阴影偏移距离（软件值）"),
            self.tr("阴影离文字的距离；本模板6.2≈剪映5"),
            minimum=0.0,
            maximum=100.0,
            decimals=1,
        )
        self.mainShadowSizeCard.spinBox.setValue(5.0)

        self.mainShadowAngleCard = SpinBoxSettingCard(
            FIF.ALIGNMENT,
            self.tr("主字幕阴影角度"),
            self.tr("范围 -180° 到 180°，-45° 指向右下"),
            minimum=-180,
            maximum=180,
        )
        self.mainShadowAngleCard.spinBox.setValue(-45)

        # 副字幕样式设置
        self.subFontCard = FontComboBoxSettingCard(
            FIF.FONT,
            self.tr("副字幕字体"),
            capcut_font_hint,
        )

        self.subSizeCard = DoubleSpinBoxSettingCard(
            FIF.FONT_SIZE,
            self.tr("副字幕字号（剪映）"),
            self.tr("直接填写剪映字号；内部自动换算为 ASS 渲染字号"),
            minimum=1.0,
            maximum=300.0,
            decimals=1,
        )

        self.subSpacingCard = DoubleSpinBoxSettingCard(
            FIF.ALIGNMENT,
            self.tr("副字幕间距"),
            self.tr("设置副字幕的字符间距"),
            minimum=0.0,
            maximum=50.0,
            decimals=1,
        )

        self.subColorCard = ColorSettingCard(
            QColor(255, 255, 255),
            FIF.PALETTE,
            self.tr("副字幕颜色"),
            self.tr("设置副字幕的颜色"),
        )

        self.subOutlineColorCard = ColorSettingCard(
            QColor(0, 0, 0),
            FIF.PALETTE,
            self.tr("副字幕描边颜色"),
            self.tr("设置副字幕的边框颜色"),
        )

        self.subOutlineSizeCard = DoubleSpinBoxSettingCard(
            FIF.ZOOM,
            self.tr("副字幕描边粗细（软件值）"),
            self.tr("越大越粗，范围0–100；主字幕换算不修改副字幕效果"),
            minimum=0.0,
            maximum=100.0,
            decimals=1,
        )

        self.subOutlineOpacityCard = SpinBoxSettingCard(
            FIF.PALETTE,
            self.tr("副字幕描边不透明度"),
            self.tr("0% 为全透明，100% 为完全不透明"),
            minimum=0,
            maximum=100,
        )
        self.subOutlineOpacityCard.spinBox.setValue(100)

        self.subShadowColorCard = ColorSettingCard(
            QColor(0, 0, 0),
            FIF.PALETTE,
            self.tr("副字幕阴影颜色"),
            self.tr("设置副字幕的阴影颜色"),
        )

        self.subShadowOpacityCard = SpinBoxSettingCard(
            FIF.PALETTE,
            self.tr("副字幕阴影不透明度"),
            self.tr("0% 为全透明，100% 为完全不透明"),
            minimum=0,
            maximum=100,
        )
        self.subShadowOpacityCard.spinBox.setValue(80)

        self.subShadowBlurCard = SpinBoxSettingCard(
            FIF.ZOOM,
            self.tr("副字幕阴影模糊度"),
            self.tr("剪映风格百分比，0% 清晰，100% 最模糊"),
            minimum=0,
            maximum=100,
        )
        self.subShadowBlurCard.spinBox.setValue(15)

        self.subShadowSizeCard = DoubleSpinBoxSettingCard(
            FIF.ZOOM,
            self.tr("副字幕阴影距离"),
            self.tr("直接填写剪映距离；内部自动换算为 ASS 阴影偏移"),
            minimum=0.0,
            maximum=100.0,
            decimals=1,
        )
        self.subShadowSizeCard.spinBox.setValue(5.0)

        self.subShadowAngleCard = SpinBoxSettingCard(
            FIF.ALIGNMENT,
            self.tr("副字幕阴影角度"),
            self.tr("范围 -180° 到 180°，-45° 指向右下"),
            minimum=-180,
            maximum=180,
        )
        self.subShadowAngleCard.spinBox.setValue(-45)

        # 预览设置
        self.previewTextCard = ComboBoxSettingCard(
            FIF.MESSAGE,
            self.tr("预览文字"),
            self.tr("设置预览显示的文字内容"),
            texts=PERVIEW_TEXTS.keys(),
            parent=self.previewGroup,
        )

        self.orientationCard = ComboBoxSettingCard(
            FIF.LAYOUT,
            self.tr("预览方向"),
            self.tr("设置预览图片的显示方向"),
            texts=["横屏", "竖屏"],
            parent=self.previewGroup,
        )

        self.previewImageCard = PushSettingCard(
            self.tr("选择图片"),
            FIF.PHOTO,
            self.tr("预览背景"),
            self.tr("选择预览使用的背景图片"),
            parent=self.previewGroup,
        )

    def _initLayout(self):
        """初始化布局"""
        # 添加卡片到组
        self.layoutGroup.addSettingCard(self.layoutCard)
        self.layoutGroup.addSettingCard(self.verticalSpacingCard)
        self.layoutGroup.addSettingCard(self.capcutConvertCard)
        self.mainGroup.addSettingCard(self.mainFontCard)
        self.mainGroup.addSettingCard(self.mainSizeCard)
        self.mainGroup.addSettingCard(self.mainSpacingCard)
        self.mainGroup.addSettingCard(self.mainColorCard)
        self.mainGroup.addSettingCard(self.mainOutlineColorCard)
        self.mainGroup.addSettingCard(self.mainOutlineOpacityCard)
        self.mainGroup.addSettingCard(self.mainOutlineSizeCard)
        self.mainGroup.addSettingCard(self.mainShadowColorCard)
        self.mainGroup.addSettingCard(self.mainShadowOpacityCard)
        self.mainGroup.addSettingCard(self.mainShadowBlurCard)
        self.mainGroup.addSettingCard(self.mainShadowSizeCard)
        self.mainGroup.addSettingCard(self.mainShadowAngleCard)

        self.subGroup.addSettingCard(self.subFontCard)
        self.subGroup.addSettingCard(self.subSizeCard)
        self.subGroup.addSettingCard(self.subSpacingCard)
        self.subGroup.addSettingCard(self.subColorCard)
        self.subGroup.addSettingCard(self.subOutlineColorCard)
        self.subGroup.addSettingCard(self.subOutlineOpacityCard)
        self.subGroup.addSettingCard(self.subOutlineSizeCard)
        self.subGroup.addSettingCard(self.subShadowColorCard)
        self.subGroup.addSettingCard(self.subShadowOpacityCard)
        self.subGroup.addSettingCard(self.subShadowBlurCard)
        self.subGroup.addSettingCard(self.subShadowSizeCard)
        self.subGroup.addSettingCard(self.subShadowAngleCard)

        self.previewGroup.addSettingCard(self.previewTextCard)
        self.previewGroup.addSettingCard(self.orientationCard)
        self.previewGroup.addSettingCard(self.previewImageCard)

        # 添加组到布局
        self.settingsLayout.addWidget(self.layoutGroup)
        self.settingsLayout.addWidget(self.mainGroup)
        self.settingsLayout.addWidget(self.subGroup)
        self.settingsLayout.addWidget(self.previewGroup)
        self.settingsLayout.addStretch(1)

        # 添加左右两侧到主布局
        self.hBoxLayout.addWidget(self.settingsScrollArea)
        self.hBoxLayout.addWidget(self.previewCard)

    def _initStyle(self):
        """初始化样式"""
        self.settingsWidget.setObjectName("settingsWidget")
        self.setStyleSheet(
            """        
            SubtitleStyleInterface, #settingsWidget {
                background-color: transparent;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """
        )

    def __setValues(self):
        """设置初始值"""
        # 设置字幕排布
        self.layoutCard.comboBox.setCurrentText(cfg.get(cfg.subtitle_layout))
        # 设置字幕样式
        self.styleNameComboBox.comboBox.setCurrentText(cfg.get(cfg.subtitle_style_name))

        # 获取样式目录下的所有txt文件名
        style_files = [f.stem for f in SUBTITLE_STYLE_PATH.glob("*.txt")]
        if "default" in style_files:
            style_files.insert(0, style_files.pop(style_files.index("default")))
        else:
            style_files.insert(0, "default")
            self.saveStyle("default")
        self.styleNameComboBox.comboBox.addItems(style_files)

        # 加载默认样式
        subtitle_style_name = cfg.get(cfg.subtitle_style_name)
        if subtitle_style_name in style_files:
            self.loadStyle(subtitle_style_name)
            self.styleNameComboBox.comboBox.setCurrentText(subtitle_style_name)
        else:
            self.loadStyle(style_files[0])
            self.styleNameComboBox.comboBox.setCurrentText(style_files[0])

    def connectSignals(self):
        signalBus.customer_template_applied.connect(self.reload_customer_style)
        """连接所有设置变更的信号到预览更新函数"""
        # 字幕排布
        self.layoutCard.currentTextChanged.connect(self.onSettingChanged)
        self.layoutCard.currentTextChanged.connect(
            lambda: cfg.set(cfg.subtitle_layout, self.layoutCard.comboBox.currentText())
        )
        # 垂直间距
        self.verticalSpacingCard.spinBox.valueChanged.connect(self.onSettingChanged)

        # 主字幕样式
        self.mainFontCard.currentTextChanged.connect(self.onSettingChanged)
        self.mainSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainSpacingCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainColorCard.colorChanged.connect(self.onSettingChanged)
        self.mainOutlineColorCard.colorChanged.connect(self.onSettingChanged)
        self.mainOutlineOpacityCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainOutlineSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainShadowColorCard.colorChanged.connect(self.onSettingChanged)
        self.mainShadowOpacityCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainShadowBlurCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainShadowSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.mainShadowAngleCard.spinBox.valueChanged.connect(self.onSettingChanged)

        # 副字幕样式
        self.subFontCard.currentTextChanged.connect(self.onSettingChanged)
        self.subSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subSpacingCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subColorCard.colorChanged.connect(self.onSettingChanged)
        self.subOutlineColorCard.colorChanged.connect(self.onSettingChanged)
        self.subOutlineOpacityCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subOutlineSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subShadowColorCard.colorChanged.connect(self.onSettingChanged)
        self.subShadowOpacityCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subShadowBlurCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subShadowSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)
        self.subShadowAngleCard.spinBox.valueChanged.connect(self.onSettingChanged)

        # 预览设置
        self.previewTextCard.currentTextChanged.connect(self.onSettingChanged)
        self.orientationCard.currentTextChanged.connect(self.onOrientationChanged)
        self.previewImageCard.clicked.connect(self.selectPreviewImage)

        # 连接样式切换信号
        self.styleNameComboBox.currentTextChanged.connect(self.loadStyle)
        self.newStyleButton.clicked.connect(self.createNewStyle)
        self.openStyleFolderButton.clicked.connect(self.on_open_style_folder_clicked)

        # 连接字幕排布信号
        self.layoutCard.comboBox.currentTextChanged.connect(
            signalBus.subtitle_layout_changed
        )
        signalBus.subtitle_layout_changed.connect(self.on_subtitle_layout_changed)

    def on_open_style_folder_clicked(self):
        """打开样式文件夹"""
        if sys.platform == "win32":
            os.startfile(SUBTITLE_STYLE_PATH)
        elif sys.platform == "darwin":  # macOS
            subprocess.run(["open", SUBTITLE_STYLE_PATH])
        else:  # Linux
            subprocess.run(["xdg-open", SUBTITLE_STYLE_PATH])

    def on_subtitle_layout_changed(self, layout: str):
        cfg.subtitle_layout.value = layout
        self.layoutCard.setCurrentText(layout)

    def onOrientationChanged(self):
        """当预览方向改变时调用"""
        orientation = self.orientationCard.comboBox.currentText()
        preview_image = (
            DEFAULT_BG_LANDSCAPE if orientation == "横屏" else DEFAULT_BG_PORTRAIT
        )
        cfg.set(cfg.subtitle_preview_image, str(Path(preview_image["path"])))
        self.updatePreview()

    def onSettingChanged(self, *_args):
        """当任何设置改变时调用"""
        # 如果正在加载样式，不触发更新
        if self._loading_style:
            return

        # 先保存，避免预览线程异常或拖动过快时丢失参数。
        current_style = self.styleNameComboBox.comboBox.currentText()
        try:
            self.saveStyle(current_style or 'default')
        except (OSError, RuntimeError) as exc:
            InfoBar.error('共享模板未保存', str(exc), duration=6000, parent=self)
            return
        self.updatePreview()

    def openCapcutConversion(self):
        dialog = CapcutCalibrationDialog(self)
        if not dialog.exec_():
            return
        values = dialog.values()
        # Always start from the frozen measured baseline, not arbitrary current styling.
        baseline = SUBTITLE_STYLE_PATH / "测试卷自己.txt"
        if not baseline.exists():
            InfoBar.error("无法换算", "找不到测试卷自己模板", parent=self)
            return
        name = "剪映换算"
        index = 1
        while (SUBTITLE_STYLE_PATH / f"{name}.txt").exists():
            name = f"剪映换算_{index}"
            index += 1
        self.loadStyle("测试卷自己")
        self._loading_style = True
        try:
            self.mainSizeCard.spinBox.setValue(values['size'])
            margin = calibrated_margin(values['y'], values['height'])
            self.verticalSpacingCard.spinBox.setValue(max(0, margin))
            self._conversion_canvas = (values['width'], values['height'])
            self.mainOutlineSizeCard.spinBox.setValue(values['outline'] * 4.8)
            self.mainOutlineOpacityCard.spinBox.setValue(round(values['outline_opacity']))
            self.mainShadowOpacityCard.spinBox.setValue(round(values['shadow_opacity']))
            self.mainShadowBlurCard.spinBox.setValue(round(values['blur'] * 82 / 15))
            self.mainShadowSizeCard.spinBox.setValue(values['distance'] * 6.2 / 5)
            self.mainShadowAngleCard.spinBox.setValue(round(values['angle']))
            self.saveStyle(name)
            self.styleNameComboBox.addItem(name)
            self.styleNameComboBox.comboBox.setCurrentText(name)
        finally:
            self._loading_style = False
        cfg.set(cfg.subtitle_style_name, name)
        self.updatePreview()
        InfoBar.success("已换算并保存", f"已应用新模板：{name}", duration=4000, parent=self)

    def selectPreviewImage(self):
        """选择预览背景图片"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择背景图片"),
            "",
            self.tr("图片文件") + " (*.png *.jpg *.jpeg)",
        )
        if file_path:
            cfg.set(cfg.subtitle_preview_image, file_path)
            self.updatePreview()

    @staticmethod
    def _ass_color(color: QColor, opacity: int = 100) -> str:
        """Convert QColor + user-facing opacity to ASS AABBGGRR."""
        opacity = max(0, min(100, int(opacity)))
        ass_alpha = round(255 * (100 - opacity) / 100)
        return f"&H{ass_alpha:02X}{color.blue():02X}{color.green():02X}{color.red():02X}"

    @staticmethod
    def _number(value: float) -> str:
        """Format numeric ASS fields without noisy trailing zeroes."""
        text = f"{float(value):.2f}".rstrip("0").rstrip(".")
        return "0" if text == "-0" else text

    @staticmethod
    def _capcut_font_to_ass(value: float) -> float:
        return float(value) * CAPCUT_FONT_TO_ASS

    @staticmethod
    def _ass_font_to_capcut(value: float) -> float:
        return float(value) / CAPCUT_FONT_TO_ASS

    @staticmethod
    def _capcut_outline_to_ass(value: float) -> float:
        return float(value) * CAPCUT_OUTLINE_TO_ASS

    @staticmethod
    def _parse_outline_widths(style_content: str):
        widths = {}
        for line in style_content.splitlines():
            match = OUTLINE_RE.match(line.strip())
            if match:
                widths[match.group(1).strip()] = max(0.0, float(match.group(2)))
        return widths

    @staticmethod
    def _load_ass_color(card, value: str, opacity_card=None):
        value = value.strip()
        if not value.upper().startswith("&H"):
            return
        color_hex = value[2:].strip().zfill(8)[-8:]
        try:
            ass_alpha = int(color_hex[0:2], 16)
            blue = int(color_hex[2:4], 16)
            green = int(color_hex[4:6], 16)
            red = int(color_hex[6:8], 16)
        except ValueError:
            return
        card.setColor(QColor(red, green, blue))
        if opacity_card is not None:
            opacity_card.spinBox.setValue(round((255 - ass_alpha) * 100 / 255))

    def generateAssStyles(self) -> str:
        """生成 ASS 样式及 VideoCaptioner 扩展效果参数。"""
        style_format = "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding"
        vertical_spacing = int(self.verticalSpacingCard.spinBox.value())

        main_font = self.mainFontCard.currentText()
        main_size = self._number(
            self._capcut_font_to_ass(self.mainSizeCard.spinBox.value())
        )
        main_color = self._ass_color(self.mainColorCard.colorPicker.color)
        main_outline_color = self._ass_color(
            self.mainOutlineColorCard.colorPicker.color,
            self.mainOutlineOpacityCard.spinBox.value(),
        )
        main_shadow_color = self._ass_color(
            self.mainShadowColorCard.colorPicker.color,
            self.mainShadowOpacityCard.spinBox.value(),
        )
        main_spacing = self.mainSpacingCard.spinBox.value()
        main_outline_width = self.mainOutlineSizeCard.spinBox.value()
        main_outline_size = self._number(
            self._capcut_outline_to_ass(main_outline_width)
        )
        main_shadow_distance = self.mainShadowSizeCard.spinBox.value()
        main_shadow_blur = self.mainShadowBlurCard.spinBox.value()
        main_shadow_angle = self.mainShadowAngleCard.spinBox.value()

        sub_font = self.subFontCard.currentText()
        sub_size = self._number(
            self._capcut_font_to_ass(self.subSizeCard.spinBox.value())
        )
        sub_color = self._ass_color(self.subColorCard.colorPicker.color)
        sub_outline_color = self._ass_color(
            self.subOutlineColorCard.colorPicker.color,
            self.subOutlineOpacityCard.spinBox.value(),
        )
        sub_shadow_color = self._ass_color(
            self.subShadowColorCard.colorPicker.color,
            self.subShadowOpacityCard.spinBox.value(),
        )
        sub_spacing = self.subSpacingCard.spinBox.value()
        sub_outline_width = self.subOutlineSizeCard.spinBox.value()
        sub_outline_size = self._number(
            self._capcut_outline_to_ass(sub_outline_width)
        )
        sub_shadow_distance = self.subShadowSizeCard.spinBox.value()
        sub_shadow_blur = self.subShadowBlurCard.spinBox.value()
        sub_shadow_angle = self.subShadowAngleCard.spinBox.value()

        # Shadow=0 keeps old renderers from adding a second diagonal shadow.
        # ass_effects.py applies directional x/y shadow tags to each event.
        # Jianying's bundled "system font" already uses a Medium face.  Do not
        # apply ASS bold on top of it, otherwise the glyphs and outline become
        # visibly heavier than Jianying's bold_width=0 output.
        main_style = f"Style: Default,{main_font},{main_size},{main_color},&H000000FF,{main_outline_color},{main_shadow_color},0,0,0,0,100,100,{main_spacing},0,1,{main_outline_size},0,2,10,10,{vertical_spacing},1"
        sub_style = f"Style: Secondary,{sub_font},{sub_size},{sub_color},&H000000FF,{sub_outline_color},{sub_shadow_color},0,0,0,0,100,100,{sub_spacing},0,1,{sub_outline_size},0,2,10,10,{vertical_spacing},1"
        effects = (
            f"; VideoCaptionerEffect: Default,blur={main_shadow_blur},distance={main_shadow_distance},angle={main_shadow_angle}\n"
            f"; VideoCaptionerEffect: Secondary,blur={sub_shadow_blur},distance={sub_shadow_distance},angle={sub_shadow_angle}"
        )
        outlines = (
            f"; VideoCaptionerOutline: Default,width={self._number(main_outline_width)}\n"
            f"; VideoCaptionerOutline: Secondary,width={self._number(sub_outline_width)}"
        )
        canvas = getattr(self, '_conversion_canvas', None)
        metadata = f"\n; VideoCaptionerCalibrationCanvas: {canvas[0]},{canvas[1]}" if canvas else ""
        generated = f"[V4+ Styles]\n{style_format}\n{main_style}\n{sub_style}\n{effects}\n{outlines}{metadata}"
        # The editor has no horizontal-margin controls. Preserve those fields
        # and customer-only wrap metadata instead of silently resetting them.
        loaded = getattr(self, '_loaded_style_content', '')
        margins = {}
        extras = []
        for line in loaded.splitlines():
            if line.startswith('Style:'):
                fields = line.split(',')
                if len(fields) > 20:
                    margins[fields[0]] = fields[19:21]
            elif line.startswith(('; VideoCaptionerEnglish', '; CustomerReference', '; VideoCaptionerOriginalLanguage:')):
                extras.append(line)
        output = []
        for line in generated.splitlines():
            if line.startswith('Style:'):
                fields = line.split(',')
                if fields[0] in margins:
                    fields[19:21] = margins[fields[0]]
                line = ','.join(fields)
            output.append(line)
        return '\n'.join(output + extras)

    def updatePreview(self):
        """防抖请求预览，快速调参时只渲染最后一次。"""
        self._preview_requested = True
        self.previewTimer.start()

    def _startPreview(self):
        """串行启动 FFmpeg 预览，避免多个 QThread 争用同一缓存文件。"""
        if self.preview_thread is not None and self.preview_thread.isRunning():
            self._preview_requested = True
            return

        self._preview_requested = False
        # 生成 ASS 样式字符串
        style_str = self.generateAssStyles()

        # 获取预览文本
        main_text, sub_text = PERVIEW_TEXTS[self.previewTextCard.comboBox.currentText()]

        # 字幕布局
        layout = self.layoutCard.comboBox.currentText()
        if layout == "译文在上":
            main_text, sub_text = sub_text, main_text
        elif layout == "原文在上":
            main_text, sub_text = main_text, sub_text
        elif layout == "仅译文":
            main_text, sub_text = sub_text, None
        elif layout == "仅原文":
            main_text, sub_text = main_text, None

        # 获取预览方向
        orientation = self.orientationCard.comboBox.currentText()
        default_preview = DEFAULT_BG_LANDSCAPE if orientation == "横屏" else DEFAULT_BG_PORTRAIT
        
        # 检查是否存在用户自定义背景图片
        user_bg_path = cfg.get(cfg.subtitle_preview_image)
        if user_bg_path and Path(user_bg_path).exists():
            path = user_bg_path
            # 可以保持默认宽高或获取实际图片宽高
            width = default_preview["width"]
            height = default_preview["height"]
        else:
            path = default_preview["path"]
            width = default_preview["width"]
            height = default_preview["height"]

        # 创建预览线程
        if getattr(self, '_conversion_canvas', None):
            width, height = self._conversion_canvas
            # Preview at proportional dimensions without allocating a 32k image.
            scale = min(1.0, 1280 / max(width, height))
            width, height = max(16, round(width * scale)), max(16, round(height * scale))
        self.preview_thread = PreviewThread(
            style_str=style_str,
            preview_text=(main_text, sub_text),
            bg_path=path,
            width=width,
            height=height,
        )
        self.preview_thread.previewReady.connect(self.onPreviewReady)
        self.preview_thread.finished.connect(self._onPreviewFinished)
        self.preview_thread.start()

    def _onPreviewFinished(self):
        """当调参发生在渲染期间时，立即补做最新一次预览。"""
        thread = self.preview_thread
        self.preview_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._preview_requested:
            self.previewTimer.start(0)

    def onPreviewReady(self, preview_path):
        """预览图片生成完成的回调"""
        self.previewImage.setImage(preview_path)
        self.updatePreviewImage()

    def updatePreviewImage(self):
        """更新预览图片"""
        height = int(self.previewTopWidget.height() * 0.98)
        width = int(self.previewTopWidget.width() * 0.98)
        self.previewImage.scaledToWidth(width)
        if self.previewImage.height() > height:
            self.previewImage.scaledToHeight(height)
        self.previewImage.setBorderRadius(8, 8, 8, 8)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.updatePreviewImage()

    def showEvent(self, event):
        """窗口显示事件"""
        super().showEvent(event)
        self.updatePreviewImage()

    def reload_customer_style(self):
        """Load the selected customer's saved preset, never save the old controls."""
        name = cfg.get(cfg.subtitle_style_name)
        combo = self.styleNameComboBox.comboBox
        previous = combo.blockSignals(True)
        try:
            if combo.findText(name) < 0:
                combo.addItem(name)
            combo.setCurrentText(name)
        finally:
            combo.blockSignals(previous)
        self.loadStyle(name)

    def loadStyle(self, style_name):
        """加载指定样式"""
        style_path = SUBTITLE_STYLE_PATH / f"{style_name}.txt"

        if not style_path.exists():
            return

        # 设置标志位，防止触发onSettingChanged
        self._loading_style = True

        with open(style_path, "r", encoding="utf-8") as f:
            style_content = f.read()
        self._loaded_style_content = style_content
        self._style_versions[str(style_path)] = hashlib.sha256(style_path.read_bytes()).hexdigest()

        effects = parse_effects(style_content)
        canvas_match = re.search(r"^; VideoCaptionerCalibrationCanvas: (\d+),(\d+)$", style_content, re.MULTILINE)
        self._conversion_canvas = tuple(map(int, canvas_match.groups())) if canvas_match else None
        outline_widths = self._parse_outline_widths(style_content)

        # 解析样式内容，同时兼容没有扩展参数的旧样式。
        for line in style_content.split("\n"):
            if line.startswith("Style: Default"):
                parts = line.split(",")
                self.mainFontCard.setCurrentText(parts[1])
                self.mainFontCard.setContent('当前模板字体（悬停查看全名）')
                self.mainFontCard.comboBox.setToolTip(parts[1])
                self.mainSizeCard.spinBox.setValue(
                    self._ass_font_to_capcut(float(parts[2]))
                )
                self.verticalSpacingCard.spinBox.setValue(int(float(parts[21])))
                self._load_ass_color(self.mainColorCard, parts[3])
                self._load_ass_color(
                    self.mainOutlineColorCard,
                    parts[5],
                    self.mainOutlineOpacityCard,
                )
                self._load_ass_color(
                    self.mainShadowColorCard,
                    parts[6],
                    self.mainShadowOpacityCard,
                )
                self.mainSpacingCard.spinBox.setValue(float(parts[13]))
                self.mainOutlineSizeCard.spinBox.setValue(
                    outline_widths.get("Default", float(parts[16]) / CAPCUT_OUTLINE_TO_ASS)
                )
                effect = effects.get("Default")
                if effect:
                    self.mainShadowBlurCard.spinBox.setValue(round(effect["blur"]))
                    self.mainShadowSizeCard.spinBox.setValue(effect["distance"])
                    self.mainShadowAngleCard.spinBox.setValue(round(effect["angle"]))
                else:
                    self.mainShadowBlurCard.spinBox.setValue(0)
                    self.mainShadowSizeCard.spinBox.setValue(float(parts[17]))
                    self.mainShadowAngleCard.spinBox.setValue(-45)
            elif line.startswith("Style: Secondary"):
                parts = line.split(",")
                self.subFontCard.setCurrentText(parts[1])
                self.subFontCard.setContent('当前模板字体（悬停查看全名）')
                self.subFontCard.comboBox.setToolTip(parts[1])
                self.subSizeCard.spinBox.setValue(
                    self._ass_font_to_capcut(float(parts[2]))
                )
                self._load_ass_color(self.subColorCard, parts[3])
                self._load_ass_color(
                    self.subOutlineColorCard,
                    parts[5],
                    self.subOutlineOpacityCard,
                )
                self._load_ass_color(
                    self.subShadowColorCard,
                    parts[6],
                    self.subShadowOpacityCard,
                )
                self.subSpacingCard.spinBox.setValue(float(parts[13]))
                self.subOutlineSizeCard.spinBox.setValue(
                    outline_widths.get("Secondary", float(parts[16]) / CAPCUT_OUTLINE_TO_ASS)
                )
                effect = effects.get("Secondary")
                if effect:
                    self.subShadowBlurCard.spinBox.setValue(round(effect["blur"]))
                    self.subShadowSizeCard.spinBox.setValue(effect["distance"])
                    self.subShadowAngleCard.spinBox.setValue(round(effect["angle"]))
                else:
                    self.subShadowBlurCard.spinBox.setValue(0)
                    self.subShadowSizeCard.spinBox.setValue(float(parts[17]))
                    self.subShadowAngleCard.spinBox.setValue(-45)

        cfg.set(cfg.subtitle_style_name, style_name)

        # 重置标志位
        self._loading_style = False

        # 手动更新一次预览
        self.updatePreview()

        # 显示加载成功提示
        InfoBar.success(
            title=self.tr("成功"),
            content=self.tr("已加载样式 ") + style_name,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1500,
            parent=self,
        )

    def createNewStyle(self):
        """创建新样式"""
        dialog = StyleNameDialog(self)
        if dialog.exec():
            style_name = dialog.nameLineEdit.text().strip()
            if not style_name:
                return

            # 检查是否已存在同名样式
            if (SUBTITLE_STYLE_PATH / f"{style_name}.txt").exists():
                InfoBar.warning(
                    title=self.tr("警告"),
                    content=self.tr("样式 ") + style_name + self.tr(" 已存在"),
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2000,
                    parent=self,
                )
                return

            # 保存新样式
            self.saveStyle(style_name)

            # 更新样式列表并选中新样式
            self.styleNameComboBox.addItem(style_name)
            self.styleNameComboBox.comboBox.setCurrentText(style_name)

            # 显示创建成功提示
            InfoBar.success(
                title=self.tr("成功"),
                content=self.tr("已创建新样式 ") + style_name,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )

    def saveStyle(self, style_name):
        """保存样式
        Args:
            style_name (str): 样式名称
        """
        # 确保样式目录存在
        SUBTITLE_STYLE_PATH.mkdir(parents=True, exist_ok=True)

        # 生成样式内容并保存
        style_content = self.generateAssStyles()
        style_path = SUBTITLE_STYLE_PATH / f"{style_name}.txt"

        with shared_lock(style_path):
            current = hashlib.sha256(style_path.read_bytes()).hexdigest() if style_path.exists() else None
            expected = self._style_versions.get(str(style_path))
            if current is not None and current != expected:
                raise RuntimeError('该模板已被其他人修改。请重新加载模板后再编辑，或另存新模板。')
            atomic_text(style_path, style_content)
            self._style_versions[str(style_path)] = hashlib.sha256(style_path.read_bytes()).hexdigest()


class StyleNameDialog(MessageBoxBase):
    """样式名称输入对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = BodyLabel(self.tr("新建样式"), self)
        self.nameLineEdit = LineEdit(self)

        self.nameLineEdit.setPlaceholderText(self.tr("输入样式名称"))
        self.nameLineEdit.setClearButtonEnabled(True)

        # 添加控件到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.nameLineEdit)

        # 设置按钮文本
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))

        self.widget.setMinimumWidth(350)
        self.yesButton.setDisabled(True)
        self.nameLineEdit.textChanged.connect(self._validateInput)

    def _validateInput(self, text):
        self.yesButton.setEnabled(bool(text.strip()))
