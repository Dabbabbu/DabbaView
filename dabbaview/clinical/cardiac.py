# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
심장 분석 - 윤곽(LV/RV Endo/Epi), 용적(Simpson), EF·SV·심근 질량, 17분절 Bull's Eye,
LGE 경색 정량(n-SD, FWHM), Feature Tracking 스트레인

윤곽은 영상 좌표(픽셀 모서리 기준, 픽셀 중심 = +0.5) 다각형으로 저장하며
영상마다 슬라이스 위치(mm)와 심장 위상(TriggerTime)을 함께 기록한다.
"""
import math

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from ..annotations import instance_key
from ..roi import polygon_mask
from .data import position_key, slice_param

CONTOUR_TYPES = {
    "lv_endo": ("LV Endo", (255, 80, 80)),
    "lv_epi": ("LV Epi", (80, 220, 80)),
    "rv_endo": ("RV Endo", (80, 160, 255)),
}
MYOCARDIUM_DENSITY = 1.05   # g/mL

# AHA 17분절 이름
SEGMENT_NAMES = {
    1: "basal anterior", 2: "basal anteroseptal", 3: "basal inferoseptal",
    4: "basal inferior", 5: "basal inferolateral", 6: "basal anterolateral",
    7: "mid anterior", 8: "mid anteroseptal", 9: "mid inferoseptal",
    10: "mid inferior", 11: "mid inferolateral", 12: "mid anterolateral",
    13: "apical anterior", 14: "apical septal", 15: "apical inferior", 16: "apical lateral",
    17: "apex",
}


def polygon_area_mm2(pts, spacing):
    """다각형 면적 (mm²) - spacing = (행 간격, 열 간격)"""
    p = np.asarray(pts, dtype=float)
    if len(p) < 3:
        return 0.0
    x = p[:, 0] * spacing[1]
    y = p[:, 1] * spacing[0]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


class ContourStore(QObject):
    """심장 윤곽 저장소 (모든 뷰포트가 공유, 화면에 그리기용 조회 포함)"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def add(self, series, index, kind, pts):
        """같은 영상·같은 종류가 있으면 교체"""
        ds = series.slices[index]
        from .. import dicom_info
        spacing = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
        item = {"series_uid": series.series_uid, "index": index, "kind": kind,
                "sop": instance_key(ds, str(index)),
                "pts": [tuple(map(float, p)) for p in pts],
                "position": position_key(ds, 0.1) / 10.0,
                "phase": slice_param(ds, "phase"),
                "phase_index": _phase_index(series, index),
                "spacing": spacing, "area_mm2": polygon_area_mm2(pts, spacing)}
        self._items = [c for c in self._items
                       if not (c["sop"] == item["sop"] and c["kind"] == kind)]
        self._items.append(item)
        self.changed.emit()
        return item

    def remove_for_image(self, sop, kind=None):
        self._items = [c for c in self._items
                       if not (c["sop"] == sop and (kind is None or c["kind"] == kind))]
        self.changed.emit()

    def clear(self, series_uid=None):
        self._items = [c for c in self._items
                       if series_uid is not None and c["series_uid"] != series_uid]
        self.changed.emit()

    def for_image(self, sop):
        return [c for c in self._items if c["sop"] == sop]

    def for_series(self, series_uid):
        return [c for c in self._items if c["series_uid"] == series_uid]


def _phase_index(series, index):
    """같은 위치 영상들 중 이 영상의 심장 위상 순번 (0 = 첫 위상).
    슬라이스마다 TriggerTime이 몇 ms씩 달라도 같은 위상끼리 묶이게 함"""
    ds = series.slices[index]
    key = position_key(ds)
    times = sorted({round(slice_param(d, "phase") or 0.0, 1) for d in series.slices
                    if position_key(d) == key})
    t = round(slice_param(ds, "phase") or 0.0, 1)
    return times.index(t) if t in times else None


def phase_key(c):
    """윤곽의 위상 키: 위상 순번이 있으면 그것, 없으면 TriggerTime(ms)"""
    if c.get("phase_index") is not None:
        return int(c["phase_index"])
    return round(c["phase"], 1) if c.get("phase") is not None else 0.0


def phase_ms(contours, key):
    """위상 키 → 대표 TriggerTime (ms, 중앙값)"""
    vals = [c["phase"] for c in contours if phase_key(c) == key and c.get("phase") is not None]
    return float(np.median(vals)) if vals else float(key)


# ═══ 용적 ═══

