# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
클라우드 → 로컬: 폴더 계획(재귀) + 캐시를 거친 병렬 다운로드

1. plan: 고른 폴더를 하위 폴더까지 훑어서 불러올 파일 목록을 만든다.
   로컬 'Open Folder'와 같은 기준 (DICOM 후보 + 지원하는 다른 형식)만 고르고 나머지는 건너뜀.
   직접 고른 파일은 확장자와 관계없이 포함 (로컬 'Open File'과 같음).
2. fetch: 파일마다 캐시(파일 ID + 버전)에 있으면 그대로 쓰고, 없으면 내려받아 캐시에 넣는다.
   이번에 연 파일은 세션 폴더에 원래 폴더 구조대로 하드 링크 → 그 폴더를 불러온다.
"""
import concurrent.futures
import json
import os
import shutil
import threading
import time

from .. import cache
from . import CloudError, safe_name

WORKERS = 6            # 전체 파일 받기 (큰 파일 - 대역폭이 병목)
HEAD_WORKERS = 12      # 헤더만 받기 - 구글 분당 한도(rateLimitExceeded)에 걸리지 않는 선


def wanted(name):
    """로컬 폴더 열기와 같은 파일 선별"""
    from ..dicom_loader import TEXT_REPORT_EXTENSIONS, is_candidate_file
    from ..formats.readers import file_kind
    if name.startswith("."):
        return False
    if name.lower().endswith(TEXT_REPORT_EXTENSIONS):
        return True
    return is_candidate_file(name) or file_kind(name) != "dicom"


class Throttle:
    """요청 한도에 걸리면 잠시 쉬었다가 천천히 회복 (구글 분당 한도 대응)"""

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0
        self._penalty = 0.0

    def wait(self):
        while True:
            with self._lock:
                remain = self._until - time.monotonic()
            if remain <= 0:
                return
            time.sleep(min(remain, 0.5))

    def hit_limit(self):
        """한도 응답을 받았을 때: 쉬는 시간을 늘림 (최대 20초)"""
        with self._lock:
            self._penalty = min(20.0, (self._penalty or 1.0) * 2)
            self._until = time.monotonic() + self._penalty
            return self._penalty

    def ok(self):
        """성공하면 벌점을 조금씩 줄임"""
        with self._lock:
            self._penalty = max(0.0, self._penalty * 0.5)


def _human_time(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분 {seconds % 60}초"
    return f"{seconds // 3600}시간 {(seconds % 3600) // 60}분"


def _drop_mixed_images(files):
    """DICOM과 그림이 섞여 있으면 그림은 받지 않음 → (남길 파일, 뺀 장수)

    논문 그림·캡처가 수만 장 섞인 폴더에서 쓸데없이 몇 GB를 내려받는 것을 막는다.
    (로컬 폴더 열기와 같은 기준: dicom_loader.MIXED_IMAGE_LIMIT)
    """
    from ..dicom_loader import MIXED_IMAGE_LIMIT
    from ..formats.readers import file_kind
    dicoms = sum(1 for rel, _ in files if file_kind(rel) == "dicom")
    pictures = [i for i, (rel, _) in enumerate(files) if file_kind(rel) == "image"]
    if not dicoms or len(pictures) <= MIXED_IMAGE_LIMIT:
        return files, 0
    drop = set(pictures)
    return [f for i, f in enumerate(files) if i not in drop], len(pictures)


def plan(provider, items, progress=None, cancelled=None, drop_mixed=False):
    """→ (files [(상대 경로, CloudItem)], 건너뛴 파일 수, 최상위 경로 목록)

    폴더를 너비 우선으로 훑으며 '확인한 폴더 / 찾은 폴더'와 파일 수를 알린다.
    (하위 폴더는 들어가 봐야 알 수 있어서 전체 수가 점점 늘어난다 — 그대로 보여 준다)
    """
    files, skipped, tops = [], 0, []
    queue = []          # [(folder, rel)]
    listed = 0
    start = time.monotonic()
    seen = {}           # 확장자 -> [개수, 용량] (걸러지기 전 전체)
    seen_bytes = [0]

    def note(name, size):
        ext = (os.path.splitext(name)[1] or "(확장자 없음)").lower()
        entry = seen.setdefault(ext, [0, 0])
        entry[0] += 1
        entry[1] += size or 0
        seen_bytes[0] += size or 0

    def report(where=""):
        if progress:
            progress(("scan", listed, listed + len(queue), len(files),
                      time.monotonic() - start, where,
                      {k: list(v) for k, v in seen.items()}, seen_bytes[0]))

    for item in items:
        rel = safe_name(item.name)
        tops.append(rel)
        if item.is_folder:
            queue.append((item, rel))
        elif item.downloadable:
            files.append((rel, item))
    report()
    for rel, item in list(files):       # 파일을 직접 고른 경우도 집계
        note(item.name, getattr(item, "size", 0))
    while queue:
        if cancelled and cancelled():
            raise CloudError("취소했습니다.")
        folder, rel = queue.pop(0)  # noqa: E501 - 너비 우선(처음 고른 폴더부터 차례로)
        for child in provider.list_children(folder):
            child_rel = os.path.join(rel, safe_name(child.name))
            if child.is_folder:
                queue.append((child, child_rel))
            elif child.downloadable and wanted(child.name):
                files.append((child_rel, child))
                note(child.name, getattr(child, "size", 0))
            else:
                skipped += 1
                note(child.name, getattr(child, "size", 0))
        listed += 1
        report(rel)
    mixed = 0
    if drop_mixed:
        files, mixed = _drop_mixed_images(files)
        skipped += mixed
    summary = {"folders": listed, "files": len(files), "skipped": skipped,
               "bytes": sum(getattr(i, "size", 0) or 0 for _rel, i in files),
               "all_bytes": seen_bytes[0], "by_ext": seen,
               "seconds": time.monotonic() - start, "mixed_images": mixed}
    if progress:
        progress(("summary", summary))
    return files, skipped, tops


def _version(item):
    return item.extra.get("version") or f"{item.modified}|{item.size}"


def cached_path(provider, item):
    """캐시에 이 버전이 있으면 경로, 없으면 None"""
    entry = cache.cloud_entry_dir(provider.key, item.id, _version(item))
    data = os.path.join(entry, "data")
    if os.path.isfile(data) and (not item.size or os.path.getsize(data) == item.size):
        return data
    return None


def _fetch_one(provider, item, on_bytes, cancelled):
    """캐시 확인 → 없으면 내려받아 캐시에 저장. (경로, 캐시 적중 여부)"""
    entry = cache.cloud_entry_dir(provider.key, item.id, _version(item))
    data = os.path.join(entry, "data")
    hit = cached_path(provider, item)
    if hit:
        cache.touch(entry)
        return hit, True
    os.makedirs(entry, exist_ok=True)
    part = data + ".part"
    try:
        provider.download_file(item, part, on_bytes, cancelled)
        os.replace(part, data)   # 끝까지 받은 것만 캐시로 인정
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    with open(os.path.join(entry, "info.json"), "w", encoding="utf-8") as f:
        json.dump({"name": item.name, "id": item.id, "version": _version(item),
                   "size": item.size}, f, ensure_ascii=False)
    return data, False


HEAD_BYTES = 32 * 1024        # DICOM 헤더용으로 받을 앞부분 크기 (대부분 여기서 끝남)


def fetch_heads(provider, files, tops, progress=None, cancelled=None, workers=HEAD_WORKERS,
                dest_root=None, head_bytes=HEAD_BYTES):
    """파일 앞부분(헤더)만 받아 둔다 → (경로 목록, 통계)

    받은 파일은 lazy 모듈에 등록되어, 픽셀이 필요해지는 순간 전체를 내려받는다.
    """
    from . import lazy
    session = session_dir(provider.key, dest_root)
    total = len(files)
    lock = threading.Lock()
    stats = {"done": 0, "hits": 0, "bytes": 0, "total": total, "folder": session,
             "heads": 0}
    start = time.monotonic()
    paths = []

    def report():
        elapsed = max(0.001, time.monotonic() - start)
        done = stats["done"]
        speed = done / elapsed
        parts = [f"{done:,}/{total:,} 파일 ({done * 100 // max(1, total)}%)",
                 f"헤더 {cache.human_size(stats['bytes'])} 받음"]
        if speed > 0.1:
            parts.append(f"{speed:.0f}개/초")
            if done < total:
                parts.append(f"남은 시간 약 {_human_time((total - done) / speed)}")
        if progress:
            progress(("count", done, total,
                      "빠른 열기 - 메타데이터만 받는 중  ·  " + "  ·  ".join(parts)))

    throttle = Throttle()
    stats["failed"] = 0

    def task(rel, item):
        dest = os.path.join(session, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        cached = cached_path(provider, item)
        if cached:                                  # 이미 전체가 캐시에 있으면 그대로 사용
            _link(cached, dest)
            with lock:
                stats["hits"] += 1
        else:
            got = 0
            for attempt in range(5):
                if cancelled and cancelled():
                    return
                throttle.wait()
                try:
                    got = provider.download_head(item, dest, head_bytes)
                    lazy.register(dest, provider, item)   # 나머지는 볼 때 받음
                    throttle.ok()
                    break
                except Exception as e:  # noqa: BLE001
                    if _is_rate_limit(e):
                        throttle.hit_limit()          # 한도 → 전체 속도를 잠시 낮춤
                        continue
                    if attempt >= 2:                  # 다른 오류: 통째로 받아 보고, 그래도 안 되면 건너뜀
                        try:
                            path, _hit = _fetch_one(provider, item, None, cancelled)
                            _link(path, dest)
                            got = getattr(item, "size", 0) or 0
                        except Exception:  # noqa: BLE001
                            with lock:
                                stats["failed"] += 1
                                stats["done"] += 1
                            report()
                            return
                        break
                    time.sleep(0.3 * (attempt + 1))
            with lock:
                stats["bytes"] += got
                stats["heads"] += 1
        with lock:
            stats["done"] += 1
            paths.append(dest)
        report()

    report()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, rel, item) for rel, item in files]
        for future in concurrent.futures.as_completed(futures):
            if cancelled and cancelled():
                for f in futures:
                    f.cancel()
                raise CloudError("취소했습니다.")
            try:
                future.result()
            except Exception:  # noqa: BLE001 - 개별 파일 실패는 건너뛰고 계속
                with lock:
                    stats["failed"] = stats.get("failed", 0) + 1
    return sorted(paths), stats


def _link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)        # 하드 링크: 공간을 더 쓰지 않고, 캐시가 지워져도 유지
    except OSError:
        shutil.copy2(src, dst)


def _is_rate_limit(error):
    """구글·MS의 '요청이 너무 많음' 응답인지"""
    text = str(error)
    return ("rateLimitExceeded" in text or "userRateLimitExceeded" in text
            or "Quota exceeded" in text or "429" in text
            or "HTTP 403" in text or "HttpError 403" in text)


def session_dir(provider_key, dest_root=None):
    """이번에 받을 파일을 둘 폴더. dest_root를 주면 그 아래에 날짜 폴더로 만든다"""
    if not dest_root:
        return cache.new_session_dir(provider_key)
    name = time.strftime(f"{provider_key}_%Y%m%d_%H%M%S")
    path = os.path.join(os.path.expanduser(dest_root), name)
    os.makedirs(path, exist_ok=True)
    return path


def fetch(provider, files, tops, progress=None, cancelled=None, workers=WORKERS,
          dest_root=None):
    """계획한 파일들을 캐시를 거쳐 저장 폴더로 → (불러올 경로 목록, 통계 dict)"""
    session = session_dir(provider.key, dest_root)
    total = len(files)
    lock = threading.Lock()
    stats = {"done": 0, "hits": 0, "bytes": 0, "total": total, "folder": session}
    in_flight = {}

    total_bytes = sum(getattr(i, "size", 0) or 0 for _rel, i in files)
    start = time.monotonic()

    def report():
        elapsed = max(0.001, time.monotonic() - start)
        got = stats["bytes"]
        speed = got / elapsed
        parts = [f"{stats['done']:,}/{total:,} 파일 ({stats['done'] * 100 // max(1, total)}%)",
                 f"{cache.human_size(got)} / {cache.human_size(total_bytes)}"]
        if speed > 0:
            parts.append(f"{cache.human_size(speed)}/s")
            left = total_bytes - got
            if left > 0 and stats["done"] < total:
                parts.append(f"남은 시간 약 {_human_time(left / speed)}")
        if stats["hits"]:
            parts.append(f"캐시 {stats['hits']}개")
        if stats.get("throttled"):
            parts.append(f"요청 한도로 잠시 천천히 ({stats['throttled']}회)")
        if progress:
            progress(("count", stats["done"], total, "내려받는 중  ·  " + "  ·  ".join(parts)))

    def task(rel, item):
        def on_bytes(_name, done_bytes):
            with lock:
                stats["bytes"] += done_bytes - in_flight.get(item.id, 0)
                in_flight[item.id] = done_bytes
            report()
        path, hit = _fetch_one(provider, item, on_bytes, cancelled)
        _link(path, os.path.join(session, rel))
        with lock:
            stats["done"] += 1
            stats["hits"] += int(hit)
        report()

    report()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, rel, item) for rel, item in files]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()   # 오류가 있으면 여기서 다시 발생
                if cancelled and cancelled():
                    raise CloudError("취소했습니다.")
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    cache.enforce_limit(force=True)
    paths = [os.path.join(session, top) for top in tops
             if os.path.exists(os.path.join(session, top))]
    return paths, stats
