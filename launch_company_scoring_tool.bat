@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo Using Python: %PYTHON_EXE%
"%PYTHON_EXE%" company_scoring_tool.py
if errorlevel 1 (
  echo.
  echo Launch failed. Please run:
  echo "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
  pause
)
