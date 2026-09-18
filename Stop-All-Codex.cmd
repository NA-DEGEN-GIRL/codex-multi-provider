@echo off
setlocal
echo This will force-close Codex, its background processes, and the Codex workspace app.
echo Running Codex tasks will stop. Conversation files and login files are not deleted.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-All-Codex.ps1" %*
set "CODEX_STOP_RESULT=%ERRORLEVEL%"
echo.
if "%CODEX_STOP_RESULT%"=="0" (
  echo Done. You can now open Open-Synced-Codex.cmd.
) else (
  echo Some processes remain. See the report above. If access was denied, run this file as administrator.
)
pause
exit /b %CODEX_STOP_RESULT%
