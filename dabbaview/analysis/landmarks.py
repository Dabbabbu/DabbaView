# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
랜드마크 / Fiducial (3D Slicer Markups에 해당)

점은 환자 좌표 LPS (mm)로 저장 → 같은 Frame of Reference의 어느 시리즈·평면에서도 표시.
내보내기: CSV, JSON (3D Slicer .mrk.json 호환)
"""
import csv
import json

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

SLICER_SCHEMA = ("https://raw.githubusercontent.com/slicer/slicer/main/Modules/Loadable/"
                 "Markups/Resources/Schema/markups-schema-v1.0.3.json#")


class LandmarkStore(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points = []   # {name, label, position(LPS), frame_uid, series_uid}
        self._counter = 0

    def __iter__(self):
        return iter(self._points)

    def __len__(self):
        return len(self._points)

    def next_name(self):
        return f"F-{self._counter + 1}"

    def add(self, position, name=None, label="", frame_uid="", series_uid=""):
        self._counter += 1
        point = {"name": name or f"F-{self._counter}", "label": label,
                 "position": [float(v) for v in position], "frame_uid": frame_uid,
                 "series_uid": series_uid}
        self._points.append(point)
        self.changed.emit()
        return point

    def remove(self, index):
        if 0 <= index < len(self._points):
            del self._points[index]
            self.changed.emit()

    def clear(self):
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
