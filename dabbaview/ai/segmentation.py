# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
세그멘테이션 마스크 편집 (Brush / Eraser / Magic Wand / Threshold / 슬라이스 보간)

시리즈마다 라벨 마스크 mask[k, row, col] (uint8, 0 = 배경)를 하나씩 가지며
앱 데이터 폴더(ai/masks/<SeriesInstanceUID>.npz)에 자동 저장된다.
"""
import hashlib
import math
import os

import numpy as np
from scipy import ndimage
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from . import data_dir
from .volume import series_shape, series_spacing_affine

UNDO_LIMIT = 30

TOOL_NONE = None
TOOL_BRUSH = "brush"
TOOL_ERASER = "eraser"
TOOL_WAND = "wand"
TOOL_THRESHOLD = "threshold"   # 클릭한 슬라이스에 임계값 범위 적용
TOOLS = (TOOL_BRUSH, TOOL_ERASER, TOOL_WAND, TOOL_THRESHOLD)


def _mask_file(series_uid):
    # UID가 길거나 파일명에 안 맞는 문자가 있을 수 있어 해시 사용
    name = hashlib.sha1(series_uid.encode()).hexdigest()[:24]
    return os.path.join(data_dir("masks"), f"{name}.npz")


def disk(radius):
    """반지름(픽셀) 원형 브러시 마스크"""
    r = max(0, int(math.ceil(radius)))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= radius * radius + 0.25


def interpolate_slices(mask, label_id):
    """라벨이 칠해진 슬라이스 사이의 빈 슬라이스를 형태 기반 보간으로 채움

    각 슬라이스의 부호 거리 맵(안쪽 음수)을 선형 보간 → 0 미만인 곳이 라벨.
    다른 라벨이 이미 칠해진 픽셀은 덮어쓰지 않는다. 채운 슬라이스 수 반환.
    """
    present = [k for k in range(mask.shape[0]) if np.any(mask[k] == label_id)]
    filled = 0
    for k0, k1 in zip(present, present[1:]):
        if k1 - k0 < 2:
            continue
        s0 = _signed_distance(mask[k0] == label_id)
        s1 = _signed_distance(mask[k1] == label_id)
        for k in range(k0 + 1, k1):
            t = (k - k0) / (k1 - k0)
            region = ((1 - t) * s0 + t * s1) < 0
            target = mask[k]
            target[region & (target == 0)] = label_id
            filled += 1
    return filled


def _signed_distance(binary):
    if not binary.any():
        return np.full(binary.shape, 1e6, dtype=np.float32)
    inside = ndimage.distance_transform_edt(binary)
    outside = ndimage.distance_transform_edt(~binary)
    return (outside - inside).astype(np.float32)


def label_statistics(mask, labels, spacing):
    """라벨별 통계 [{id, name, voxels, volume_ml, slices, first, last}]"""
    counts = np.bincount(mask.ravel(), minlength=256) if mask is not None else None
    voxel_ml = float(np.prod(spacing)) / 1000.0
    stats = []
    for label in labels:
        lid = label["id"]
        voxels = int(counts[lid]) if counts is not None else 0
        slices = []
        if voxels:
            slices = np.nonzero((mask == lid).any(axis=(1, 2)))[0]
        stats.append({"id": lid, "name": label["name"], "voxels": voxels,
                      "volume_ml": voxels * voxel_ml, "slices": len(slices),
                      "first": int(slices[0]) + 1 if len(slices) else None,
                      "last": int(slices[-1]) + 1 if len(slices) else None})
    return stats


class SegCase:
    """시리즈 하나의 마스크 + 되돌리기 기록"""

    def __init__(self, series):
        self.series = series
        self.uid = series.series_uid
        self.shape = series_shape(series)   # None이면 편집 불가 (크기 혼재)
        self.spacing, self.affine_lps = series_spacing_affine(series)
        self._mask = None
        self._loaded = False
        self.version = 0          # 변경될 때마다 증가 (오버레이 캐시 키)
        self.dirty = False
        self._undo = []

    @property
    def editable(self):
        return self.shape is not None

    @property
    def mask(self):
        """마스크 배열 (처음 접근 시 저장 파일에서 불러오거나 빈 배열 생성)"""
        if self._mask is None and self.editable:
            self._mask = self._load() if not self._loaded else None
            self._loaded = True
            if self._mask is None:
                self._mask = np.zeros(self.shape, dtype=np.uint8)
        return self._mask

    def has_saved(self):
        return os.path.exists(_mask_file(self.uid))

    def slice_mask(self, k):
        """슬라이스 k의 마스크 (저장된 게 없고 아직 안 만들었으면 None)"""
        if not self.editable or not (0 <= k < self.shape[0]):
            return None
        if self._mask is None and not self.has_saved():
            return None
        return self.mask[k]

    def is_empty(self):
        if self._mask is None:
            return not self.has_saved()
        return not self._mask.any()

    # ─── 되돌리기 ───

    def push_undo(self, k=None):
        """편집 전 상태 저장. k가 있으면 그 슬라이스만, 없으면 전체"""
        data = self.mask[k].copy() if k is not None else self.mask.copy()
        self._undo.append((k, data))
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)

    def undo(self):
        if not self._undo:
            return False
        k, data = self._undo.pop()
        if k is None:
            self._mask[...] = data
        else:
            self._mask[k] = data
        self.touch()
        return True

    def can_undo(self):
        return bool(self._undo)

    def touch(self):
        self.version += 1
        self.dirty = True

    def set_mask(self, mask):
        """마스크 전체 교체 (모델 추론 결과 등) - 되돌리기 가능"""
        mask = np.asarray(mask, dtype=np.uint8)
        if mask.shape != tuple(self.shape):
            raise ValueError(f"마스크 크기 {mask.shape}가 영상 {tuple(self.shape)}와 다릅니다.")
        self.push_undo()
        self._mask[...] = mask
        self.touch()

    # ─── 저장 ───

    def _load(self):
        path = _mask_file(self.uid)
        try:
            with np.load(path) as data:
                mask = data["mask"]
            if mask.shape == tuple(self.shape):
                return mask.astype(np.uint8)
        except (OSError, KeyError, ValueError):
            pass
        return None

    def save(self):
        if not self.dirty or self._mask is None:
            return
        path = _mask_file(self.uid)
        if not self._mask.any():
            if os.path.exists(path):
                os.remove(path)
        else:
            np.savez_compressed(path, mask=self._mask, series_uid=self.uid)
        self.dirty = False


class SegmentationController(QObject):
    """뷰포트 ↔ 마스크 편집 연결. 모든 뷰포트가 하나를 공유"""

    changed = pyqtSignal(str)          # 변경된 시리즈 UID (오버레이 다시 그리기)
    tool_changed = pyqtSignal(object)  # 현재 도구 (None = 세그멘테이션 끔)
    status = pyqtSignal(str)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self.labels = labels
        self._cases = {}
        self.tool = TOOL_NONE
        self.active_label = labels.ids()[0] if len(labels) else 1
        self.brush_radius = 8.0          # 영상 픽셀
        self.wand_tolerance = 50.0       # 픽셀 값 (HU 등)
        self.wand_3d = False
        self.threshold_range = (-100.0, 200.0)
        self.opacity = 0.45
        self.show_overlay = True
        self._stroke = None              # (case, k, 마지막 위치)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1500)
        self._save_timer.timeout.connect(self.save_all)
        labels.changed.connect(lambda: self.changed.emit(""))

    # ─── 케이스 ───

    def case(self, series):
        if series is None:
            return None
        case = self._cases.get(series.series_uid)
        if case is None or case.series is not series:
            if case is not None:
                case.save()  # 같은 시리즈를 다시 불러온 경우: 편집 내용 보존 후 새로 읽음
            case = SegCase(series)
            self._cases[series.series_uid] = case
        return case

    def cases(self):
        return list(self._cases.values())

    def _edited(self, case):
        case.touch()
        self.changed.emit(case.uid)
        self._save_timer.start()

    def save_all(self):
        for case in self._cases.values():
            case.save()

    # ─── 설정 ───

    def set_tool(self, tool):
        if tool == self.tool:
            return
        self.tool = tool
        self._stroke = None
        self.tool_changed.emit(tool)

    def set_opacity(self, opacity):
        self.opacity = opacity
        self.changed.emit("")

    def set_show_overlay(self, show):
        self.show_overlay = show
        self.changed.emit("")

    def overlay_rgba(self, series, k):
        """슬라이스 k의 컬러 오버레이 (h, w, 4) uint8. 없으면 None"""
        if not self.show_overlay or series is None:
            return None
        case = self._cases.get(series.series_uid)
        if case is None:
            if not os.path.exists(_mask_file(series.series_uid)):
                return None
            case = self.case(series)
        sl = case.slice_mask(k)
        if sl is None or not sl.any():
            return None
        return self.labels.lut(self.opacity)[sl]

    # ─── 편집 동작 (뷰포트에서 호출, 좌표 = 영상 픽셀 (x, y)) ───

    def press(self, series, k, pos, pixels=None):
        case = self.case(series)
        if case is None or not case.editable or pos is None:
            if case is not None and not case.editable:
                self.status.emit("슬라이스 크기가 섞인 시리즈는 세그멘테이션할 수 없습니다.")
            return False
        if self.tool in (TOOL_BRUSH, TOOL_ERASER):
            case.push_undo(k)
            self._stroke = (case, k, pos)
            self._stamp(case, k, pos, pos)
            return True
        if self.tool == TOOL_WAND:
            self._magic_wand(case, k, pos)
            return True
        if self.tool == TOOL_THRESHOLD:
            self.apply_threshold(series, slices=[k])
            return True
        return False

    def move(self, pos):
        if self._stroke is None or pos is None:
            return
        case, k, last = self._stroke
        self._stamp(case, k, last, pos)
        self._stroke = (case, k, pos)

    def release(self):
        self._stroke = None

    def _stamp(self, case, k, p0, p1):
        """p0→p1 선분을 따라 원형 브러시 찍기"""
        mask = case.mask[k]
        h, w = mask.shape
        r = self.brush_radius
        brush = disk(r)
        br = brush.shape[0] // 2
        length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        steps = max(1, int(math.ceil(length / max(1.0, r / 4))))
        value = 0 if self.tool == TOOL_ERASER else self.active_label
        for i in range(steps + 1):
            t = i / steps
            cx = int(round(p0[0] + (p1[0] - p0[0]) * t - 0.5))
            cy = int(round(p0[1] + (p1[1] - p0[1]) * t - 0.5))
            x0, x1 = max(0, cx - br), min(w, cx + br + 1)
            y0, y1 = max(0, cy - br), min(h, cy + br + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            b = brush[y0 - (cy - br):y1 - (cy - br), x0 - (cx - br):x1 - (cx - br)]
            region = mask[y0:y1, x0:x1]
            if value == 0:
                # 지우개는 현재 라벨만 지움 (다른 라벨 보존)
                region[b & (region == self.active_label)] = 0
            else:
                region[b] = value
        self._edited(case)

    def _magic_wand(self, case, k, pos):
        """클릭한 픽셀 값 ±허용범위 안에서 연결된 영역을 현재 라벨로"""
        series = case.series
        x, y = int(pos[0]), int(pos[1])
        d, h, w = case.shape
        if not (0 <= x < w and 0 <= y < h):
            return
        pixels = series.get_pixel_array(k)
        if pixels is None or pixels.ndim != 2:
            return
        seed = float(pixels[y, x])
        tol = self.wand_tolerance
        if self.wand_3d:
            self.status.emit("Magic Wand 3D: 전체 슬라이스 읽는 중...")
            vol = series.get_volume_array()
            within = np.abs(vol - seed) <= tol
            labeled, _ = ndimage.label(within)
            region = labeled == labeled[k, y, x]
            case.push_undo()
            case.mask[region] = self.active_label
            n = int(region.sum())
        else:
            within = np.abs(pixels - seed) <= tol
            labeled, _ = ndimage.label(within)
            region = labeled == labeled[y, x]
            case.push_undo(k)
            case.mask[k][region] = self.active_label
            n = int(region.sum())
        self._edited(case)
        self.status.emit(f"Magic Wand: 값 {seed:g} ±{tol:g} → {n:,} 픽셀")

    def apply_threshold(self, series, slices=None, preview_only=False):
        """값 범위 [lo, hi]를 현재 라벨로. slices=None이면 전체 슬라이스"""
        case = self.case(series)
        if case is None or not case.editable:
            return 0
        lo, hi = sorted(self.threshold_range)
        indices = range(case.shape[0]) if slices is None else slices
        case.push_undo(None if slices is None or len(slices) > 1 else slices[0])
        total = 0
        for k in indices:
            pixels = series.get_pixel_array(k)
            if pixels is None or pixels.ndim != 2:
                continue
            region = (pixels >= lo) & (pixels <= hi)
            case.mask[k][region] = self.active_label
            total += int(region.sum())
        self._edited(case)
        where = "전체 슬라이스" if slices is None else f"슬라이스 {slices[0] + 1}"
        self.status.emit(f"Threshold {lo:g}~{hi:g} ({where}) → {total:,} 픽셀")
        return total

    def threshold_preview(self, series, k):
        """임계값 범위에 드는 픽셀 (h, w) bool - 화면 미리보기용"""
        pixels = series.get_pixel_array(k) if series is not None else None
        if pixels is None or pixels.ndim != 2:
            return None
        lo, hi = sorted(self.threshold_range)
        return (pixels >= lo) & (pixels <= hi)

    def interpolate(self, series):
        case = self.case(series)
        if case is None or not case.editable:
            return 0
        case.push_undo()
        filled = interpolate_slices(case.mask, self.active_label)
        if filled:
            self._edited(case)
        self.status.emit(f"슬라이스 보간: {self.labels.name(self.active_label)} "
                         f"{filled}장 채움" if filled else
                         "보간할 빈 슬라이스가 없습니다 (라벨이 칠해진 슬라이스가 2장 이상 필요).")
        return filled

    def clear_label(self, series, label_id=None, k=None):
        """라벨 지우기 (label_id=None이면 모든 라벨, k가 있으면 그 슬라이스만)"""
        case = self.case(series)
        if case is None or not case.editable:
            return
        case.push_undo(k)
        target = case.mask if k is None else case.mask[k]
        if label_id is None:
            target[...] = 0
        else:
            target[target == label_id] = 0
        self._edited(case)

    def remove_label_everywhere(self, label_id):
        """라벨 삭제 시 저장된 모든 마스크에서 그 값 제거"""
        for case in self._cases.values():
            if case.editable and case._mask is not None and (case._mask == label_id).any():
                case._mask[case._mask == label_id] = 0
                self._edited(case)

    def undo(self, series):
        case = self.case(series)
        if case is not None and case.undo():
            self.changed.emit(case.uid)
            self._save_timer.start()
            return True
        return False

    def set_mask(self, series, mask):
        case = self.case(series)
        case.set_mask(mask)
        self.changed.emit(case.uid)
        self._save_timer.start()

    def statistics(self, series):
        case = self.case(series)
        if case is None or not case.editable:
            return []
        mask = None if case.is_empty() else case.mask
        return label_statistics(mask, list(self.labels), case.spacing)
