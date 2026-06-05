@echo off
rem Use the folder this .bat lives in, wherever it is.
cd /d "%~dp0"

rem Prefer pythonw.exe (NO console window) so running the watcher never leaves a
rem persistent console/taskbar window. Fall back to python.exe if pythonw missing.
set "PYW=C:\Users\fmani\miniconda3\pythonw.exe"
set "PY=C:\Users\fmani\miniconda3\python.exe"

if not exist "main.py" (
    echo main.py not found in this folder:
    echo %CD%
    echo Make sure all the .py files are next to this .bat.
    echo.
    pause
    exit /b 1
)

if exist "%PYW%" (
    rem 'start "" pythonw main.py' launches detached and returns immediately, so
    rem this cmd window (and the bat) does not stay open holding anything.
    start "" "%PYW%" main.py
) else if exist "%PY%" (
    start "" "%PY%" main.py
) else (
    echo Python not found at:
    echo %PYW%
    echo %PY%
    echo.
    pause
    exit /b 1
)
