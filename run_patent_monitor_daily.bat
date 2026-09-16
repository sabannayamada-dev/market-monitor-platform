@echo off
cd /d "%~dp0"
python -c "import yfinance" >nul 2>&1
if errorlevel 1 python -m pip install -r requirements_patent_monitor.txt
python patent_monitor_daily.py >> "outputs\patent_monitor\daily_task.log" 2>&1
exit /b %errorlevel%
