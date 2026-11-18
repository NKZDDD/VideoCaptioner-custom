@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "SRC=%ROOT%字幕阴影补丁\app\view\subtitle_style_interface.py"
set "DST=%ROOT%app\view\subtitle_style_interface.py"
set "CACHE=%ROOT%app\view\__pycache__\subtitle_style_interface.cpython-311.pyc"

if not exist "%SRC%" (
  echo [错误] 找不到补丁文件。
  echo 请确认此 BAT 文件位于 VideoCaptioner 程序根目录。
  pause
  exit /b 1
)
if not exist "%DST%" (
  echo [错误] 找不到 VideoCaptioner 的 app\view\subtitle_style_interface.py
  echo 请把此文件夹放到 VideoCaptioner.exe 所在的根目录后再运行。
  pause
  exit /b 1
)

echo 正在备份原文件...
copy /Y "%DST%" "%DST%.bak" >nul
if errorlevel 1 (
  echo [错误] 无法创建备份，请检查文件是否被占用。
  pause
  exit /b 1
)

echo 正在安装字幕阴影功能...
copy /Y "%SRC%" "%DST%" >nul
if errorlevel 1 (
  echo [错误] 替换失败，请关闭 VideoCaptioner 后重新运行。
  pause
  exit /b 1
)

if exist "%CACHE%" del /Q "%CACHE%" >nul 2>&1

echo.
echo ========================================
echo 字幕阴影功能安装完成！
echo ========================================
echo.
echo 请重新启动 VideoCaptioner。
echo 在「字幕样式」中会看到：
echo   主字幕阴影颜色
echo   主字幕阴影大小
echo   副字幕阴影颜色
echo   副字幕阴影大小
echo.
echo 阴影大小设为 0 = 关闭
pause