def _slice_gap(positions, fallback):
    p = sorted(set(positions))
    if len(p) >= 2:
        return float(np.median(np.diff(p)))
    return float(fallback)


def volumes_by_phase(contours, slice_spacing=None):
    """{위상: {종류: (부피 mL, 슬라이스 수)}} - Simpson 원반 합산 (면적 × 슬라이스 간격)"""
    positions = [c["position"] for c in contours]
    gap = slice_spacing or _slice_gap(positions, 8.0)
    result = {}
    for c in contours:
        by_kind = result.setdefault(phase_key(c), {})
        vol, n = by_kind.get(c["kind"], (0.0, 0))
        by_kind[c["kind"]] = (vol + c["area_mm2"] * gap / 1000.0, n + 1)
    return result, gap


def ventricular_function(contours, slice_spacing=None, heart_rate=None):
    """LV/RV 기능 지표. ED = 내강 부피 최대 위상, ES = 최소 위상"""
    by_phase, gap = volumes_by_phase(contours, slice_spacing)
    out = {"slice_gap_mm": gap}
    for side, endo, epi in (("LV", "lv_endo", "lv_epi"), ("RV", "rv_endo", None)):
        phases = {p: v[endo][0] for p, v in by_phase.items() if endo in v}
        if not phases:
            continue
        ed = max(phases, key=phases.get)
        es = min(phases, key=phases.get)
        edv, esv = phases[ed], phases[es]
        sv = edv - esv
        res = {"ED phase (ms)": phase_ms(contours, ed), "ES phase (ms)": phase_ms(contours, es),
               "EDV (mL)": edv, "ESV (mL)": esv,
               "SV (mL)": sv, "EF (%)": sv / edv * 100 if edv > 0 else 0.0}
        if heart_rate:
            res["CO (L/min)"] = sv * heart_rate / 1000.0
        if epi and epi in by_phase.get(ed, {}):
            myo = by_phase[ed][epi][0] - edv
            res["Myocardial volume (mL)"] = myo
            res["Myocardial mass (g)"] = myo * MYOCARDIUM_DENSITY
        if len(phases) < 2:
            res["note"] = "ED/ES 두 위상 이상 윤곽이 필요합니다"
        out[side] = res
    return out


# ═══ 17분절 ═══

def _centroid(pts):
    p = np.asarray(pts, dtype=float)
    return p.mean(axis=0)


def _ray_hit(pts, center, angle):
    """중심에서 angle 방향 반직선이 다각형과 만나는 가장 먼 점까지 거리 (픽셀)"""
    p = np.asarray(pts, dtype=float)
    d = np.array([math.cos(angle), math.sin(angle)])
    best = 0.0
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        e = b - a
        m = np.array([[d[0], -e[0]], [d[1], -e[1]]])
        det = np.linalg.det(m)
        if abs(det) < 1e-12:
            continue
        t, u = np.linalg.solve(m, a - center)
        if t >= 0 and 0 <= u <= 1:
            best = max(best, t)
    return best


def _septum_frame(center, rv_center, septum_angle):
    """(중격 중심 각도, 앞벽 쪽 회전 부호). 화면 좌표(y 아래)에서 앞벽 = 화면 위쪽"""
    if septum_angle is not None:
        sept = septum_angle
    elif rv_center is not None:
        v = np.asarray(rv_center, dtype=float) - center
        sept = math.atan2(v[1], v[0])
    else:
        sept = math.pi   # 표준 단축 영상: 중격이 화면 왼쪽
    diff = (-math.pi / 2 - sept + math.pi) % (2 * math.pi) - math.pi
    return sept, (1 if diff >= 0 else -1)


# 중격 중심에서 앞벽 쪽으로 잰 각도(도) → 분절 (고리마다)
_SIX = [2, 1, 6, 5, 4, 3]          # 0~60 anteroseptal, 60~120 anterior, ...
_FOUR = [14, 13, 16, 15]           # 중격 ±45°, 앞벽, 측벽, 하벽


