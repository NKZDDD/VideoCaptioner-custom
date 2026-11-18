# -*- coding: utf-8 -*-
from pathlib import Path
import shutil
import datetime
import sys


def fail(msg):
    print('ERROR: ' + msg)
    input('Press Enter to exit...')
    raise SystemExit(1)


def backup(path):
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    b = path.with_name(path.name + '.bak_video_quality_' + stamp)
    shutil.copy2(path, b)
    print('Backup: ' + str(b))


def patch_file(path, replacements, label):
    if not path.exists():
        fail('Missing file: ' + str(path))
    s = path.read_text(encoding='utf-8-sig')
    original = s
    for old, new in replacements:
        if old in s:
            s = s.replace(old, new, 1)
    if s == original:
        if 'VIDEO_QUALITY_PATCH_V1' in s or ('video_quality' in s and label == 'config'):
            print('Already patched: ' + label)
            return False
        fail('Could not find expected code in: ' + str(path))
    backup(path)
    path.write_text(s, encoding='utf-8')
    print('Patched: ' + label)
    return True


script_dir = Path(__file__).resolve().parent
candidates = [script_dir, script_dir.parent, script_dir.parent / 'VideoCaptioner']
root = None
for c in candidates:
    if (c / 'app' / 'core' / 'utils' / 'video_utils.py').exists():
        root = c
        break
if root is None:
    # recursive fallback, but only accept an exact old-version structure
    for p in script_dir.rglob('video_utils.py'):
        if p.as_posix().endswith('/app/core/utils/video_utils.py'):
            root = p.parents[4]
            break
if root is None:
    fail('Could not locate VideoCaptioner app folder. Put this patch folder beside the VideoCaptioner folder or inside it.')

print('VideoCaptioner root: ' + str(root))

config = root / 'app' / 'common' / 'config.py'
entities = root / 'app' / 'core' / 'entities.py'
task_factory = root / 'app' / 'core' / 'task_factory.py'
thread = root / 'app' / 'thread' / 'video_synthesis_thread.py'
video_utils = root / 'app' / 'core' / 'utils' / 'video_utils.py'
ui = root / 'app' / 'view' / 'video_synthesis_interface.py'

# Config
patch_file(config, [
('    need_video = ConfigItem("Video", "NeedVideo", True, BoolValidator())\n',
 '    need_video = ConfigItem("Video", "NeedVideo", True, BoolValidator())\n    # VIDEO_QUALITY_PATCH_V1: H.264/VP9 CRF, lower value = higher quality/larger file\n    video_quality = OptionsConfigItem(\n        "Synthesis", "VideoQuality", "23", OptionsValidator(["18", "20", "23", "28"]),\n    )\n')], 'config')

# Entities
patch_file(entities, [
('class SynthesisConfig:\n    """视频合成配置类"""\n\n    need_video: bool = True\n    soft_subtitle: bool = True\n',
 'class SynthesisConfig:\n    """视频合成配置类"""\n\n    need_video: bool = True\n    soft_subtitle: bool = True\n    # VIDEO_QUALITY_PATCH_V1\n    video_quality: str = "23"\n')], 'entities')

# Task factory
patch_file(task_factory, [
('        config = SynthesisConfig(\n            need_video=cfg.need_video.value,\n            soft_subtitle=cfg.soft_subtitle.value,\n        )\n',
 '        config = SynthesisConfig(\n            need_video=cfg.need_video.value,\n            soft_subtitle=cfg.soft_subtitle.value,\n            video_quality=cfg.video_quality.value,\n        )\n')], 'task_factory')

# Thread
patch_file(thread, [
('            soft_subtitle = self.task.synthesis_config.soft_subtitle\n            need_video = self.task.synthesis_config.need_video\n',
 '            soft_subtitle = self.task.synthesis_config.soft_subtitle\n            need_video = self.task.synthesis_config.need_video\n            video_quality = self.task.synthesis_config.video_quality\n'),
('                soft_subtitle=soft_subtitle,\n                progress_callback=self.progress_callback,\n',
 '                soft_subtitle=soft_subtitle,\n                crf=int(video_quality),\n                progress_callback=self.progress_callback,\n')], 'thread')

