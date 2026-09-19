# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
병변·해부 구조 측정 - 종양(RECIST, 부피, 추적 비교), 폐(결절, 폐기종, GGO),
PET(SUV), 근골격(단면적, 지방 침윤), 혈관(직경, 협착률, Curved MPR, 동맥류)
"""
import math

import numpy as np
from scipy import ndimage


# ═══ 직경 (Feret) ═══

def _boundary_points_mm(mask2d, spacing):
    from skimage import measure
    pts = []
    for contour in measure.find_contours(np.pad(mask2d.astype(float), 1), 0.5):
        pts.append(contour - 1)                     # (row, col)
    if not pts:
        return np.zeros((0, 2))
    p = np.vstack(pts)
    return np.column_stack([p[:, 1] * spacing[1], p[:, 0] * spacing[0]])   # (x, y) mm


def _hull(points):
    if len(points) < 4:
        return points
    from scipy.spatial import ConvexHull
    try:
        return points[ConvexHull(points).vertices]
    except Exception:  # noqa: BLE001 - 일직선 등 퇴화한 경우
        return points


def feret_2d(mask2d, spacing):
    """2D 마스크 → (장경 mm, 단경 mm, 장경 양 끝점, 단경 양 끝점) - 점은 (x, y) 픽셀 좌표

    장경: 외곽선 점 사이 최대 거리. 단경: 장경에 수직인 방향의 최대 폭 (RECIST 방식)
    """
    pts = _boundary_points_mm(mask2d, spacing)
    if len(pts) < 2:
        return 0.0, 0.0, None, None
    hull = _hull(pts)
    d = np.linalg.norm(hull[:, None] - hull[None], axis=2)
    i, j = np.unravel_index(np.argmax(d), d.shape)
    long_mm = float(d[i, j])
    a, b = hull[i], hull[j]
    u = (b - a) / max(long_mm, 1e-9)
    v = np.array([-u[1], u[0]])
    # 장경 방향으로 0.5mm 구간마다 수직 폭 → 최대
    proj_u = (pts - a) @ u
    proj_v = (pts - a) @ v
    bins = np.round(proj_u / 0.5).astype(int)
    short_mm, short_pts = 0.0, None
    for key in np.unique(bins):
        sel = bins == key
        lo, hi = proj_v[sel].min(), proj_v[sel].max()
        if hi - lo > short_mm:
            short_mm = float(hi - lo)
            base = a + u * proj_u[sel].mean()
            short_pts = (base + v * lo, base + v * hi)
    to_px = np.array([1 / spacing[1], 1 / spacing[0]])
    long_px = (tuple(a * to_px + 0.5), tuple(b * to_px + 0.5))
    short_px = tuple(tuple(p * to_px + 0.5) for p in short_pts) if short_pts else None
    return long_mm, short_mm, long_px, short_px


def recist(mask3d, spacing3):
    """3D 마스크 (k, row, col) → 장경이 가장 긴 슬라이스의 RECIST 측정"""
    best = {"slice": None, "long_mm": 0.0, "short_mm": 0.0, "long_pts": None, "short_pts": None}
    for k in np.nonzero(mask3d.any(axis=(1, 2)))[0]:
        long_mm, short_mm, lp, sp = feret_2d(mask3d[k], spacing3[1:])
        if long_mm > best["long_mm"]:
            best = {"slice": int(k), "long_mm": long_mm, "short_mm": short_mm,
                    "long_pts": lp, "short_pts": sp}
    return best


def volume_ml(mask, spacing3):
    return float(np.count_nonzero(mask)) * float(np.prod(spacing3)) / 1000.0


def max_3d_diameter(mask3d, spacing3):
    """3D 최대 직경 (경계 복셀 볼록 껍질의 최대 거리, mm)"""
    m = np.asarray(mask3d, dtype=bool)
    if not m.any():
        return 0.0
    surface = m & ~ndimage.binary_erosion(m)
    pts = np.argwhere(surface) * np.asarray(spacing3, dtype=float)
    hull = _hull(pts) if len(pts) > 4 else pts
    if len(hull) > 3000:
        hull = hull[np.random.default_rng(0).choice(len(hull), 3000, replace=False)]
    d = np.linalg.norm(hull[:, None] - hull[None], axis=2)
    return float(d.max())


def lesion_summary(mask3d, spacing3, image=None):
    r = recist(mask3d, spacing3)
    out = {"Volume (mL)": volume_ml(mask3d, spacing3),
           "Long axis (mm)": r["long_mm"], "Short axis (mm)": r["short_mm"],
           "Max 3D diameter (mm)": max_3d_diameter(mask3d, spacing3),
           "RECIST slice": (r["slice"] + 1) if r["slice"] is not None else None}
    if image is not None and np.any(mask3d):
        vals = image[mask3d.astype(bool)]
        out.update({"Mean": float(vals.mean()), "SD": float(vals.std()),
                    "Min": float(vals.min()), "Max": float(vals.max())})
    return out, r


def recist_response(prior_sum_mm, current_sum_mm, nadir_mm=None):
    """단일/합계 장경 기준 RECIST 1.1 반응 (새 병변은 고려하지 않음)"""
    if current_sum_mm == 0:
        return "CR (완전 관해)"
    change = (current_sum_mm - prior_sum_mm) / prior_sum_mm * 100 if prior_sum_mm else 0
    nadir = nadir_mm if nadir_mm is not None else prior_sum_mm
    growth = (current_sum_mm - nadir) / nadir * 100 if nadir else 0
    if growth >= 20 and current_sum_mm - nadir >= 5:
        return "PD (진행)"
    if change <= -30:
        return "PR (부분 관해)"
    return "SD (안정)"


def doubling_time_days(v1_ml, v2_ml, days):
    """부피 배가 시간 (일). 줄었으면 음수(반감), 변화 없으면 inf"""
    if v1_ml <= 0 or v2_ml <= 0 or days <= 0 or v1_ml == v2_ml:
        return float("inf")
    return days * math.log(2) / math.log(v2_ml / v1_ml)


def follow_up(prior, current, days=None):
    """prior/current: lesion_summary 결과 dict"""
    out = {}
    for key in ("Volume (mL)", "Long axis (mm)", "Short axis (mm)"):
        a, b = prior.get(key) or 0, current.get(key) or 0
        out[f"{key} prior"] = a
        out[f"{key} current"] = b
        out[f"{key} change (%)"] = (b - a) / a * 100 if a else float("nan")
    out["RECIST (장경)"] = recist_response(prior.get("Long axis (mm)", 0),
                                          current.get("Long axis (mm)", 0))
    if days:
        out["Interval (days)"] = days
        out["Volume doubling time (days)"] = doubling_time_days(
            prior.get("Volume (mL)", 0), current.get("Volume (mL)", 0), days)
    return out


# ═══ 폐 ═══

def lung_mask(ct, threshold=-400):
    """CT (k, row, col, HU) → 폐 마스크 (몸 밖 공기·기도 일부 제외, 좌우 두 덩어리)"""
    air = ct < threshold
    labels, n = ndimage.label(air)
    if n == 0:
        return np.zeros_like(air)
    # 영상 가장자리에 닿는 공기 = 몸 밖
    border = set(np.unique(np.concatenate([labels[:, 0, :].ravel(), labels[:, -1, :].ravel(),
                                           labels[:, :, 0].ravel(), labels[:, :, -1].ravel()])))
    sizes = ndimage.sum(air, labels, range(1, n + 1))
    candidates = [(sizes[i - 1], i) for i in range(1, n + 1) if i not in border]
    candidates.sort(reverse=True)
    if not candidates:
        return np.zeros_like(air)
    keep = [candidates[0][1]]
    if len(candidates) > 1 and candidates[1][0] > 0.2 * candidates[0][0]:
        keep.append(candidates[1][1])
    mask = np.isin(labels, keep)
    # 혈관·결절 구멍 채우기 (슬라이스별)
    mask = np.stack([ndimage.binary_fill_holes(ndimage.binary_closing(s, iterations=2))
                     for s in mask])
    return mask


def emphysema(ct, lung, spacing3):
    vals = ct[lung]
    if vals.size == 0:
        raise ValueError("폐 영역이 없습니다.")
    vox = float(np.prod(spacing3)) / 1000.0
    return {"Lung volume (mL)": vals.size * vox,
            "LAA%-950": float((vals < -950).mean() * 100),
            "LAA%-910": float((vals < -910).mean() * 100),
            "Perc15 (HU)": float(np.percentile(vals, 15)),
            "Mean lung density (HU)": float(vals.mean())}


def ggo(ct, lung, spacing3, lo=-700, hi=-300):
    vals = ct[lung]
    if vals.size == 0:
        raise ValueError("폐 영역이 없습니다.")
    sel = (vals >= lo) & (vals <= hi)
    return {"GGO range (HU)": f"{lo} ~ {hi}", "GGO (% of lung)": float(sel.mean() * 100),
            "GGO volume (mL)": float(sel.sum() * np.prod(spacing3) / 1000.0),
            "Consolidation (> -100 HU, %)": float((vals > -100).mean() * 100)}


def grow_nodule(ct, seed, spacing3, threshold=-500, radius_mm=25, open_mm=1.5):
    """seed (k, row, col)에서 threshold 이상 연결 영역 (반경 제한 + 열림으로 혈관 분리)"""
    k0, r0, c0 = (int(round(v)) for v in seed)
    rad = [max(1, int(radius_mm / s)) for s in spacing3]
    sl = tuple(slice(max(0, c - r), min(n, c + r + 1)) for c, r, n in
               zip((k0, r0, c0), rad, ct.shape))
    sub = ct[sl] >= threshold
    zz, yy, xx = np.ogrid[tuple(slice(s.start, s.stop) for s in sl)]
    ball = (((zz - k0) * spacing3[0]) ** 2 + ((yy - r0) * spacing3[1]) ** 2 +
            ((xx - c0) * spacing3[2]) ** 2) <= radius_mm ** 2
    sub &= ball
    struct = np.ones((3, 3, 3), bool)
    iters = max(1, int(round(open_mm / min(spacing3[1:]))))
    opened = ndimage.binary_opening(sub, structure=struct, iterations=iters)
    if opened[k0 - sl[0].start, r0 - sl[1].start, c0 - sl[2].start]:
        sub = opened
    labels, _ = ndimage.label(sub)
    lab = labels[k0 - sl[0].start, r0 - sl[1].start, c0 - sl[2].start]
    mask = np.zeros(ct.shape, bool)
    if lab:
        region = labels == lab
        # 열림으로 깎인 결절 표면을 원래 임계 영역 안에서만 다시 채움 (가는 혈관은 다시 붙지 않음)
        region = ndimage.binary_dilation(region, structure=struct, iterations=iters,
                                         mask=(ct[sl] >= threshold) & ball)
        mask[sl] = region
    return mask


# ═══ PET ═══

def suv_stats(suv, mask, spacing3, threshold_pct=None):
    """SUV 영상 + 마스크 → SUVmax/mean/peak, MTV, TLG

    threshold_pct: SUVmax의 몇 %로 MTV 경계를 정할지 (예: 41). None이면 마스크 그대로
    """
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        raise ValueError("ROI/라벨 영역이 없습니다.")
    vals = suv[m]
    suv_max = float(vals.max())
    if threshold_pct:
        m = m & (suv >= suv_max * threshold_pct / 100.0)
        vals = suv[m]
    # SUVpeak: 최대점 중심 1 cm³ 구(반지름 6.2 mm) 평균
    k, r, c = np.unravel_index(np.argmax(np.where(mask, suv, -np.inf)), suv.shape)
    rad = 6.2
    zz, yy, xx = np.ogrid[:suv.shape[0], :suv.shape[1], :suv.shape[2]]
    sphere = (((zz - k) * spacing3[0]) ** 2 + ((yy - r) * spacing3[1]) ** 2 +
              ((xx - c) * spacing3[2]) ** 2) <= rad ** 2
    mtv = volume_ml(m, spacing3)
    return {"SUVmax": suv_max, "SUVmean": float(vals.mean()),
            "SUVpeak (1 cm³)": float(suv[sphere].mean()), "MTV (mL)": mtv,
            "TLG (SUVmean × MTV)": float(vals.mean()) * mtv}


# ═══ 근골격 ═══

def fat_fraction(image, roi, lo, hi):
    """ROI 안에서 [lo, hi] 범위(지방) 픽셀 비율 %"""
    vals = image[np.asarray(roi, bool)]
    if vals.size == 0:
        raise ValueError("ROI가 없습니다.")
    return float(((vals >= lo) & (vals <= hi)).mean() * 100)


def otsu_threshold(values):
    from skimage.filters import threshold_otsu
    return float(threshold_otsu(np.asarray(values)))


def line_angle_deg(p0, p1, spacing):
    dx = (p1[0] - p0[0]) * spacing[1]
    dy = (p1[1] - p0[1]) * spacing[0]
    return math.degrees(math.atan2(-dy, dx))   # 화면 위쪽을 +로


def angle_between_lines(l1, l2, spacing):
    a1 = line_angle_deg(*l1, spacing)
    a2 = line_angle_deg(*l2, spacing)
    d = abs(a1 - a2) % 180
    return min(d, 180 - d)


# ═══ 혈관 ═══

def vessel_metrics(mask2d, spacing):
    area = float(np.count_nonzero(mask2d)) * spacing[0] * spacing[1]
    long_mm, short_mm, _lp, _sp = feret_2d(mask2d, spacing)
    return {"Area (mm²)": area, "Equivalent diameter (mm)": 2 * math.sqrt(area / math.pi),
            "Max diameter (mm)": long_mm, "Min diameter (mm)": short_mm}


def stenosis(normal, stenotic, kind="diameter"):
    """(정상 − 협착) / 정상 × 100. kind: diameter / area"""
    if normal <= 0:
        raise ValueError("정상 부위 값이 0입니다.")
    pct = (normal - stenotic) / normal * 100
    out = {f"Stenosis by {kind} (%)": pct}
    if kind == "diameter":
        out["Equivalent area stenosis (%)"] = (1 - (stenotic / normal) ** 2) * 100
    return out


def _spline(points, step):
    from scipy.interpolate import CubicSpline
    p = np.asarray(points, dtype=float)
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    keep = np.concatenate([[True], seg > 1e-6])
    p = p[keep]
    s = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    if len(p) < 2:
        raise ValueError("중심선 점이 2개 이상 필요합니다 (랜드마크로 혈관을 따라 찍으세요).")
    if len(p) == 2:
        samples = np.arange(0, s[-1] + 1e-9, step)
        return p[0] + np.outer(samples / s[-1], p[1] - p[0]), samples
    cs = CubicSpline(s, p, axis=0, bc_type="natural")
    samples = np.arange(0, s[-1] + 1e-9, step)
    return cs(samples), samples


def curved_mpr(volume, affine_lps, points_lps, width_mm=40.0, step_mm=0.5):
    """중심선(환자 좌표 점들)을 따라 펼친 볼륨 (S, 행, 열) - 각 슬라이스는 중심선에 수직

    평행 이동(rotation-minimizing) 좌표계로 단면이 비틀리지 않게 함.
    반환 (단면 볼륨, 길이 방향 CPR 2D 영상, 중심선 길이 mm)
    """
    curve, s = _spline(points_lps, step_mm)
    tangents = np.gradient(curve, axis=0)
    tangents /= np.maximum(np.linalg.norm(tangents, axis=1, keepdims=True), 1e-9)
    ref = np.eye(3)[int(np.argmin(np.abs(tangents[0])))]
    normal = np.cross(tangents[0], ref)
    normal /= np.linalg.norm(normal)
    normals = [normal]
    for i in range(1, len(curve)):
        n = normals[-1] - np.dot(normals[-1], tangents[i]) * tangents[i]
        normals.append(n / max(np.linalg.norm(n), 1e-9))
    normals = np.array(normals)
    binormals = np.cross(tangents, normals)
    half = width_mm / 2
    grid = np.arange(-half, half + 1e-9, step_mm)
    uu, vv = np.meshgrid(grid, grid)                # (행 = binormal, 열 = normal)
    inv = np.linalg.inv(affine_lps)
    out = np.zeros((len(curve),) + uu.shape, dtype=np.float32)
    for i in range(len(curve)):
        world = curve[i] + uu[..., None] * normals[i] + vv[..., None] * binormals[i]
        idx = inv @ np.concatenate([world.reshape(-1, 3).T, np.ones((1, world.size // 3))])
        coords = [idx[2], idx[1], idx[0]]           # (k, row, col)
        out[i] = ndimage.map_coordinates(volume, coords, order=1, mode="constant",
                                         cval=float(volume.min())).reshape(uu.shape)
    cpr = out[:, len(grid) // 2, :]                 # 중심선을 지나는 길이 방향 단면
    return out, cpr, float(s[-1])
