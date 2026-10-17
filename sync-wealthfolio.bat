@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo   GnuCash to Wealthfolio Sync Launcher
echo ===================================================
echo.

cmd /c powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-wealthfolio.ps1" %* <nul
exit /b %ERRORLEVEL%
