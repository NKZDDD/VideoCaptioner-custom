# -*- coding: utf-8 -*-
"""VIDEO_QUALITY_PATCH_V2: shared bitrate/CRF selector."""

from qfluentwidgets import ComboBox

from app.common.config import cfg
from app.common.signal_bus import signalBus


QUALITY_ITEMS = [
    ("跟随原视频码率（推荐）", "source"),
    ("超清画质（CRF 18）", "18"),
    ("高清画质（CRF 20）", "20"),
    ("标准画质（CRF 23）", "23"),
    ("节省空间（CRF 28）", "28"),
]


def fill_video_quality_combo(combo: ComboBox, tr=None):
    combo.clear()
    for text, value in QUALITY_ITEMS:
        combo.addItem(tr(text) if tr else text, userData=value)
    combo.setToolTip(
        (tr("仅硬字幕重编码时生效。跟随原视频码率会尽量保持源片码率和文件体积；CRF 数值越低画质越高。软字幕会复制原视频流。")
         if tr else
         "仅硬字幕重编码时生效。跟随原视频码率会尽量保持源片码率和文件体积；CRF 数值越低画质越高。软字幕会复制原视频流。")
    )
    sync_combo_from_cfg(combo)
    combo.currentIndexChanged.connect(lambda _i, c=combo: _on_combo_changed(c))
    if not getattr(combo, "_vq_bus_connected", False):
        signalBus.video_quality_changed.connect(lambda _v, c=combo: sync_combo_from_cfg(c))
        combo._vq_bus_connected = True


def sync_combo_from_cfg(combo: ComboBox):
    quality = str(cfg.video_quality.value)
    index = combo.findData(quality)
    combo.blockSignals(True)
    combo.setCurrentIndex(index if index >= 0 else 0)
    combo.blockSignals(False)


def _on_combo_changed(combo: ComboBox):
    value = combo.currentData()
    if not value:
        return
    if str(cfg.video_quality.value) == str(value):
        return
    cfg.set(cfg.video_quality, str(value))
    signalBus.video_quality_changed.emit(str(value))
