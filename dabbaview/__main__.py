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

    # 슬롯에서 처리되지 않은 예외가 나도 앱 전체가 죽지 않게 (로그 + 상태바 알림)
    from . import crash_guard
    crash_guard.install()

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
    crash_guard.set_window(window)
    window.setAcceptDrops(True)  # 드래그 앤 드롭 활성화
    window.show()

    # 커맨드라인 인자로 파일/폴더 지정 가능
    if len(sys.argv) > 1:
        window.load_path(sys.argv[1], remember=False)

    code = app.exec_()
    _fast_exit(window, code)


def _fast_exit(window, code):
    """정상 종료 처리 뒤 곧바로 프로세스 종료

    창·설정 정리와 atexit 처리(클라우드 읽기 프로세스 종료 등)를 끝낸 다음 os._exit로 나감.
    C++ 라이브러리의 전역 소멸자(onnxruntime 원격 수집 정리 등)가 종료 중에
    충돌(abort)해서 '예기치 않게 종료됨' 창이 뜨는 것을 막기 위함.
    """
    # 다른 라이브러리의 atexit(PyQt 래퍼 정리 등)는 부르지 않음: 창이 살아 있는 상태에서
    # 돌리면 오히려 충돌함. DabbaView가 할 정리만 직접
    try:
        window._settings.sync()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .dicom_loader import kill_fetch_processes
        kill_fetch_processes()   # 진행 중인 클라우드 읽기 프로세스
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


if __name__ == "__main__":
    main()
