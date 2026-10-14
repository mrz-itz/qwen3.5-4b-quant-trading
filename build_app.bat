@echo off
rem 编译 Windows 原生应用程序启动器 (基于 Windows 内置 csc 编译器)
cd /d "%~dp0"
echo [1/2] 正在编译 大模型量化交易.exe ...
"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /target:winexe /win32icon:app.ico /r:System.Windows.Forms.dll,System.dll,System.Drawing.dll,Microsoft.CSharp.dll /optimize+ /out:"大模型量化交易.exe" Launcher.cs
if %errorlevel% neq 0 (
    echo [ERROR] 编译失败，请检查错误提示。
    pause
    exit /b %errorlevel%
)
copy /y "大模型量化交易.exe" "start_app.exe" >nul
echo [2/2] 正在刷新桌面快捷方式...
powershell -NoProfile -Command "$s=(New-Object -comObject WScript.Shell).CreateShortcut([System.IO.Path]::Combine([System.Environment]::GetFolderPath('Desktop'),'大模型量化交易.lnk')); $s.TargetPath=(Join-Path (Get-Location) '大模型量化交易.exe'); $s.WorkingDirectory=(Get-Location).Path; $s.IconLocation=(Join-Path (Get-Location) 'app.ico')+ ',0'; $s.Description='大模型量化交易 · 模拟盘控制台'; $s.Save()"
echo ========================================================
echo  大模型量化交易.exe 编译成功！桌面快捷方式已就绪。
echo ========================================================
pause
