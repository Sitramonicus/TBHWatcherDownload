@echo off
rem Visible-console runner for troubleshooting. Use this (instead of Run.bat) when
rem you need to SEE startup errors. It uses python.exe so output is printed and the
rem window stays open on a crash. For normal use, prefer Run.bat (no console).
cd /d "%~dp0"

set "PYTHON=C:\Users\fmani\miniconda3\python.exe"
if not exist "%PYTHON%" (
    echo Python not found at:
    echo %PYTHON%
    echo.
    pause
    exit /b 1
)

if not exist "main.py" (
    echo main.py not found in this folder:
    echo %CD%
    echo Make sure all the .py files are next to this .bat.
    echo.
    pause
    exit /b 1
)

"%PYTHON%" main.py
if errorlevel 1 (
    echo.
    echo ===================================================
    echo The program exited with an error ^(see above^).
    echo ===================================================
    pause
)
