# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
세그멘테이션 라벨 목록 (전체 데이터셋이 같은 라벨 번호 체계를 공유)

마스크 값 0 = 배경, 1~255 = 라벨 번호.
"""
import json
import os

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from . import data_dir

MAX_LABEL = 255

DEFAULT_LABELS = [
    {"id": 1, "name": "Liver", "color": [230, 60, 60]},
    {"id": 2, "name": "Kidney", "color": [60, 110, 240]},
    {"id": 3, "name": "Lesion", "color": [250, 210, 40]},
]

# 새 라벨에 차례로 쓰는 색 (구분이 잘 되는 색)
PALETTE = [(230, 60, 60), (60, 110, 240), (250, 210, 40), (60, 200, 90),
           (200, 80, 220), (40, 210, 210), (250, 140, 30), (160, 110, 60),
           (240, 120, 170), (140, 200, 40), (120, 120, 255), (255, 90, 90)]


class LabelSet(QObject):
    """라벨 목록 + 표시 여부. 변경 시 changed 시그널, 파일(labels.json)에 저장"""

    changed = pyqtSignal()

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self._path = path or os.path.join(data_dir(), "labels.json")
        self._labels = []
        self._hidden = set()
        self._lut_cache = None   # (opacity, table)
        self._load()
        self.changed.connect(self._invalidate_lut)

    def _invalidate_lut(self):
        self._lut_cache = None

    # ─── 저장 ───

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            labels = [{"id": int(l["id"]), "name": str(l["name"]),
                       "color": [int(c) for c in l["color"]][:3]} for l in data]
            if labels:
                self._labels = labels
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self._labels = [dict(l) for l in DEFAULT_LABELS]

    def save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._labels, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _changed(self):
        self.save()
        self.changed.emit()

    # ─── 조회 ───

    def __iter__(self):
        return iter(self._labels)

    def __len__(self):
        return len(self._labels)

    def ids(self):
        return [l["id"] for l in self._labels]

    def get(self, label_id):
        for l in self._labels:
            if l["id"] == label_id:
                return l
        return None

    def by_name(self, name):
        name = name.strip().lower()
        for l in self._labels:
            if l["name"].lower() == name:
                return l
        return None

    def name(self, label_id):
        l = self.get(label_id)
        return l["name"] if l else f"Label {label_id}"

    def color(self, label_id):
        l = self.get(label_id)
        return tuple(l["color"]) if l else PALETTE[(label_id - 1) % len(PALETTE)]

    def is_visible(self, label_id):
        return label_id not in self._hidden

    def lut(self, opacity):
        """마스크 값 → RGBA 색 (256, 4) uint8. 숨긴 라벨·배경은 투명"""
        if self._lut_cache is not None and self._lut_cache[0] == opacity:
            return self._lut_cache[1]
        table = np.zeros((256, 4), dtype=np.uint8)
        alpha = int(round(max(0.0, min(1.0, opacity)) * 255))
        for label_id in range(1, 256):
            if label_id in self._hidden:
                continue
            table[label_id, :3] = self.color(label_id)
            table[label_id, 3] = alpha
        self._lut_cache = (opacity, table)
        return table

    # ─── 편집 ───

    def add(self, name=None, color=None):
        used = set(self.ids())
        new_id = next((i for i in range(1, MAX_LABEL + 1) if i not in used), None)
        if new_id is None:
            return None
        label = {"id": new_id, "name": name or f"Label {new_id}",
                 "color": list(color or PALETTE[(new_id - 1) % len(PALETTE)])}
        self._labels.append(label)
        self._changed()
        return label

    def ensure(self, name, label_id=None):
        """이름이 같은 라벨이 있으면 그 라벨, 없으면 새로 추가 (가능하면 label_id 사용)"""
        existing = self.by_name(name)
        if existing is not None:
            return existing
        if label_id is not None and self.get(label_id) is None and 0 < label_id <= MAX_LABEL:
            label = {"id": int(label_id), "name": name,
                     "color": list(PALETTE[(label_id - 1) % len(PALETTE)])}
            self._labels.append(label)
            self._labels.sort(key=lambda l: l["id"])
            self._changed()
            return label
        return self.add(name)

    def remove(self, label_id):
        self._labels = [l for l in self._labels if l["id"] != label_id]
        self._hidden.discard(label_id)
        self._changed()

    def rename(self, label_id, name):
        l = self.get(label_id)
        if l and name.strip():
            l["name"] = name.strip()
            self._changed()

    def set_color(self, label_id, color):
        l = self.get(label_id)
        if l:
            l["color"] = [int(c) for c in color][:3]
            self._changed()

    def set_visible(self, label_id, visible):
        if visible:
            self._hidden.discard(label_id)
        else:
            self._hidden.add(label_id)
        self.changed.emit()

    def to_list(self):
        return [dict(l) for l in self._labels]
