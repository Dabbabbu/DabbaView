#!/bin/bash
# PyInstaller로 빌드 (py2app이 안 될 경우 대안)

echo "=== DabbaView PyInstaller Build ==="

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller Pillow

# 아이콘 생성
python create_icon.py

# 빌드
echo "🔨 빌드 중..."
pyinstaller \
    --windowed \
    --name "DabbaView" \
    --icon "resources/DabbaView.icns" \
    --add-data "dabbaview:dabbaview" \
    --hidden-import "pydicom.encoders.gdcm" \
    --hidden-import "pydicom.encoders.pylibjpeg" \
    --hidden-import "pydicom.encoders.native" \
    --noconfirm \
    run.py

if [ -d "dist/DabbaView.app" ]; then
    echo ""
    echo "✅ 빌드 성공! dist/DabbaView.app"
    echo ""
    echo "Applications에 복사: cp -r dist/DabbaView.app /Applications/"
    open dist/
else
    echo "❌ 빌드 실패"
fi
