@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python market_cap_model_analysis_tool.py
) else (
  py market_cap_model_analysis_tool.py
)
if errorlevel 1 pause
