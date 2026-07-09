@echo off
setlocal
cd /d "%~dp0"

set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
set PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright

python -m pip install -i %PIP_INDEX_URL% --trusted-host %PIP_TRUSTED_HOST% -r requirements.txt || exit /b 1
python -m playwright install chromium || exit /b 1

echo.
echo Environment installed.
