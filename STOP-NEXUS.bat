@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0STOP-NEXUS.ps1"
echo.
pause
endlocal
