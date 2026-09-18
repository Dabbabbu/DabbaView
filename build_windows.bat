@echo off
REM RadiantView Windows build script (PyInstaller)
REM Output: dist\RadiantView\RadiantView.exe

setlocal
cd /d "%~dp0"

REM Scripts print emoji/Korean text; avoid UnicodeEncodeError on non-UTF-8 consoles
set PYTHONUTF8=1

echo === RadiantView Windows Build ===

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
pyinstaller --noconfirm --windowed --name RadiantView ^
    --icon resources\radiantview.ico ^
    --add-data "radiantview\resources;radiantview\resources" ^
    run.py || goto :error

if not exist dist\RadiantView\RadiantView.exe goto :error

echo.
echo Build succeeded: dist\RadiantView\RadiantView.exe
exit /b 0

:error
echo.
echo Build FAILED.
exit /b 1
