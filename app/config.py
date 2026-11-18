import logging
import os
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

VERSION = "v1.3.3"
YEAR = 2025
APP_NAME = "VideoCaptioner"
AUTHOR = "Weifeng"

HELP_URL = "https://github.com/WEIFENG2333/VideoCaptioner"
GITHUB_REPO_URL = "https://github.com/WEIFENG2333/VideoCaptioner"
RELEASE_URL = "https://github.com/WEIFENG2333/VideoCaptioner/releases/latest"
FEEDBACK_URL = "https://github.com/WEIFENG2333/VideoCaptioner/issues"

# 路径
ROOT_PATH = Path(__file__).parent

SHARED_RESOURCE_PATH = ROOT_PATH.parent / "resource"
_local_resource = os.environ.get('VC_LOCAL_RESOURCE_ROOT', '')
_resource_candidate = Path(_local_resource) if _local_resource else None
RESOURCE_PATH = (_resource_candidate if _resource_candidate and
    (_resource_candidate.parent/'READY').is_file() and
    (_resource_candidate/'bin/ffmpeg.exe').is_file() and
    (_resource_candidate/'assets/logo.png').is_file() else SHARED_RESOURCE_PATH)
APPDATA_PATH = ROOT_PATH.parent / "AppData"
SHARED_APPDATA_PATH = APPDATA_PATH
SESSION_ID = uuid.uuid4().hex
_installation = hashlib.sha256(str(ROOT_PATH.parent.resolve()).lower().encode('utf-8')).hexdigest()[:12]
LOCAL_PROFILE_PATH = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'VideoCaptioner' / _installation
SESSION_PATH = LOCAL_PROFILE_PATH / 'sessions' / SESSION_ID
SESSION_PATH.mkdir(parents=True, exist_ok=True)
WORK_PATH = SESSION_PATH / 'work-dir'


BIN_PATH = RESOURCE_PATH / "bin"
ASSETS_PATH = RESOURCE_PATH / "assets"
SUBTITLE_STYLE_PATH = SHARED_RESOURCE_PATH / "subtitle_style"

LOG_PATH = SESSION_PATH / "logs"
SETTINGS_PATH = SESSION_PATH / "settings.json"
CACHE_PATH = SESSION_PATH / "cache"
MODEL_PATH = APPDATA_PATH / "models"

# Every instance has its own writable settings. Reopen with the last saved local
# preferences; on a new workstation inherit the existing shared settings once.
_candidates = sorted(LOCAL_PROFILE_PATH.glob('sessions/*/settings.json'),
                     key=lambda path: path.stat().st_mtime, reverse=True)
_candidates.append(SHARED_APPDATA_PATH / 'settings.json')
SETTINGS_SEED_PATH = None
for _candidate in _candidates:
    try:
        _settings = json.loads(_candidate.read_text(encoding='utf-8'))
        if not isinstance(_settings, dict):
            continue
        _settings.setdefault('Save', {})['Work_Dir'] = str(WORK_PATH)
        SETTINGS_PATH.write_text(json.dumps(_settings, ensure_ascii=False, indent=2), encoding='utf-8')
        SETTINGS_SEED_PATH = _candidate
        break
    except (OSError, ValueError, TypeError):
        continue

FASER_WHISPER_PATH = BIN_PATH / "Faster-Whisper-XXL"

# 日志配置
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# 环境变量添加 bin 路径，添加到PATH开头以优先使用
os.environ["PATH"] = str(BIN_PATH) + os.pathsep + os.environ["PATH"]
os.environ["PATH"] = str(FASER_WHISPER_PATH) + os.pathsep + os.environ["PATH"]

# 添加 VLC 路径
os.environ["PYTHON_VLC_MODULE_PATH"] = str(BIN_PATH / "vlc")

# 创建路径
for p in [CACHE_PATH, LOG_PATH, WORK_PATH, MODEL_PATH]:
    p.mkdir(parents=True, exist_ok=True)
