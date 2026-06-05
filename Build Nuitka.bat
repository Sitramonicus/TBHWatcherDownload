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
"%PYTHON%" -m pip install nuitka psutil pystray pillow pefile

echo.
echo Building EXE with Nuitka...
echo (Note: Nuitka compiles Python into native C++ machine code. The first time you run this,
echo it may ask to download gcc/MinGW. Press Enter to allow it to auto-setup.)
echo.

rem If ico.ico is present, use it for the EXE icon AND ship it next to the EXE so
rem the running app can load it for the tray.
set "ICONARG="
if exist "ico.ico" (
    echo Using ico.ico for the EXE/tray icon.
    set "ICONARG=--windows-icon-from-ico=ico.ico --include-data-files=ico.ico=ico.ico"
) else (
    echo No ico.ico found; using the default icon.
)

rem --follow-imports pulls in the sibling modules (watcher_config, speedy_dll, boot, refresher).
rem --include-module ensures the dynamic pystray win32 backend is bundled.
"%PYTHON%" -m nuitka ^
    --onefile ^
    --follow-imports ^
    --windows-disable-console ^
    --include-module=pystray._win32 ^
    --include-module=pefile ^
    --include-package=PIL ^
    %ICONARG% ^
    --output-dir=dist ^
    --output-filename=TBHWatcher-ntk.exe ^
    main.py

if exist "dist\TBHWatcher-ntk.exe" (
    echo.
    echo ===================================================
    echo Nuitka Build complete!
    echo Output: %CD%\dist\TBHWatcher-ntk.exe
    echo ===================================================
) else (
    echo.
    echo ===================================================
    echo Nuitka Build failed. See the output logs above.
    echo ===================================================
)
pause
