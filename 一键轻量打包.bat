@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "APP_ROOT=%~dp0"
set "SEVEN_ZIP=%APP_ROOT%resource\bin\7z.exe"

if not exist "%SEVEN_ZIP%" (
    echo [错误] 未找到内置 7z：%SEVEN_ZIP%
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq VideoCaptioner.exe" 2>nul | find /I "VideoCaptioner.exe" >nul
if not errorlevel 1 (
    echo [提示] VideoCaptioner 当前仍在运行。
    echo 请先完全退出软件，再重新双击本工具，避免设置文件在打包时发生变化。
    pause
    exit /b 2
)

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "PACK_STAMP=%%I"
set "PACK_OUTPUT=%APP_ROOT%..\VideoCaptioner_轻量分发_%PACK_STAMP%.7z"

echo.
echo 正在生成轻量分发包...
echo 输出：%PACK_OUTPUT%
echo.
echo 保留：Medium、Small、字体、字幕模板、当前设置和全部程序功能
echo 排除：历史补丁备份、运行日志、识别缓存、临时工作目录和 Python 缓存
echo 模式：仅封装不压缩，优先缩短打包时间
echo.

pushd "%APP_ROOT%"
"%SEVEN_ZIP%" a -t7z -mx=0 -mmt=on "%PACK_OUTPUT%" ".\*" ^
    "-xr!capcut_style_patch_backups\*" ^
    "-xr!AppData\cache\*" ^
    "-xr!AppData\logs\*" ^
    "-xr!work-dir\*" ^
    "-xr!AppData\models\faster-whisper-medium\._____temp\*" ^
    "-xr!__pycache__\*" ^
    "-xr!*.pyc"
set "PACK_EXIT=%ERRORLEVEL%"
popd

if not "%PACK_EXIT%"=="0" (
    echo.
    echo [失败] 打包过程返回错误码 %PACK_EXIT%
    pause
    exit /b %PACK_EXIT%
)

echo.
echo [完成] 轻量分发包已经生成：
echo %PACK_OUTPUT%
echo.
echo 在另一台电脑上新建文件夹并解压全部内容，然后运行 VideoCaptioner.exe。
pause
exit /b 0
