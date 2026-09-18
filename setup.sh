#!/bin/bash
# DabbaView 설치 및 실행 스크립트 (macOS)

echo "=== DabbaView Setup ==="

# Python 버전 확인
python3 --version || { echo "Python3가 필요합니다. brew install python3"; exit 1; }

# 가상환경 생성
if [ ! -d "venv" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv venv
fi

# 가상환경 활성화
source venv/bin/activate

# 의존성 설치
echo "패키지 설치 중..."
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=== 설치 완료! ==="
echo ""
echo "실행 방법:"
echo "  source venv/bin/activate"
echo "  python run.py"
echo ""
echo "또는 DICOM 폴더를 지정하여 실행:"
echo "  python run.py /path/to/dicom/folder"
echo ""
