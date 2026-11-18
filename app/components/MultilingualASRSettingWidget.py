from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QLineEdit,
)
from qfluentwidgets import BodyLabel, CardWidget, ComboBox, ComboBoxSettingCard
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    HyperlinkCard,
    RangeSettingCard,
    SettingCardGroup,
    SingleDirectionScrollArea,
    SwitchSettingCard,
)

from ..common.config import cfg
from ..core.entities import TranscribeLanguageEnum
from .EditComboBoxSettingCard import EditComboBoxSettingCard
from .LineEditSettingCard import LineEditSettingCard


class MultilingualASRSettingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        # 创建单向滚动区域和容器
        self.scrollArea = SingleDirectionScrollArea(orient=Qt.Vertical, parent=self)
        self.scrollArea.setStyleSheet(
            "QScrollArea{background: transparent; border: none}"
        )

        self.container = QWidget(self)
        self.container.setStyleSheet("QWidget{background: transparent}")
        self.containerLayout = QVBoxLayout(self.container)

        self.setting_group = SettingCardGroup(self.tr("多语言 ASR（云端服务尚需部署）"), self)

        # 服务地址
        self.base_url_card = LineEditSettingCard(
            cfg.multilingual_asr_base,
            FIF.LINK,
            self.tr("服务地址"),
            self.tr("输入转录服务地址（以 /v1 结尾）"),
            "http://127.0.0.1:8000/v1",
            self.setting_group,
        )

        # API Key
        self.api_key_card = LineEditSettingCard(
            cfg.multilingual_asr_key,
            FIF.FINGERPRINT,
            self.tr("API Key"),
            self.tr("输入转录服务密钥；不使用大模型密钥"),
            "",
            self.setting_group,
        )

        self.api_key_card.lineEdit.setEchoMode(QLineEdit.Password)

        # Model
        self.model_card = LineEditSettingCard(
            cfg.multilingual_asr_model,
            FIF.ROBOT,
            self.tr("转录模型"),
            self.tr("输入服务端提供的模型名称"),
            "由服务端提供",
            self.setting_group,
        )

        # 添加 Language 选择
        self.language_card = ComboBoxSettingCard(
            cfg.transcribe_language,
            FIF.LANGUAGE,
            self.tr("原语言"),
            self.tr("音频的原语言"),
            [lang.value for lang in TranscribeLanguageEnum],
            self.setting_group,
        )

        # 添加 Prompt
        self.prompt_card = LineEditSettingCard(
            cfg.multilingual_asr_dialect,
            FIF.CHAT,
            self.tr("口音提示（可选）"),
            self.tr("例如沙特阿拉伯语、埃及阿拉伯语；实际支持取决于模型"),
            "",
            self.setting_group,
        )

        # 设置最小宽度
        self.base_url_card.lineEdit.setMinimumWidth(200)
        self.api_key_card.lineEdit.setMinimumWidth(200)
        self.model_card.lineEdit.setMinimumWidth(200)
        self.language_card.comboBox.setMinimumWidth(200)
        self.prompt_card.lineEdit.setMinimumWidth(200)

        # 使用 addSettingCard 添加所有卡片到组
        self.setting_group.addSettingCard(self.base_url_card)
        self.setting_group.addSettingCard(self.api_key_card)
        self.setting_group.addSettingCard(self.model_card)
        self.setting_group.addSettingCard(self.language_card)
        self.setting_group.addSettingCard(self.prompt_card)

        # 将设置组添加到容器布局
        self.containerLayout.addWidget(self.setting_group)
        self.containerLayout.addStretch(1)

        # 设置滚动区域
        self.scrollArea.setWidget(self.container)
        self.scrollArea.setWidgetResizable(True)

        # 将滚动区域添加到主布局
        self.main_layout.addWidget(self.scrollArea)
