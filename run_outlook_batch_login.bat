@echo off
setlocal
cd /d "%~dp0"

echo This legacy Outlook-only launcher is disabled.
echo Use run_steam_web_ui.bat for the three supported task flows, or run_steam_outlook_batch.bat for the hardened CLI worker.
echo The old outlook_batch_login.py path does not include the current Steam/Dota/forwarding recovery and status handling.
exit /b 2
