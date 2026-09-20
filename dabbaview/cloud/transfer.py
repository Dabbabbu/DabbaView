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

from .. import cache
from . import CloudError, safe_name

WORKERS = 6


def wanted(name):
    """로컬 폴더 열기와 같은 파일 선별"""
    from ..dicom_loader import is_candidate_file
    from ..formats.readers import file_kind
    return is_candidate_file(name) or (not name.startswith(".") and file_kind(name) != "dicom")


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


def plan(provider, items, progress=None, cancelled=None):
    """→ (files [(상대 경로, CloudItem)], 건너뛴 파일 수, 최상위 경로 목록)"""
    files, skipped, tops = [], 0, []

    def walk(folder, rel):
        nonlocal skipped
        for child in provider.list_children(folder):
            if cancelled and cancelled():
                raise CloudError("취소했습니다.")
            child_rel = os.path.join(rel, safe_name(child.name))
            if child.is_folder:
                walk(child, child_rel)
            elif child.downloadable and wanted(child.name):
                files.append((child_rel, child))
            else:
                skipped += 1
        if progress:
            progress(f"폴더 확인 중... 파일 {len(files)}개 찾음 ({rel})")

    for item in items:
        rel = safe_name(item.name)
        tops.append(rel)
        if item.is_folder:
            walk(item, rel)
        elif item.downloadable:
            files.append((rel, item))
    files, mixed = _drop_mixed_images(files)
    skipped += mixed
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


def _link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)        # 하드 링크: 공간을 더 쓰지 않고, 캐시가 지워져도 유지
    except OSError:
        shutil.copy2(src, dst)


def fetch(provider, files, tops, progress=None, cancelled=None, workers=WORKERS):
    """계획한 파일들을 캐시를 거쳐 세션 폴더로 → (불러올 경로 목록, 통계 dict)"""
    session = cache.new_session_dir(provider.key)
    total = len(files)
    lock = threading.Lock()
    stats = {"done": 0, "hits": 0, "bytes": 0, "total": total}
    in_flight = {}

    def report():
        text = (f"다운로드 중... {stats['done']}/{total} files · "
                f"{cache.human_size(stats['bytes'])} 받음 · 캐시 {stats['hits']}개")
        if progress:
            progress(("count", stats["done"], total, text))

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
