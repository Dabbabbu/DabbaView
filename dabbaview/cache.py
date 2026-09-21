# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
로컬 캐시 - 폴더 메타데이터 / 썸네일 / 클라우드 다운로드 파일

위치: macOS ~/Library/Caches/DabbaView, Windows %LOCALAPPDATA%\\DabbaView\\cache,
      기타 ~/.cache/DabbaView  (환경 변수 DABBAVIEW_CACHE_DIR로 바꿀 수 있음)

  metadata/<키>.pkl.gz      폴더 하나의 DICOM 메타데이터 (파일 목록·수정일·크기가 같을 때만 사용)
  thumbnails/<키>.png       시리즈 썸네일
  cloud/<provider>/<키>/    클라우드 파일 하나(파일 ID + 수정 시각)의 내려받은 사본
  sessions/<시각>/          이번 실행에서 연 클라우드 파일 (캐시로의 하드 링크, 시작할 때 비움)

용량 관리: 항목(파일/폴더)마다 마지막 사용 시각(mtime)을 갱신하고, 합계가 한도를 넘으면
오래 안 쓴 것부터 지운다 (LRU). sessions는 하드 링크라 용량 계산·삭제 대상에서 제외 -
캐시에서 지워도 지금 열려 있는 영상은 계속 읽을 수 있다.
"""
import gzip
import hashlib
import os
import pickle
import shutil
import sys
import threading
import time

from PyQt5.QtCore import QSettings

DEFAULT_LIMIT_GB = 5
MIN_LIMIT_GB, MAX_LIMIT_GB = 1, 50
CATEGORIES = ("metadata", "thumbnails", "cloud", "archives")   # archives: 풀어 둔 압축파일
FORMAT_VERSION = 1

_lock = threading.Lock()
_last_enforce = 0.0


def cache_root():
    override = os.environ.get("DABBAVIEW_CACHE_DIR")
    if override:
        root = override
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Caches/DabbaView")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        root = os.path.join(base, "DabbaView", "cache")
    else:
        root = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                            "DabbaView")
    os.makedirs(root, exist_ok=True)
    return root


def category_dir(name):
    path = os.path.join(cache_root(), name)
    os.makedirs(path, exist_ok=True)
    return path


def _settings():
    return QSettings("DabbaView", "DabbaView")


def limit_gb():
    value = _settings().value("cache_limit_gb", DEFAULT_LIMIT_GB, type=int)
    return max(MIN_LIMIT_GB, min(MAX_LIMIT_GB, value or DEFAULT_LIMIT_GB))


def set_limit_gb(gb):
    _settings().setValue("cache_limit_gb", int(max(MIN_LIMIT_GB, min(MAX_LIMIT_GB, gb))))


def enabled():
    return _settings().value("cache_enabled", True, type=bool)


def touch(path):
    """LRU: 마지막 사용 시각 갱신"""
    try:
        os.utime(path, None)
    except OSError:
        pass


def _key(*parts):
    return hashlib.sha1("\x00".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()


# ─── 용량 ───

def _size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def entries():
    """[(마지막 사용 시각, 크기, 경로, 분류)] - LRU 단위 항목"""
    result = []
    root = cache_root()
    for category in CATEGORIES:
        base = os.path.join(root, category)
        if not os.path.isdir(base):
            continue
        if category == "cloud":
            parents = [os.path.join(base, p) for p in os.listdir(base)
                       if os.path.isdir(os.path.join(base, p))]
        else:
            parents = [base]
        for parent in parents:
            for name in os.listdir(parent):
                if name.startswith("."):
                    continue
                path = os.path.join(parent, name)
                try:
                    used = os.path.getmtime(path)
                except OSError:
                    continue
                result.append((used, _size(path), path, category))
    return result


def usage():
    """{분류: 바이트, "total": 합계}"""
    totals = {c: 0 for c in CATEGORIES}
    for _used, size, _path, category in entries():
        totals[category] += size
    totals["total"] = sum(totals[c] for c in CATEGORIES)
    return totals


def _remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def enforce_limit(limit_bytes=None, force=False):
    """한도를 넘으면 오래 안 쓴 항목부터 삭제. 지운 바이트 수 반환 (30초에 한 번까지)"""
    global _last_enforce
    now = time.monotonic()
    if not force and now - _last_enforce < 30:
        return 0
    with _lock:
        _last_enforce = now
        limit = limit_bytes if limit_bytes is not None else limit_gb() * 1024 ** 3
        items = sorted(entries())
        total = sum(size for _u, size, _p, _c in items)
        removed = 0
        for _used, size, path, _category in items:
            if total <= limit:
                break
            _remove(path)
            total -= size
            removed += size
        return removed


def clear():
    """캐시 전부 삭제 (지금 열린 세션 파일은 하드 링크라 영향 없음)"""
    with _lock:
        for category in CATEGORIES:
            _remove(os.path.join(cache_root(), category))


def clear_sessions(max_age_hours=12):
    """지난 실행의 세션 폴더 정리 (앱 시작 시) - 다른 창이 쓰는 중일 수 있어 오래된 것만"""
    base = os.path.join(cache_root(), "sessions")
    if not os.path.isdir(base):
        return
    limit = time.time() - max_age_hours * 3600
    for name in os.listdir(base):
        path = os.path.join(base, name)
        try:
            if os.path.getmtime(path) < limit:
                _remove(path)
        except OSError:
            pass


def new_session_dir(label):
    path = os.path.join(category_dir("sessions"), time.strftime("%Y%m%d-%H%M%S-") +
                        _key(label, time.time())[:6])
    os.makedirs(path, exist_ok=True)
    return path


# ─── 폴더 메타데이터 ───

def folder_signature(files):
    """파일 목록 서명: 개수 + 각 파일의 상대 경로·수정 시각·크기"""
    h = hashlib.sha1()
    count = 0
    for path in sorted(files):
        try:
            st = os.stat(path)
        except OSError:
            continue
        h.update(f"{path}\x00{st.st_mtime_ns}\x00{st.st_size}\n".encode("utf-8", "replace"))
        count += 1
    return f"{count}:{h.hexdigest()}"


def _metadata_path(folder, recursive):
    return os.path.join(category_dir("metadata"),
                        _key(os.path.abspath(folder), recursive) + ".pkl.gz")


def load_metadata(folder, recursive, signature):
    """서명이 같으면 저장해 둔 결과 dict, 아니면 None (다르면 캐시 삭제)"""
    if not enabled():
        return None
    path = _metadata_path(folder, recursive)
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rb") as f:
            data = pickle.load(f)
    except Exception:  # noqa: BLE001 - 손상된 캐시는 버림
        _remove(path)
        return None
    if data.get("version") != FORMAT_VERSION or data.get("signature") != signature:
        _remove(path)   # 파일이 바뀜 → 무효화
        return None
    touch(path)
    return data


def save_metadata(folder, recursive, signature, datasets, errors, extra=None):
    if not enabled():
        return
    path = _metadata_path(folder, recursive)
    tmp = path + ".tmp"
    try:
        with gzip.open(tmp, "wb", compresslevel=3) as f:
            data = {"version": FORMAT_VERSION, "folder": os.path.abspath(folder),
                    "signature": signature, "datasets": datasets, "errors": errors,
                    "saved": time.time()}
            data.update(extra or {})
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - 캐시 저장 실패는 무시 (다음에 다시 파싱)
        _remove(tmp)
        return
    enforce_limit()


# ─── 썸네일 ───

def thumbnail_path(series_uid, signature):
    return os.path.join(category_dir("thumbnails"), _key(series_uid, signature) + ".png")


# ─── 클라우드 파일 ───

def cloud_entry_dir(provider, file_id, version):
    """클라우드 파일 한 버전의 캐시 폴더 (파일 ID + 수정 시각/버전)"""
    return os.path.join(category_dir("cloud"), provider, _key(file_id, version))


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return ""
