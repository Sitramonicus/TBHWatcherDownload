@echo off
setlocal
rem Use the folder this .bat lives in, wherever it is.
cd /d "%~dp0"

rem Use your Miniconda Python directly so it matches the env that already has packages.
set "PYTHON=C:\Users\fmani\miniconda3\python.exe"

if not exist "%PYTHON%" (
    echo Python not found at:
    echo %PYTHON%
    pause
    exit /b 1
)

if not exist "main.py" (
    echo main.py not found in this folder:
    echo %CD%
    echo Make sure all the .py files are next to this .bat.
    pause
    exit /b 1
)

echo Installing build dependencies...
"%PYTHON%" -m pip install --upgrade pip
"%PYTHON%" -m pip install pyinstaller psutil pystray pillow pefile

rem If ico.ico is present, use it as the EXE icon AND bundle it so the running app
rem can load it for the tray. Otherwise build with the default icon.
set "ICONARG="
if exist "ico.ico" (
    echo Using ico.ico for the EXE/tray icon.
    set "ICONARG=--icon ico.ico --add-data ico.ico;."
) else (
    echo No ico.ico found; using the default icon.
)

echo Building EXE...
rem --paths . lets PyInstaller find the sibling modules (watcher_config, speedy_dll, boot, refresher).
rem --hidden-import bundles the dynamic pystray win32 backend that PyInstaller can miss.
"%PYTHON%" -m PyInstaller --noconfirm --onefile --windowed ^
    --name TBHWatcher ^
    --paths . ^
    --hidden-import pystray._win32 ^
    --hidden-import pefile ^
    %ICONARG% ^
    main.py

if exist "dist\TBHWatcher.exe" (
    echo.
    echo Build complete:
    echo %CD%\dist\TBHWatcher.exe
) else (
    echo.
    echo Build failed. Check the output above.
)
pause
