@echo off
rem Starts Set-UpdateKey.ps1 regardless of the machine's execution policy (only for this call, nothing is changed
rem permanently). Run in an administrator prompt, e.g.  Set-UpdateKey.cmd -Step Add -Key "<base64 public key>"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-UpdateKey.ps1" %*
exit /b %ERRORLEVEL%
