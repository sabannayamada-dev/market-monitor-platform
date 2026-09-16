@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if exist "%PYTHON_EXE%" goto run_app

where python >nul 2>nul
if errorlevel 1 goto no_python
set "PYTHON_EXE=python"

:run_app
echo Starting stock app v5.7.0
echo App: %~dp0app.py
echo Python: %PYTHON_EXE%
"%PYTHON_EXE%" "%~dp0app.py"
if errorlevel 1 (
  echo.
  echo The app stopped with an error.
  pause
)
exit /b

:no_python
echo Python was not found.
echo Install Python 3.12 or adjust PYTHON_EXE in this file.
pause
exit /b 1
