@echo off
setlocal
cd /d "%~dp0"

rem ---- find Python / Pythonw ----
set "PY="
set "PYW="

where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"

where pyw >nul 2>&1 && set "PYW=pyw"
if not defined PYW where pythonw >nul 2>&1 && set "PYW=pythonw"

if not defined PY (
    echo Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

rem ---- install deps once ----
if not exist ".deps_installed" (
    %PY% -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo Dependency install failed.
        pause
        exit /b 1
    )
    type nul > ".deps_installed"
)

rem ---- check index ----
if not exist "data\corpus.sqlite" (
    echo Index not found. Run these first:
    echo   %PY% build_index.py --scan
    echo   %PY% build_index.py
    pause
    exit /b 1
)

rem ---- start app in background ----
rem Browser opening is handled inside app.py.
if defined PYW (
    start "" /b %PYW% app.py
) else (
    rem Fallback: app.py will try to relaunch itself via pythonw.
    start "" /b %PY% app.py
)

endlocal
exit /b 0
