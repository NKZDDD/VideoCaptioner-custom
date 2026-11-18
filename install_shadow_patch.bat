@echo off
setlocal
cd /d "%~dp0"
title VideoCaptioner Shadow Patch v3
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=%~dp0VideoCaptioner\runtime\python.exe"
if not exist "%PY%" set "PY=python.exe"
"%PY%" "%~dp0patch_shadow.py"
endlocal
