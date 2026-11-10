@echo off
rem ASCII-only wrapper (see start.bat for why).
cd /d "%~dp0"
python main.py stop
timeout /t 2 >nul
