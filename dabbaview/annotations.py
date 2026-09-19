# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
주석/측정 + Key Image 저장소

영상 키 = SOPInstanceUID (없으면 'SeriesInstanceUID#index').
뷰포트들이 하나의 저장소를 공유하므로 시리즈를 바꿨다 돌아와도 주석이 남고,
JSON 파일로 저장/불러오기 할 수 있다.
"""
import copy
import json
import time
import uuid
from contextlib import contextmanager

from PyQt5.QtCore import QObject, pyqtSignal

UNDO_LIMIT = 200

# 측정·ROI 종류 (ROI Manager·결과 표에서 구분)
ROI_TYPES = ("roi", "ellipse", "rect")              # 픽셀 통계가 있는 ROI
MEASURE_TYPES = ("distance", "path", "angle", "cobb", "area")


FILE_FORMAT = "dabbaview-annotations"
LEGACY_FORMATS = ("radiantview-annotations",)  # 이름 변경 전(RadiantView)에 저장한 파일
FILE_VERSION = 1


def image_key(series, index):
    """영상을 식별하는 키 (SOPInstanceUID 우선)"""
    if series is None or not (0 <= index < series.num_slices):
        return None
    return instance_key(series.slices[index], f"{series.series_uid}#{index}")


def instance_key(ds, fallback=""):
    """영상 한 장의 키: SOPInstanceUID (멀티프레임 파일의 프레임이면 '#f프레임' 추가)"""
    uid = str(getattr(ds, "SOPInstanceUID", "") or "")
    if not uid:
        return fallback
    frame = getattr(ds, "_dv_frame", None)
    return uid if frame is None else f"{uid}#f{frame}"


def _to_jsonable(value):
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "tolist"):  # numpy
        return value.tolist()
    return value


def _from_json_annotation(ann):
    ann = dict(ann)
    ann["pts"] = [tuple(p) for p in ann.get("pts", [])]
    return ann


def new_id():
    return uuid.uuid4().hex[:12]


def ensure_fields(ann):
    """ROI Manager용 공통 필드: id, name, visible, locked (color는 없으면 종류 기본색)"""
    ann.setdefault("id", new_id())
    ann.setdefault("visible", True)
    ann.setdefault("locked", False)
    ann.setdefault("name", "")
    return ann


class AnnotationStore(QObject):
    """영상별 주석 목록 + Key Image 집합"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = {}        # key → [annotation dict]
        self._key_images = {}   # key → {"series_uid", "index", "description"}
        self._init_history()

    # 주석
    def items(self, key):
        return self._items.get(key, []) if key else []

    def all_items(self):
        """[(영상 키, 주석)] 모든 영상"""
        return [(key, ann) for key, anns in self._items.items() for ann in anns]

    def find(self, ann_id):
        """id로 (영상 키, 주석) 찾기. 없으면 (None, None)"""
        for key, anns in self._items.items():
            for ann in anns:
                if ann.get("id") == ann_id:
                    return key, ann
        return None, None

    def add(self, key, ann):
        if key:
            ensure_fields(ann)
            with self.edit(key):
                self._items.setdefault(key, []).append(ann)

    def update(self, ann_id, **changes):
        """주석 필드 변경 (이름·색·표시·잠금·좌표·측정값). 바뀌었으면 True"""
        key, ann = self.find(ann_id)
        if ann is None:
            return False
        if all(ann.get(k) == v for k, v in changes.items()):
            return False
        with self.edit(key):
            ann.update(changes)
        return True

    def remove(self, key, ann_id):
        items = self._items.get(key) or []
        for ann in items:
            if ann.get("id") == ann_id:
                with self.edit(key):
                    items.remove(ann)
                    if not items:
                        del self._items[key]
                return True
        return False

    def remove_last(self, key, skip_locked=True):
        items = self._items.get(key)
        if not items:
            return False
        for ann in reversed(items):
            if skip_locked and ann.get("locked"):
                continue
            return self.remove(key, ann["id"] if "id" in ann else ensure_fields(ann)["id"])
        return False

    def clear_image(self, key):
        if key in self._items:
            with self.edit(key):
                self._items.pop(key, None)

    def clear(self):
        with self.edit(*list(self._items)):
            self._items.clear()

    def count(self):
        return sum(len(v) for v in self._items.values())

    # 되돌리기 / 다시 하기 (영상 키 단위 스냅샷)
    def _init_history(self):
        if not hasattr(self, "_undo"):
            self._undo, self._redo = [], []
            self._group = None
            self.last_edit_time = 0.0

    @contextmanager
    def edit(self, *keys):
        """이 블록 안의 변경을 되돌리기 한 단계로 기록 (group() 안이면 합침)"""
        self._init_history()
        before = {k: copy.deepcopy(self._items.get(k)) for k in keys}
        yield
        after = {k: copy.deepcopy(self._items.get(k)) for k in keys}
        if before == after:
            return
        if self._group is not None:
            for k in keys:
                self._group[0].setdefault(k, before[k])
                self._group[1][k] = after[k]
        else:
            self._push(before, after)
        self.changed.emit()

    @contextmanager
    def group(self):
        """여러 영상에 걸친 작업(다른 슬라이스에 붙이기, 템플릿 적용 등)을 되돌리기 한 번으로"""
        self._init_history()
        if self._group is not None:
            yield
            return
        self._group = ({}, {})
        try:
            yield
        finally:
            before, after = self._group
            self._group = None
            if before:
                self._push(before, after)

    def _push(self, before, after):
        self._undo.append((before, after))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        self.last_edit_time = time.monotonic()

    def _restore(self, state):
        for key, anns in state.items():
            if anns:
                self._items[key] = copy.deepcopy(anns)
            else:
                self._items.pop(key, None)
        self.changed.emit()

    def can_undo(self):
        self._init_history()
        return bool(self._undo)

    def can_redo(self):
        self._init_history()
        return bool(self._redo)

    def undo(self):
        self._init_history()
        if not self._undo:
            return False
        before, after = self._undo.pop()
        self._redo.append((before, after))
        self._restore(before)
        return True

    def redo(self):
        self._init_history()
        if not self._redo:
            return False
        before, after = self._redo.pop()
        self._undo.append((before, after))
        self._restore(after)
        return True

    # Key Image
    def is_key_image(self, key):
        return key in self._key_images

    def toggle_key_image(self, series, index):
        """토글 후 새 상태(True=마킹됨) 반환"""
        key = image_key(series, index)
        if key is None:
            return False
        if key in self._key_images:
            del self._key_images[key]
            marked = False
        else:
            self._key_images[key] = {"series_uid": series.series_uid, "index": index,
                                     "description": series.description}
            marked = True
        self.changed.emit()
        return marked

    def key_images(self):
        """[(key, info)] 마킹한 순서대로"""
        return list(self._key_images.items())

    def clear_key_images(self):
        self._key_images.clear()
        self.changed.emit()

    # JSON
    def to_dict(self):
        return {
            "format": FILE_FORMAT,
            "version": FILE_VERSION,
            "annotations": {k: _to_jsonable(v) for k, v in self._items.items()},
            "key_images": _to_jsonable(self._key_images),
        }

    def save_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def load_json(self, path, merge=False):
        """반환: 불러온 주석 수. 형식이 다르면 ValueError"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") not in (FILE_FORMAT,) + LEGACY_FORMATS:
            raise ValueError("DabbaView 주석 파일이 아닙니다.")
        if not merge:
            self._items.clear()
            self._key_images.clear()
        count = 0
        for key, anns in data.get("annotations", {}).items():
            for ann in anns:
                self._items.setdefault(key, []).append(ensure_fields(_from_json_annotation(ann)))
                count += 1
        for key, info in data.get("key_images", {}).items():
            self._key_images[key] = info
        self.changed.emit()
        return count