def _segment_of(rel_deg, level):
    """level 0=기저, 1=중간, 2=심첨"""
    if level < 2:
        return _SIX[int(rel_deg // 60) % 6] + 6 * level
    return _FOUR[int(((rel_deg + 45) % 360) // 90)]


def segment_values(slices, septum_angle=None, value_fn=None, n_rays=72):
    """slices: 기저→심첨 순서의 [{endo, epi, spacing, rv_center(선택), image(선택)}]

    분절 값: value_fn(image, mask)가 있으면 분절 마스크 안의 값(예: T1 평균, LGE %),
    없으면 벽 두께(mm, 중심에서 반직선으로 잰 심외막−심내막 거리)
    """
    n = len(slices)
    if n == 0:
        return {}
    levels = np.array_split(np.arange(n), 3)   # 기저 / 중간 / 심첨
    values = {s: [] for s in range(1, 18)}
    for level, idxs in enumerate(levels):
        for i in idxs:
            sl = slices[i]
            center = _centroid(sl["endo"])
            sept, sign = _septum_frame(center, sl.get("rv_center"), septum_angle)
            if value_fn is None:
                sp = sl["spacing"]
                for r in range(n_rays):
                    angle = 2 * math.pi * r / n_rays
                    rel = math.degrees(((angle - sept) * sign) % (2 * math.pi))
                    thick = _ray_hit(sl["epi"], center, angle) - _ray_hit(sl["endo"], center, angle)
                    scale = math.hypot(math.cos(angle) * sp[1], math.sin(angle) * sp[0])
                    values[_segment_of(rel, level)].append(max(thick, 0) * scale)
            else:
                img = sl["image"]
                h, w = img.shape
                myo = polygon_mask(sl["epi"], (h, w)) & ~polygon_mask(sl["endo"], (h, w))
                yy, xx = np.mgrid[0:h, 0:w]
                ang = np.arctan2(yy + 0.5 - center[1], xx + 0.5 - center[0])
                rel = np.degrees(((ang - sept) * sign) % (2 * math.pi))
                seg_map = np.vectorize(lambda d: _segment_of(d, level))(rel)
                for seg in np.unique(seg_map[myo]):
                    values[int(seg)].append(value_fn(img, myo & (seg_map == seg)))
    # 17 = apex: 심첨 고리(13~16) 값의 평균으로 근사
    apex = [v for seg in (13, 14, 15, 16) for v in values[seg]]
    values[17] = apex
    return {seg: float(np.mean(v)) if v else float("nan") for seg, v in values.items()}


# Bull's Eye 표준 배치 (1 anterior가 위, 중격이 왼쪽) - 분절 중심 각도(도, 반시계)
_PLOT_ANGLE = {1: 90, 2: 150, 3: 210, 4: 270, 5: 330, 6: 30,
               13: 90, 14: 180, 15: 270, 16: 0}


def plot_bullseye(ax, seg_values, title="", cmap="viridis", vmin=None, vmax=None, unit=""):
    """matplotlib 축에 AHA 17분절 Bull's Eye"""
    import matplotlib
    from matplotlib.patches import Circle, Wedge
    vals = np.array([seg_values.get(s, np.nan) for s in range(1, 18)], dtype=float)
    finite = vals[np.isfinite(vals)]
    lo = vmin if vmin is not None else (float(finite.min()) if finite.size else 0.0)
    hi = vmax if vmax is not None else (float(finite.max()) if finite.size else 1.0)
    norm = matplotlib.colors.Normalize(vmin=lo, vmax=max(hi, lo + 1e-6))
    colormap = matplotlib.colormaps[cmap]
    ax.set_aspect("equal")
    ax.axis("off")

    def color(v):
        return colormap(norm(v)) if np.isfinite(v) else (0.3, 0.3, 0.3, 1)

    def label(seg, v):
        return f"{seg}\n{v:.1f}" if np.isfinite(v) else str(seg)
    for r_out, r_in, segs, width in ((1.0, 0.75, range(1, 7), 60),
                                     (0.75, 0.5, range(7, 13), 60),
                                     (0.5, 0.25, range(13, 17), 90)):
        for seg in segs:
            mid = _PLOT_ANGLE[seg if seg > 12 else (seg - 1) % 6 + 1]
            v = vals[seg - 1]
            ax.add_patch(Wedge((0, 0), r_out, mid - width / 2, mid + width / 2,
                               width=r_out - r_in, facecolor=color(v), edgecolor="white", lw=1))
            rr = (r_out + r_in) / 2
            ax.text(rr * math.cos(math.radians(mid)), rr * math.sin(math.radians(mid)),
                    label(seg, v), ha="center", va="center", fontsize=7, color="white")
    ax.add_patch(Circle((0, 0), 0.25, facecolor=color(vals[16]), edgecolor="white", lw=1))
    ax.text(0, 0, label(17, vals[16]), ha="center", va="center", fontsize=7, color="white")
    ax.set_xlim(-1.05, 1.05)
    ax.set_ylim(-1.05, 1.05)
    ax.set_title(title + (f" ({unit})" if unit else ""), fontsize=9, color="#ddd")
    return norm, colormap


# ═══ LGE ═══

def lge_quantify(images, myo_masks, spacing, slice_gap, method="nsd", n_sd=5.0,
                 remote=None):
    """images/myo_masks: 슬라이스 목록. method: 'nsd' (remote 평균 + n·SD) / 'fwhm' (최대의 50%)

    remote: 정상 심근 값 배열 (nsd에 필요). 반환 dict + 경색 마스크 목록
    """
    myo_values = np.concatenate([img[m] for img, m in zip(images, myo_masks)]) \
        if myo_masks else np.array([])
    if myo_values.size == 0:
        raise ValueError("심근 영역이 없습니다 (LV Endo와 Epi 윤곽이 모두 필요).")
    if method == "fwhm":
        threshold = 0.5 * float(myo_values.max())
    else:
        if remote is None or len(remote) < 5:
            raise ValueError("n-SD 방법은 정상(remote) 심근 ROI가 필요합니다.")
        threshold = float(np.mean(remote) + n_sd * np.std(remote))
    infarct = [m & (img >= threshold) for img, m in zip(images, myo_masks)]
    vox_ml = spacing[0] * spacing[1] * slice_gap / 1000.0
    myo_ml = sum(int(m.sum()) for m in myo_masks) * vox_ml
    inf_ml = sum(int(m.sum()) for m in infarct) * vox_ml
    return {"threshold": threshold, "Myocardium (mL)": myo_ml,
            "Myocardial mass (g)": myo_ml * MYOCARDIUM_DENSITY,
            "LGE volume (mL)": inf_ml, "LGE mass (g)": inf_ml * MYOCARDIUM_DENSITY,
            "LGE (% of LV myocardium)": inf_ml / myo_ml * 100 if myo_ml else 0.0}, infarct


# ═══ 스트레인 (Feature Tracking) ═══

def resample_contour(pts, center, n=48):
    """중심에서 같은 각도 간격의 반직선이 윤곽과 만나는 점 n개"""
    return np.array([center + _ray_hit(pts, center, a) * np.array([math.cos(a), math.sin(a)])
                     for a in np.linspace(0, 2 * math.pi, n, endpoint=False)])


def track_strain(frames, endo, epi, spacing, n_points=48):
    """cine 프레임(T, H, W) + ED 프레임 윤곽 → 원주·방사 스트레인 곡선 (%)

    심내막·심외막 점을 LK 광학 흐름으로 따라가며
    GCS = 중간벽 둘레 변화율, GRS = 평균 벽 두께 변화율
    """
    import cv2
    frames = np.asarray(frames, dtype=np.float32)
    lo, hi = np.percentile(frames, [1, 99.5])
    imgs = [np.clip((f - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
            for f in frames]
    center = _centroid(endo)
    p_endo = resample_contour(endo, center, n_points)
    p_epi = resample_contour(epi, center, n_points)
    pts = np.vstack([p_endo, p_epi]).astype(np.float32) - 0.5   # 픽셀 중심 좌표
    track = [pts.copy()]
    lk = dict(winSize=(21, 21), maxLevel=3,
              criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    for a, b in zip(imgs[:-1], imgs[1:]):
        nxt, status, _err = cv2.calcOpticalFlowPyrLK(a, b, pts.reshape(-1, 1, 2), None, **lk)
        nxt = nxt.reshape(-1, 2)
        lost = status.ravel() == 0
        nxt[lost] = pts[lost]          # 놓친 점은 이전 위치 유지
        pts = nxt
        track.append(pts.copy())
    track = np.array(track)            # (T, 2n, 2)
    scale = np.array([spacing[1], spacing[0]])
    mm = track * scale
    endo_t, epi_t = mm[:, :n_points], mm[:, n_points:]
    mid = (endo_t + epi_t) / 2

    def perimeter(p):
        return np.sum(np.linalg.norm(np.roll(p, -1, axis=1) - p, axis=2), axis=1)
    length = perimeter(mid)
    thickness = np.linalg.norm(epi_t - endo_t, axis=2).mean(axis=1)
    gcs = (length / length[0] - 1) * 100
    grs = (thickness / thickness[0] - 1) * 100
    return {"GCS": gcs, "GRS": grs, "track": track, "n": n_points,
            "peak GCS (%)": float(gcs.min()), "peak GRS (%)": float(grs.max())}
