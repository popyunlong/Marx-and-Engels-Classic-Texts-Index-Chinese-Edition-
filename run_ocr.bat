@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_ocr.ps1"
echo.
echo Done. Press any key to close.
pause >nul
