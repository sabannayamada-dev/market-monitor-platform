@echo off
setlocal
cd /d "%~dp0"
python sec_financial_enrichment_tool.py
if errorlevel 1 (
  echo.
  echo Launch failed. Activate the Anaconda environment and run: pip install -r requirements.txt
  pause
)
