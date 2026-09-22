# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
랜드마크 / Fiducial (3D Slicer Markups에 해당)

점은 환자 좌표 LPS (mm)로 저장 → 같은 Frame of Reference의 어느 시리즈·평면에서도 표시.
내보내기: CSV, JSON (3D Slicer .mrk.json 호환)

찍은 점은 검사(StudyInstanceUID)별로 이 컴퓨터의 앱 데이터 폴더(…/DabbaView/landmarks)에
저장되고, 같은 검사를 다시 열면 저절로 다시 나온다 (영상 파일은 건드리지 않음).
"""
import csv
import json
import os

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

SLICER_SCHEMA = ("https://raw.githubusercontent.com/slicer/slicer/main/Modules/Loadable/"
                 "Markups/Resources/Schema/markups-schema-v1.0.3.json#")


class LandmarkStore(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # {name, label, position(LPS), frame_uid, series_uid, study_uid, slice, series_desc}
        self._points = []
        self._counter = 0
        self._studies = set()   # 저장 파일을 읽었거나 쓴 검사 (점을 다 지우면 파일도 비움)

    def __iter__(self):
        return iter(self._points)

    def __len__(self):
        return len(self._points)

    def next_name(self):
        return f"F-{self._counter + 1}"

    def add(self, position, name=None, label="", frame_uid="", series_uid="",
            study_uid="", slice_index=None, series_desc=""):
        self._counter += 1
        point = {"name": name or f"F-{self._counter}", "label": label,
                 "position": [float(v) for v in position], "frame_uid": frame_uid,
                 "series_uid": series_uid, "study_uid": study_uid,
                 "slice": slice_index, "series_desc": series_desc}
        if study_uid:
            self._studies.add(study_uid)
        self._points.append(point)
        self.changed.emit()
        return point

    def count_by_series(self):
        """시리즈 UID → 그 시리즈에서 찍은 점 수 (시리즈 목록의 📍 표시)"""
        counts = {}
        for p in self._points:
            uid = p.get("series_uid")
            if uid:
                counts[uid] = counts.get(uid, 0) + 1
        return counts

    # ─── 검사별 자동 저장 · 불러오기 ───

    @staticmethod
    def folder():
        from ..worksave import base_dir
        return os.path.join(os.path.dirname(base_dir()), "landmarks")

    @classmethod
    def study_path(cls, study_uid):
        safe = "".join(c for c in str(study_uid) if c.isalnum() or c in "._-")[:120] or "unknown"
        return os.path.join(cls.folder(), safe + ".json")

    def load_studies(self, study_uids):
        """열린 검사의 저장된 점을 불러옴 (이미 있는 점은 다시 넣지 않음) → 새로 들어온 수"""
        added = 0
        have = {(p.get("study_uid"), p["name"], tuple(round(v, 3) for v in p["position"]))
                for p in self._points}
        for uid in study_uids:
            if not uid or uid in self._studies:
                continue
            self._studies.add(uid)
            try:
                with open(self.study_path(uid), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            for p in data.get("points", []):
                try:
                    key = (uid, p["name"], tuple(round(float(v), 3) for v in p["position"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if key in have:
                    continue
                point = {"name": str(p["name"]), "label": str(p.get("label", "")),
                         "position": [float(v) for v in p["position"]],
                         "frame_uid": str(p.get("frame_uid", "")),
                         "series_uid": str(p.get("series_uid", "")), "study_uid": uid,
                         "slice": p.get("slice"), "series_desc": str(p.get("series_desc", ""))}
                self._points.append(point)
                have.add(key)
                added += 1
                number = point["name"][2:] if point["name"].startswith("F-") else ""
                if number.isdigit():
                    self._counter = max(self._counter, int(number))
        if added:
            self.changed.emit()
        return added

    def save_studies(self):
        """검사별 파일로 저장 (점이 없어진 검사는 파일을 지움)"""
        by_study = {}
        for p in self._points:
            if p.get("study_uid"):
                by_study.setdefault(p["study_uid"], []).append(p)
        os.makedirs(self.folder(), exist_ok=True)
        for uid in self._studies | set(by_study):
            path = self.study_path(uid)
            points = by_study.get(uid, [])
            try:
                if not points:
                    if os.path.exists(path):
                        os.remove(path)
                    continue
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "coordinate_system": "LPS", "points": points},
                              f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
            except OSError:
                pass

    def remove(self, index):
        if 0 <= index < len(self._points):
            del self._points[index]
            self.changed.emit()

    def clear(self):
        """모든 점 지우기 (저장 파일도 다음 저장 때 비워짐)"""
        self._points.clear()
        self._counter = 0
        self.changed.emit()

    def update(self, index, **fields):
        if 0 <= index < len(self._points):
            self._points[index].update(fields)
            self.changed.emit()

    def points_near(self, geometry, index, tolerance_mm, frame_uid=""):
        """슬라이스 평면(geometry, index)에서 tolerance 안에 있는 점 → [(i, col, row, 거리)]"""
        result = []
        for i, p in enumerate(self._points):
            if frame_uid and p.get("frame_uid") and p["frame_uid"] != frame_uid:
                continue
            col, row, dist = geometry.patient_to_pixel(index, p["position"])
            if abs(dist) <= tolerance_mm:
                result.append((i, col, row, dist))
        return result

    # ─── 파일 ───

    def save_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "label", "x_mm (L+)", "y_mm (P+)", "z_mm (S+)", "coordinate_system"])
            for p in self._points:
                w.writerow([p["name"], p["label"]] + [f"{v:.3f}" for v in p["position"]] + ["LPS"])

    def save_json(self, path):
        """3D Slicer Markups 형식 (Fiducial, LPS)"""
        data = {"@schema": SLICER_SCHEMA, "markups": [{
            "type": "Fiducial", "coordinateSystem": "LPS", "coordinateUnits": "mm",
            "controlPoints": [{"id": str(i + 1), "label": p["name"],
                               "description": p["label"], "position": p["position"],
                               "positionStatus": "defined"}
                              for i, p in enumerate(self._points)]}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_json(self, path):
        """Slicer .mrk.json 또는 이 앱이 쓴 JSON. RAS 좌표면 LPS로 변환"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        added = 0
        for markup in data.get("markups", []):
            ras = str(markup.get("coordinateSystem", "LPS")).upper() == "RAS"
            for cp in markup.get("controlPoints", []):
                pos = np.array(cp.get("position", [0, 0, 0]), dtype=float)
                if ras:
                    pos[:2] *= -1
                self.add(pos, name=cp.get("label"), label=cp.get("description", ""))
                added += 1
        return added
