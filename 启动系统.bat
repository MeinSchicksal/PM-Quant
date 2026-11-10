@echo off
chcp 65001 >nul
title PM-Quant 量化做市系统
cd /d "%~dp0"

echo ==================================================================
echo        PM-Quant 全自动量化做市与高频对冲系统 (一键启动)
echo ==================================================================
echo.
echo [*] 正在检查运行环境...

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python 环境！请确保已安装 Python 并添加至系统环境变量 PATH。
    pause
    exit /b 1
)

echo [*] 正在拉起看板 Web 服务 (http://127.0.0.1:8900/#market)...
echo [*] 正在启动 1Hz 实时交易管道 (Pipeline Driver)...
echo [*] 即将自动在默认浏览器中打开监控看板...
echo [*] 提示: 关闭此窗口或按 Ctrl+C 即可一键安全停止所有服务。
echo ------------------------------------------------------------------
echo.

python main.py start

if errorlevel 1 (
    echo.
    echo [提示] 进程已退出。如遇异常请查看上方报错信息。
    pause
)
