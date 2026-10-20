@echo off
REM Install platform dependencies into the local .venv; preserve application data.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
