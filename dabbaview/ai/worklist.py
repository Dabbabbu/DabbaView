# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
데이터셋 워크리스트 - 라벨링할 케이스 목록과 진행 상태 (ai/worklist.json)

케이스 = 시리즈 하나. 폴더 경로를 기억해 두었다가 다시 열 수 있다.
"""
import json
import os

from PyQt5.QtCore import QObject, pyqtSignal

from . import data_dir

STATUS_TODO = "미완"
STATUS_IN_PROGRESS = "진행중"
STATUS_DONE = "완료"
STATUSES = (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE)


class Worklist(QObject):
    changed = pyqtSignal()

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self._path = path or os.path.join(data_dir(), "worklist.json")
        self._entries = []
        self._load()

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._entries = [e for e in data if isinstance(e, dict) and e.get("series_uid")]
        except (OSError, ValueError):
            self._entries = []

    def save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _changed(self):
        self.save()
        self.changed.emit()

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def get(self, series_uid):
        for e in self._entries:
            if e["series_uid"] == series_uid:
                return e
        return None

    def add(self, series):
        """시리즈 추가 (이미 있으면 정보만 갱신). 추가됐으면 True"""
        series.sort_slices()
        first = series.slices[0] if series.slices else None
        filename = getattr(first, "filename", "") or ""
        entry = self.get(series.series_uid)
        info = {
            "series_uid": series.series_uid,
            "study_uid": series.study_uid,
            "patient_id": series.patient_id,
            "patient_name": series.patient_name,
            "study_date": series.study_date,
            "modality": series.modality,
            "description": series.description,
            "slices": series.num_slices,
            "folder": os.path.dirname(str(filename)) if filename else "",
        }
        if entry is None:
            info["status"] = STATUS_TODO
            self._entries.append(info)
            self._changed()
            return True
        entry.update(info)
        self._changed()
        return False

    def remove(self, series_uid):
        self._entries = [e for e in self._entries if e["series_uid"] != series_uid]
        self._changed()

    def set_status(self, series_uid, status):
        entry = self.get(series_uid)
        if entry is not None and status in STATUSES and entry.get("status") != status:
            entry["status"] = status
            self._changed()

    def mark_edited(self, series_uid):
        """라벨을 칠하면 미완 → 진행중 (완료로 표시한 건 그대로)"""
        entry = self.get(series_uid)
        if entry is not None and entry.get("status", STATUS_TODO) == STATUS_TODO:
            entry["status"] = STATUS_IN_PROGRESS
            self._changed()

    def set_stats(self, series_uid, stats):
        entry = self.get(series_uid)
        if entry is not None:
            entry["stats"] = stats
            self.save()

    def counts(self):
        result = {s: 0 for s in STATUSES}
        for e in self._entries:
            result[e.get("status", STATUS_TODO)] = result.get(e.get("status", STATUS_TODO), 0) + 1
        return result
