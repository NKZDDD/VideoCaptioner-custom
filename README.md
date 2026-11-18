# VideoCaptioner custom

本仓库保存卡卡字幕工具的定制源码与必要界面资源。

基于 [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) v1.3.3，包含客户模板、剧本绑定、Studio 编辑器、多语言排版、质检与共享盘协作等定制功能。原项目作者：Weifeng；许可证见 LICENSE。

## 本次上传范围

保留 app 源码、vendored 辅助代码、启动及补丁脚本、图标、字体及随附许可、翻译、ASS 样式和相关技能文件。源码中的 public_api_key 默认值已在此副本清空。

排除 API 配置、客户模板数据、素材和成片、日志缓存、历史补丁备份、嵌入式 Python、FFmpeg/VLC、模型以及本机启动缓存包。运行环境和工具需另行准备，因此本仓库不是完整的便携可运行包。

requirements-upstream-v1.3.3.txt 为原项目依赖清单，供恢复环境时参考；定制功能可能需要额外依赖。原目录的程序和配置保持原样。

导出日期：2026-11-18（Asia/Shanghai）。
