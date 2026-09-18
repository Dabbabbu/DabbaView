@echo off
REM DabbaView Windows build script (PyInstaller)
REM Output: dist\DabbaView\DabbaView.exe

setlocal
cd /d "%~dp0"

REM Scripts print emoji/Korean text; avoid UnicodeEncodeError on non-UTF-8 consoles
set PYTHONUTF8=1

echo === DabbaView Windows Build ===

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv || goto :error
)

REM "call" is required: without it, activate.bat ends this script
call venv\Scripts\activate || goto :error

python -m pip install --upgrade pip || goto :error
pip install -r requirements.txt || goto :error
pip install pyinstaller || goto :error

echo Generating icons...
python create_icon.py || goto :error

echo Building...
REM --add-data: start-screen logo (Windows uses ';' as the src;dest separator)
pyinstaller --noconfirm --windowed --name DabbaView ^
    --icon resources\dabbaview.ico ^
    --add-data "dabbaview\resources;dabbaview\resources" ^
    run.py || goto :error

if not exist dist\DabbaView\DabbaView.exe goto :error

echo.
echo Build succeeded: dist\DabbaView\DabbaView.exe
exit /b 0

:error
echo.
echo Build FAILED.
exit /b 1
