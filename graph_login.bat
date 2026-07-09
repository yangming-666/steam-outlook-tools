@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt || exit /b 1

if "%~1"=="" (
  echo Usage: graph_login.bat security_email@example.com
  echo Example: graph_login.bat security@example.com
  exit /b 1
)

set EMAIL=%~1
set CACHE=.graph-token-%EMAIL:@=_% .bin
set CACHE=%CACHE: =%
python graph_mail.py --cache "%CACHE%" login --email "%EMAIL%"
