@echo off
chcp 65001 >nul
title PM-Quant 停止服务
cd /d "%~dp0"

echo ==================================================================
echo               PM-Quant 服务快速停止与清理工具
echo ==================================================================
echo.
echo [*] 正在停止所有 PM-Quant 交易管道与 Web 看板进程...

python main.py stop

echo.
echo [*] 清理完成，3 秒后自动关闭此窗口...
timeout /t 3 >nul
