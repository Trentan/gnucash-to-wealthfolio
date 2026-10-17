@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo   Wealthfolio Category Mapping & Backups Editor
echo ===================================================
echo.
echo Launching local editor server at http://127.0.0.1:8765 ...
echo Opening your browser automatically...
echo Keep this terminal window open while using the editor.
echo Press Ctrl+C or close this window to stop the server when done.
echo.

cmd /c python -m gnucash_to_wealthfolio.cli edit %* <nul
exit /b %ERRORLEVEL%
