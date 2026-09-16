@echo off
setlocal
cd /d "%~dp0"
python us_stock_research_tool.py
if errorlevel 1 (
  echo.
  echo Launch failed. Activate the Anaconda environment and run:
  echo pip install -r requirements_us_stock_research.txt
  pause
)
