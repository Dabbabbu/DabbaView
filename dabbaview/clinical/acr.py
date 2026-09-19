# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
ACR 대형 MRI 팬텀 자동 QC 엔진 (numpy)

흐름
1. find_sets(): 불러온 영상에서 localizer · T1 11장 · T2 11장을 찾음
   - DICOM: 시리즈·EchoTime별로 묶어 격자 영상(slice 5)이 5번째인 11장
   - 이미지 파일(콘솔 캡처 JPEG 등): 순서대로 보고 격자 영상 기준 (T2 이중 에코는 2장 간격)
2. place_*(): 검사마다 ROI·측정선을 자동 배치 → 이름 붙은 주석 ("ACR PIU large" 등)
3. compute(): 주석의 모양으로 값 계산 → 사용자가 ROI를 옮기면 같은 함수로 다시 계산
4. evaluate(): 기준값(3.0T 기본, Settings에서 변경)으로 Pass/Fail

좌표: 주석 점 (x, y)는 viewport와 같이 픽셀 i의 중심이 i + 0.5.
8비트 화면 캡처는 표시 창(W/L)이 적용된 값이라 PIU·고스팅은 근삿값.
"""
import math
import re

import numpy as np

TESTS = [
    ("geometry", "1. 기하학적 정확도"),
    ("resolution", "2. 고대조도 공간 분해능"),
    ("thickness", "3. 절편 두께 정확도"),
    ("position", "4. 절편 위치 정확도"),
    ("uniformity", "5. 영상 강도 균일도 (PIU)"),
    ("ghosting", "6. 고스팅 비율 (PSG)"),
    ("low_contrast", "7. 저대조도 검출능"),
]
TEST_NAMES = dict(TESTS)

DEFAULT_CRITERIA = {
    "field": "3T",
    "loc_nominal": 148.0, "axial_nominal": 190.0, "geo_tol": 2.0,
    "thk_nominal": 5.0, "thk_tol": 0.7,
    "pos_max": 5.0,
    "piu_min": 82.0,
    "ghost_max": 2.5,
    "lc_min": 37,
    "res_max": 1.0,
}
PRESETS = {"3T": {"piu_min": 82.0, "lc_min": 37}, "1.5T": {"piu_min": 87.5, "lc_min": 9}}

RES_SIZES = (1.1, 1.0, 0.9)
LC_RADII_MM = (12.8, 25.6, 38.4)                              # 스포크 안 원판 3개 (중심에서)
LC_DIAMETERS_MM = tuple(7.0 - k * 5.5 / 9 for k in range(10))  # 스포크 1 → 10 (7.0 → 1.5 mm)
LARGE_ROI_MM2 = 20000.0      # 200 cm²
SMALL_ROI_MM2 = 100.0        # 1 cm²
GHOST_ROI_MM2 = 1000.0       # 10 cm², 4:1 타원


def criteria_with(overrides=None):
    c = dict(DEFAULT_CRITERIA)
    for k, v in (overrides or {}).items():
        if k in c:
            c[k] = type(DEFAULT_CRITERIA[k])(v)
    return c


# ═══ 영상 ═══

class Img:
    """분석할 한 장: 값 배열 + 픽셀 간격 (행, 열 mm) + 원래 위치 ref=(series, k)"""

    def __init__(self, arr, px, ref=None, screen_capture=False, te=None):
        arr = np.asarray(arr, dtype=float)
        if arr.ndim == 3:
            arr = arr.mean(-1)
        self.a = arr
        self.px = (float(px[0]), float(px[1]))
        self.ref = ref
        self.te = te
        self.text = text_mask(arr) if screen_capture else None
        self.valid = ~self.text if self.text is not None else np.ones(arr.shape, bool)
        self._fit = None

    @property
    def shape(self):
        return self.a.shape

    def fit(self):
        if self._fit is None:
            self._fit = phantom_fit(self)
        return self._fit

    def mm(self, dx, dy):
        return math.hypot(dx * self.px[1], dy * self.px[0])


def text_mask(arr):
    """콘솔 캡처의 글자·자(ruler) 오버레이 (거의 흰색 + 그림자) - 팬텀이 흰색으로 포화되면 None"""
    from scipy import ndimage as ndi
    bright = arr >= 245
    if bright.mean() > 0.08:        # W/L로 팬텀이 포화된 화면 캡처
        return None
    return ndi.binary_dilation(bright, iterations=2)


def phantom_fit(img):
    """팬텀 원: 중심 (cx, cy, 배열 인덱스), 반지름 r(px), 신호 level, 채운 마스크"""
    from scipy import ndimage as ndi
    from skimage.filters import threshold_otsu
    a = np.where(img.valid, img.a, 0.0)
    thr = threshold_otsu(a)
    m = ndi.binary_opening(a > thr, iterations=2)
    lab, n = ndi.label(m)
    if n == 0:
        raise ValueError("팬텀을 찾지 못했습니다.")
    sizes = ndi.sum(m, lab, range(1, n + 1))
    big = lab == int(np.argmax(sizes)) + 1
    filled = ndi.binary_fill_holes(big)
    edge = filled & ~ndi.binary_erosion(filled)
    ys, xs = np.nonzero(edge)
    cx, cy, r = _circle_fit(xs.astype(float), ys.astype(float))
    for _ in range(3):   # 노치·글자로 튀는 점 빼고 다시
        res = np.abs(np.hypot(xs - cx, ys - cy) - r)
        keep = res < max(2.0, np.percentile(res, 80))
        if keep.sum() < 20:
            break
        cx, cy, r = _circle_fit(xs[keep].astype(float), ys[keep].astype(float))
    yy, xx = np.indices(a.shape)
    inside = np.hypot(xx - cx, yy - cy) < 0.85 * r
    sig = img.a[inside & big & img.valid]
    level = float(np.median(sig)) if sig.size else float(thr)
    y0, y1 = np.nonzero(filled.any(1))[0][[0, -1]]
    x0, x1 = np.nonzero(filled.any(0))[0][[0, -1]]
    box_fill = filled.sum() / max(1, (y1 - y0 + 1) * (x1 - x0 + 1))
    return {"cx": float(cx), "cy": float(cy), "r": float(r), "level": level, "otsu": float(thr),
            "mask": big, "filled": filled, "box": (int(x0), int(y0), int(x1), int(y1)),
            "box_fill": float(box_fill)}


def _circle_fit(xs, ys):
    A = np.c_[2 * xs, 2 * ys, np.ones(len(xs))]
    sol = np.linalg.lstsq(A, xs ** 2 + ys ** 2, rcond=None)[0]
    cx, cy = sol[0], sol[1]
    return cx, cy, math.sqrt(max(1e-6, sol[2] + cx ** 2 + cy ** 2))


def _sample(a, xs, ys):
    from scipy import ndimage as ndi
    return ndi.map_coordinates(a, [ys, xs], order=1, mode="constant", cval=0.0)


def _crossings(p, thr, run):
    """프로파일 양 끝에서 안쪽으로 가장자리 (보간 인덱스).
    thr 위로 run개 이상 이어지는 첫 지점을 찾은 뒤, 그 가장자리 안쪽 평탄부와 바깥 배경의
    중간값(국소 반치)으로 다시 잡음 → 코일 음영으로 한쪽이 어두워도 정확"""
    above = p > thr
    n = len(p)
    if n < run + 2:
        return None
    sustained = np.convolve(above.astype(int), np.ones(run, int), "valid") == run
    idx = np.nonzero(sustained)[0]
    if not len(idx):
        return None
    i, j = int(idx[0]), int(idx[-1]) + run - 1
    if i == 0 or j >= n - 1:
        return None
    return _refine(p, i, 1, run), _refine(p, j, -1, run)


def _refine(p, i, direction, run):
    """i: 대략의 가장자리 (안쪽 첫 점). direction=1이면 왼쪽이 바깥"""
    n = len(p)
    inner = p[max(0, min(n, i + direction * 2 * run)):max(0, min(n, i + direction * 5 * run)):direction] \
        if direction > 0 else p[max(0, i - 5 * run):max(0, i - 2 * run)]
    outer = p[max(0, i - direction * 5 * run):max(0, i - direction * 2 * run)] if direction > 0 \
        else p[min(n, i + 2 * run):min(n, i + 5 * run)]
    if not len(inner) or not len(outer):
        k0, k1 = (i - 1, i) if direction > 0 else (i + 1, i)
        thr = (p[k0] + p[k1]) / 2
    else:
        thr = (float(np.median(inner)) + float(np.median(outer))) / 2
    # 대략 위치 ±2run 안에서 바깥 → 안쪽으로 thr를 처음 넘는 곳
    lo, hi = max(1, i - 2 * run), min(n - 2, i + 2 * run)
    rng = range(lo, hi + 1) if direction > 0 else range(hi, lo - 1, -1)
    for k in rng:
        prev = k - direction
        if p[k] > thr >= p[prev]:
            v0, v1 = p[prev], p[k]
            return prev + direction * (thr - v0) / (v1 - v0) if v1 != v0 else k
    return float(i)


def chord(img, cx, cy, angle, offset, thr, r, step=0.25):
    """중심에서 offset(px) 떨어진 angle 방향 현의 양 끝 (x, y) - 글자 근처면 None"""
    u = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])
    nrm = np.array([-u[1], u[0]])
    c0 = np.array([cx, cy]) + offset * nrm
    t = np.arange(-1.3 * r, 1.3 * r, step)
    xs, ys = c0[0] + t * u[0], c0[1] + t * u[1]
    p = _sample(img.a, xs, ys)
    hit = _crossings(p, thr, int(3 / step))
    if hit is None:
        return None
    t0 = t[0] + hit[0] * step
    t1 = t[0] + hit[1] * step
    e0, e1 = c0 + t0 * u, c0 + t1 * u
    if img.text is not None:
        for e in (e0, e1):
            x, y = int(round(e[0])), int(round(e[1]))
            win = img.text[max(0, y - 6):y + 7, max(0, x - 6):x + 7]
            if win.any():
                return None
    return e0, e1


# ═══ 주석 도우미 ═══

def ann_line(name, p0, p1, test, color="#ffff00"):
    return {"type": "distance", "pts": [(float(p0[0]) + 0.5, float(p0[1]) + 0.5),
                                        (float(p1[0]) + 0.5, float(p1[1]) + 0.5)],
            "name": name, "acr": test, "color": color}


def ann_ellipse(name, cx, cy, rx, ry, test, color="#50ff78", circle=False):
    ann = {"type": "ellipse", "pts": [(cx - rx + 0.5, cy - ry + 0.5), (cx + rx + 0.5, cy + ry + 0.5)],
           "name": name, "acr": test, "color": color}
    if circle:
        ann["circle"] = True
    return ann


def ann_rect(name, x0, y0, x1, y1, test, color="#ff5ad2"):
    return {"type": "rect", "pts": [(x0, y0), (x1, y1)], "name": name, "acr": test, "color": color}


def line_mm(img, ann):
    (x0, y0), (x1, y1) = ann["pts"][:2]
    return img.mm(x1 - x0, y1 - y0)


def roi_values(img, ann):
    from ..roi_tools import mask_of
    m = mask_of(ann, img.shape) & img.valid
    return img.a[m]


def roi_mean(img, ann):
    v = roi_values(img, ann)
    return float(v.mean()) if v.size else float("nan")


# ═══ 1. 기하학적 정확도 ═══

def diameter(img, angle, fit=None):
    """angle 방향 지름 (px 끝점 두 개). 여러 평행 현을 보정해 노치·글자·내부 구조 영향 제거"""
    fit = fit or img.fit()
    cx, cy, r, thr = fit["cx"], fit["cy"], fit["r"], fit["level"] / 4   # 찾기는 1/4, 위치는 국소 반치
    ests = []
    for off in np.linspace(-0.4 * r, 0.4 * r, 33):   # 위쪽 노치(약 ±0.23R)를 벗어난 현까지
        ends = chord(img, cx, cy, angle, off, thr, r)
        if ends is None:
            continue
        length = float(np.linalg.norm(ends[1] - ends[0]))
        ests.append(math.sqrt(length ** 2 + 4 * off ** 2))
    if not ests:
        raise ValueError("팬텀 가장자리를 찾지 못했습니다.")
    d = _main_cluster(ests)
    u = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])
    c = np.array([cx, cy])
    return c - u * d / 2, c + u * d / 2


def _main_cluster(values, gap=1.0):
    """정렬해서 이웃 차이가 gap(px) 이하로 이어진 가장 큰 무리의 중앙값.
    진짜 가장자리 현은 서로 거의 같고, 노치·내부 구조(짧게)나 글자(길게)에 걸린 현은 흩어짐"""
    v = sorted(values)
    groups, cur = [], [v[0]]
    for x in v[1:]:
        if x - cur[-1] <= gap:
            cur.append(x)
        else:
            groups.append(cur)
            cur = [x]
    groups.append(cur)
    best = max(groups, key=lambda g: (len(g), g[-1]))
    return float(np.median(best))


def place_geometry(img, which):
    """which: 'S1' (가로·세로), 'S5' (가로·세로·대각 둘), 'LOC' (위아래 길이)"""
    if which == "LOC":
        return [place_localizer(img)]
    angles = [(0, "H"), (90, "V")] + ([(45, "D1"), (135, "D2")] if which == "S5" else [])
    out = []
    for angle, tag in angles:
        p0, p1 = diameter(img, angle)
        out.append(ann_line(f"ACR Geo {which} {tag}", p0, p1, "geometry"))
    return out


def place_localizer(img):
    """시상 localizer: 팬텀 위아래(S-I) 길이. 여러 세로선 중 긴 쪽 (내부 막대는 짧게만 만듦)"""
    fit = img.fit()
    x0, y0, x1, y1 = fit["box"]
    thr = fit["level"] / 4
    best = []
    for x in np.linspace(x0 + 0.2 * (x1 - x0), x1 - 0.2 * (x1 - x0), 25):
        ys = np.arange(max(0, y0 - 20), min(img.shape[0] - 1, y1 + 20), 0.25)
        p = _sample(img.a, np.full_like(ys, x), ys)
        hit = _crossings(p, thr, 12)
        if hit is None:
            continue
        ya, yb = ys[0] + hit[0] * 0.25, ys[0] + hit[1] * 0.25
        if img.text is not None and (img.text[int(ya), int(x)] or img.text[min(img.shape[0] - 1, int(yb)), int(x)]):
            continue
        best.append((yb - ya, x, ya, yb))
    if not best:
        raise ValueError("localizer에서 팬텀 길이를 찾지 못했습니다.")
    best.sort()
    length, x, ya, yb = best[int(0.8 * (len(best) - 1))]
    return ann_line("ACR Geo LOC", (x, ya), (x, yb), "geometry")


# ═══ 3. 절편 두께 (slice 1 경사판 두 개) ═══

def _bar_rows(img, fit):
    """slice 1 가운데 가로 막대 (두께 경사판이 들어 있는 어두운 띠) 행 범위"""
    cx, cy, r, level = fit["cx"], fit["cy"], fit["r"], fit["level"]
    x0, x1 = int(cx - 0.8 * r), int(cx + 0.8 * r)
    rows = np.arange(max(0, int(cy - 0.5 * r)), min(img.shape[0], int(cy + 0.5 * r)))
    frac = (img.a[rows, x0:x1] < 0.5 * level).mean(1)
    dark = frac > 0.85
    blocks, start = [], None
    for i, d in enumerate(np.r_[dark, False]):
        if d and start is None:
            start = i
        elif not d and start is not None:
            blocks.append((rows[start], rows[i - 1]))
            start = None
    blocks = [b for b in blocks if 3.0 <= (b[1] - b[0] + 1) * img.px[0] <= 25.0]
    if not blocks:
        raise ValueError("절편 두께 막대를 찾지 못했습니다 (slice 1이 맞는지 확인).")
    return min(blocks, key=lambda b: abs((b[0] + b[1]) / 2 - cy))


def place_thickness(img):
    fit = img.fit()
    cx, r = fit["cx"], fit["r"]
    yb0, yb1 = _bar_rows(img, fit)
    xa, xb = int(cx - 0.1 * r), int(cx + 0.1 * r) + 1
    rows = np.arange(yb0 + 1, yb1)
    prof = img.a[rows, xa:xb].mean(1)
    half = len(rows) // 2
    if half < 2:
        raise ValueError("두께 경사판 막대가 너무 얇습니다.")
    i_top = int(np.argmax(prof[:half]))
    i_bot = half + int(np.argmax(prof[half:]))
    split = i_top + int(np.argmin(prof[i_top:i_bot + 1]))
    top = [rows[i] for i in range(0, split) if prof[i] > 0.5 * prof[i_top]]
    bot = [rows[i] for i in range(split + 1, len(rows)) if prof[i] > 0.5 * prof[i_bot]]
    if not top or not bot:
        raise ValueError("두께 경사판 두 개를 구분하지 못했습니다.")
    out = [ann_rect("ACR ST ROI top", xa, top[0], xb, top[-1] + 1, "thickness"),
           ann_rect("ACR ST ROI bottom", xa, bot[0], xb, bot[-1] + 1, "thickness")]
    m_top = roi_mean(img, out[0])
    m_bot = roi_mean(img, out[1])
    thr = (m_top + m_bot) / 4          # ACR: 두 경사판 평균의 절반을 기준
    for name, strip in (("top", top), ("bottom", bot)):
        y = (strip[0] + strip[-1]) / 2
        p = img.a[strip[0]:strip[-1] + 1].mean(0)
        x_lo, x_hi = _run_around(p, int(cx), thr, int(cx - 0.9 * r), int(cx + 0.9 * r))
        out.append(ann_line(f"ACR ST {name}", (x_lo, y), (x_hi, y), "thickness", "#00e5ff"))
    return out


def _run_around(p, start, thr, lo, hi):
    """p[start] 근처 최댓값에서 양쪽으로 thr 아래가 될 때까지 (보간 위치)"""
    lo, hi = max(1, lo), min(len(p) - 2, hi)
    w = 12
    s = max(lo, start - w) + int(np.argmax(p[max(lo, start - w):min(hi, start + w)]))
    if p[s] <= thr:
        raise ValueError("경사판 신호가 기준보다 낮습니다.")
    i = s
    while i > lo and p[i - 1] > thr:
        i -= 1
    j = s
    while j < hi and p[j + 1] > thr:
        j += 1

    def interp(k_out, k_in):
        v0, v1 = p[k_out], p[k_in]
        return k_out + (thr - v0) / (v1 - v0) if v1 != v0 else k_in
    return interp(i - 1, i), interp(j + 1, j)


# ═══ 4. 절편 위치 (slice 1·11 위쪽 쐐기 막대 두 개) ═══

def _longest_run(mask):
    """True가 가장 길게 이어진 구간 (시작, 끝 포함) - 없으면 None"""
    best, start = None, None
    for i, v in enumerate(np.r_[mask, False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if best is None or i - start > best[1] - best[0] + 1:
                best = (start, i - 1)
            start = None
    return best


def place_position(img, which):
    """팬텀 위쪽 가운데 쐐기 막대 두 개 (나란히 붙은 긴 어두운 막대)의 길이 → 오른쪽 − 왼쪽"""
    fit = img.fit()
    cx, cy, r, level = fit["cx"], fit["cy"], fit["r"], fit["level"]
    thr = level / 2
    y_top = cy - r
    ya, yb = int(max(0, y_top - 4)), int(min(img.shape[0], y_top + 0.5 * r))
    xa, xb = int(cx - 0.15 * r), int(cx + 0.15 * r) + 1
    dark = img.a[ya:yb, xa:xb] < thr
    runs = np.zeros(dark.shape[1])
    for c in range(dark.shape[1]):
        run = _longest_run(dark[:, c])
        if run is not None and run[0] <= 0.12 * r:      # 위 가장자리에서 시작하는 막대만
            runs[c] = run[1] - run[0] + 1
    cols = np.nonzero(runs > 0.18 * r)[0]                # 옆의 둥근 홈(짧음)은 제외
    if len(cols) < 4:
        raise ValueError("절편 위치 막대를 찾지 못했습니다.")
    groups = np.split(cols, np.nonzero(np.diff(cols) > 1)[0] + 1)
    groups.sort(key=len, reverse=True)
    if len(groups) >= 2 and len(groups[1]) >= 3:
        left, right = sorted(groups[:2], key=lambda g: g[0])
    else:
        g = groups[0]
        mid = len(g) // 2
        left, right = g[:mid], g[mid:]
    out = []
    for tag, g in (("L", left), ("R", right)):
        core = g[len(g) // 4: len(g) - len(g) // 4] if len(g) >= 4 else g
        x = xa + float(np.mean(core)) + 0.0
        p = img.a[ya:yb, xa + core[0]:xa + core[-1] + 1].mean(1)
        run = _longest_run(p < thr)
        k = run[1]
        v0, v1 = p[k], p[min(len(p) - 1, k + 1)]
        end = k + ((thr - v0) / (v1 - v0) if v1 != v0 else 0.5)
        out.append(ann_line(f"ACR SP {which} {tag}", (x, ya + run[0]), (x, ya + end), "position", "#ff9f40"))
    # 두 막대는 같은 기준(위 가장자리)에서 시작 → 길이 차이 = 아래 끝 차이
    y0 = min(out[0]["pts"][0][1], out[1]["pts"][0][1])
    for ann in out:
        ann["pts"][0] = (ann["pts"][0][0], y0)
    return out


# ═══ 5·6. 균일도 · 고스팅 (slice 7) ═══

def _disk_mean_map(img, radius_px):
    """반지름 radius_px 원 안 평균 (글자 픽셀 제외) 지도 + 원 안 유효 비율"""
    from scipy.signal import fftconvolve
    r = int(math.ceil(radius_px))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    k = (np.hypot(xx, yy) <= radius_px).astype(float)
    v = img.valid.astype(float)
    s = fftconvolve(img.a * v, k, mode="same")
    n = fftconvolve(v, k, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        return s / np.maximum(n, 1e-6), n / k.sum()


def _large_roi(img):
    """200 cm² 원: 팬텀 중심. 위쪽 구조물(노치 아래 막대)에 닿으면 닿지 않을 때까지 아래로"""
    from scipy import ndimage as ndi
    fit = img.fit()
    cx, cy, r, level = fit["cx"], fit["cy"], fit["r"], fit["level"]
    r_mm = math.sqrt(LARGE_ROI_MM2 / math.pi)
    rx, ry = r_mm / img.px[1], r_mm / img.px[0]
    yy, xx = np.indices(img.shape)
    structure = ndi.binary_dilation((img.a < 0.5 * level) & fit["filled"], iterations=2)
    shift = 0.0
    while shift < 0.1 * r:
        inside = ((xx - cx) / rx) ** 2 + ((yy - cy - shift) / ry) ** 2 <= 1
        if not (inside & structure).any():
            break
        shift += 1.0
    return cx, cy + shift, rx, ry, structure


def place_uniformity(img):
    """200 cm² 큰 ROI 안에서 1 cm² 평균이 가장 높은 곳·낮은 곳 (구조물·글자 제외)"""
    cx, cy, rx, ry, structure = _large_roi(img)
    rs_mm = math.sqrt(SMALL_ROI_MM2 / math.pi)
    rs = rs_mm / min(img.px)
    means, cover = _disk_mean_map(img, rs)
    _m, clean = _disk_mean_map_mask(img, ~structure, rs)
    yy, xx = np.indices(img.shape)
    ok = ((((xx - cx) / (rx - rs)) ** 2 + ((yy - cy) / (ry - rs)) ** 2 <= 1)
          & (cover > 0.99) & (clean > 0.999))
    if not ok.any():
        raise ValueError("균일도 ROI를 놓을 자리가 없습니다.")
    vals = np.where(ok, means, np.nan)
    iy, ix = np.unravel_index(np.nanargmax(vals), vals.shape)
    jy, jx = np.unravel_index(np.nanargmin(vals), vals.shape)
    srx, sry = rs_mm / img.px[1], rs_mm / img.px[0]
    return [ann_ellipse("ACR PIU large", cx, cy, rx, ry, "uniformity", "#50ff78", circle=True),
            ann_ellipse("ACR PIU max", ix, iy, srx, sry, "uniformity", "#ff5050", circle=True),
            ann_ellipse("ACR PIU min", jx, jy, srx, sry, "uniformity", "#5aa0ff", circle=True)]


def _disk_mean_map_mask(img, mask, radius_px):
    """원 안에서 mask가 차지하는 비율 지도"""
    from scipy.signal import fftconvolve
    r = int(math.ceil(radius_px))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    k = (np.hypot(xx, yy) <= radius_px).astype(float)
    return None, fftconvolve(mask.astype(float), k, mode="same") / k.sum()


def place_ghosting(img):
    """팬텀 바깥 위·아래·왼쪽·오른쪽 타원 (10 cm², 4:1). 글자·자 오버레이는 피해서"""
    fit = img.fit()
    cx, cy, r = fit["cx"], fit["cy"], fit["r"]
    h, w = img.shape
    a_mm = math.sqrt(GHOST_ROI_MM2 * 4 / math.pi)   # 긴 반지름
    b_mm = a_mm / 4
    out = []   # 팬텀 신호는 균일도의 큰 ROI (ACR PIU large)를 같이 씀
    sides = {"top": (0, -1), "bottom": (0, 1), "left": (-1, 0), "right": (1, 0)}
    for name, (dx, dy) in sides.items():
        vertical = dx != 0
        edge = (cx + dx * r) if vertical else (cy + dy * r)
        limit = (w if dx > 0 else 0) if vertical else (h if dy > 0 else 0)
        gap = abs(limit - edge)
        half_short = (b_mm / img.px[1]) if vertical else (b_mm / img.px[0])
        half_long = (a_mm / img.px[0]) if vertical else (a_mm / img.px[1])
        scale = 1.0
        if 2 * half_short + 4 > gap:     # FOV가 좁으면 비율 유지하며 줄임
            scale = max(0.3, (gap - 4) / (2 * half_short))
        hs, hl = half_short * scale, half_long * scale
        center_n = edge + (dx if vertical else dy) * gap / 2
        best = None
        for shift in (0, 10, -10, 20, -20, 35, -35, 50, -50, 70, -70, 90, -90):
            ecx, ecy = (center_n, cy + shift) if vertical else (cx + shift, center_n)
            rx, ry = (hs, hl) if vertical else (hl, hs)
            if ecx - rx < 0 or ecx + rx > w or ecy - ry < 0 or ecy + ry > h:
                continue
            ann = ann_ellipse(f"ACR Ghost {name}", ecx, ecy, rx, ry, "ghosting", "#ffd24a")
            bad = 0
            if img.text is not None:
                from ..roi_tools import mask_of
                bad = int((mask_of(ann, img.shape) & img.text).sum())
            if best is None or bad < best[0]:
                best = (bad, ann)
            if bad == 0:
                break
        if best is not None:
            if scale < 1:
                best[1]["note"] = f"FOV가 좁아 {scale * 100:.0f}% 크기"
            out.append(best[1])
    return out


# ═══ 7. 저대조도 검출능 (slice 8–11) ═══

def lc_center(img):
    """저대조도 원판 영역 (어두운 테두리 원) 중심·안쪽 반지름"""
    fit = img.fit()
    cx, cy, r, level = fit["cx"], fit["cy"], fit["r"], fit["level"]
    yy, xx = np.indices(img.shape)
    dark = (np.hypot(xx - cx, yy - cy) < 0.75 * r) & (img.a < 0.5 * level) & img.valid
    ys, xs = np.nonzero(dark)
    if len(xs) < 50:
        return cx, cy, 0.47 * r
    rcx, rcy, rr = _circle_fit(xs.astype(float), ys.astype(float))
    for _ in range(2):
        d = np.abs(np.hypot(xs - rcx, ys - rcy) - rr)
        keep = d < max(3.0, np.percentile(d, 85))
        rcx, rcy, rr = _circle_fit(xs[keep].astype(float), ys[keep].astype(float))
    dist = np.hypot(xs - rcx, ys - rcy)
    inner = float(np.percentile(dist[np.abs(dist - rr) < 0.1 * rr], 3))
    return float(rcx), float(rcy), inner


def _disk_contrast(img, x, y, rad, plate=None):
    """원판 안 평균 − 둘레 평균, 둘레 표준편차. plate=(cx, cy, 안쪽 반지름, 어두움 기준)이면
    원판 영역 밖(테두리)·어두운 픽셀은 둘레에서 뺌"""
    pad = int(rad + max(2.5, rad) + 3)
    x0, x1 = max(0, int(x) - pad), int(x) + pad + 1
    y0, y1 = max(0, int(y) - pad), int(y) + pad + 1
    sub = img.a[y0:y1, x0:x1]
    if sub.size == 0:
        return 0.0, 1.0
    yy, xx = np.indices(sub.shape)
    d = np.hypot(xx + x0 - x, yy + y0 - y)
    ring_m = (d >= rad + 1.5) & (d <= rad + 1.5 + max(2.5, rad))
    if plate is not None:
        pcx, pcy, inner, dark = plate
        ring_m &= (np.hypot(xx + x0 - pcx, yy + y0 - pcy) < inner - 2) & (sub > dark)
    inner_v = sub[d <= max(1.0, 0.6 * rad)]
    ring = sub[ring_m]
    if not inner_v.size or ring.size < 6:
        return 0.0, 1.0
    return float(inner_v.mean() - ring.mean()), float(max(0.5, ring.std()))


def low_contrast(img, prior=None, search=None, refine=True):
    """스포크 10개 × 원판 3개 → 원판별 대비·보임 여부 → 완전한 스포크 수 (큰 스포크부터 연속)

    prior: (회전각, 크기 배율, 중심 x, 중심 y) 추정값. search: 회전 탐색 범위 ± 도 (None이면 전체)
    원판은 슬라이스마다 9°씩 돌아가 있고 중심은 같으므로 slice 11에서 찾은 값으로 나머지를 예측
    (각 슬라이스의 테두리 원은 기포 때문에 어긋날 수 있음).
    """
    rcx, rcy, inner = lc_center(img)
    plate = (rcx, rcy, inner, 0.5 * img.fit()["level"])
    if prior is not None and prior[2] is not None:
        rcx, rcy = prior[2], prior[3]
    px = (img.px[0] + img.px[1]) / 2

    def disks(theta, scale=1.0, dx=0.0, dy=0.0):
        out = []
        for k in range(10):
            ang = math.radians(theta + 36 * k)
            for j, rad_mm in enumerate(LC_RADII_MM):
                rr = rad_mm * scale / px
                out.append((k, j, rcx + dx + rr * math.sin(ang), rcy + dy - rr * math.cos(ang),
                            LC_DIAMETERS_MM[k] / 2 / px))
        return out

    def score(theta, scale=1.0, dx=0.0, dy=0.0, spokes=6):
        total = 0.0
        for k, j, x, y, rad in disks(theta, scale, dx, dy):
            if k < spokes:
                c, _s = _disk_contrast(img, x, y, rad, plate)
                total += c * (1.0 + 0.2 * (spokes - 1 - k))
        return total

    theta0, scale = (prior[0], prior[1]) if prior is not None else (None, 1.0)
    dx = dy = 0.0
    if theta0 is None or search is None:
        grid = np.arange(0, 360, 2.0)
    else:
        grid = np.arange(theta0 - search, theta0 + search + 0.01, 1.0)
    theta = float(max(grid, key=lambda t: score(t, scale, dx, dy)))
    theta = float(max(np.arange(theta - 1.5, theta + 1.51, 0.5), key=lambda t: score(t, scale, dx, dy)))
    # 중심·크기 미세 조정 (작은 원판은 1 px 차이에도 놓침). slice 11에서 찾은 값을 받은
    # 나머지 슬라이스는 중심만 ±1 px (기울기로 조금씩 다름), 크기는 그대로
    for _ in range(2 if refine else 1):
        dx, dy = max(((dx + ex, dy + ey) for ex in (-1, -0.5, 0, 0.5, 1) for ey in (-1, -0.5, 0, 0.5, 1)),
                     key=lambda d: score(theta, scale, d[0], d[1], 10))
        if refine:
            scale = max((scale * f for f in (0.985, 0.9925, 1.0, 1.0075, 1.015)),
                        key=lambda sc: score(theta, sc, dx, dy, 10))
        theta = float(max(np.arange(theta - 1, theta + 1.01, 0.25), key=lambda t: score(t, scale, dx, dy, 10)))
    items = []
    for k, j, x, y, rad in disks(theta, scale, dx, dy):
        c, sd = _disk_contrast(img, x, y, rad, plate)
        n = max(1.0, math.pi * (0.6 * rad) ** 2)
        snr = c / sd * math.sqrt(n)
        items.append({"spoke": k + 1, "disk": j + 1, "x": x, "y": y, "r": rad, "contrast": c,
                      "snr": snr, "visible": bool(c > 0 and snr > LC_SNR_MIN)})
    spokes = 0
    for k in range(10):
        if all(d["visible"] for d in items if d["spoke"] == k + 1):
            spokes += 1
        else:
            break
    return {"spokes": spokes, "disks": items, "rotation": (theta % 360, scale, rcx + dx, rcy + dy),
            "center": (rcx + dx, rcy + dy), "inner": inner}


def low_contrast_set(imgs, prior=None):
    """slice 8, 9, 10, 11 네 장 → [결과]. slice 11(대비 가장 큼)에서 찾고 9°씩 되돌려 예측"""
    last = low_contrast(imgs[3], (prior[0], prior[1], None, None) if prior else None,
                        search=12 if prior else None)
    theta, scale, cx, cy = last["rotation"]
    out = [None, None, None, last]
    for i in (2, 1, 0):
        guess = (theta - LC_STEP_DEG * (3 - i), scale, cx, cy)
        out[i] = low_contrast(imgs[i], guess, search=6, refine=False)
    return out


LC_STEP_DEG = 9.0
LC_SNR_MIN = 4.0


# ═══ 2. 고대조도 분해능 (slice 1 삽입물) ═══

def resolution(img):
    """삽입물의 구멍 배열 6개 (1.1·1.0·0.9 mm × 위왼쪽 UL / 아래오른쪽 LR) → 크기별 분해 여부"""
    from scipy import ndimage as ndi
    fit = img.fit()
    cx, cy, r, level = fit["cx"], fit["cy"], fit["r"], fit["level"]
    yb0, yb1 = _bar_rows(img, fit)
    y0, y1 = yb1 + 2, int(min(img.shape[0], cy + 0.75 * r))
    x0, x1 = int(cx - 0.75 * r), int(cx + 0.75 * r)
    region = img.a[y0:y1, x0:x1]
    dark = ndi.binary_opening(region < 0.5 * level, iterations=1)
    lab, n = ndi.label(dark)
    if n == 0:
        raise ValueError("분해능 삽입물을 찾지 못했습니다.")
    sizes = ndi.sum(dark, lab, range(1, n + 1))
    ins = ndi.binary_fill_holes(lab == int(np.argmax(sizes)) + 1)
    ys, xs = np.nonzero(ins)
    bx0, by0, bx1, by1 = xs.min() + x0, ys.min() + y0, xs.max() + x0, ys.max() + y0
    sub = img.a[by0:by1 + 1, bx0:bx1 + 1]
    inside = ins[by0 - y0:by1 - y0 + 1, bx0 - x0:bx1 - x0 + 1]
    bg = float(np.median(sub[inside]))
    t = bg + 0.15 * (level - bg)
    holes = (sub > t) & inside
    grown = ndi.binary_dilation(holes, iterations=max(2, int(round(1.5 / img.px[1]))))
    lab, n = ndi.label(grown)
    pairs = []   # 짝 (UL 배열과 LR 배열이 대각선으로 붙어 한 덩어리) - 실제 구멍 픽셀의 범위
    for i, sl in enumerate(ndi.find_objects(lab)):
        h_mm = (sl[0].stop - sl[0].start) * img.px[0]
        w_mm = (sl[1].stop - sl[1].start) * img.px[1]
        if 11 <= h_mm <= 28 and 11 <= w_mm <= 28:
            ys_, xs_ = np.nonzero(holes[sl] & (lab[sl] == i + 1))
            pairs.append((xs_.min() + sl[1].start + bx0, ys_.min() + sl[0].start + by0,
                          xs_.max() + sl[1].start + bx0 + 1, ys_.max() + sl[0].start + by0 + 1))
    pairs.sort(key=lambda b: b[0])
    result = {"box": (int(bx0), int(by0), int(bx1), int(by1)), "arrays": [], "ul": None, "lr": None,
              "note": ""}
    if len(pairs) < 3:
        result["note"] = f"구멍 배열 짝 {len(pairs)}개만 찾음 - 영상을 보고 직접 입력하세요."
        return result
    for size, (x0, y0, x1, y1) in zip(RES_SIZES, pairs[-3:]):   # 오른쪽 3짝 (왼쪽은 큰 사각형)
        ax, ay = 8 * size / img.px[1], 8 * size / img.px[0]      # 배열 한 변 = 구멍 4개 × 2d
        m = 2
        ul = (int(x0 - m), int(y0 - m), int(math.ceil(x0 + ax + m)), int(math.ceil(y0 + ay + m)))
        lr = (int(x1 - ax - m), int(y1 - ay - m), int(math.ceil(x1 + m)), int(math.ceil(y1 + m)))
        result["arrays"].append({"size": size, "ul_box": ul, "lr_box": lr,
                                 "ul": _array_resolved(img.a, ul, bg, axis=1),
                                 "lr": _array_resolved(img.a, lr, bg, axis=0)})
    for key in ("ul", "lr"):
        best = None
        for arr in result["arrays"]:
            if arr[key]:
                best = arr["size"]
            else:
                break
        result[key] = best
    return result


RES_DIP = 0.25


def _array_resolved(a, box, bg, axis):
    """4×4 구멍 배열: 한 줄(axis=1 → 가로 줄)이라도 구멍 4개가 모두 따로 보이면 True"""
    from scipy.signal import find_peaks
    x0, y0, x1, y1 = box
    sub = a[y0:y1, x0:x1]
    if sub.size == 0:
        return False
    lines = sub if axis == 1 else sub.T          # 각 줄 = 구멍 방향 프로파일
    energy = lines.mean(1)
    peaks, _ = find_peaks(energy)
    rows = sorted(peaks, key=lambda i: energy[i], reverse=True)[:4] or [int(np.argmax(energy))]
    for i in rows:
        p = lines[max(0, i - 0):i + 1].mean(0)
        top, _ = find_peaks(p)
        top = sorted(sorted(top, key=lambda k: p[k], reverse=True)[:4])
        if len(top) < 4:
            continue
        amp = min(p[top]) - bg
        if amp <= 0:
            continue
        dips = [min(p[top[m]], p[top[m + 1]]) - p[top[m]:top[m + 1] + 1].min() for m in range(3)]
        if min(dips) >= RES_DIP * amp:
            return True
    return False


# ═══ 영상 찾기 ═══

def classify(img):
    """'localizer' | 'grid' | 'axial' | 'saturated' | 'none' + 특징"""
    try:
        fit = img.fit()
    except ValueError:
        return "none", {}
    cx, cy, r, level = fit["cx"], fit["cy"], fit["r"], fit["level"]
    a = img.a
    if a.max() <= 255 and (a >= 250).mean() > 0.08:   # 8비트 화면 캡처에서 W/L로 흰색 포화
        return "saturated", fit
    from scipy import ndimage as ndi
    filled = fit["filled"]
    ys, xs = np.nonzero(filled & ~ndi.binary_erosion(filled))
    resid = float(np.median(np.abs(np.hypot(xs - cx, ys - cy) - r))) / r if len(xs) else 1.0
    yy, xx = np.indices(a.shape)
    inner = np.hypot(xx - cx, yy - cy) < 0.55 * r
    dark_frac = float((a[inner] < 0.5 * level).mean())
    fit["circle_resid"], fit["dark_center"] = resid, dark_frac
    if dark_frac > 0.8:          # slice 5: 가운데 격자 (대부분 어두움)
        return "grid", fit
    if resid > 0.08:             # 원이 아님 → 시상 localizer
        return "localizer", fit
    return "axial", fit


def has_bar(img):
    try:
        _bar_rows(img, img.fit())
        return True
    except ValueError:
        return False


def find_sets(images, progress=None):
    """images: [Img] (불러온 순서). → {'LOC': Img|None, 'T1': [11], 'T2': [11]|None, 'note'}"""
    kinds = []
    for i, img in enumerate(images):
        kinds.append(classify(img)[0])
        if progress:
            progress(i + 1, len(images))
    with_te = [img.te for img in images if img.te is not None]
    groups = []
    if with_te:
        by = {}
        for img, kind in zip(images, kinds):
            by.setdefault((id(img.ref[0]) if img.ref else 0, round(img.te or 0, 1)), []).append((img, kind))
        groups = list(by.values())
    else:
        groups = [list(zip(images, kinds))]
    found = []
    for g in groups:
        ks = [k for _, k in g]
        for gi, kind in enumerate(ks):
            if kind != "grid":
                continue
            for stride in (1, 2):
                s1 = gi - 4 * stride
                last = s1 + 10 * stride
                if s1 < 0 or last >= len(g):
                    continue
                idx = [s1 + stride * n for n in range(11)]
                if any(ks[i] not in ("axial", "grid") for i in idx):
                    continue
                if sum(ks[i] == "grid" for i in idx) != 1 or not has_bar(g[s1][0]):
                    continue
                found.append((g, idx, stride, gi))
                break
    if not found:
        raise ValueError("ACR 팬텀 slice 1–11 (격자 영상이 5번째)을 찾지 못했습니다.")
    sets = []   # (영상 11장, 간격, 첫 영상 순번)
    order = {id(img): i for i, img in enumerate(images)}
    for g, idx, stride, gi in found:
        imgs = [g[i][0] for i in idx]
        if any(set(map(id, imgs)) & set(map(id, s[0])) for s in sets):
            continue
        sets.append((imgs, stride, order[id(imgs[0])]))
    if with_te:   # DICOM: 짧은 TE = T1, 긴 TE(≥50 ms) = T2
        sets.sort(key=lambda s: s[0][0].te or 0)
        t1 = sets[0][0]
        t2 = next((s[0] for s in sets[1:] if (s[0][0].te or 0) >= 50), None)
    else:         # 이미지 순서: 첫 세트 = T1, 다음 = T2. 이중 에코(2장 간격 두 세트)는 둘째 에코
        sets.sort(key=lambda s: s[2])
        t1 = sets[0][0]
        t2 = None
        rest = sets[1:]
        if rest:
            t2 = rest[0][0]
            if (len(rest) > 1 and rest[0][1] == 2 and rest[1][1] == 2
                    and abs(rest[1][2] - rest[0][2]) == 1):
                t2 = rest[1][0]
    first = next(i for i, img in enumerate(images) if img is t1[0])
    loc = None
    for i in range(first - 1, -1, -1):
        if kinds[i] == "localizer":
            loc = images[i]
            break
    if loc is None:
        loc = next((img for img, k in zip(images, kinds) if k == "localizer"), None)
    return {"LOC": loc, "T1": t1, "T2": t2, "kinds": kinds}


# ═══ 값 계산 (주석 → 결과) ═══

def compute(roles, images, lc_counts, res_values):
    """roles: {주석 이름: (Img, 주석)}. lc_counts: {'T1': [s8..s11], 'T2': [...]}.
    res_values: {'T1': (ul, lr), ...} → {(test, seq): {값...}}"""
    out = {}

    def get(name):
        return roles.get(name)

    # 1. 기하
    geo = {}
    for name, (img, ann) in roles.items():
        if name.startswith("T1|ACR Geo "):
            geo[name[len("T1|ACR Geo "):]] = line_mm(img, ann)
    if geo:
        out[("geometry", "T1")] = geo
    for seq in ("T1", "T2"):
        # 3. 두께
        t, b = get(f"{seq}|ACR ST top"), get(f"{seq}|ACR ST bottom")
        if t and b:
            lt, lb = line_mm(*t), line_mm(*b)
            thk = 0.2 * lt * lb / (lt + lb) if lt + lb > 0 else float("nan")
            out[("thickness", seq)] = {"top": lt, "bottom": lb, "thickness": thk}
        # 4. 위치
        pos = {}
        for s in ("S1", "S11"):
            left, right = get(f"{seq}|ACR SP {s} L"), get(f"{seq}|ACR SP {s} R")
            if left and right:
                pos[s] = line_mm(*right) - line_mm(*left)
        if pos:
            out[("position", seq)] = pos
        # 5. 균일도
        large, hi, lo = get(f"{seq}|ACR PIU large"), get(f"{seq}|ACR PIU max"), get(f"{seq}|ACR PIU min")
        if large and hi and lo:
            m_hi, m_lo = roi_mean(*hi), roi_mean(*lo)
            out[("uniformity", seq)] = {"large": roi_mean(*large), "max": m_hi, "min": m_lo,
                                        "piu": 100 * (1 - (m_hi - m_lo) / (m_hi + m_lo))
                                        if m_hi + m_lo > 0 else float("nan")}
        # 6. 고스팅
        big = get(f"{seq}|ACR Ghost large") or get(f"{seq}|ACR PIU large")
        sides = {s: get(f"{seq}|ACR Ghost {s}") for s in ("top", "bottom", "left", "right")}
        if big and all(sides.values()):
            m = {s: roi_mean(*v) for s, v in sides.items()}
            m_big = roi_mean(*big)
            ratio = abs((m["top"] + m["bottom"]) - (m["left"] + m["right"])) / (2 * m_big) * 100
            out[("ghosting", seq)] = dict(m, large=m_big, ratio=ratio)
        # 7. 저대조도
        counts = lc_counts.get(seq)
        if counts is not None:
            out[("low_contrast", seq)] = {"slices": list(counts), "total": int(sum(counts))}
        # 2. 분해능
        res = res_values.get(seq)
        if res is not None:
            out[("resolution", seq)] = {"ul": res[0], "lr": res[1]}
    return out


def evaluate(values, criteria):
    """→ [(test, seq, 측정값 문자열, 기준 문자열, True/False/None)]"""
    c = criteria
    rows = []
    for test, name in TESTS:
        for seq in ("T1", "T2"):
            v = values.get((test, seq))
            if v is None:
                continue
            if test == "geometry":
                parts, ok = [], True
                for key, mm in v.items():
                    nominal = c["loc_nominal"] if key == "LOC" else c["axial_nominal"]
                    good = abs(mm - nominal) <= c["geo_tol"]
                    ok &= good
                    parts.append(f"{key} {mm:.1f}")
                rows.append((test, seq, ", ".join(parts),
                             f"LOC {c['loc_nominal']:g}±{c['geo_tol']:g}, 축상 {c['axial_nominal']:g}±{c['geo_tol']:g} mm", ok))
            elif test == "resolution":
                ul, lr = v["ul"], v["lr"]
                ok = None if ul is None and lr is None else (
                    ul is not None and lr is not None and ul <= c["res_max"] + 1e-6 and lr <= c["res_max"] + 1e-6)
                rows.append((test, seq, f"UL {_res(ul)} / LR {_res(lr)}", f"≤ {c['res_max']:g} mm", ok))
            elif test == "thickness":
                ok = abs(v["thickness"] - c["thk_nominal"]) <= c["thk_tol"]
                rows.append((test, seq, f"{v['thickness']:.2f} mm (위 {v['top']:.1f} / 아래 {v['bottom']:.1f})",
                             f"{c['thk_nominal']:g} ± {c['thk_tol']:g} mm", ok))
            elif test == "position":
                ok = all(abs(d) <= c["pos_max"] for d in v.values())
                rows.append((test, seq, ", ".join(f"{s} {d:+.1f} mm" for s, d in v.items()),
                             f"|차이| ≤ {c['pos_max']:g} mm", ok))
            elif test == "uniformity":
                rows.append((test, seq, f"{v['piu']:.1f} %", f"≥ {c['piu_min']:g} %", v["piu"] >= c["piu_min"]))
            elif test == "ghosting":
                rows.append((test, seq, f"{v['ratio']:.2f} %", f"≤ {c['ghost_max']:g} %",
                             v["ratio"] <= c["ghost_max"] if seq == "T1" else None))
            elif test == "low_contrast":
                rows.append((test, seq, f"{v['total']} ({' + '.join(map(str, v['slices']))})",
                             f"≥ {c['lc_min']}", v["total"] >= c["lc_min"]))
    return rows


def _res(v):
    return f"{v:.1f} mm" if v is not None else "-"


def overall(rows):
    judged = [r[4] for r in rows if r[4] is not None]
    if not judged:
        return None
    return all(judged)


def date_from_text(text):
    """경로·설명에서 YYYYMMDD 날짜"""
    for m in re.finditer(r"(20\d{2})(\d{2})(\d{2})", text or ""):
        y, mo, d = m.groups()
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    return ""
