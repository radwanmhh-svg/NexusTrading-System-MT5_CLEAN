@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0START-NEXUS.ps1"
echo.
pause
endlocal
