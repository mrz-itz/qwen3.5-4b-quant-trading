' Silent launcher for the quant desktop console (no console flash at all).
' Double-click this file; wscript runs it with window style 0 (hidden).
Option Explicit
Dim fso, base, pythonw, app, shell
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = base & "\.venv\Scripts\pythonw.exe"
app = base & "\app_platform.py"
Set shell = CreateObject("WScript.Shell")
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & app & Chr(34), 0, False
