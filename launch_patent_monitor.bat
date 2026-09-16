@echo off
cd /d "%~dp0"
python -c "import yfinance" >nul 2>&1
if errorlevel 1 (
  echo Initial setup: installing the market feedback dependency...
  python -m pip install -r requirements_patent_monitor.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
)
python patent_monitor_tool.py
