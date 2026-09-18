#!/bin/bash
# RadiantView macOS .app 빌드 스크립트

echo "=== RadiantView macOS App Builder ==="
echo ""

# 가상환경 확인/생성
if [ ! -d "venv" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv venv
fi
source venv/bin/activate

# 의존성 설치
echo "📦 패키지 설치 중..."
pip install --upgrade pip
pip install -r requirements.txt
pip install py2app Pillow

# 아이콘 생성
echo "🎨 아이콘 생성 중..."
python create_icon.py

# 기존 빌드 제거
rm -rf build dist

# .app 빌드
echo "🔨 앱 빌드 중... (시간이 좀 걸립니다)"
python setup_app.py py2app

if [ -d "dist/RadiantView.app" ]; then
    echo ""
    echo "✅ 빌드 성공!"
    echo "📍 위치: dist/RadiantView.app"
    echo ""
    echo "실행: open dist/RadiantView.app"
    echo ""
    echo "Applications 폴더에 복사하려면:"
    echo "  cp -r dist/RadiantView.app /Applications/"
    echo ""

    # 앱 바로 열기
    read -p "지금 앱을 실행할까요? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        open dist/RadiantView.app
    fi
else
    echo ""
    echo "❌ 빌드 실패. 에러 메시지를 확인해주세요."
    echo ""
    echo "대안: PyInstaller로 시도"
    echo "  pip install pyinstaller"
    echo "  pyinstaller --windowed --name RadiantView --icon resources/RadiantView.icns run.py"
fi