# Video utils: add CRF argument and inject it into hard-subtitle command.
patch_file(video_utils, [
('    vcodec: str = "libx264",\n    soft_subtitle: bool = False,\n',
 '    vcodec: str = "libx264",\n    soft_subtitle: bool = False,\n    crf: int = 23,\n'),
('                "-preset",\n                quality,\n                "-vf",\n',
 '                "-preset",\n                quality,\n                "-crf",\n                str(crf),\n                "-vf",\n')], 'video_utils')

# UI imports + quality row + save/load + signal.
patch_file(ui, [
('    BodyLabel, CardWidget, CommandBar\n',
 '    BodyLabel, CardWidget, CommandBar, ComboBox\n'),
('        self.main_layout.addWidget(self.config_card)\n',
 '''        # VIDEO_QUALITY_PATCH_V1: video output quality selector\n        self.video_quality_layout = QHBoxLayout()\n        self.video_quality_layout.setSpacing(15)\n        self.video_quality_label = BodyLabel(self.tr("视频质量"), self)\n        self.video_quality_combo = ComboBox(self)\n        self.video_quality_combo.addItem(self.tr("超高质量（CRF 18）"), "18")\n        self.video_quality_combo.addItem(self.tr("高质量（CRF 20）"), "20")\n        self.video_quality_combo.addItem(self.tr("标准质量（CRF 23）"), "23")\n        self.video_quality_combo.addItem(self.tr("节省空间（CRF 28）"), "28")\n        self.video_quality_combo.setToolTip(self.tr("数值越低画质越高，文件通常越大；数值越高压缩越强"))\n        self.video_quality_layout.addWidget(self.video_quality_label)\n        self.video_quality_layout.addWidget(self.video_quality_combo)\n        self.config_layout.addLayout(self.video_quality_layout)\n\n        self.main_layout.addWidget(self.config_card)\n'''),
('        self.soft_subtitle_action.setChecked(cfg.soft_subtitle.value)\n        self.need_video_action.setChecked(cfg.need_video.value)\n',
 '        self.soft_subtitle_action.setChecked(cfg.soft_subtitle.value)\n        self.need_video_action.setChecked(cfg.need_video.value)\n        quality = str(cfg.video_quality.value)\n        index = self.video_quality_combo.findData(quality)\n        self.video_quality_combo.setCurrentIndex(index if index >= 0 else 2)\n'),
('        signalBus.need_video_changed.connect(self.on_need_video_changed)\n',
 '        signalBus.need_video_changed.connect(self.on_need_video_changed)\n        self.video_quality_combo.currentIndexChanged.connect(self.on_video_quality_changed)\n'),
('    def choose_subtitle_file(self):\n',
 '''    def on_video_quality_changed(self, index):\n        value = self.video_quality_combo.itemData(index)\n        if value:\n            cfg.set(cfg.video_quality, value)\n\n    def choose_subtitle_file(self):\n''')], 'ui')

# Clear pycache files for modified app modules.
for p in root.rglob('__pycache__'):
    if p.is_dir():
        try:
            shutil.rmtree(p)
            print('Removed cache: ' + str(p))
        except Exception:
            pass

# Syntax check using this Python interpreter; packaged runtime will recompile when launched.
import py_compile
for p in [config, entities, task_factory, thread, video_utils, ui]:
    try:
        py_compile.compile(str(p), doraise=True)
        print('Syntax OK: ' + str(p))
    except Exception as e:
        fail('Syntax error in ' + str(p) + ': ' + str(e))

print('')
print('OK: Video quality selector installed.')
print('Options: CRF 18 / 20 / 23 / 28')
print('Important: hard subtitles re-encode video; soft subtitles keep original video stream.')
input('Press Enter to exit...')
