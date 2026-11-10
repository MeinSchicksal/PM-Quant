@echo off
rem ASCII-only wrapper. This file used to be UTF-8 and `call` a Chinese-named
rem launcher, which cmd.exe (cp936) misparsed into "dp0...bat is not recognized".
rem Keep this file ASCII and CRLF so double-clicking it always works.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    pause
    exit /b 1
)

python main.py start %*
if errorlevel 1 (
    echo.
    echo [ERROR] PM-Quant exited with an error; see the messages above.
    pause
)
