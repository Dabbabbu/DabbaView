# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""ROI·측정 계산 (연구용): 모양 만들기, 통계, 환자 좌표 변환, ROI 세트 저장/적용, 부피

좌표계
- 이미지 좌표 (x, y): 픽셀 i는 [i, i+1) 구간, 중심은 i + 0.5  (viewport와 같음)
- 환자 좌표 LPS (mm): IPP + (x-0.5)·열간격·행방향 + (y-0.5)·행간격·열방향
ROI 세트 파일(.roi.json)은 점을 환자 좌표(mm)와 영상 상대 위치 둘 다 저장해서
같은 좌표계(T1 → T2)에는 mm로, 다른 환자에는 상대 위치로 적용할 수 있음.
"""
import json
import math

import numpy as np

from . import dicom_info
from .annotations import ROI_TYPES, ensure_fields, new_id
from .roi import (ellipse_mask, polygon_area_mm2, polygon_mask, polygon_perimeter_mm)

ROI_FILE_FORMAT = "dabbaview-roi"
ROI_FILE_VERSION = 1

DEFAULT_COLORS = {"roi": "#ff8c00", "ellipse": "#50ff78", "rect": "#ff5ad2",
                  "distance": "#ffff00", "path": "#ffd24a", "angle": "#00ffff",
                  "area": "#78dcff", "cobb": "#ffc83c"}
TYPE_NAMES = {"roi": "Freehand", "ellipse": "Ellipse", "rect": "Rectangle",
              "distance": "Distance", "path": "Path", "angle": "Angle", "area": "Area",
              "cobb": "Cobb", "arrow": "Arrow", "text": "Text"}


def type_name(ann):
    if ann["type"] == "ellipse" and ann.get("circle"):
        return "Circle"
    return TYPE_NAMES.get(ann["type"], ann["type"])


def color_of(ann):
    return ann.get("color") or DEFAULT_COLORS.get(ann["type"], "#ffffff")


# ─── 모양 ───

def make_shape(kind, center, width_mm, height_mm, spacing):
    """정량 ROI: 중심(이미지 좌표) + 가로/세로(mm) → 주석 점 (ellipse/rect는 두 모서리)"""
    row_sp, col_sp = spacing
    hx, hy = width_mm / 2 / col_sp, height_mm / 2 / row_sp
    cx, cy = center
    return [(cx - hx, cy - hy), (cx + hx, cy + hy)]


def shape_params(ann, spacing):
    """(중심 x, 중심 y, 가로 mm, 세로 mm) - 다각형은 외접 사각형 기준"""
    row_sp, col_sp = spacing
    pts = np.asarray(ann["pts"], dtype=float)
    if ann["type"] in ("ellipse", "rect"):
        (x0, y0), (x1, y1) = pts[:2]
        return ((x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) * col_sp, abs(y1 - y0) * row_sp)
    lo, hi = pts.min(0), pts.max(0)
    center = pts.mean(0) if len(pts) else lo
    return (float(center[0]), float(center[1]), float((hi[0] - lo[0]) * col_sp),
            float((hi[1] - lo[1]) * row_sp))


def moved(ann, dx, dy):
    return [(x + dx, y + dy) for x, y in ann["pts"]]


def resized(ann, center, width_mm, height_mm, spacing):
    """새 중심·크기로 점 변환 (ellipse/rect는 모서리, 다각형은 중심 기준 확대/축소)"""
    if ann["type"] in ("ellipse", "rect"):
        return make_shape(ann["type"], center, width_mm, height_mm, spacing)
    cx, cy, w, h = shape_params(ann, spacing)
    sx = width_mm / w if w > 0 else 1.0
    sy = height_mm / h if h > 0 else 1.0
    return [(center[0] + (x - cx) * sx, center[1] + (y - cy) * sy) for x, y in ann["pts"]]


def mirrored(ann, image_width):
    """좌우 대칭 (영상 가운데 세로선 기준) - 반대쪽 같은 위치에 붙이기"""
    return [(image_width - x, y) for x, y in ann["pts"]]


def polygon_of(ann, n=96):
    """ROI 외곽선을 다각형 점으로 (부피·마스크·다른 영상 투영에 사용)"""
    kind = ann["type"]
    if kind == "ellipse":
        (x0, y0), (x1, y1) = ann["pts"][:2]
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
        return [(cx + rx * math.cos(t), cy + ry * math.sin(t))
                for t in np.linspace(0, 2 * math.pi, n, endpoint=False)]
    if kind == "rect":
        (x0, y0), (x1, y1) = ann["pts"][:2]
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return list(ann["pts"])


def mask_of(ann, shape):
    kind = ann["type"]
    if kind == "ellipse":
        return ellipse_mask(ann["pts"][0], ann["pts"][1], shape)
    if kind == "rect":
        rows, cols = shape[:2]
        (x0, y0), (x1, y1) = ann["pts"][:2]
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        xx = np.arange(cols) + 0.5
        yy = np.arange(rows) + 0.5
        return ((yy[:, None] >= ya) & (yy[:, None] <= yb)) & ((xx[None] >= xa) & (xx[None] <= xb))
    return polygon_mask(ann["pts"], shape)


def roi_statistics(ann, arr, spacing, factor=1.0):
    """Area(mm²), Perimeter(mm), Mean, StdDev, Min, Max, Median, Pixels"""
    kind = ann["type"]
    row_sp, col_sp = spacing
    if kind == "ellipse":
        (x0, y0), (x1, y1) = ann["pts"][:2]
        a, b = abs(x1 - x0) / 2 * col_sp, abs(y1 - y0) / 2 * row_sp
        h = ((a - b) / (a + b)) ** 2 if a + b > 0 else 0
        area = math.pi * a * b
        perimeter = math.pi * (a + b) * (1 + 3 * h / (10 + math.sqrt(4 - 3 * h)))
    else:
        poly = polygon_of(ann)
        area = polygon_area_mm2(poly, spacing)
        perimeter = polygon_perimeter_mm(poly, spacing)
    stats = {"area_mm2": float(area), "perimeter_mm": float(perimeter), "pixels": 0}
    if arr is None or np.ndim(arr) != 2:
        return stats
    values = np.asarray(arr)[mask_of(ann, arr.shape)] * factor
    stats["pixels"] = int(values.size)
    if values.size:
        stats.update(mean=float(values.mean()), std=float(values.std()),
                     min=float(values.min()), max=float(values.max()),
                     median=float(np.median(values)))
    return stats


def path_length_mm(pts, spacing):
    return polygon_perimeter_mm(pts, spacing, closed=False)


def angle_deg(p1, vertex, p3, spacing):
    row_sp, col_sp = spacing
    v1 = ((p1[0] - vertex[0]) * col_sp, (p1[1] - vertex[1]) * row_sp)
    v2 = ((p3[0] - vertex[0]) * col_sp, (p3[1] - vertex[1]) * row_sp)
    m1, m2 = math.hypot(*v1), math.hypot(*v2)
    if m1 == 0 or m2 == 0:
        return None
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)))
    return math.degrees(math.acos(cos))


def recompute(ann, arr, spacing, factor=1.0, label=None):
    """점을 바꾼 뒤 측정값 다시 계산 (주석 dict를 고쳐서 반환)"""
    from .roi import cobb_angle
    kind = ann["type"]
    pts = ann["pts"]
    if kind == "distance":
        ann["mm"] = path_length_mm(pts[:2], spacing)
    elif kind == "path":
        ann["mm"] = path_length_mm(pts, spacing)
    elif kind == "angle" and len(pts) == 3:
        value = angle_deg(pts[0], pts[1], pts[2], spacing)
        ann["deg"] = value if value is not None else ann.get("deg", 0.0)
    elif kind == "cobb" and len(pts) == 4:
        value = cobb_angle(*pts, spacing)
        ann["deg"] = value if value is not None else ann.get("deg", 0.0)
    elif kind == "area":
        ann["area"] = polygon_area_mm2(pts, spacing)
        ann["perimeter"] = polygon_perimeter_mm(pts, spacing)
    elif kind in ROI_TYPES:
        ann["stats"] = roi_statistics(ann, arr, spacing, factor)
        if label is not None:
            ann["label"] = label
    return ann


# ─── 환자 좌표 (mm) ───

def _geometry(ds):
    try:
        ipp = np.array([float(v) for v in ds.ImagePositionPatient])
        iop = [float(v) for v in ds.ImageOrientationPatient]
        row_sp, col_sp = [float(v) for v in ds.PixelSpacing]
    except (AttributeError, TypeError, ValueError):
        return None
    row_dir, col_dir = np.array(iop[:3]), np.array(iop[3:])
    return ipp, row_dir, col_dir, row_sp, col_sp


def image_to_patient(ds, pts):
    """이미지 좌표 목록 → 환자 좌표 LPS (mm). 공간 정보가 없으면 None"""
    g = _geometry(ds)
    if g is None:
        return None
    ipp, row_dir, col_dir, row_sp, col_sp = g
    return [(ipp + (x - 0.5) * col_sp * row_dir + (y - 0.5) * row_sp * col_dir).tolist()
            for x, y in pts]


def patient_to_image(ds, lps_pts):
    """환자 좌표 → 이 영상의 이미지 좌표와 평면까지 거리(mm) [(x, y, 거리)]"""
    g = _geometry(ds)
    if g is None:
        return None
    ipp, row_dir, col_dir, row_sp, col_sp = g
    normal = np.cross(row_dir, col_dir)
    out = []
    for p in lps_pts:
        d = np.asarray(p, dtype=float) - ipp
        out.append((float(d @ row_dir) / col_sp + 0.5, float(d @ col_dir) / row_sp + 0.5,
                    float(d @ normal)))
    return out


def slice_spacing(series):
    """슬라이스 간격 (mm): 위치 차이 → SpacingBetweenSlices → SliceThickness"""
    series.sort_slices()
    slices = series.slices
    if len(slices) >= 2:
        a, b = image_to_patient(slices[0], [(0.5, 0.5)]), image_to_patient(slices[1], [(0.5, 0.5)])
        if a and b:
            gap = float(np.linalg.norm(np.subtract(b[0], a[0])))
            if gap > 1e-3:
                return gap
    for keyword in ("SpacingBetweenSlices", "SliceThickness"):
        value = getattr(slices[0], keyword, None) if slices else None
        try:
            if value is not None and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            pass
    return 1.0


def roi_volume(entries, series):
    """같은 이름 ROI들이 여러 슬라이스에 있을 때 부피 (Cavalieri: 면적 합 × 슬라이스 간격)

    entries: [(slice index, 주석)]  → dict: 이름별 {slices, area_sum_mm2, volume_ml, spacing}
    """
    spacing_z = slice_spacing(series)
    groups = {}   # (이름, 슬라이스) → [주석]
    for index, ann in entries:
        if ann["type"] not in ROI_TYPES + ("area",):
            continue
        groups.setdefault((ann.get("name") or type_name(ann), index), []).append(ann)
    out = {}
    for (name, index), anns in sorted(groups.items(), key=lambda kv: kv[0][1]):
        ds = series.slices[index]
        sp = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
        if len(anns) == 1:
            ann = anns[0]
            area = (ann["stats"]["area_mm2"] if "stats" in ann
                    else polygon_area_mm2(polygon_of(ann), sp))
        else:   # 같은 슬라이스에 같은 이름이 여럿: 겹친 부분을 두 번 세지 않도록 합집합 픽셀 면적
            shape = (int(getattr(ds, "Rows", 0) or 0), int(getattr(ds, "Columns", 0) or 0))
            union = np.zeros(shape, dtype=bool)
            for ann in anns:
                union |= mask_of(ann, shape)
            area = float(union.sum()) * sp[0] * sp[1]
        row = out.setdefault(name, {"slices": set(), "area_sum_mm2": 0.0})
        row["slices"].add(index)
        row["area_sum_mm2"] += area
    for row in out.values():
        row["slices"] = sorted(row["slices"])
        row["spacing"] = spacing_z
        row["volume_ml"] = row["area_sum_mm2"] * spacing_z / 1000.0
    return out


# ─── ROI 세트 파일 (.roi.json) ───

def export_rois(entries, series):
    """[(slice index, 주석)] → 저장용 dict (환자 좌표 mm + 영상 상대 위치 + 크기 mm)"""
    rois = []
    for index, ann in entries:
        ds = series.slices[index]
        sp = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
        rows, cols = int(getattr(ds, "Rows", 0) or 0), int(getattr(ds, "Columns", 0) or 0)
        cx, cy, w_mm, h_mm = shape_params(ann, sp)
        center_lps = image_to_patient(ds, [(cx, cy)])
        rois.append({
            "type": ann["type"], "circle": bool(ann.get("circle")),
            "name": ann.get("name", ""), "color": color_of(ann),
            "slice_index": index,
            "points_lps": image_to_patient(ds, ann["pts"]),
            "center_lps": center_lps[0] if center_lps else None,
            "points_relative": [(x / cols, y / rows) if rows and cols else (x, y)
                                for x, y in ann["pts"]],
            "points_image": [list(p) for p in ann["pts"]],
            "size_mm": {"width": w_mm, "height": h_mm},
            "pixel_spacing": list(sp),
        })
    first = series.slices[0] if series.slices else None
    return {"format": ROI_FILE_FORMAT, "version": ROI_FILE_VERSION,
            "source": {"patient_id": str(getattr(first, "PatientID", "") or ""),
                       "study_uid": series.study_uid, "series_uid": series.series_uid,
                       "series_description": series.description,
                       "frame_of_reference": str(getattr(first, "FrameOfReferenceUID", "") or "")},
            "rois": rois}


def save_rois(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_rois(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != ROI_FILE_FORMAT:
        raise ValueError("DabbaView ROI 파일(.roi.json)이 아닙니다.")
    return data


def same_frame(data, series):
    first = series.slices[0] if series.slices else None
    ref = str(getattr(first, "FrameOfReferenceUID", "") or "")
    return bool(ref) and ref == data.get("source", {}).get("frame_of_reference")


def place_rois(data, series, mode="auto", current_index=None):
    """ROI 세트를 series에 배치 → ([(slice index, 새 주석)], 안내 문구 목록)

    mode: "patient" = 환자 좌표(mm)로 가장 가까운 슬라이스에 투영 (같은 검사의 다른 시리즈)
          "relative" = 영상 상대 위치 + 크기(mm) 유지, 슬라이스는 current_index (다른 환자·템플릿)
          "auto" = 같은 좌표계(FrameOfReference)면 patient, 아니면 relative
    """
    if mode == "auto":
        mode = "patient" if same_frame(data, series) else "relative"
    series.sort_slices()
    notes = []
    placed = []
    n = series.num_slices
    current_index = min(max(0, current_index or 0), n - 1) if n else 0
    for roi in data.get("rois", []):
        ann = {"type": roi["type"], "name": roi.get("name", ""), "color": roi.get("color"),
               "id": new_id()}
        if roi.get("circle"):
            ann["circle"] = True
        index = None
        if mode == "patient" and roi.get("points_lps") and roi.get("center_lps"):
            best = None
            for k, ds in enumerate(series.slices):
                proj = patient_to_image(ds, [roi["center_lps"]])
                if proj is None:
                    best = None
                    break
                dist = abs(proj[0][2])
                if best is None or dist < best[0]:
                    best = (dist, k)
            if best is not None:
                dist, index = best
                pts = patient_to_image(series.slices[index], roi["points_lps"])
                ann["pts"] = [(x, y) for x, y, _d in pts]
                thickness = slice_spacing(series)
                if dist > max(1.0, thickness):
                    notes.append(f"'{roi.get('name') or roi['type']}': 가장 가까운 슬라이스도 "
                                 f"{dist:.1f} mm 떨어져 있음")
        if index is None:   # 상대 위치: 같은 비율 위치, 크기는 mm 유지
            index = current_index if mode == "relative" else min(roi.get("slice_index", 0), n - 1)
            ds = series.slices[index]
            rows, cols = int(getattr(ds, "Rows", 0) or 0), int(getattr(ds, "Columns", 0) or 0)
            sp = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
            rel = roi.get("points_relative") or roi.get("points_image")
            pts = [(x * cols, y * rows) for x, y in rel]
            ann["pts"] = pts
            size = roi.get("size_mm") or {}
            if size.get("width") and size.get("height"):
                cx, cy, _w, _h = shape_params(ann, sp)
                ann["pts"] = resized(ann, (cx, cy), size["width"], size["height"], sp)
        placed.append((index, ensure_fields(ann)))
    if mode == "relative":
        notes.insert(0, "영상 상대 위치로 적용 (크기는 mm 유지). 위치를 확인하세요.")
    return placed, notes


def place_on_slice(data, series, index):
    """ROI 세트를 series의 index 슬라이스 한 장에 붙이기 (Ctrl+V)

    같은 시리즈: 같은 이미지 좌표 / 같은 좌표계: 환자 좌표를 이 평면에 투영 /
    그 밖: 영상 상대 위치 + 크기(mm) 유지
    """
    series.sort_slices()
    ds = series.slices[index]
    same_series = data.get("source", {}).get("series_uid") == series.series_uid
    frame = same_frame(data, series)
    rows, cols = int(getattr(ds, "Rows", 0) or 0), int(getattr(ds, "Columns", 0) or 0)
    sp = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
    out = []
    for roi in data.get("rois", []):
        ann = {"type": roi["type"], "name": roi.get("name", ""), "color": roi.get("color"),
               "id": new_id()}
        if roi.get("circle"):
            ann["circle"] = True
        pts = None
        if same_series and roi.get("points_image"):
            pts = [tuple(p) for p in roi["points_image"]]
        elif frame and roi.get("points_lps"):
            proj = patient_to_image(ds, roi["points_lps"])
            pts = [(x, y) for x, y, _d in proj] if proj else None
        if pts is None:
            rel = roi.get("points_relative") or roi.get("points_image")
            pts = [(x * cols, y * rows) for x, y in rel]
            ann["pts"] = pts
            size = roi.get("size_mm") or {}
            if size.get("width") and size.get("height"):
                cx, cy, _w, _h = shape_params(ann, sp)
                pts = resized(ann, (cx, cy), size["width"], size["height"], sp)
        ann["pts"] = [tuple(p) for p in pts]
        out.append(ensure_fields(ann))
    return out
