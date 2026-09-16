@echo off
setlocal
cd /d "%~dp0"
python audit_patent_monitor_backfill.py --days 30 --window-days 7 --max-per-company 50 %*
pause
