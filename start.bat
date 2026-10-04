@echo off
REM 启动统一游戏测试平台 MVP
rem 后台先确保用 Anaconda（含 fastapi/uvicorn）的 Python 启动
cd /d "%~dp0"
set PY=PLATFORM_PYTHON
if "%PYTHON_EXE%"=="" (set PYTHON_EXE=F:\Anaconda3\python.exe)
"%PYTHON_EXE%" backend\run.py %*