# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
작업(ROI · 측정 · 주석 · Key Image) 저장 - 종료할 때 묻기 · 자동 저장 · 다시 열 때 복원

저장 방법 세 가지
1. 원본에 덮어쓰기: DICOM 파일 안 개인 태그(0071,"DabbaView Annotations")에 JSON으로 넣음 (.bak 백업)
2. 사본 만들어 저장: 고른 폴더에 DICOM 사본 + annotations.json
3. 어노테이션만 별도 저장: 자동 저장 폴더에 스터디별 JSON (다음에 같은 검사를 열면 복원 여부를 물음)
"""
import json
import os
import shutil
import sys

from .annotations import MEASURE_TYPES, ROI_TYPES, image_key

PRIVATE_CREATOR = "DabbaView Annotations"
PRIVATE_GROUP = 0x0071
PRIVATE_ELEMENT = 0x01


def base_dir():
    override = os.environ.get("DABBAVIEW_AUTOSAVE")
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/DabbaView")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "DabbaView")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "DabbaView")
    return os.path.join(base, "autosave")


def sidecar_path(study_uid):
    safe = "".join(c for c in str(study_uid) if c.isalnum() or c in "._-")[:120] or "unknown"
    return os.path.join(base_dir(), safe + ".json")


def counts(store):
    """→ [(이름, 개수)] 비어 있으면 []"""
    roi = measure = other = 0
    for _key, ann in store.all_items():
        kind = ann.get("type")
        if kind in ROI_TYPES:
            roi += 1
        elif kind in MEASURE_TYPES:
            measure += 1
        else:
            other += 1
    out = [("ROI", roi), ("측정", measure), ("주석 (화살표 · 메모 등)", other),
           ("Key Image", len(store.key_images()))]
    return [(name, n) for name, n in out if n]


def summary_text(store):
    items = counts(store)
    return ", ".join(f"{name} {n}개" for name, n in items) if items else "없음"


def has_work(store):
    return bool(counts(store))


def _key_to_study(series_list):
    out = {}
    for series in series_list or []:
        for k in range(series.num_slices):
            key = image_key(series, k)
            if key:
                out[key] = series.study_uid
    return out


def studies_with_work(store, series_list):
    """작업이 들어 있는 스터디 UID들"""
    mapping = _key_to_study(series_list)
    return sorted({mapping.get(key) for key, _ann in store.all_items() if mapping.get(key)})


def _subset(store, keys):
    data = store.to_dict()
    data["annotations"] = {k: v for k, v in data.get("annotations", {}).items() if k in keys}
    data["key_images"] = {k: v for k, v in data.get("key_images", {}).items() if k in keys}
    return data


def save_sidecar(store, series_list, folder=None):
    """스터디별 JSON 저장 → 만든 파일 경로들"""
    mapping = _key_to_study(series_list)
    by_study = {}
    for key, _ann in store.all_items():
        study = mapping.get(key)
        if study:
            by_study.setdefault(study, set()).add(key)
    for key, _info in store.key_images():
        study = mapping.get(key)
        if study:
            by_study.setdefault(study, set()).add(key)
    made = []
    for study, keys in by_study.items():
        path = os.path.join(folder, f"{study}.json") if folder else sidecar_path(study)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_subset(store, keys), f, ensure_ascii=False, indent=2)
        made.append(path)
    return made


def sidecar_for_studies(study_uids):
    """다시 열 때: 저장해 둔 파일이 있는 스터디 → [(study_uid, 경로, 주석 수)]"""
    out = []
    for study in study_uids:
        path = sidecar_path(study)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            n = sum(len(v) for v in data.get("annotations", {}).values())
        except (OSError, ValueError):
            continue
        if n:
            out.append((study, path, n))
    return out


def _files_of(store, series_list):
    """주석이 있는 영상 → {파일 경로: [주석]} (원본 수정 · 사본 저장용)"""
    files = {}
    for series in series_list or []:
        for k in range(series.num_slices):
            key = image_key(series, k)
            items = store.items(key)
            if not items:
                continue
            ds = series.slices[k]
            path = getattr(ds, "filename", None)
            if path and os.path.exists(path):
                files.setdefault(path, []).extend(items)
    return files


def write_into_dicom(store, series_list, backup=True):
    """원본 DICOM에 개인 태그로 저장 → (성공 수, [실패 메시지])"""
    import pydicom
    done, errors = 0, []
    for path, items in _files_of(store, series_list).items():
        try:
            ds = pydicom.dcmread(path)
            if backup and not os.path.exists(path + ".bak"):
                shutil.copy2(path, path + ".bak")
            block = ds.private_block(PRIVATE_GROUP, PRIVATE_CREATOR, create=True)
            block.add_new(PRIVATE_ELEMENT, "UT", json.dumps({"format": "dabbaview-annotations-1",
                                                             "annotations": items},
                                                            ensure_ascii=False, default=str))
            ds.save_as(path)
            done += 1
        except Exception as e:  # noqa: BLE001 - 파일 하나가 실패해도 나머지는 진행
            errors.append(f"{os.path.basename(path)}: {e}")
    return done, errors


def read_from_dicom(ds):
    """개인 태그에 저장해 둔 주석 (없으면 None)"""
    try:
        block = ds.private_block(PRIVATE_GROUP, PRIVATE_CREATOR)
        value = block[PRIVATE_ELEMENT].value
    except (KeyError, AttributeError, ValueError):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def collect_from_dicom(series_list, store=None):
    """열려 있는 영상의 개인 태그에 저장된 주석 → {영상 키: [주석]} (이미 있는 영상은 건너뜀)"""
    found = {}
    for series in series_list or []:
        for k in range(series.num_slices):
            key = image_key(series, k)
            if not key or (store is not None and store.items(key)):
                continue
            data = read_from_dicom(series.slices[k])
            items = (data or {}).get("annotations") or []
            if items:
                found[key] = items
    return found


def save_copy(store, series_list, dest):
    """사본 폴더에 DICOM + annotations.json → (복사한 파일 수, annotations.json 경로)"""
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for series in series_list or []:
        has = any(store.items(image_key(series, k)) for k in range(series.num_slices))
        if not has:
            continue
        name = "".join(c for c in (series.description or series.series_uid)[:40]
                       if c.isalnum() or c in " ._-").strip() or series.series_uid[-8:]
        folder = os.path.join(dest, f"{series.series_number or ''}_{name}".strip("_"))
        os.makedirs(folder, exist_ok=True)
        for k in range(series.num_slices):
            path = getattr(series.slices[k], "filename", None)
            if path and os.path.exists(path):
                shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
                copied += 1
    out = os.path.join(dest, "annotations.json")
    store.save_json(out)
    return copied, out
