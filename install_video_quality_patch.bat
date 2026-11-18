@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=%~dp0VideoCaptioner\runtime\python.exe"
if exist "%PY%" (
  "%PY%" "%~dp0patch_video_quality.py"
) else (
  echo Bundled Python was not found.
  echo Put this patch folder inside the VideoCaptioner folder, then run this file again.
  pause
  exit /b 1
)
endlocal
