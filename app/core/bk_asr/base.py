import io
import json
import os
import tempfile
import threading
import time
import wave
import zlib
from pathlib import Path
from typing import Optional, Union

from app.config import CACHE_PATH
from app.core.storage.cache_manager import CacheManager

from .asr_data import ASRData, ASRDataSeg


class BaseASR:
    SUPPORTED_SOUND_FORMAT = ["flac", "m4a", "mp3", "wav"]
    # 本地客户端限流参数。远程服务仍可能有自己的独立额度。
    RATE_LIMIT_MAX_CALLS = 1000
    RATE_LIMIT_MAX_DURATION = 24 * 3600
    RATE_LIMIT_TIME_WINDOW = 7 * 24 * 3600
    RATE_LIMIT_ENABLED = False
    RATE_LIMIT_STATE_FILE = CACHE_PATH / "asr_local_rate_limit.json"
    _lock = threading.Lock()

    def __init__(
        self,
        audio_path: Optional[Union[str, bytes]] = None,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
    ):
        self.audio_path = audio_path
        self.file_binary = None
        self.use_cache = use_cache
        self._set_data()
        self.cache_manager = CacheManager(str(CACHE_PATH)) if self.use_cache else None

    def _set_data(self):
        if isinstance(self.audio_path, bytes):
            self.file_binary = self.audio_path
        else:
            ext = self.audio_path.split(".")[-1].lower()
            assert (
                ext in self.SUPPORTED_SOUND_FORMAT
            ), f"Unsupported sound format: {ext}"
            assert os.path.exists(self.audio_path), f"File not found: {self.audio_path}"
            with open(self.audio_path, "rb") as f:
                self.file_binary = f.read()
        crc32_value = zlib.crc32(self.file_binary) & 0xFFFFFFFF
        self.crc32_hex = format(crc32_value, "08x")

    def run(self, callback=None, **kwargs) -> ASRData:
        if self.use_cache:
            cached_result = self.cache_manager.get_asr_result(
                self._get_key(), self.__class__.__name__
            )
            if cached_result:
                segments = self._make_segments(cached_result)

                return ASRData(segments)

        if self.RATE_LIMIT_ENABLED:
            audio_duration = self._get_audio_duration_seconds()
            self._check_rate_limit(audio_duration)

        resp_data = self._run(callback, **kwargs)

        if self.RATE_LIMIT_ENABLED:
            self._record_rate_limit_usage(audio_duration)

        if self.use_cache:
            self.cache_manager.set_asr_result(
                self._get_key(), self.__class__.__name__, resp_data
            )

        segments = self._make_segments(resp_data)
        return ASRData(segments)

    def _get_audio_duration_seconds(self) -> float:
        """读取 WAV 音频时长；无法读取时返回 0，不阻断正常转录。"""
        try:
            source = (
                io.BytesIO(self.audio_path)
                if isinstance(self.audio_path, bytes)
                else str(self.audio_path)
            )
            with wave.open(source, "rb") as wav_file:
                frame_rate = wav_file.getframerate()
                if frame_rate <= 0:
                    return 0.0
                return wav_file.getnframes() / float(frame_rate)
        except (OSError, EOFError, wave.Error, TypeError, ValueError):
            return 0.0

    @classmethod
    def _load_rate_limit_events(cls, now: float) -> list[dict]:
        state_path = Path(cls.RATE_LIMIT_STATE_FILE)
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            events = raw.get("events", []) if isinstance(raw, dict) else []
        except (OSError, ValueError, TypeError):
            events = []

        cutoff = now - cls.RATE_LIMIT_TIME_WINDOW
        cleaned = []
        for event in events:
            try:
                timestamp = float(event["timestamp"])
                duration = max(0.0, float(event.get("duration", 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
            if timestamp >= cutoff:
                cleaned.append({"timestamp": timestamp, "duration": duration})
        return cleaned

    @classmethod
    def _save_rate_limit_events(cls, events: list[dict]) -> None:
        state_path = Path(cls.RATE_LIMIT_STATE_FILE)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps({"events": events}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(state_path)

    @classmethod
    def _check_rate_limit(cls, audio_duration: float) -> None:
        now = time.time()
        with cls._lock:
            events = cls._load_rate_limit_events(now)
            used_duration = sum(event["duration"] for event in events)
            if len(events) >= cls.RATE_LIMIT_MAX_CALLS:
                raise RuntimeError(
                    f"本地ASR限制：{cls.RATE_LIMIT_TIME_WINDOW // 86400}天内最多"
                    f"调用{cls.RATE_LIMIT_MAX_CALLS}次"
                )
            if used_duration + max(0.0, audio_duration) > cls.RATE_LIMIT_MAX_DURATION:
                raise RuntimeError(
                    f"本地ASR限制：{cls.RATE_LIMIT_TIME_WINDOW // 86400}天内累计"
                    f"音频最多{cls.RATE_LIMIT_MAX_DURATION // 3600}小时"
                )
            # 顺便清除窗口之外的旧记录。
            cls._save_rate_limit_events(events)

    @classmethod
    def _record_rate_limit_usage(cls, audio_duration: float) -> None:
        now = time.time()
        with cls._lock:
            events = cls._load_rate_limit_events(now)
            events.append(
                {"timestamp": now, "duration": max(0.0, float(audio_duration))}
            )
            cls._save_rate_limit_events(events)

    def _get_key(self):
        """获取缓存key"""
        return self.crc32_hex

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        """将响应数据转换为ASRDataSeg列表"""
        raise NotImplementedError(
            "_make_segments method must be implemented in subclass"
        )

    def _run(self, callback=None, **kwargs) -> dict:
        """运行ASR服务并返回响应数据"""
        raise NotImplementedError("_run method must be implemented in subclass")
