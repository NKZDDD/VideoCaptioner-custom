# VideoCaptioner custom

本仓库保存卡卡字幕工具的定制源码与必要界面资源。

基于 [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) v1.3.3，包含客户模板、剧本绑定、Studio 编辑器、多语言排版、质检与共享盘协作等定制功能。原项目作者：Weifeng；许可证见 LICENSE。

## 在 Mac 上本地转录视频

把定制版源码放在 Mac 上可写的本地目录。在项目根目录打开终端，使用 [Homebrew](https://brew.sh/) 安装依赖。FFmpeg 负责提取音频，whisper.cpp 负责本机识别，aria2 用于界面下载模型，VLC 用于编辑器预览。

```sh
brew install python@3.11 ffmpeg whisper.cpp aria2
brew install --cask vlc
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install -r requirements-macos.txt
.venv/bin/python -m app
```

1. 打开左侧「服务设置」→「转录配置」，选择 `WhisperCpp`，点击「管理模型」。首次可下载多语言 `tiny`（约 78 MB）试跑。下载完成后会自动选中该模型。在「ASR 原语言」选择视频对白的语言，例如「中文」。
2. 已有 whisper.cpp 模型时，也可以点击「打开模型文件夹」，把对应的 GGML 文件（例如 `ggml-small.bin`）放入项目的 `AppData/models/`，然后在「Whisper 本地模型」选择 `small`。
3. 回到「制作中心」，选择「批量转录」，确认「本批转录」显示 `WhisperCpp`。添加本地视频并开始。使用已有客户模板时，原语言按模板执行。
4. 字幕写在素材旁，例如 `采访.mp4` 会生成 `采访.srt`。模型下载后可断网重复转录，无需 API 密钥。以后启动只需在项目目录运行 `.venv/bin/python -m app`。

Mac 使用 [whisper.cpp 的 Homebrew 版本](https://formulae.brew.sh/formula/whisper.cpp)，由运行时选择可用的计算设备。Windows 便携版继续使用原启动器及随包的 `whisper-cpp.exe`。

## 本次上传范围

保留 app 源码、vendored 辅助代码、启动及补丁脚本、图标、字体及随附许可、翻译、ASS 样式和相关技能文件。源码中的 public_api_key 默认值已在此副本清空。

排除 API 配置、客户模板数据、素材和成片、日志缓存、历史补丁备份、嵌入式 Python、FFmpeg/VLC、模型以及本机启动缓存包。运行环境和工具需另行准备，因此本仓库不是完整的便携可运行包。

requirements-upstream-v1.3.3.txt 为原项目依赖清单，供恢复环境时参考；定制功能可能需要额外依赖。原目录的程序和配置保持原样。

导出日期：2026-11-18（Asia/Shanghai）。
