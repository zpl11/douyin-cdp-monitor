@echo off
REM 启动 CDP Monitor GUI
cd /d "%~dp0"
python main.py
if errorlevel 1 pause
