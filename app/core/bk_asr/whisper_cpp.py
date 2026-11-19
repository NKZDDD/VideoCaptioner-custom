import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

from ...config import MODEL_PATH
from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg, ASRData
from .base import BaseASR

logger = setup_logger("whisper_asr")


def whisper_program_name():
    return "whisper-cli" if sys.platform == "darwin" else "whisper-cpp"


def find_model(model):
    if not model:
        raise ValueError("请选择 Whisper 模型")
    name = getattr(model, "value", model)
    exact = MODEL_PATH / f"ggml-{name}.bin"
    if exact.is_file():
        return exact
    if sys.platform != "darwin":
        # Portable Windows installations may use a renamed or quantized model.
        for candidate in sorted(MODEL_PATH.glob(f"*ggml*{name}*.bin")):
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(f"找不到模型 {exact}，请在 WhisperCpp 的「管理模型」中下载")


class WhisperCppASR(BaseASR):
    def __init__(
        self, audio_path, language="en", whisper_cpp_path=None, whisper_model=None,
        use_cache=False, need_word_time_stamp=False, cancel_requested=None,
    ):
        super().__init__(str(audio_path), use_cache=False)
        self.model_path = find_model(whisper_model)
        program = str(whisper_cpp_path or whisper_program_name())
        self.whisper_cpp_path = shutil.which(program)
        if not self.whisper_cpp_path:
            hint = "，请运行 brew install whisper.cpp" if sys.platform == "darwin" else ""
            raise FileNotFoundError(f"找不到 Whisper 程序 {program}{hint}")
        self.const_me = sys.platform == "win32" and Path(program).stem.lower() != "whisper-cli"
        self.need_word_time_stamp = need_word_time_stamp
        self.language = language or "auto"
        self.cancel_requested = cancel_requested

    def _make_segments(self, resp_data: str) -> list[ASRDataSeg]:
        asr_data = ASRData.from_srt(resp_data)
        # 过滤掉纯音乐标记
        filtered_segments = []
        for seg in asr_data.segments:
            text = seg.text.strip()
            # 保留不以【、[、(、（开头的文本
            if not (
                text.startswith("【")
                or text.startswith("[")
                or text.startswith("(")
                or text.startswith("（")
            ):
                filtered_segments.append(seg)
        return filtered_segments

    def _build_command(self, wav_path, output_path):
        command = [self.whisper_cpp_path, "-m", str(self.model_path), "-f", str(wav_path),
                   "-l", self.language, "--output-srt"]
        if not self.const_me:
            command.extend(["--output-file", str(output_path.with_suffix(""))])
            if sys.platform != "darwin":
                command.append("--no-gpu")
            if self.need_word_time_stamp:
                command.extend(["-ml", "1"])
        if self.language == "zh":
            command.extend(["--prompt", "你好，我们需要使用简体中文，以下是普通话的句子。"])
        return command

    def _run(self, callback=None):
        total = self._get_audio_duration_seconds()
        with tempfile.TemporaryDirectory(prefix="whisper-") as folder:
            output = Path(folder) / "audio.srt"
            wav = Path(self.audio_path)
            if self.const_me:
                # Const-me writes next to the input file.
                wav = Path(folder) / "audio.wav"
                shutil.copyfile(self.audio_path, wav)
            command = self._build_command(wav, output)
            logger.info("Whisper: %s", command)
            log = Path(folder) / "whisper.log"
            tail = deque(maxlen=8)
            with log.open("wb") as writer, log.open("r", encoding="utf-8", errors="replace") as reader:
                with subprocess.Popen(command, stdout=writer, stderr=subprocess.STDOUT) as process:
                    try:
                        while True:
                            if self.cancel_requested and self.cancel_requested():
                                raise RuntimeError("转录已取消")
                            done = process.poll() is not None
                            for line in reader:
                                tail.append(line.rstrip())
                                match = re.search(r"--> (\d+):(\d+):(\d+\.\d+)", line)
                                if match and callback and total:
                                    h, m, s = map(float, match.groups())
                                    progress = min(98, int((h * 3600 + m * 60 + s) / total * 100))
                                    callback(progress, f"{progress}% 正在转录")
                            if done:
                                break
                            try:
                                process.wait(timeout=0.25)
                            except subprocess.TimeoutExpired:
                                pass
                    finally:
                        if process.poll() is None:
                            process.kill()
                    if process.returncode:
                        detail = "\n".join(tail)
                        raise RuntimeError(f"Whisper 转录失败（退出码 {process.returncode}）\n{detail}")
            if self.const_me and not output.exists():
                output = Path(str(wav) + ".srt")
            if not output.is_file():
                raise RuntimeError("Whisper 没有生成 SRT 字幕\n" + "\n".join(tail))
            text = output.read_text(encoding="utf-8-sig")
            if callback:
                callback(100, "转录完成")
            return text
