@echo off
setlocal
cd /d "%~dp0"

python -m pip show playwright >nul 2>nul
if errorlevel 1 (
  call install_fast_cn.bat || exit /b 1
)

python -m pip show pywinauto >nul 2>nul
if errorlevel 1 (
  call install_fast_cn.bat || exit /b 1
)

set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
python -m playwright install chromium || exit /b 1
python steam_outlook_batch.py %*
