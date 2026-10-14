@echo off
rem 大模型量化交易 · 桌面应用程序启动入口
cd /d "%~dp0"

if exist "大模型量化交易.exe" (
    start "" "大模型量化交易.exe" %*
    exit /b
)
if exist "start_app.exe" (
    start "" "start_app.exe" %*
    exit /b
)

rem 兜底：若未编译 exe 则通过 pythonw 启动
if not "%~1"=="hidden" (
    start /min "" "%~f0" hidden
    exit /b
)
start "" ".venv\Scripts\pythonw.exe" "app_platform.py"
exit /b
