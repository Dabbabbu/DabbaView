# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DabbaView 실행 엔트리포인트
python -m dabbaview 로 실행
"""
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from .main_window import MainWindow


def main():
    # High DPI 지원
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("DabbaView")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("DabbaView")

    window = MainWindow()
    window.setAcceptDrops(True)  # 드래그 앤 드롭 활성화
    window.show()

    # 커맨드라인 인자로 파일/폴더 지정 가능
    if len(sys.argv) > 1:
        window.load_path(sys.argv[1], remember=False)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
