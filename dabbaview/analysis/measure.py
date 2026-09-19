# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
측정: 히스토그램 통계, 라인 프로파일, 입자(객체) 분석 (ImageJ Analyze 메뉴에 해당)
"""
import csv
import math

import numpy as np
from scipy import ndimage


# ─── 히스토그램 ───

def statistics(values):
    """Mean, StdDev, Min, Max, Median, Mode, N (values: 1D)"""
    values = np.asarray(values, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    if np.all(np.mod(values[:10000], 1) == 0):
        uniq, counts = np.unique(values, return_counts=True)
        mode = float(uniq[np.argmax(counts)])
    else:
        # 실수 값: 256구간 히스토그램의 최빈 구간 중앙 (ImageJ와 같은 방식)
        counts, edges = np.histogram(values, bins=256)
        i = int(np.argmax(counts))
        mode = float((edges[i] + edges[i + 1]) / 2)
    return {"N": int(values.size), "Mean": float(values.mean()),
            "StdDev": float(values.std(ddof=1)) if values.size > 1 else 0.0,
            "Min": float(values.min()), "Max": float(values.max()),
            "Median": float(np.median(values)), "Mode": mode}


def histogram(values, bins=256, value_range=None):
    values = np.asarray(values, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    return np.histogram(values, bins=bins, range=value_range)


# ─── 라인 프로파일 ───

def line_profile(image, p0, p1, spacing=(1.0, 1.0), width=1):
    """(x, y) 영상 좌표 p0→p1 선을 따라 값 (선형 보간)

    반환: (거리 mm 배열, 값 배열). spacing = (행 간격, 열 간격) mm.
    width > 1이면 선에 수직으로 width 픽셀 평균 (ImageJ line width).
    """
    (x0, y0), (x1, y1) = p0, p1
    length_px = math.hypot(x1 - x0, y1 - y0)
    n = max(2, int(math.ceil(length_px)) + 1)
    t = np.linspace(0, 1, n)
    # 영상 좌표는 픽셀 모서리 기준 → 픽셀 중심 인덱스로 0.5 이동
    xs = x0 + (x1 - x0) * t - 0.5
    ys = y0 + (y1 - y0) * t - 0.5
    if width > 1 and length_px > 0:
        nx, ny = -(y1 - y0) / length_px, (x1 - x0) / length_px
        offsets = np.linspace(-(width - 1) / 2, (width - 1) / 2, int(width))
        values = np.mean([ndimage.map_coordinates(image, [ys + o * ny, xs + o * nx],
                                                  order=1, mode="nearest")
                          for o in offsets], axis=0)
    else:
        values = ndimage.map_coordinates(np.asarray(image, dtype=np.float64), [ys, xs],
                                         order=1, mode="nearest")
    d_row, d_col = spacing
    length_mm = math.hypot((x1 - x0) * d_col, (y1 - y0) * d_row)
    return t * length_mm, values


def save_profile_csv(path, distances, values, meta=None):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if meta:
            for k, v in meta.items():
                w.writerow([f"# {k}", v])
        w.writerow(["distance_mm", "value"])
        for d, v in zip(distances, values):
            w.writerow([f"{d:.4f}", f"{v:.4f}"])


# ─── 입자 / 객체 분석 ───

def analyze_particles(binary, spacing=(1.0, 1.0), min_area_px=1, pixel_to_world=None,
                      exclude_edges=False):
    """2D 이진 마스크 → 객체별 측정 목록

    area(px, mm²), perimeter(mm), circularity(4πA/P², 1 = 원), 중심(픽셀, mm), bbox
    pixel_to_world(col, row) → (x, y, z) 환자 좌표 (있으면 중심 좌표에 추가)
    """
    from skimage import measure
    binary = np.asarray(binary, dtype=bool)
    labels, n = ndimage.label(binary, structure=np.ones((3, 3)))
    d_row, d_col = spacing
    pixel_area = d_row * d_col
    results = []
    h, w = binary.shape
    for region in measure.regionprops(labels):
        if region.area < min_area_px:
            continue
        r0, c0, r1, c1 = region.bbox
        if exclude_edges and (r0 == 0 or c0 == 0 or r1 == h or c1 == w):
            continue
        mask = labels[r0:r1, c0:c1] == region.label
        perimeter_mm = _perimeter_mm(mask, d_row, d_col)
        area_mm2 = region.area * pixel_area
        circularity = (4 * math.pi * area_mm2 / perimeter_mm ** 2) if perimeter_mm > 0 else 0.0
        cy, cx = region.centroid
        item = {"id": len(results) + 1, "area_px": int(region.area),
                "area_mm2": area_mm2, "perimeter_mm": perimeter_mm,
                "circularity": min(1.0, circularity),
                "centroid_x": cx, "centroid_y": cy,
                "bbox": (int(c0), int(r0), int(c1 - c0), int(r1 - r0))}
        if pixel_to_world is not None:
            item["world"] = tuple(float(v) for v in pixel_to_world(cx, cy))
        results.append(item)
    return results


def _perimeter_mm(mask, d_row, d_col):
    """외곽선 길이 (mm)

    정사각 픽셀이면 Crofton 추정(계단 효과 보정, 원 반지름 10px에서 오차 ~4%),
    아니면 윤곽선 꼭짓점 사이 거리 합 (이방성 간격 반영).
    """
    from skimage import measure
    if abs(d_row - d_col) <= 1e-6 * max(d_row, d_col):
        return float(measure.perimeter_crofton(np.pad(mask, 1), directions=4)) * d_row
    padded = np.pad(mask.astype(np.uint8), 1)
    total = 0.0
    for contour in measure.find_contours(padded, 0.5):
        if len(contour) < 2:
            continue
        d = np.diff(contour, axis=0)
        total += float(np.sum(np.hypot(d[:, 0] * d_row, d[:, 1] * d_col)))
    return total


PARTICLE_COLUMNS = [("id", "#"), ("area_mm2", "Area (mm²)"), ("area_px", "Area (px)"),
                    ("perimeter_mm", "Perimeter (mm)"), ("circularity", "Circularity"),
                    ("centroid_x", "X (px)"), ("centroid_y", "Y (px)")]


def save_particles_csv(path, particles, slice_index=None):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        header = ["slice"] if slice_index is not None else []
        header += [label for _, label in PARTICLE_COLUMNS] + ["X (mm)", "Y (mm)", "Z (mm)"]
        w.writerow(header)
        for p in particles:
            row = [slice_index + 1] if slice_index is not None else []
            row += [f"{p[k]:.4f}" if isinstance(p[k], float) else p[k] for k, _ in PARTICLE_COLUMNS]
            row += [f"{v:.3f}" for v in p.get("world", ("", "", ""))] if "world" in p else ["", "", ""]
            w.writerow(row)
