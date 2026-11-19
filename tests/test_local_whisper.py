import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.bk_asr import whisper_cpp
from app.core.entities import WhisperModelEnum


class WhisperLocalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.model = self.root / "ggml-small.bin"
        self.model.touch()
        self.wav = self.root / "采访 sample.wav"
        with wave.open(str(self.wav), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x00" * 16000)
        for name, value in (("MODEL_PATH", self.root), ("sys", SimpleNamespace(platform="darwin"))):
            mocked = patch.object(whisper_cpp, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def asr(self, **kwargs):
        with patch.object(whisper_cpp.shutil, "which", return_value=sys.executable):
            return whisper_cpp.WhisperCppASR(self.wav, whisper_model=WhisperModelEnum.SMALL, **kwargs)

    def test_mac_and_windows_commands(self):
        local = self.asr(need_word_time_stamp=True)
        output = self.root / "audio.srt"
        args = local._build_command(self.wav, output)
        self.assertEqual(whisper_cpp.whisper_program_name(), "whisper-cli")
        self.assertEqual(args[args.index("--output-file") + 1], str(output.with_suffix("")))
        self.assertIn("-ml", args)
        self.assertNotIn("--no-gpu", args)
        whisper_cpp.sys.platform = "win32"
        self.assertEqual(whisper_cpp.whisper_program_name(), "whisper-cpp")
        self.assertNotIn("--output-file", self.asr()._build_command(self.wav, output))

    def test_missing_model_or_binary_gives_install_hint(self):
        with patch.object(whisper_cpp.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "brew install whisper.cpp"):
                whisper_cpp.WhisperCppASR(self.wav, whisper_model="small")
        self.model.rename(self.root / "ggml-small.en.bin")
        with self.assertRaisesRegex(FileNotFoundError, "ggml-small.bin"):
            self.asr()

    def run_cli(self, script, *, local=None, callback=None):
        local = local or self.asr()
        args = local._build_command
        def command(wav, path):
            return [sys.executable, "-c", script, *args(wav, path)[1:]]
        with patch.object(local, "_build_command", side_effect=command):
            return local.run(callback)

    def test_subprocess_eof_progress_and_srt(self):
        callback = Mock()
        script = (
            "import pathlib,sys\n"
            "print('[00:00:00.000 --> 00:00:00.750]', file=sys.stderr)\n"
            "path = pathlib.Path(sys.argv[sys.argv.index('--output-file')+1]+'.srt')\n"
            "path.write_text('1\\n00:00:00,000 --> 00:00:00,750\\nHello\\n', encoding='utf-8')"
        )
        data = self.run_cli(script, callback=callback)
        self.assertEqual([(s.text, s.start_time, s.end_time) for s in data], [("Hello", 0, 750)])
        callback.assert_any_call(75, "75% 正在转录")
        self.assertEqual(callback.call_args.args[0], 100)

    def test_error_and_missing_output(self):
        with self.assertRaisesRegex(RuntimeError, "model error"):
            self.run_cli("import sys; print('model error', file=sys.stderr); sys.exit(3)")
        with self.assertRaisesRegex(RuntimeError, "没有生成 SRT"):
            self.run_cli("pass")

    def test_windows_reads_const_me_output_next_to_temporary_input(self):
        whisper_cpp.sys.platform = "win32"
        script = (
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[sys.argv.index('-f') + 1]).with_suffix('.srt')\n"
            "path.write_text('1\\n00:00:00,000 --> 00:00:01,000\\nHello\\n', encoding='utf-8')"
        )
        self.assertEqual(self.run_cli(script).segments[0].text, "Hello")
        self.assertFalse(self.wav.with_suffix(".srt").exists())

    def test_cancel_silent_process(self):
        local = self.asr(cancel_requested=Mock(side_effect=[False, True]))
        with self.assertRaisesRegex(RuntimeError, "取消"):
            self.run_cli("import time; time.sleep(5)", local=local)
