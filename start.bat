@echo off
REM Start the unified test platform.
rem Explicit override, local venv, then Python on PATH; no machine-specific drive.
cd /d "%~dp0"
if defined PYTHON_EXE goto configured
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" backend\run.py %*
  exit /b
)
python backend\run.py %*
exit /b
:configured
"%PYTHON_EXE%" backend\run.py %*
