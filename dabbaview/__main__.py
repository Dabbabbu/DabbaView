# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DabbaView 실행 엔트리포인트
python -m dabbaview 로 실행
"""
import os
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from . import __version__
from .main_window import MainWindow
from . import shortcut_fallback


def main():
    # macOS: Control+좌클릭을 우클릭으로 바꾸지 않음 → Control+드래그(ROI W/L)가 좌클릭으로 도착
    os.environ.setdefault("QT_MAC_DONT_OVERRIDE_CTRL_LMB", "1")

    # High DPI 지원
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("DabbaView")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("DabbaView")
    shortcut_fallback.install(app)  # 한글 입력 상태에서도 T/R/P 등 단축키 동작

    # 지난 실행의 클라우드 세션 폴더 정리 + 캐시 용량 한도 적용
    try:
        from . import cache
        cache.clear_sessions()
        cache.enforce_limit(force=True)
    except OSError:
        pass

    window = MainWindow()
    window.setAcceptDrops(True)  # 드래그 앤 드롭 활성화
    window.show()

    # 커맨드라인 인자로 파일/폴더 지정 가능
    if len(sys.argv) > 1:
        window.load_path(sys.argv[1], remember=False)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
