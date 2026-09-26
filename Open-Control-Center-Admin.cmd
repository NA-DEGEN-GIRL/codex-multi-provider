@echo off
setlocal
set "MANAGER_ROOT=%~dp0"
start "" "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%MANAGER_ROOT%scripts\start-manager-admin.ps1"
exit /b 0
