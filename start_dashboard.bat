@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动可视化控制台 ...
echo 浏览器将自动打开 http://127.0.0.1:8800
echo 关闭本窗口 = 停止控制台(不影响已启动的盯盘/模型服务)
start "" ".venv\Scripts\python.exe" dashboard.py
timeout /t 2 >nul
