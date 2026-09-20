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
REM --exclude-module onnxruntime: PyInstaller가 분석하면서 onnxruntime을 import할 때
REM Windows 러너에서 프로세스가 죽어(access violation) 빌드가 실패한다. 앱은 onnxruntime을
REM 쓸 때만 import하므로 빼도 나머지 기능은 그대로 (ONNX·MedSAM 추론만 비활성).
pyinstaller --noconfirm --windowed --name DabbaView ^
    --icon resources\dabbaview.ico ^
    --add-data "dabbaview\resources;dabbaview\resources" ^
    --exclude-module onnxruntime ^
    run.py || goto :error

if not exist dist\DabbaView\DabbaView.exe goto :error

echo.
echo Build succeeded: dist\DabbaView\DabbaView.exe
exit /b 0

:error
echo.
echo Build FAILED.
exit /b 1
