@echo off
chcp 65001 >nul
title 创建 PM-Quant 桌面快捷方式
cd /d "%~dp0"

echo ==================================================================
echo             PM-Quant 桌面快捷方式一键生成工具
echo ==================================================================
echo.
python main.py shortcut
echo.
echo 按任意键退出...
pause >nul
