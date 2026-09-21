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

WORKERS = 10           # 동시에 받는 파일 수 (구글 한도 1인당 초당 약 27건 안쪽, 넘치면 자동 재시도)


def wanted(name):
    """로컬 폴더 열기와 같은 파일 선별"""
    from ..dicom_loader import TEXT_REPORT_EXTENSIONS, is_candidate_file
    from ..formats.readers import file_kind
    if name.startswith("."):
        return False
    if name.lower().endswith(TEXT_REPORT_EXTENSIONS):
        return True
    from ..archives import is_archive_name
    if is_archive_name(name):
        return True                     # 압축파일도 받아서 풀어 연다
    return is_candidate_file(name) or file_kind(name) != "dicom"


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


RETRIES = 4                      # 네트워크가 잠깐 끊겼을 때 파일 하나를 다시 받는 횟수
RETRY_WAIT = (1, 3, 6, 12)       # 다시 받기 전 기다리는 초


def is_transient(exc):
    """잠깐 끊김 · 시간 초과처럼 다시 받으면 되는 오류인지 (인증 · 없는 파일 등은 아님)"""
    import http.client
    import socket
    import ssl
    if isinstance(exc, (http.client.IncompleteRead, http.client.RemoteDisconnected,
                        ConnectionError, TimeoutError, socket.timeout, ssl.SSLError)):
        return True
    name = type(exc).__name__
    if name in ("ServerNotFoundError", "IncompleteRead", "ChunkedEncodingError",
                "ReadTimeout", "ConnectTimeout", "ProtocolError"):
        return True
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) in (408, 429, 500, 502, 503, 504)
    except (TypeError, ValueError):
        return False


def _fetch_one(provider, item, on_bytes, cancelled):
    """캐시 확인 → 없으면 내려받아 캐시에 저장. (경로, 캐시 적중 여부)

    받다가 네트워크가 끊기면(IncompleteRead 등) 잠시 뒤 처음부터 다시 받는다 (최대 RETRIES번).
    """
    attempt = 0
    while True:
        try:
            return _fetch_one_try(provider, item, on_bytes, cancelled)
        except Exception as exc:                 # noqa: BLE001 - 다시 받을 오류만 골라 재시도
            if attempt >= RETRIES or not is_transient(exc) or (cancelled and cancelled()):
                raise
            on_bytes(item.name, 0)               # 받다 만 양은 되돌림
            wait = RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)]
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if cancelled and cancelled():
                    raise CloudError("취소했습니다.") from exc
                time.sleep(0.2)
            attempt += 1


def _fetch_one_try(provider, item, on_bytes, cancelled):
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


def _link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)        # 하드 링크: 공간을 더 쓰지 않고, 캐시가 지워져도 유지
    except OSError:
        shutil.copy2(src, dst)


def session_dir(provider_key, dest_root=None):
    """이번에 받을 파일을 둘 폴더. dest_root를 주면 그 아래에 날짜 폴더로 만든다"""
    if not dest_root:
        return cache.new_session_dir(provider_key)
    name = time.strftime(f"{provider_key}_%Y%m%d_%H%M%S")
    path = os.path.join(os.path.expanduser(dest_root), name)
    os.makedirs(path, exist_ok=True)
    return path


def group_by_folder(files):
    """파일을 들어 있는 폴더별로 묶음 (보통 한 폴더 = 한 시리즈). 처음 나온 순서 유지"""
    groups = {}
    for rel, item in files:
        groups.setdefault(os.path.dirname(rel), []).append((rel, item))
    return list(groups.items())


def fetch(provider, files, tops, progress=None, cancelled=None, workers=WORKERS,
          dest_root=None, on_group_done=None):
    """계획한 파일들을 캐시를 거쳐 저장 폴더로 → (불러올 경로 목록, 통계 dict)

    폴더(≈시리즈) 단위로 차례대로 받는다. 한 폴더를 다 받으면
    on_group_done(폴더, [파일 경로들], 끝난 폴더 수, 전체 폴더 수)를 불러
    다른 폴더를 받는 동안에도 그 시리즈를 바로 볼 수 있게 한다.
    """
    session = session_dir(provider.key, dest_root)
    groups = group_by_folder(files)
    files = [f for _g, members in groups for f in members]      # 폴더 순서대로 받음
    total = len(files)
    lock = threading.Lock()
    stats = {"done": 0, "hits": 0, "bytes": 0, "total": total, "folder": session,
             "groups_done": 0, "groups": len(groups), "failed": [], "elapsed": 0.0}
    in_flight = {}
    left_in_group = {g: len(members) for g, members in groups}
    group_paths = {g: [] for g, _m in groups}
    group_of = {rel: g for g, members in groups for rel, _i in members}

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
        if stats["failed"]:
            parts.append(f"못 받음 {len(stats['failed'])}개")
        if stats["groups"] > 1:
            parts.append(f"폴더 {stats['groups_done']}/{stats['groups']} 완료")
        if progress:
            progress(("count", stats["done"], total, "내려받는 중  ·  " + "  ·  ".join(parts)))

    def task(rel, item):
        def on_bytes(_name, done_bytes):
            with lock:
                stats["bytes"] += done_bytes - in_flight.get(item.id, 0)
                in_flight[item.id] = done_bytes
            report()
        try:
            path, hit = _fetch_one(provider, item, on_bytes, cancelled)
        except CloudError:
            raise                                 # 취소 · 받을 수 없는 형식 등은 그대로
        except Exception as exc:                 # noqa: BLE001
            if not is_transient(exc):
                raise                             # 인증 등: 다른 파일도 안 되므로 멈춤
            path = None                           # 다시 받아도 안 됨 → 이 파일만 빼고 계속
            with lock:
                stats["failed"].append((rel, f"{type(exc).__name__}: {exc}"))
                stats["bytes"] -= in_flight.pop(item.id, 0)
        finished = None
        if path is not None:
            dest = os.path.join(session, rel)
            _link(path, dest)
        with lock:
            group = group_of[rel]
            if path is not None:
                stats["done"] += 1
                stats["hits"] += int(hit)
                group_paths[group].append(dest)
            left_in_group[group] -= 1
            if left_in_group[group] == 0:            # 이 폴더(시리즈)는 다 받음
                stats["groups_done"] += 1
                finished = (group, sorted(group_paths[group]), stats["groups_done"])
        report()
        if finished and on_group_done is not None:
            on_group_done(finished[0], finished[1], finished[2], stats["groups"])

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
    stats["elapsed"] = time.monotonic() - start
    cache.enforce_limit(force=True)
    paths = [os.path.join(session, top) for top in tops
             if os.path.exists(os.path.join(session, top))]
    return paths, stats
