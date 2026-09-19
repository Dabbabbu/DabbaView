# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""예상 못 한 예외로 앱 전체가 죽지 않게

PyQt5는 슬롯(시그널 처리 함수)에서 처리되지 않은 예외가 나면 기본적으로 qFatal → 앱 강제 종료.
sys.excepthook을 바꿔 두면 종료 대신 여기로 오므로, 트레이스백을 로그 파일에 남기고
상태바에 알린 뒤 계속 실행함. 백그라운드 스레드 예외(threading.excepthook)도 로그로.
"""
import datetime
import os
import sys
import threading
import traceback

_window = None
_last_notice = [0.0]


def log_path():
    if sys.platform == "darwin":
        folder = os.path.expanduser("~/Library/Logs/DabbaView")
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
        folder = os.path.join(base, "DabbaView", "logs")
    return os.path.join(folder, "errors.log")


def _write(text):
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:   # 너무 커지면 새로
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _format(exc_type, exc, tb, where):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (f"\n=== {stamp} {where} ===\n"
            + "".join(traceback.format_exception(exc_type, exc, tb)))


def _notify(exc_type, exc):
    """메인 창 상태바에 한 줄 알림 (UI 스레드에서만, 연달아 나면 5초에 한 번)"""
    import time
    window = _window
    if window is None or threading.current_thread() is not threading.main_thread():
        return
    now = time.monotonic()
    if now - _last_notice[0] < 5:
        return
    _last_notice[0] = now
    try:
        window.statusBar().showMessage(
            f"⚠ 내부 오류가 있었지만 계속 실행합니다 ({exc_type.__name__}: {str(exc)[:80]}) "
            f"— 로그: {log_path()}", 15000)
    except Exception:  # noqa: BLE001 - 알림 실패는 무시
        pass


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    text = _format(exc_type, exc, tb, "UI")
    sys.stderr.write(text)
    _write(text)
    _notify(exc_type, exc)


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    text = _format(args.exc_type, args.exc_value, args.exc_traceback, f"thread {name}")
    sys.stderr.write(text)
    _write(text)


def install(window=None):
    """앱 시작 시 한 번. window를 주면 상태바로 알림"""
    global _window
    _window = window
    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook


def set_window(window):
    global _window
    _window = window
