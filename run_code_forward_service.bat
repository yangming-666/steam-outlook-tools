@echo off
cd /d "%~dp0"
python code_forward_service.py --accounts-config graph_accounts.json --config code_forward_config.json --port 8770
pause
