@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
set "PATH=%~dp0runtime;%~dp0runtime\Library\bin;%~dp0runtime\Scripts;%~dp0tools;%SystemRoot%\System32;%SystemRoot%"
if not exist "%~dp0runtime\python.exe" (
  echo 缺少独立 Python，请先完整解压原始 ZIP。
  pause
  exit /b 1
)
"%~dp0runtime\python.exe" -I -B -u -X utf8 "%~dp0launcher.py"
set "STUDYBUDDY_EXIT=%ERRORLEVEL%"
if not "%STUDYBUDDY_EXIT%"=="0" pause
exit /b %STUDYBUDDY_EXIT%
