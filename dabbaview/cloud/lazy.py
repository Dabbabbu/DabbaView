# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
빠른 열기 - 메타데이터만 먼저 받고, 픽셀은 볼 때 받는다

클라우드(Drive·OneDrive)는 파일 앞부분만 받는 Range 요청이 되므로,
DICOM 헤더(보통 수십 KB)만 받아 시리즈 목록을 바로 만들고,
실제로 영상을 볼 때 그 파일만 통째로 받아 채운다.

  11,428개 × 6.3 GB 전부 받기 (수십 분)  →  헤더 64 KB씩 (≈ 700 MB, 몇 분)
  → 보는 시리즈의 파일만 그때 내려받음

동기화 폴더(구글 드라이브 앱)로 열면 한 바이트만 읽어도 운영체제가 파일 전체를
받아버리기 때문에, 이 방식은 API로 연결했을 때만 가능하다.
"""
import os
import threading

_lock = threading.Lock()
_pending = {}        # 로컬 경로 -> (provider, item)
_locks = {}          # 로컬 경로 -> Lock (같은 파일을 두 번 받지 않게)
_listeners = []      # 받기 시작·끝 알림 (경로, 상태)


def register(path, provider, item):
    """헤더만 받아 둔 파일 등록 (나중에 전체가 필요하면 여기 정보로 받음)"""
    with _lock:
        _pending[os.path.abspath(path)] = (provider, item)


def clear():
    with _lock:
        _pending.clear()
        _locks.clear()


def pending_count():
    with _lock:
        return len(_pending)


def is_pending(path):
    with _lock:
        return os.path.abspath(path) in _pending


def add_listener(fn):
    """fn(path, state) - state: 'start' | 'done' | 'failed'"""
    _listeners.append(fn)


def _notify(path, state):
    for fn in list(_listeners):
        try:
            fn(path, state)
        except Exception:  # noqa: BLE001 - 알림 실패가 로딩을 막지 않게
            pass


def pending_paths():
    with _lock:
        return list(_pending.keys())


def pending_bytes():
    """아직 안 받은 파일들의 대략적인 크기 합계"""
    with _lock:
        return sum(getattr(item, "size", 0) or 0 for _p, item in _pending.values())


def prefetch(paths, workers=6, cancelled=None, progress=None):
    """지금 보는 시리즈 등을 백그라운드로 미리 받아 둔다 (버퍼링 방지)

    이미 받은 파일은 건너뛴다. progress(done, total)로 진행을 알린다.
    """
    import concurrent.futures
    todo = [p for p in paths if is_pending(p)]
    if not todo:
        return 0
    done = [0]

    def one(path):
        if cancelled and cancelled():
            return
        ensure(path)
        done[0] += 1
        if progress:
            progress(done[0], len(todo))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    return done[0]


def ensure(path):
    """이 파일의 전체 내용이 로컬에 있게 한다 → 받았으면 True

    헤더만 있는 파일이면 지금 내려받아 제자리에 채운다 (여러 번 불러도 한 번만).
    """
    if not path:
        return False
    full = os.path.abspath(path)
    with _lock:
        entry = _pending.get(full)
        if entry is None:
            return False
        file_lock = _locks.setdefault(full, threading.Lock())
    with file_lock:
        with _lock:
            entry = _pending.get(full)
        if entry is None:      # 다른 스레드가 이미 받음
            return True
        provider, item = entry
        _notify(full, "start")
        part = full + ".part"
        try:
            provider.download_file(item, part)
            os.replace(part, full)
        except Exception:  # noqa: BLE001 - 네트워크 오류 등은 그대로 두고 알림만
            try:
                os.remove(part)
            except OSError:
                pass
            _notify(full, "failed")
            return False
        with _lock:
            _pending.pop(full, None)
            _locks.pop(full, None)
        _notify(full, "done")
        return True
