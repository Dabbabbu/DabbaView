# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Freehand ROI 계산: 면적/둘레(mm), 영역 내 픽셀 통계

좌표계: 뷰포트 이미지 좌표 (x, y) - 픽셀 i는 [i, i+1) 구간, 중심은 i + 0.5
"""
import cv2
import numpy as np


def polygon_area_mm2(points, spacing):
    """신발끈 공식으로 다각형 면적 (mm²). spacing = (행 간격, 열 간격)"""
    if len(points) < 3:
        return 0.0
    row_sp, col_sp = spacing
    pts = np.asarray(points, dtype=float)
    x, y = pts[:, 0] * col_sp, pts[:, 1] * row_sp
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def polygon_perimeter_mm(points, spacing, closed=True):
    if len(points) < 2:
        return 0.0
    row_sp, col_sp = spacing
    pts = np.asarray(points, dtype=float)
    if closed:
        pts = np.vstack([pts, pts[:1]])
    d = np.diff(pts, axis=0) * [col_sp, row_sp]
    return float(np.sum(np.hypot(d[:, 0], d[:, 1])))


def polygon_mask(points, shape):
    """다각형 내부에 중심이 있는 픽셀 마스크 (bool, shape=(rows, cols))"""
    mask = np.zeros(shape[:2], dtype=np.uint8)
    if len(points) < 3:
        return mask.astype(bool)
    # 픽셀 중심 좌표계로 옮기고 1/16 픽셀 정밀도로 채움
    shift = 4
    pts = np.round((np.asarray(points, dtype=float) - 0.5) * (1 << shift))
    cv2.fillPoly(mask, [pts.astype(np.int32)], 1, lineType=cv2.LINE_8,
                 shift=shift)
    return mask.astype(bool)


def ellipse_mask(p1, p2, shape):
    """두 모서리(이미지 좌표)로 정의한 축 정렬 타원 내부 픽셀 마스크"""
    rows, cols = shape[:2]
    (x0, y0), (x1, y1) = p1, p2
    cx, cy = (x0 + x1) / 2 - 0.5, (y0 + y1) / 2 - 0.5  # 픽셀 중심 좌표계
    ax, ay = abs(x1 - x0) / 2, abs(y1 - y0) / 2
    if ax <= 0 or ay <= 0:
        return np.zeros((rows, cols), dtype=bool)
    yy, xx = np.ogrid[:rows, :cols]
    return ((xx - cx) / ax) ** 2 + ((yy - cy) / ay) ** 2 <= 1.0


def ellipse_statistics(arr, p1, p2, spacing, factor=1.0):
    """타원 ROI 통계: area_mm2 (π·a·b), perimeter_mm (Ramanujan 근사), 픽셀 통계"""
    row_sp, col_sp = spacing
    a = abs(p2[0] - p1[0]) / 2 * col_sp
    b = abs(p2[1] - p1[1]) / 2 * row_sp
    h = ((a - b) / (a + b)) ** 2 if a + b > 0 else 0
    stats = {
        "area_mm2": float(np.pi * a * b),
        "perimeter_mm": float(np.pi * (a + b) * (1 + 3 * h / (10 + np.sqrt(4 - 3 * h)))),
        "pixels": 0,
    }
    if arr is None or arr.ndim != 2:
        return stats
    values = arr[ellipse_mask(p1, p2, arr.shape)] * factor
    stats["pixels"] = int(values.size)
    if values.size:
        stats.update(mean=float(values.mean()), std=float(values.std()),
                     min=float(values.min()), max=float(values.max()))
    return stats


def cobb_angle(a1, a2, b1, b2, spacing):
    """두 직선 사이 각도 (0~90°, mm 공간 기준)"""
    row_sp, col_sp = spacing
    u = np.array([(a2[0] - a1[0]) * col_sp, (a2[1] - a1[1]) * row_sp])
    v = np.array([(b2[0] - b1[0]) * col_sp, (b2[1] - b1[1]) * row_sp])
    nu, nv = np.linalg.norm(u), np.linalg.norm(v)
    if nu == 0 or nv == 0:
        return None
    cos = min(1.0, abs(float(u @ v)) / (nu * nv))
    return float(np.degrees(np.arccos(cos)))


def roi_statistics(arr, points, spacing, factor=1.0):
    """ROI 통계 dict: area_mm2, perimeter_mm, pixels, mean, std, min, max

    arr: Rescale 적용된 2D 배열 (컬러 영상이면 통계 없이 면적만)
    factor: 표시 단위 환산 계수 (예: SUV)
    """
    stats = {
        "area_mm2": polygon_area_mm2(points, spacing),
        "perimeter_mm": polygon_perimeter_mm(points, spacing),
        "pixels": 0,
    }
    if arr is None or arr.ndim != 2:
        return stats
    mask = polygon_mask(points, arr.shape)
    values = arr[mask] * factor
    stats["pixels"] = int(values.size)
    if values.size:
        stats.update(mean=float(values.mean()), std=float(values.std()),
                     min=float(values.min()), max=float(values.max()))
    return stats
