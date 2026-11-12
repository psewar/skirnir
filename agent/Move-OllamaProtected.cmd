@echo off
rem Starts Move-OllamaProtected.ps1 regardless of the machine's execution policy (only for this call, nothing is changed
rem permanently). Double-click it, or run it in a prompt:  Move-OllamaProtected.cmd [-Check] [-Target <dir>]
rem Without administrator rights the script asks for them (UAC) and continues in an elevated window.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Move-OllamaProtected.ps1" %*
exit /b %ERRORLEVEL%
