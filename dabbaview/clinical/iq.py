# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
영상 화질 평가 (Image Quality Assessment) 엔진 - numpy · scipy

좌표: 점 (x, y)는 viewport 주석과 같이 픽셀 i의 중심이 i + 0.5 (배열 인덱스 = 좌표 − 0.5).
px = (행 간격, 열 간격) mm. valid = 계산에 쓸 픽셀 (화면 캡처의 글자 오버레이 제외).

- MTF: 기울어진 에지(slanted edge) - 에지 위치를 줄마다 찾아 직선 맞춤 → 4배 과표본 ESF → LSF → |FFT|
- SNR: 단일 영상 (신호 평균 / 배경 SD, 크기 영상은 Rayleigh 보정 0.655) · 두 영상 차분 (NEMA)
- CNR, 균일도 (NEMA 5-ROI · PIU · 지도), 고스팅 (PSG · 지도), 기하 왜곡 (격자점 · 격자 맞춤)
- NPS (2D FFT, 방사 평균, 백색/구조 잡음 판별), NEQ = S²·MTF² / NPS, FWHM · 바 패턴, 아티팩트
"""
import math

import numpy as np
from scipy import ndimage as ndi

RAYLEIGH = 0.655     # 크기(magnitude) 영상 배경 SD → 실제 잡음 σ (NEMA MS 1)


# ═══ 공통 ═══

def grid_region(arr):
    """격자 팬텀 영역: 밝은 부분을 닫고 구멍을 채운 가장 큰 덩어리 (칸이 따로 떨어져 있어도)"""
    from skimage.filters import threshold_otsu
    a = np.asarray(arr, float)
    m = a > threshold_otsu(a)
    m = ndi.binary_closing(m, iterations=max(3, min(a.shape) // 40))
    m = ndi.binary_fill_holes(m)
    lab, n = ndi.label(m)
    if n == 0:
        return np.ones(a.shape, bool)
    big = lab == int(np.argmax(ndi.sum(m, lab, range(1, n + 1)))) + 1
    return ndi.binary_erosion(big, iterations=3)


def object_fit(arr, valid=None):
    """물체(팬텀) 마스크·중심·반지름 - acr.phantom_fit과 같은 방식 (원형이 아니어도 동작)"""
    from .acr import Img
    img = Img(arr, (1.0, 1.0))
    if valid is not None:
        img.valid = valid
    fit = img.fit()
    fit["mask"] = fit["filled"]
    return fit


def _circle(cx, cy, r):
    return {"type": "ellipse", "pts": [(cx - r + 0.5, cy - r + 0.5), (cx + r + 0.5, cy + r + 0.5)],
            "circle": True}


def _rect(x0, y0, x1, y1):
    return {"type": "rect", "pts": [(x0, y0), (x1, y1)]}


def roi_stats(arr, ann, valid=None):
    """(mean, sd, n) - 주석 모양 안 (valid만)"""
    from ..roi_tools import mask_of
    m = mask_of(ann, arr.shape)
    if valid is not None:
        m &= valid
    v = np.asarray(arr, float)[m]
    if not v.size:
        return float("nan"), float("nan"), 0
    return float(v.mean()), float(v.std(ddof=1) if v.size > 1 else 0.0), int(v.size)


def signal_roi(fit, fraction=0.75):
    """물체 면적의 fraction 원 (NEMA: 75%)"""
    return _circle(fit["cx"], fit["cy"], math.sqrt(fraction) * fit["r"])


def background_rois(arr, obj_mask, valid=None, side_frac=0.1, level=None):
    """배경(공기) 사각형: 네 모서리 + 물체와 영상 끝 사이 네 곳. 물체·글자·고스팅처럼 밝은 픽셀이
    섞인 후보는 버림 (콘솔 화면 캡처의 모서리 글자 등)"""
    a = np.asarray(arr, float)
    h, w = a.shape
    grown = ndi.binary_dilation(obj_mask, iterations=max(3, int(0.03 * min(h, w))))
    level = level if level is not None else (float(np.median(a[obj_mask])) if obj_mask.any() else a.max())
    s = max(6, int(side_frac * min(h, w)))
    m = max(2, int(0.04 * min(h, w)))
    ys, xs = np.nonzero(obj_mask) if obj_mask.any() else (np.array([h // 2]), np.array([w // 2]))
    top, bottom, left, right = ys.min(), ys.max(), xs.min(), xs.max()
    cands = {"UL": (m, m), "UR": (w - m - s, m), "LL": (m, h - m - s), "LR": (w - m - s, h - m - s),
             "T": ((w - s) // 2, (top - s) // 2), "B": ((w - s) // 2, (bottom + h - s) // 2),
             "L": ((left - s) // 2, (h - s) // 2), "R": ((right + w - s) // 2, (h - s) // 2)}
    out = []
    for name, (x0, y0) in cands.items():
        if x0 < 0 or y0 < 0 or x0 + s > w or y0 + s > h:
            continue
        box = np.zeros(a.shape, bool)
        box[y0:y0 + s, x0:x0 + s] = True
        if (box & grown).any():
            continue
        if valid is not None and (box & ~valid).mean() > 0.02:
            continue
        if (a[box] > 0.2 * level).mean() > 0.005:     # 글자·자·물체 조각
            continue
        ann = _rect(x0, y0, x0 + s, y0 + s)
        ann["name"] = f"BG {name}"
        out.append(ann)
    return out


# ═══ 1. MTF (slanted edge) ═══

def mtf_edge(arr, p0, p1, px, width=24.0, oversample=4):
    """p0→p1: 에지를 가로지르는 선 (이미지 좌표). → dict(f, mtf, esf, lsf, mtf50, mtf10, nyquist, angle)"""
    a = np.asarray(arr, float)
    p0 = np.asarray(p0, float) - 0.5
    p1 = np.asarray(p1, float) - 0.5
    length = float(np.linalg.norm(p1 - p0))
    if length < 8:
        raise ValueError("선이 너무 짧습니다 (8 픽셀 이상, 에지를 가로질러 그으세요).")
    u = (p1 - p0) / length
    n = np.array([-u[1], u[0]])
    half = width / 2
    corners = np.array([p0 + n * half, p0 - n * half, p1 + n * half, p1 - n * half])
    x0, y0 = np.floor(corners.min(0)).astype(int)
    x1, y1 = np.ceil(corners.max(0)).astype(int)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(a.shape[1] - 1, x1), min(a.shape[0] - 1, y1)
    yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
    d = np.stack([xx - p0[0], yy - p0[1]], -1).astype(float)
    t = d @ u
    s = d @ n
    keep = (t >= 0) & (t <= length) & (np.abs(s) <= half)
    t, s, v = t[keep], s[keep], a[yy[keep], xx[keep]]
    if v.size < 50:
        raise ValueError("에지 영역 픽셀이 부족합니다.")
    lo, hi = np.percentile(v, 5), np.percentile(v, 95)
    if hi - lo <= 0:
        raise ValueError("선 위에 에지(밝기 차이)가 없습니다.")
    # 줄(s 방향 1 px 띠)마다 50% 지점 → 에지 직선 t = a + b·s
    edges = []
    for s0 in np.arange(-half, half, 1.0):
        sel = (s >= s0) & (s < s0 + 1)
        if sel.sum() < 6:
            continue
        order = np.argsort(t[sel])
        tt, vv = t[sel][order], v[sel][order]
        vv = np.convolve(vv, np.ones(3) / 3, "same")
        g = np.abs(np.gradient(vv))
        k = int(np.argmax(g[1:-1])) + 1
        mid = (lo + hi) / 2
        lo_k, hi_k = max(0, k - 4), min(len(vv) - 1, k + 4)
        seg_t, seg_v = tt[lo_k:hi_k + 1], vv[lo_k:hi_k + 1]
        cross = np.nonzero(np.diff(np.sign(seg_v - mid)))[0]
        if not len(cross):
            continue
        c = cross[0]
        ta, tb, va, vb = seg_t[c], seg_t[c + 1], seg_v[c], seg_v[c + 1]
        edges.append((s0 + 0.5, ta + (mid - va) / (vb - va) * (tb - ta) if vb != va else ta))
    if len(edges) >= 4:
        e = np.array(edges)
        b, a0 = np.polyfit(e[:, 0], e[:, 1], 1)
        res = e[:, 1] - (a0 + b * e[:, 0])
        good = np.abs(res) < max(1.0, 3 * np.std(res))
        if good.sum() >= 4:
            b, a0 = np.polyfit(e[good, 0], e[good, 1], 1)
    elif edges:
        b, a0 = 0.0, float(np.median([e[1] for e in edges]))
    else:
        raise ValueError("에지를 찾지 못했습니다.")
    cosang = math.cos(math.atan(b))
    dist = (t - (a0 + b * s)) * cosang
    step = 1.0 / oversample
    bins = np.round(dist / step).astype(int)
    bmin, bmax = bins.min(), bins.max()
    sums = np.bincount(bins - bmin, weights=v, minlength=bmax - bmin + 1)
    counts = np.bincount(bins - bmin, minlength=bmax - bmin + 1)
    x = (np.arange(bmin, bmax + 1)) * step
    ok = counts > 0
    esf = np.interp(x, x[ok], sums[ok] / counts[ok])
    # 에지 중심에서 ±(가능한 범위) 대칭으로 자름
    lim = min(-x[0], x[-1])
    sel = np.abs(x) <= lim
    x, esf = x[sel], esf[sel]
    if esf[0] > esf[-1]:
        esf = esf[::-1]
    lsf = np.gradient(esf)
    peak = int(np.argmax(np.abs(lsf)))
    shift = len(lsf) // 2 - peak
    lsf_c = np.roll(lsf, shift)
    lsf_w = lsf_c * np.hanning(len(lsf_c))
    spec = np.abs(np.fft.rfft(lsf_w))
    if spec[0] <= 0:
        raise ValueError("LSF가 비었습니다.")
    mtf = spec / spec[0]
    # 방향에 따른 픽셀 크기 (mm) - 비등방 픽셀 고려
    px_dir = math.hypot(u[0] * px[1], u[1] * px[0]) / math.hypot(u[0], u[1])
    bin_mm = step * px_dir
    f = np.fft.rfftfreq(len(lsf_w), d=bin_mm)
    corr = np.sinc(2 * f * bin_mm)           # 중앙 차분(미분) 보정
    corr[corr < 0.2] = 0.2
    mtf = np.clip(mtf / corr, 0, None)
    nyq = 1.0 / (2 * px_dir)
    keep = f <= 2 * nyq
    f, mtf = f[keep], mtf[keep]
    return {"f": f, "mtf": mtf, "x_mm": x * px_dir, "esf": esf, "lsf": lsf,
            "mtf50": _crossing(f, mtf, 0.5), "mtf10": _crossing(f, mtf, 0.1),
            "nyquist": nyq, "edge_angle": math.degrees(math.atan(b)),
            "mtf_at_nyq": float(np.interp(nyq, f, mtf)) if f[-1] >= nyq else float("nan"),
            "rect": [tuple(c + 0.5) for c in (corners[0], corners[2], corners[3], corners[1])]}


def _crossing(f, y, level):
    """y가 level 아래로 처음 내려가는 주파수 (보간). 없으면 nan"""
    below = np.nonzero(y < level)[0]
    if not len(below) or below[0] == 0:
        return float("nan")
    i = below[0]
    return float(f[i - 1] + (level - y[i - 1]) / (y[i] - y[i - 1]) * (f[i] - f[i - 1]))


def auto_edge_line(fit, px, side="right", span=0.25):
    """물체 가장자리(원 경계)를 가로지르는 선 - Auto IQ의 MTF용"""
    cx, cy, r = fit["cx"], fit["cy"], fit["r"]
    d = {"right": (1, 0), "left": (-1, 0), "top": (0, -1), "bottom": (0, 1)}[side]
    a = (cx + d[0] * r * (1 - span) + 0.5, cy + d[1] * r * (1 - span) + 0.5)
    b = (cx + d[0] * r * (1 + span) + 0.5, cy + d[1] * r * (1 + span) + 0.5)
    return a, b


# ═══ 2. SNR ═══

def snr_single(arr, sig_ann, bg_anns, valid=None, rayleigh=True):
    s, s_sd, _n = roi_stats(arr, sig_ann, valid)
    sds = [roi_stats(arr, b, valid) for b in bg_anns]
    sds = [x for x in sds if x[2] > 5]
    if not sds:
        raise ValueError("배경 ROI가 없습니다 (물체 밖 공기 영역이 필요합니다).")
    bg_sd = float(np.sqrt(np.mean([x[1] ** 2 for x in sds])))
    bg_mean = float(np.mean([x[0] for x in sds]))
    noise = bg_sd / RAYLEIGH if rayleigh else bg_sd
    return {"signal": s, "signal_sd": s_sd, "bg_mean": bg_mean, "bg_sd": bg_sd, "noise": noise,
            "snr": s / noise if noise > 0 else float("inf"),
            "snr_signal_sd": s / s_sd if s_sd > 0 else float("inf")}


def snr_difference(arr1, arr2, sig_ann, valid=None):
    """NEMA 두 영상 방법: SNR = S / (SD(차영상) / √2)"""
    diff = np.asarray(arr1, float) - np.asarray(arr2, float)
    s, _sd, _n = roi_stats(arr1, sig_ann, valid)
    _m, dsd, _n = roi_stats(diff, sig_ann, valid)
    noise = dsd / math.sqrt(2)
    return {"signal": s, "diff_sd": dsd, "noise": noise, "snr": s / noise if noise > 0 else float("inf")}


# ═══ 3. CNR ═══

def cnr(arr, ann1, ann2, valid=None, bg_sd=None):
    m1, s1, _ = roi_stats(arr, ann1, valid)
    m2, s2, _ = roi_stats(arr, ann2, valid)
    pooled = math.sqrt((s1 ** 2 + s2 ** 2) / 2)
    out = {"mean1": m1, "sd1": s1, "mean2": m2, "sd2": s2, "contrast": abs(m1 - m2),
           "cnr_pooled": abs(m1 - m2) / pooled if pooled > 0 else float("inf")}
    if bg_sd:
        out["cnr_background"] = abs(m1 - m2) / bg_sd
    return out


# ═══ 4. 균일도 ═══

def nema_five(fit, radius_frac=0.15, offset_frac=0.6):
    cx, cy, r = fit["cx"], fit["cy"], fit["r"]
    out = []
    for name, (dx, dy) in (("center", (0, 0)), ("top", (0, -1)), ("bottom", (0, 1)),
                           ("left", (-1, 0)), ("right", (1, 0))):
        ann = _circle(cx + dx * offset_frac * r, cy + dy * offset_frac * r, radius_frac * r)
        ann["name"] = f"U {name}"
        out.append(ann)
    return out


def uniformity_five(arr, anns, valid=None):
    means = {a["name"][2:]: roi_stats(arr, a, valid)[0] for a in anns}
    hi, lo = max(means.values()), min(means.values())
    nu = (hi - lo) / (hi + lo) * 100 if hi + lo else float("nan")
    return {"means": means, "max": hi, "min": lo, "non_uniformity": nu, "uniformity": 100 - nu}


def piu(arr, fit, px, valid=None):
    """큰 ROI (200 cm² 또는 물체의 75%) 안 1 cm² 평균 최대·최소 → PIU"""
    from scipy.signal import fftconvolve
    cx, cy, r = fit["cx"], fit["cy"], fit["r"]
    pxm = (px[0] + px[1]) / 2
    r_big = min(math.sqrt(20000 / math.pi) / pxm, math.sqrt(0.75) * r)
    rs = math.sqrt(100 / math.pi) / pxm
    if rs * 2 > r_big:
        rs = r_big / 4
    a = np.asarray(arr, float)
    v = np.ones(a.shape, bool) if valid is None else valid
    k = int(math.ceil(rs))
    yy, xx = np.mgrid[-k:k + 1, -k:k + 1]
    ker = (np.hypot(xx, yy) <= rs).astype(float)
    s = fftconvolve(a * v, ker, mode="same")
    n = fftconvolve(v.astype(float), ker, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s / n
    Y, X = np.indices(a.shape)
    ok = (np.hypot(X - cx, Y - cy) <= r_big - rs) & (n / ker.sum() > 0.99)
    vals = np.where(ok, mean, np.nan)
    iy, ix = np.unravel_index(np.nanargmax(vals), vals.shape)
    jy, jx = np.unravel_index(np.nanargmin(vals), vals.shape)
    hi, lo = float(mean[iy, ix]), float(mean[jy, jx])
    big = _circle(cx, cy, r_big)
    big["name"] = "PIU large"
    mx = _circle(ix, iy, rs)
    mx["name"] = "PIU max"
    mn = _circle(jx, jy, rs)
    mn["name"] = "PIU min"
    return {"piu": 100 * (1 - (hi - lo) / (hi + lo)), "max": hi, "min": lo,
            "large_mean": roi_stats(a, big, valid)[0], "rois": [big, mx, mn],
            "large_area_mm2": math.pi * (r_big * pxm) ** 2}


def uniformity_map(arr, fit, px, valid=None, kernel_mm2=100.0):
    """1 cm² 이동 평균 / 물체 중앙값 − 1 (%) - 물체 밖은 0"""
    from scipy.signal import fftconvolve
    a = np.asarray(arr, float)
    pxm = (px[0] + px[1]) / 2
    rs = max(1.0, math.sqrt(kernel_mm2 / math.pi) / pxm)
    inside = ndi.binary_erosion(fit["mask"], iterations=max(1, int(rs)))
    v = inside & (np.ones(a.shape, bool) if valid is None else valid)
    k = int(math.ceil(rs))
    yy, xx = np.mgrid[-k:k + 1, -k:k + 1]
    ker = (np.hypot(xx, yy) <= rs).astype(float)
    s = fftconvolve(a * v, ker, mode="same")
    n = fftconvolve(v.astype(float), ker, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        local = s / n
    ref = float(np.median(a[v])) if v.any() else 1.0
    out = np.where(inside & (n > 0.5 * ker.sum()), (local / ref - 1) * 100, 0.0)
    return np.nan_to_num(out).astype(np.float32)


# ═══ 5. 고스팅 ═══

def ghosting(arr, fit, px, valid=None, large_ann=None):
    from .acr import Img, place_ghosting
    img = Img(arr, px)
    if valid is not None:
        img.valid = valid
        img.text = ~valid
    img._fit = dict(fit, filled=fit["mask"])
    rois = place_ghosting(img)
    if large_ann is None:
        large_ann = signal_roi(fit)
        large_ann["name"] = "Ghost large"
    big = roi_stats(arr, large_ann, valid)[0]
    means = {}
    for ann in rois:
        side = ann["name"].split()[-1]
        ann["name"] = f"Ghost {side}"
        means[side] = roi_stats(arr, ann, valid)[0]
    if len(means) < 4:
        raise ValueError("팬텀 바깥에 고스팅 ROI 4개를 놓을 자리가 없습니다.")
    psg = abs((means["top"] + means["bottom"]) - (means["left"] + means["right"])) / (2 * big) * 100
    return {"psg": psg, "large": big, "means": means, "rois": [large_ann] + rois}


def ghost_map(arr, fit, valid=None, large=None):
    """물체 밖 신호 / 물체 평균 × 100 (%) - 물체 안은 0"""
    a = np.asarray(arr, float)
    grown = ndi.binary_dilation(fit["mask"], iterations=3)
    big = large or float(np.median(a[fit["mask"]]))
    out = np.where(~grown, a / big * 100 if big else 0.0, 0.0)
    out = np.where(out > 50, 0.0, out)        # 50 % 넘는 것은 고스팅이 아니라 글자·물체
    if valid is not None:
        out = np.where(valid, out, 0.0)
    return out.astype(np.float32)


# ═══ 6. 기하 왜곡 (격자) ═══

def grid_points(arr, region=None, polarity="auto"):
    """격자 칸(구멍) 중심 = 규칙적인 같은 크기 덩어리의 무게중심. → (N, 2) x, y (이미지 좌표)"""
    from skimage.filters import threshold_local
    a = ndi.gaussian_filter(np.asarray(arr, float), 1.0)
    region = np.ones(a.shape, bool) if region is None else region
    best = None
    for pol in (("bright", "dark") if polarity == "auto" else (polarity,)):
        b = a if pol == "bright" else -a
        block = max(15, (min(a.shape) // 12) | 1)
        th = threshold_local(b, block, offset=0)
        m = (b > th) & region
        m = ndi.binary_opening(m, iterations=1)
        lab, n = ndi.label(m)
        if n < 9:
            continue
        idx = np.arange(1, n + 1)
        sizes = ndi.sum(m, lab, idx)
        med = np.median(sizes[sizes > 4]) if (sizes > 4).any() else 0
        ok = (sizes > 0.4 * med) & (sizes < 2.0 * med) & (sizes > 4)
        # 칸은 거의 정사각형으로 꽉 참
        objs = ndi.find_objects(lab)
        pts = []
        for i in idx[ok]:
            sl = objs[i - 1]
            hgt, wid = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
            if max(hgt, wid) > 2.2 * min(hgt, wid) or sizes[i - 1] < 0.45 * hgt * wid:
                continue
            cy, cx = ndi.center_of_mass(m, lab, i)
            pts.append((cx + 0.5, cy + 0.5))
        pts = np.array(pts)
        if len(pts) >= 9:
            regular = _lattice_regularity(pts)
            if best is None or regular > best[0]:
                best = (regular, pts, pol)
    if best is None:
        raise ValueError("격자점을 찾지 못했습니다 (격자 팬텀 영상에서 사용하세요).")
    from scipy.spatial import cKDTree
    d, _ = cKDTree(best[1]).query(best[1], k=2)
    nn = d[:, 1]
    regular = float(np.mean(np.abs(nn - np.median(nn)) < 0.15 * np.median(nn)))
    if regular < 0.7:
        raise ValueError(f"규칙적인 격자가 보이지 않습니다 (이웃 간격이 고른 점 {regular * 100:.0f} %). "
                         "격자가 선명한 영상(W/L)에서 사용하세요.")
    return best[1], best[2]


def _lattice_regularity(pts):
    from scipy.spatial import cKDTree
    tree = cKDTree(pts)
    d, _ = tree.query(pts, k=2)
    nn = d[:, 1]
    return len(pts) * (1 - min(1.0, np.std(nn) / max(1e-6, np.median(nn))))


def lattice_fit(pts, px, nominal_mm=None):
    """격자점 → 이상적 격자 (회전·이동, 간격은 nominal 또는 맞춤) → 변위 (mm)"""
    from scipy.spatial import cKDTree
    pts = np.asarray(pts, float)
    P = pts * np.array([px[1], px[0]])            # mm (x, y)
    tree = cKDTree(P)
    d, j = tree.query(P, k=5)
    vecs = (P[j[:, 1:]] - P[:, None, :]).reshape(-1, 2)
    lens = np.hypot(vecs[:, 0], vecs[:, 1])
    spacing = float(np.median(d[:, 1]))
    near = np.abs(lens - spacing) < 0.25 * spacing
    ang = np.degrees(np.arctan2(vecs[near, 1], vecs[near, 0])) % 90
    ang = np.where(ang > 45, ang - 90, ang)
    theta = math.radians(float(np.median(ang)))
    c = P.mean(0)
    origin = P[np.argmin(np.hypot(*(P - c).T))]
    R = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    ij = np.round(((P - origin) @ R) / spacing)
    # 최소자승: P ≈ o + A·ij (A = 2×2, 간격·회전·기울기)
    X = np.c_[np.ones(len(ij)), ij]
    coef, *_ = np.linalg.lstsq(X, P, rcond=None)
    for _ in range(2):
        res = np.hypot(*(P - X @ coef).T)
        good = res < max(0.5, 3 * np.median(res) + 1e-9)
        coef, *_ = np.linalg.lstsq(X[good], P[good], rcond=None)
    o, A = coef[0], coef[1:].T
    fitted_spacing = (np.linalg.norm(A[:, 0]) + np.linalg.norm(A[:, 1])) / 2
    if nominal_mm:
        # 간격 고정 · 직교 → 회전·이동만 (Procrustes)
        u = A[:, 0] / np.linalg.norm(A[:, 0])
        v = np.array([-u[1], u[0]]) * (1 if np.dot(A[:, 1], [-u[1], u[0]]) >= 0 else -1)
        A = np.c_[u, v] * nominal_mm
        o = (P - ij @ A.T).mean(0)
    ideal = o + ij @ A.T
    disp = P - ideal
    mag = np.hypot(disp[:, 0], disp[:, 1])
    center = ideal.mean(0)
    rad = np.hypot(*(ideal - center).T)
    pct = np.where(rad > spacing, mag / np.maximum(rad, 1e-6) * 100, np.nan)
    return {"points": pts, "ideal": ideal / np.array([px[1], px[0]]), "disp_mm": disp, "mag_mm": mag,
            "mean_mm": float(mag.mean()), "max_mm": float(mag.max()), "rms_mm": float(np.sqrt((mag ** 2).mean())),
            "max_pct": float(np.nanmax(pct)) if np.isfinite(pct).any() else float("nan"),
            "spacing_mm": float(fitted_spacing), "rotation_deg": math.degrees(theta), "n": int(len(pts))}


# ═══ 7. NPS ═══

def patches(arr, region, size=32, overlap=0.5, valid=None):
    """region(bool) 안에 완전히 들어가는 size×size 조각들"""
    a = np.asarray(arr, float)
    ok = region if valid is None else region & valid
    step = max(1, int(size * (1 - overlap)))
    fine = max(1, size // 8)
    ys, xs = np.nonzero(ok)
    if not len(ys):
        return []
    integral = np.pad(ok.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    fits = []
    for y in range(ys.min(), ys.max() - size + 2, fine):
        for x in range(xs.min(), xs.max() - size + 2, fine):
            total = integral[y + size, x + size] - integral[y, x + size] - integral[y + size, x] + integral[y, x]
            if total == size * size:
                fits.append((x, y))
    if not fits:
        return []
    # 가운데에 가까운 것부터, 서로 step 이상 떨어진 조각만 (겹침 ≤ overlap)
    cx, cy = np.mean([f[0] for f in fits]), np.mean([f[1] for f in fits])
    fits.sort(key=lambda f: (f[0] - cx) ** 2 + (f[1] - cy) ** 2)
    chosen = []
    for x, y in fits:
        if all(max(abs(x - a), abs(y - b)) >= step for a, b in chosen):
            chosen.append((x, y))
    return [((x, y), a[y:y + size, x:x + size]) for x, y in chosen]


def _detrend2(p):
    """2차 다항식 면 빼기 (저주파 음영 제거)"""
    n = p.shape[0]
    y, x = np.mgrid[0:n, 0:p.shape[1]]
    X = np.c_[np.ones(p.size), x.ravel(), y.ravel(), (x * x).ravel(), (y * y).ravel(), (x * y).ravel()]
    coef, *_ = np.linalg.lstsq(X, p.ravel(), rcond=None)
    return p - (X @ coef).reshape(p.shape)


def nps(patch_list, px, second=None):
    """patch_list: [2D]. second: 같은 위치의 두 번째 영상 조각들 → 차분/√2 (구조 제거)"""
    if not patch_list:
        raise ValueError("NPS를 계산할 균일 영역 조각이 없습니다 (ROI를 더 크게).")
    n = patch_list[0].shape[0]
    specs, means = [], []
    for i, p in enumerate(patch_list):
        means.append(float(p.mean()))
        q = (p - second[i]) / math.sqrt(2) if second is not None else p
        q = _detrend2(q)
        specs.append(np.abs(np.fft.fft2(q)) ** 2)
    nps2 = np.fft.fftshift(np.mean(specs, 0)) * (px[0] * px[1]) / (n * n)
    fx = np.fft.fftshift(np.fft.fftfreq(n, d=px[1]))
    fy = np.fft.fftshift(np.fft.fftfreq(n, d=px[0]))
    FX, FY = np.meshgrid(fx, fy)
    fr = np.hypot(FX, FY)
    df = 1.0 / (n * max(px))
    nb = int(fr.max() / df)
    edges = np.arange(nb + 1) * df
    idx = np.digitize(fr.ravel(), edges) - 1
    valid = (idx >= 0) & (idx < nb)
    sums = np.bincount(idx[valid], weights=nps2.ravel()[valid], minlength=nb)
    cnt = np.bincount(idx[valid], minlength=nb)
    f1 = (edges[:-1] + edges[1:]) / 2
    ok = cnt > 0
    f1, n1 = f1[ok][1:], (sums[ok] / cnt[ok])[1:]      # DC 제외
    nyq = 1 / (2 * max(px))
    in_band = f1 <= nyq
    f1, n1 = f1[in_band], n1[in_band]
    # 판별: 고역/저역 비 + 2D 스파이크
    low = n1[(f1 > 0.05 * nyq) & (f1 <= 0.3 * nyq)]
    high = n1[(f1 > 0.6 * nyq)]
    ratio = float(high.mean() / low.mean()) if len(low) and len(high) and low.mean() > 0 else float("nan")
    expected = np.interp(fr, f1, n1, left=n1[0] if len(n1) else 0, right=n1[-1] if len(n1) else 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = nps2 / np.maximum(expected, 1e-12)
    z[fr < 2 * df] = 0
    spikes = np.argwhere(z > 8)
    spike_f = sorted({(round(float(FX[i, j]), 3), round(float(FY[i, j]), 3)) for i, j in spikes})[:12]
    if spike_f:
        kind = "구조 잡음 (특정 주파수 스파이크)"
    elif np.isfinite(ratio) and ratio < 0.5:
        kind = "상관된 잡음 (저역 통과: 필터·보간·병렬영상)"
    elif np.isfinite(ratio) and ratio > 2:
        kind = "고역 강조 잡음 (선명화 필터)"
    else:
        kind = "백색 잡음 (주파수에 고르게 분포)"
    return {"nps2d": nps2, "fx": fx, "fy": fy, "f": f1, "nps": n1, "nyquist": nyq,
            "mean_signal": float(np.mean(means)), "noise_var": float(np.mean([np.var(_detrend2(p)) for p in patch_list])),
            "hf_lf_ratio": ratio, "spikes": spike_f, "kind": kind, "n_patches": len(patch_list),
            "patch": n}


# ═══ 8. NEQ ═══

def neq(mtf_res, nps_res, q=None):
    f = nps_res["f"]
    m = np.interp(f, mtf_res["f"], mtf_res["mtf"], right=0)
    s = nps_res["mean_signal"]
    with np.errstate(divide="ignore", invalid="ignore"):
        neq_f = np.where(nps_res["nps"] > 0, (s ** 2) * m ** 2 / nps_res["nps"], np.nan)
    out = {"f": f, "neq": neq_f, "mtf": m, "signal": s}
    if q:
        out["dqe"] = neq_f / q
    return out


# ═══ 9. 분해능 ═══

def profile_along(arr, p0, p1, step=0.1):
    p0 = np.asarray(p0, float) - 0.5
    p1 = np.asarray(p1, float) - 0.5
    length = float(np.linalg.norm(p1 - p0))
    t = np.arange(0, length + 1e-9, step)
    u = (p1 - p0) / max(length, 1e-9)
    xs, ys = p0[0] + t * u[0], p0[1] + t * u[1]
    v = ndi.map_coordinates(np.asarray(arr, float), [ys, xs], order=3, mode="nearest")
    return t, v, u


def fwhm(x, y):
    """(FWHM, FWTM, 왼쪽, 오른쪽, 기준선, 봉우리) - x 단위 그대로"""
    y = np.asarray(y, float)
    n = len(y)
    edge = max(1, n // 10)
    base = float(np.median(np.r_[y[:edge], y[-edge:]]))
    k = int(np.argmax(y))
    peak = float(y[k])
    if peak <= base:
        raise ValueError("봉우리(점·선 광원)가 없습니다.")

    def width(level):
        lv = base + (peak - base) * level
        i = k
        while i > 0 and y[i - 1] > lv:
            i -= 1
        j = k
        while j < n - 1 and y[j + 1] > lv:
            j += 1
        if i == 0 or j == n - 1:
            return float("nan"), None, None
        xl = x[i - 1] + (lv - y[i - 1]) / (y[i] - y[i - 1]) * (x[i] - x[i - 1])
        xr = x[j] + (lv - y[j]) / (y[j + 1] - y[j]) * (x[j + 1] - x[j])
        return float(xr - xl), float(xl), float(xr)
    w50, xl, xr = width(0.5)
    w10, _l, _r = width(0.1)
    return {"fwhm": w50, "fwtm": w10, "left": xl, "right": xr, "base": base, "peak": peak}


def point_source(arr, px, center=None, half=15):
    """가장 밝은 점 (또는 center 근처) → 가로·세로 FWHM (mm) + 2D 가우스 σ"""
    a = np.asarray(arr, float)
    if center is None:
        sm = ndi.gaussian_filter(a, 1)
        cy, cx = np.unravel_index(np.argmax(sm), a.shape)
    else:
        cx, cy = int(center[0]), int(center[1])
        y0, y1 = max(0, cy - half), min(a.shape[0], cy + half + 1)
        x0, x1 = max(0, cx - half), min(a.shape[1], cx + half + 1)
        sub = a[y0:y1, x0:x1]
        dy, dx = np.unravel_index(np.argmax(sub), sub.shape)
        cy, cx = y0 + dy, x0 + dx
    out = {"x": cx + 0.5, "y": cy + 0.5}
    for name, (p0, p1, sp) in {"horizontal": ((cx - half + 0.5, cy + 0.5), (cx + half + 0.5, cy + 0.5), px[1]),
                               "vertical": ((cx + 0.5, cy - half + 0.5), (cx + 0.5, cy + half + 0.5), px[0])}.items():
        t, v, _u = profile_along(a, p0, p1)
        r = fwhm(t * sp, v)
        out[name] = r
        out[name + "_profile"] = (t * sp, v)
    return out


def bar_pattern(t_mm, v, min_mod=0.1, prominence=0.03):
    """선 프로파일의 막대 묶음 → [(lp/mm, 변조도, 막대 수)] + 분해 가능한 최대 lp/mm"""
    from scipy.signal import find_peaks
    v = np.asarray(v, float)
    rng = v.max() - v.min()
    if rng <= 0:
        raise ValueError("프로파일에 변화가 없습니다.")
    peaks, _ = find_peaks(v, prominence=prominence * rng)
    if len(peaks) < 3:
        raise ValueError("막대(봉우리)를 3개 이상 찾지 못했습니다 - 막대 패턴을 가로질러 선을 그으세요.")
    periods = np.diff(t_mm[peaks])
    groups, cur = [], [0]
    for i in range(1, len(periods)):
        if abs(periods[i] - periods[cur[-1]]) <= 0.25 * periods[cur[-1]]:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        pk = peaks[g[0]:g[-1] + 2]
        period = float(np.mean(periods[g]))
        vmax = float(np.mean(v[pk]))
        vmin = float(np.mean([v[pk[i]:pk[i + 1] + 1].min() for i in range(len(pk) - 1)]))
        mod = (vmax - vmin) / (vmax + vmin) if vmax + vmin else 0
        out.append({"lp_mm": 1 / period, "period_mm": period, "modulation": mod, "bars": len(pk),
                    "start_mm": float(t_mm[pk[0]]), "end_mm": float(t_mm[pk[-1]])})
    resolved = [g["lp_mm"] for g in out if g["modulation"] >= min_mod]
    return {"groups": out, "limit_lp_mm": max(resolved) if resolved else float("nan")}


# ═══ 10. 아티팩트 ═══

def ring_artifacts(arr, center, rmax, valid=None, k=4.0):
    """극좌표 변환 → 반지름별 (각도 중앙값) 프로파일에서 추세를 뺀 잔차의 튀는 곳 = 링"""
    a = np.asarray(arr, float)
    cx, cy = center
    rs = np.arange(2, rmax, 0.5)
    th = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    R, T = np.meshgrid(rs, th)
    xs, ys = cx + R * np.cos(T), cy + R * np.sin(T)
    polar = ndi.map_coordinates(a, [ys, xs], order=1, mode="nearest")
    if valid is not None:
        pv = ndi.map_coordinates(valid.astype(float), [ys, xs], order=0) > 0.5
        polar = np.where(pv, polar, np.nan)
    prof = np.nanmedian(polar, 0)
    trend = ndi.median_filter(np.nan_to_num(prof), size=21, mode="nearest")
    resid = prof - trend
    sigma = 1.4826 * np.median(np.abs(resid - np.median(resid))) + 1e-9
    level = float(np.nanmean(prof)) or 1.0
    rings = []
    from scipy.signal import find_peaks
    for sign in (1, -1):
        pk, _ = find_peaks(sign * resid, height=k * sigma, distance=4)
        for p in pk:
            lo_i, hi_i = max(0, p - 8), min(polar.shape[1] - 1, p + 8)
            col = polar[:, p] - (polar[:, lo_i] + polar[:, hi_i]) / 2   # 각도마다 반지름 이웃과 비교
            agree = float(np.nanmean(np.sign(col) == sign))
            if agree >= 0.75 and rs[p] >= 5:
                rings.append({"r_px": float(rs[p]), "amp": float(resid[p]),
                              "amp_pct": float(resid[p] / level * 100), "consistency": agree})
    rings.sort(key=lambda r: -abs(r["amp"]))
    return {"rings": rings, "polar": polar, "r": rs, "profile": prof, "resid": resid, "sigma": float(sigma)}


def zipper(arr, valid=None, k=8.0):
    """한 열(또는 행) 전체에 걸친 밝은/어두운 줄: 좌우 이웃과의 차이를 열마다 평균 → 튀는 열"""
    a = np.asarray(arr, float)
    out = {}
    for axis, name in ((0, "vertical (열)"), (1, "horizontal (행)")):
        sm = ndi.median_filter(a, size=(1, 7) if axis == 0 else (7, 1))
        resid = a - sm
        if valid is not None:
            resid = np.where(valid, resid, np.nan)
        prof = np.nanmean(resid, axis=axis)
        med = np.nanmedian(prof)
        mad = 1.4826 * np.nanmedian(np.abs(prof - med)) + 1e-9
        z = (prof - med) / mad
        hits = np.nonzero(np.abs(z) > k)[0]
        lines = []
        for grp in np.split(hits, np.nonzero(np.diff(hits) > 1)[0] + 1) if len(hits) else []:
            j = grp[np.argmax(np.abs(z[grp]))]
            lines.append({"pos": int(j), "z": float(z[j]), "amp": float(prof[j]),
                          "width": int(len(grp))})
        spec = np.abs(np.fft.rfft(np.nan_to_num(prof - med)))
        fr = np.fft.rfftfreq(len(prof))
        periodic = None
        if len(spec) > 4:
            i = int(np.argmax(spec[2:])) + 2
            if spec[i] > 6 * np.median(spec[2:]) + 1e-9:
                periodic = {"cycles_per_px": float(fr[i]), "period_px": float(1 / fr[i])}
        out[name] = {"lines": lines, "z": z, "periodic": periodic}
    return out


def banding(arr, fit, px, valid=None):
    """물체 안 저주파 음영을 나눈 뒤 행·열 평균 프로파일의 주기 성분 (진폭 %, 주기 mm)"""
    a = np.asarray(arr, float)
    inside = ndi.binary_erosion(fit["mask"], iterations=5)
    if valid is not None:
        inside &= valid
    smooth = ndi.gaussian_filter(np.where(inside, a, 0), 8)
    wgt = ndi.gaussian_filter(inside.astype(float), 8)
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = np.where(inside, a / (smooth / np.maximum(wgt, 1e-6)) - 1, np.nan)
    out = {}
    for axis, name, sp in ((1, "rows (가로 띠)", px[0]), (0, "columns (세로 띠)", px[1])):
        with np.errstate(invalid="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                prof = np.nanmean(rel, axis=axis)
        prof = prof[np.isfinite(prof)]
        if len(prof) < 16:
            continue
        prof = prof - prof.mean()
        spec = np.abs(np.fft.rfft(prof * np.hanning(len(prof)))) * 2 / (len(prof) * 0.5)
        fr = np.fft.rfftfreq(len(prof), d=sp)
        i = int(np.argmax(spec[2:])) + 2 if len(spec) > 3 else 1
        out[name] = {"std_pct": float(prof.std() * 100), "p2p_pct": float((prof.max() - prof.min()) * 100),
                     "peak_amp_pct": float(spec[i] * 100), "period_mm": float(1 / fr[i]) if fr[i] else float("nan"),
                     "profile": prof}
    return out
