@echo off
setlocal
cd /d "%~dp0"

python -m pip show playwright >nul 2>nul
if errorlevel 1 (
  python -m pip install -r requirements.txt || exit /b 1
)

python -m playwright install chromium || exit /b 1
python outlook_batch_login.py --pause-after-login %*
