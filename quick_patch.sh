#!/bin/bash
# 코드만 바뀌었을 때: 설치된 앱의 dabbaview 코드만 바꿔 넣기 (5분 빌드 대신 몇 초)
#
#   ./quick_patch.sh            → /Applications/DabbaView.app 에 적용
#
# 새 라이브러리를 추가했거나 setup_app.py를 바꿨다면 전체 빌드(setup_app.py py2app)가 필요합니다.
# GitHub Release는 태그를 올리면 CI가 항상 전체 빌드하므로 영향이 없습니다.
set -euo pipefail
APP="${APP:-/Applications/DabbaView.app}"
DEST="$APP/Contents/Resources/lib/python3.11/dabbaview"
SRC="$(cd "$(dirname "$0")" && pwd)/dabbaview"

if pgrep -f "$APP/Contents/MacOS/DabbaView" >/dev/null; then
  echo "DabbaView가 실행 중입니다. 종료한 뒤 다시 실행하세요." >&2; exit 1
fi
[ -d "$DEST" ] || { echo "설치된 앱 구조가 다릅니다 — 전체 빌드가 필요합니다: $DEST" >&2; exit 1; }

start=$(date +%s)
# 코드(.py)와 리소스만 동기화 — 빌드 정보(_build_info.py)와 캐시는 건드리지 않음
rsync -a --delete \
  --exclude "__pycache__/" --exclude "_build_info.py" --exclude "*.pyc" \
  "$SRC/" "$DEST/"
find "$DEST" -name "__pycache__" -type d -prune -exec rm -rf {} +   # 옛 바이트코드 제거

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/__init__.py")
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP" >/dev/null 2>&1
codesign -v "$APP"
echo "빠른 교체 완료: v$VERSION · $(( $(date +%s) - start ))초"
