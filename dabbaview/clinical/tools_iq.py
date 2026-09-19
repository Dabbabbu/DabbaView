# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Analysis ▸ Image Quality Assessment - 영상 화질 평가 도구 페이지

모든 도구는 현재 보고 있는 영상(고른 시리즈의 현재 슬라이스)을 씁니다. 자동으로 놓은 ROI는
'IQ …' 이름의 주석이라 ROI Manager에서 보이고 옮길 수 있으며, 다시 계산 버튼은 옮긴 모양을 씁니다.
"""
import datetime
import json
import math
import os

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPen
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                             QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

from . import iq
from .iq import RAYLEIGH as RAY
from .panel import SeriesPicker, Tool, ispin, spin

ROI_KINDS = ("roi", "ellipse", "rect")
_MARKERS = {}        # 영상 키 → [("pt", x, y, color) | ("arrow", x0, y0, x1, y1, color)]


def _last(ctx):
    store = getattr(ctx.main, "_iq_last", None)
    if store is None:
        store = ctx.main._iq_last = {}
    return store


class IQTool(Tool):
    """공통: 현재 영상 · 픽셀 크기 · 글자 제외 · 물체 맞춤 · ROI 배치"""

    group = "iq"

    def series_row(self):
        from .tools_acr import _AutoPicker
        self.series = _AutoPicker(self.ctx, allow_none=True, none_text="(현재 보고 있는 영상)")
        self._pickers.append(self.series)
        self.form.addRow("시리즈:", self.series)

    def current(self):
        """(series, k, 배열, px, valid) - 시리즈를 고르지 않으면 지금 보고 있는 영상"""
        from .tools_acr import _is_screen_capture, _spacing, calibrate_capture
        s = self.series.series(required=False) or self.ctx.series()
        if s is None:
            raise ValueError("영상을 먼저 여세요.")
        vp_series = self.ctx.series()
        k = self.ctx.slice_index() if vp_series is s else s.num_slices // 2
        arr = np.asarray(self.ctx.image(s, k), dtype=float)
        if arr.ndim == 3:
            arr = arr.mean(-1)
        fov = self.ctx.main._app_settings.acr_fov()
        capture = _is_screen_capture(s)
        if capture:
            calibrate_capture(s, fov)
        px = _spacing(s, k, fov)
        valid = None
        if capture and arr.max() <= 255:
            from .acr import text_mask
            tm = text_mask(arr)
            valid = ~tm if tm is not None else None
        return s, k, arr, px, valid

    def fit(self, arr, valid):
        return iq.object_fit(arr, valid)

    def place(self, series, k, arr, px, anns, tag, color="#50c8ff"):
        """이전 같은 그룹 IQ 주석을 지우고 새로 놓음 → 놓은 주석 목록"""
        from ..annotations import image_key
        from ..roi_tools import recompute
        store = self.ctx.main._annotation_store
        key = image_key(series, k)
        for ann in list(store.items(key)):
            if ann.get("iq") == tag:
                store.remove(key, ann["id"])
        out = []
        for ann in anns:
            ann = dict(ann)
            ann["name"] = "IQ " + ann.get("name", tag)
            ann["iq"] = tag
            ann.setdefault("color", color)
            recompute(ann, arr, px)
            store.add(key, ann)
            out.append(ann)
        self._repaint()
        return out

    def placed(self, series, k, tag):
        """사용자가 옮겼을 수도 있는 그 그룹의 주석 {이름: 주석}"""
        from ..annotations import image_key
        return {a["name"][3:]: a for a in self.ctx.main._annotation_store.items(image_key(series, k))
                if a.get("iq") == tag}

    def user_rois(self):
        return [a for a in self.ctx.vp().annotations_here() if a["type"] in ROI_KINDS and not a.get("iq")]

    def markers(self, series, k, items):
        from ..annotations import image_key
        _MARKERS[image_key(series, k)] = items
        self._repaint()

    def _repaint(self):
        for vp in self.ctx.main._all_viewports():
            vp.update()

    def second_image(self, picker, slice_spin):
        s2 = picker.series(required=False)
        if s2 is None:
            return None
        k2 = min(slice_spin.value() - 1, s2.num_slices - 1)
        a = np.asarray(self.ctx.image(s2, k2), dtype=float)
        return a.mean(-1) if a.ndim == 3 else a


def _fmt(v, digits=3):
    if isinstance(v, float):
        return float("nan") if not math.isfinite(v) else round(v, digits)
    return v


# ═══ 1. MTF ═══

class MTFTool(IQTool):
    title = "MTF (Edge method)"
    help = ("에지를 가로지르는 직선(Dist, 4) 하나를 그리고 계산하세요. 에지를 줄마다 찾아 기울기를 맞추고 "
            "4배 과표본 ESF → LSF → MTF를 구합니다. '자동'은 팬텀 오른쪽 가장자리를 씁니다.")

    def build(self):
        self.series_row()
        self.width = spin(24, 6, 200, 0, suffix=" px")
        self.form.addRow("ROI 폭 (선에 수직):", self.width)
        self.button("📈 MTF 계산 (마지막 직선)", self.run)
        self.button("🤖 자동 (팬텀 가장자리)", lambda: self.run(auto=True))

    def run(self, auto=False):
        s, k, arr, px, valid = self.current()
        if auto:
            fit = self.fit(arr, valid)
            p0, p1 = iq.auto_edge_line(fit, px)
        else:
            if self.ctx.series() is not s:
                raise ValueError("MTF를 잴 시리즈를 화면에 띄우고 그 위에 선을 그리세요.")
            p0, p1 = self.ctx.last_lines(1)[0]
        r = iq.mtf_edge(arr, p0, p1, px, width=self.width.value())
        _last(self.ctx)["mtf"] = r
        self.place(s, k, arr, px, [{"type": "roi", "pts": r["rect"], "name": "MTF ROI"}], "mtf", "#ffd24a")
        show_mtf(self.ctx, r)
        return r


def show_mtf(ctx, r):
    rows = {"MTF50 (lp/mm)": _fmt(r["mtf50"]), "MTF10 (lp/mm)": _fmt(r["mtf10"]),
            "Nyquist (lp/mm)": _fmt(r["nyquist"]), "MTF @ Nyquist": _fmt(r["mtf_at_nyq"]),
            "에지 기울기 (°)": _fmt(r["edge_angle"], 2)}

    def plot(fig):
        ax = fig.add_subplot(211)
        ax.plot(r["f"], r["mtf"], color="#4a9eff")
        for level, key, c in ((0.5, "mtf50", "#fc4"), (0.1, "mtf10", "#f66")):
            if math.isfinite(r[key]):
                ax.plot([r[key]], [level], "o", color=c)
                ax.annotate(f"MTF{int(level * 100)} {r[key]:.2f}", (r[key], level), color=c, fontsize=7,
                            xytext=(4, 4), textcoords="offset points")
        ax.axvline(r["nyquist"], color="#aaa", ls="--", lw=1)
        ax.text(r["nyquist"], 0.95, " Nyquist", color="#aaa", fontsize=7)
        ax.set_xlim(0, min(r["f"][-1], 1.5 * r["nyquist"]))
        ax.set_ylim(0, 1.05)
        ax.set_xlabel("spatial frequency (lp/mm)")
        ax.set_ylabel("MTF")
        ax2 = fig.add_subplot(212)
        ax2.plot(r["x_mm"], r["esf"] / max(1e-9, np.abs(r["esf"]).max()), label="ESF", color="#8c8")
        ax2.plot(r["x_mm"], r["lsf"] / max(1e-9, np.abs(r["lsf"]).max()), label="LSF", color="#f96")
        ax2.set_xlabel("distance (mm)")
        ax2.legend(fontsize=7)
    ctx.results("MTF (edge)", rows, plot)


# ═══ 2. SNR ═══

class SNRTool(IQTool):
    title = "SNR (Signal-to-Noise Ratio)"
    help = ("단일 영상: 신호 ROI 평균 / 배경(공기) SD. MR 크기 영상은 Rayleigh 보정(÷0.655). "
            "두 영상: 같은 조건 두 장의 차영상 SD/√2를 잡음으로 (NEMA). ROI는 자동 배치 (물체 75% + 네 모서리).")

    def build(self):
        self.series_row()
        self.method = QComboBox()
        self.method.addItems(["단일 영상 (배경 SD)", "두 영상 (차분)"])
        self.form.addRow("방법:", self.method)
        self.second = SeriesPicker(self.ctx)
        self._pickers.append(self.second)
        self.second_k = ispin(2, 1, 9999)
        row = QHBoxLayout()
        row.addWidget(self.second, 1)
        row.addWidget(QLabel("영상"))
        row.addWidget(self.second_k)
        self.form.addRow("두 번째 영상:", row)
        self.rayleigh = QCheckBox("Rayleigh 보정 (MR 크기 영상)")
        self.rayleigh.setChecked(True)
        self.form.addRow(self.rayleigh)
        self.use_roi = QCheckBox("신호 ROI: 마지막으로 그린 ROI 사용 (없으면 자동)")
        self.form.addRow(self.use_roi)
        self.button("📶 SNR 계산 (ROI 자동 배치)", self.run)
        self.button("↻ 옮긴 ROI로 다시 계산", lambda: self.run(reuse=True))

    def on_show(self):
        super().on_show()
        s = self.ctx.series()
        if s is not None:
            self.rayleigh.setChecked(s.modality == "MR" or getattr(s, "source_format", "") == "image")

    def run(self, reuse=False):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        old = self.placed(s, k, "snr") if reuse else {}
        sig = old.get("SNR signal")
        if sig is None:
            user = self.user_rois() if self.use_roi.isChecked() else []
            sig = dict(user[-1]) if user else iq.signal_roi(fit)
            sig["name"] = "SNR signal"
        if self.method.currentIndex() == 0:
            bgs = [a for n, a in old.items() if n.startswith("BG")] or iq.background_rois(arr, fit["mask"], valid)
            r = iq.snr_single(arr, sig, bgs, valid, self.rayleigh.isChecked())
            self.place(s, k, arr, px, [sig] + bgs, "snr")
            rows = {"SNR": _fmt(r["snr"], 2), "신호 평균": _fmt(r["signal"]), "배경 평균": _fmt(r["bg_mean"]),
                    "배경 SD": _fmt(r["bg_sd"]), "잡음 σ": _fmt(r["noise"]),
                    "(참고) 신호/신호 SD": _fmt(r["snr_signal_sd"], 2), "배경 ROI 수": len(bgs)}
            note = "잡음 σ = 배경 SD / 0.655 (Rayleigh)" if self.rayleigh.isChecked() else "잡음 σ = 배경 SD"
            if r["bg_sd"] < 1.0 and arr.max() <= 255:
                note += " ⚠ 배경 SD < 1 (8비트 표시 값 양자화) → SNR이 과대 추정됩니다. 두 영상 방법을 권장"
        else:
            arr2 = self.second_image(self.second, self.second_k)
            if arr2 is None or arr2.shape != arr.shape:
                raise ValueError("크기가 같은 두 번째 영상을 고르세요.")
            r = iq.snr_difference(arr, arr2, sig, valid)
            self.place(s, k, arr, px, [sig], "snr")
            rows = {"SNR": _fmt(r["snr"], 2), "신호 평균 (영상 1)": _fmt(r["signal"]),
                    "차영상 SD": _fmt(r["diff_sd"]), "잡음 σ = SD/√2": _fmt(r["noise"])}
            note = "NEMA MS 1 두 영상 방법"
        _last(self.ctx)["snr"] = r
        self.ctx.results("SNR", rows, note=note)
        return r


# ═══ 3. CNR ═══

class CNRTool(IQTool):
    title = "CNR (Contrast-to-Noise Ratio)"
    help = ("현재 영상에 그린 마지막 ROI 두 개로 CNR = |m1 − m2| / √((σ1² + σ2²)/2). "
            "배경 잡음 기준 CNR = |m1 − m2| / 배경 SD도 함께 계산합니다.")

    def build(self):
        self.series_row()
        self.button("📊 CNR 계산 (마지막 ROI 2개)", self.run)

    def run(self):
        s, k, arr, px, valid = self.current()
        rois = self.user_rois()
        if len(rois) < 2:
            raise ValueError("현재 영상에 ROI(타원 E·자유곡선 8·사각형)를 두 개 그리세요.")
        a1, a2 = rois[-2], rois[-1]
        fit = self.fit(arr, valid)
        bgs = iq.background_rois(arr, fit["mask"], valid)
        bg_sd = None
        if bgs:
            bg_sd = float(np.sqrt(np.mean([iq.roi_stats(arr, b, valid)[1] ** 2 for b in bgs])))
        r = iq.cnr(arr, a1, a2, valid, bg_sd)
        rows = {"CNR (ROI SD 합성)": _fmt(r["cnr_pooled"], 2),
                "CNR (배경 잡음)": _fmt(r.get("cnr_background", float("nan")), 2),
                "ROI 1 평균 ± SD": f"{r['mean1']:.4g} ± {r['sd1']:.3g}",
                "ROI 2 평균 ± SD": f"{r['mean2']:.4g} ± {r['sd2']:.3g}", "대비 |m1 − m2|": _fmt(r["contrast"])}
        self.ctx.results("CNR", rows)
        return r


# ═══ 4. 균일도 ═══

class UniformityTool(IQTool):
    title = "Uniformity (균일도)"
    help = ("NEMA 5-ROI(중심 + 상하좌우), ACR PIU(큰 ROI 안 1 cm² 최대·최소), 균일도 지도(1 cm² 이동 평균 / "
            "중앙값 − 1, %)를 새 시리즈로 만듭니다.")

    def build(self):
        self.series_row()
        self.button("⬤ NEMA 5-ROI", self.nema)
        self.button("◎ ACR PIU", self.piu)
        self.button("🗺 균일도 지도 (컬러맵)", self.map)

    def nema(self, reuse=False):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        old = self.placed(s, k, "nema")
        anns = [old[n] for n in sorted(old)] if reuse and len(old) == 5 else iq.nema_five(fit)
        r = iq.uniformity_five(arr, anns, valid)
        self.place(s, k, arr, px, anns, "nema", "#9f7aea")
        rows = {f"{name} 평균": _fmt(v) for name, v in r["means"].items()}
        rows.update({"최대": _fmt(r["max"]), "최소": _fmt(r["min"]),
                     "비균일도 (max−min)/(max+min) %": _fmt(r["non_uniformity"], 2),
                     "균일도 %": _fmt(r["uniformity"], 2)})
        _last(self.ctx)["nema"] = r
        self.ctx.results("균일도 — NEMA 5-ROI", rows)
        return r

    def piu(self):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        r = iq.piu(arr, fit, px, valid)
        self.place(s, k, arr, px, r["rois"], "piu", "#50ff78")
        _last(self.ctx)["piu"] = r
        self.ctx.results("균일도 — ACR PIU", {"PIU %": _fmt(r["piu"], 2), "최대 1 cm²": _fmt(r["max"]),
                                             "최소 1 cm²": _fmt(r["min"]), "큰 ROI 평균": _fmt(r["large_mean"]),
                                             "큰 ROI 면적 (mm²)": _fmt(r["large_area_mm2"], 0)},
                         note="PIU = 100 × (1 − (max − min)/(max + min))")
        return r

    def map(self):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        m = iq.uniformity_map(arr, fit, px, valid)
        series = self.ctx.make_series(m[None], [(s, k)], "Uniformity map (%)", s, window=(0, 30))
        self.ctx.open_series(series, colormap="Jet", window=(0, 30))
        inside = m[fit["mask"]]
        self.ctx.results("균일도 지도", {"최대 편차 (+%)": _fmt(float(inside.max()), 2),
                                        "최소 편차 (−%)": _fmt(float(inside.min()), 2),
                                        "SD (%)": _fmt(float(inside.std()), 2),
                                        "새 시리즈": series.description},
                         note="0 = 물체 중앙값. 표시 범위 −15 ~ +15 %")


# ═══ 5. 고스팅 ═══

class GhostingTool(IQTool):
    title = "Ghosting (고스팅)"
    help = ("ACR PSG = |(위 + 아래) − (좌 + 우)| / (2 × 팬텀 평균) × 100. 팬텀 밖 네 방향 타원 ROI "
            "(10 cm², 4:1)를 글자 오버레이를 피해 자동 배치. 고스팅 비율 지도(팬텀 평균 대비 %) 생성.")

    def build(self):
        self.series_row()
        self.button("👻 PSG 계산 (ROI 자동 배치)", self.run)
        self.button("↻ 옮긴 ROI로 다시 계산", lambda: self.run(reuse=True))
        self.button("🗺 고스팅 비율 지도", self.map)

    def run(self, reuse=False):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        old = self.placed(s, k, "ghost")
        if reuse and len(old) >= 5:
            big = iq.roi_stats(arr, old["Ghost large"], valid)[0]
            means = {n.split()[-1]: iq.roi_stats(arr, a, valid)[0] for n, a in old.items() if n != "Ghost large"}
            psg = abs((means["top"] + means["bottom"]) - (means["left"] + means["right"])) / (2 * big) * 100
            r = {"psg": psg, "large": big, "means": means}
        else:
            r = iq.ghosting(arr, fit, px, valid)
            self.place(s, k, arr, px, r["rois"], "ghost", "#ffd24a")
        rows = {"PSG (%)": _fmt(r["psg"], 3), "팬텀 평균": _fmt(r["large"])}
        rows.update({f"{k_} 평균": _fmt(v) for k_, v in r["means"].items()})
        _last(self.ctx)["ghost"] = r
        self.ctx.results("고스팅 (PSG)", rows, note="ACR 기준 ≤ 2.5 %")
        return r

    def map(self):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        m = iq.ghost_map(arr, fit, valid)
        series = self.ctx.make_series(m[None], [(s, k)], "Ghosting ratio map (%)", s, window=(2.5, 5))
        self.ctx.open_series(series, colormap="Hot", window=(2.5, 5))
        self.ctx.results("고스팅 비율 지도", {"최대 (%)": _fmt(float(m.max()), 3),
                                          "99 백분위 (%)": _fmt(float(np.percentile(m[m > 0], 99)) if (m > 0).any() else 0.0, 3),
                                          "새 시리즈": series.description}, note="표시 범위 0 ~ 5 %")


# ═══ 6. 기하 왜곡 ═══

class DistortionTool(IQTool):
    title = "Geometric Distortion (기하학적 왜곡)"
    help = ("격자 팬텀 영상에서 격자 칸 중심을 자동으로 찾아 이상적인 격자(회전·이동 맞춤)와의 차이를 계산합니다. "
            "공칭 간격을 넣으면 크기 오차도 왜곡에 포함됩니다 (0 = 측정 간격). 변위는 화살표로 과장해서 표시.")

    def build(self):
        self.series_row()
        self.nominal = spin(0, 0, 100, 2, suffix=" mm")
        self.form.addRow("공칭 격자 간격:", self.nominal)
        self.polarity = QComboBox()
        self.polarity.addItems(["자동", "밝은 칸", "어두운 칸"])
        self.form.addRow("격자 칸:", self.polarity)
        self.scale = spin(10, 1, 100, 0, suffix=" ×")
        self.form.addRow("화살표 과장:", self.scale)
        self.button("▦ 격자점 찾기 · 왜곡 계산", self.run)

    def run(self):
        s, k, arr, px, valid = self.current()
        region = iq.grid_region(arr)
        if valid is not None:
            region &= valid
        pol = {0: "auto", 1: "bright", 2: "dark"}[self.polarity.currentIndex()]
        pts, used = iq.grid_points(arr, region, pol)
        r = iq.lattice_fit(pts, px, self.nominal.value() or None)
        sc = self.scale.value()
        items = []
        for (x, y), (ix, iy), mag in zip(r["points"], r["ideal"], r["mag_mm"]):
            color = "#50ff78" if mag < 1 else "#ffd24a" if mag < 2 else "#ff5050"
            items.append(("pt", ix, iy, "#888888"))
            items.append(("arrow", ix, iy, ix + (x - ix) * sc, iy + (y - iy) * sc, color))
        self.markers(s, k, items)
        _last(self.ctx)["distortion"] = r
        rows = {"격자점 수": r["n"], "칸": "밝음" if used == "bright" else "어두움",
                "측정 간격 (mm)": _fmt(r["spacing_mm"]), "회전 (°)": _fmt(r["rotation_deg"], 2),
                "평균 변위 (mm)": _fmt(r["mean_mm"]), "RMS (mm)": _fmt(r["rms_mm"]),
                "최대 변위 (mm)": _fmt(r["max_mm"]), "최대 왜곡 (% of 거리)": _fmt(r["max_pct"], 2)}

        def plot(fig):
            ax = fig.add_subplot(111)
            ideal = r["ideal"]
            d = r["disp_mm"]
            q = ax.quiver(ideal[:, 0], ideal[:, 1], d[:, 0], -d[:, 1], r["mag_mm"], cmap="jet",
                          angles="xy")
            fig.colorbar(q, ax=ax, label="mm")
            ax.invert_yaxis()
            ax.set_aspect("equal")
            ax.set_title("displacement vector field")
        self.ctx.results("기하학적 왜곡", rows, plot, note="초록 < 1 mm, 노랑 < 2 mm, 빨강 ≥ 2 mm")
        return r


# ═══ 7. NPS ═══

class NPSTool(IQTool):
    title = "NPS (Noise Power Spectrum)"
    help = ("균일 영역을 겹치는 정사각 조각으로 나누어 2차 추세를 빼고 2D FFT 평균 → 2D NPS와 방사 평균 1D NPS. "
            "영역: 마지막 ROI (없으면 물체 가운데 60%). 두 번째 영상을 고르면 차분으로 구조를 없앱니다.")

    def build(self):
        self.series_row()
        self.size = QComboBox()
        self.size.addItems(["32", "64", "128"])
        self.size.setCurrentIndex(1)
        self.form.addRow("조각 크기 (px):", self.size)
        from .tools_acr import _AutoPicker
        self.second = _AutoPicker(self.ctx, allow_none=True, none_text="(사용 안 함)")
        self._pickers.append(self.second)
        self.second_k = ispin(2, 1, 9999)
        row = QHBoxLayout()
        row.addWidget(self.second, 1)
        row.addWidget(QLabel("영상"))
        row.addWidget(self.second_k)
        self.form.addRow("두 번째 영상 (선택):", row)
        self.button("〰 NPS 계산", self.run)

    def run(self):
        from scipy import ndimage as ndi

        from ..roi_tools import mask_of
        s, k, arr, px, valid = self.current()
        size = int(self.size.currentText())
        rois = self.user_rois()
        note = ""
        pl = iq.patches(arr, mask_of(rois[-1], arr.shape), size, valid=valid) if rois else []
        if rois and not pl:
            note = "마지막 ROI가 조각보다 작아 물체 가운데 60% 영역을 썼습니다. "
        if not pl:
            fit = self.fit(arr, valid)
            yy, xx = np.indices(arr.shape)
            region = (np.hypot(xx - fit["cx"], yy - fit["cy"]) < 0.6 * fit["r"]) & fit["mask"]
            region = ndi.binary_erosion(region, iterations=1)
            pl = iq.patches(arr, region, size, valid=valid)
        if len(pl) < 1:
            raise ValueError(f"영역 안에 {size}×{size} 조각이 들어가지 않습니다 (조각을 작게 하거나 ROI를 크게).")
        arr2 = self.second_image(self.second, self.second_k)
        second = None
        if arr2 is not None:
            if arr2.shape != arr.shape:
                raise ValueError("두 번째 영상 크기가 다릅니다.")
            second = [arr2[y:y + size, x:x + size] for (x, y), _p in pl]
        r = iq.nps([p for _xy, p in pl], px, second)
        _last(self.ctx)["nps"] = r
        self.place(s, k, arr, px, [{"type": "rect", "pts": [(x, y), (x + size, y + size)],
                                    "name": f"NPS patch {i + 1}"} for i, ((x, y), _p) in enumerate(pl[:40])],
                   "nps", "#78dcff")
        rows = {"판별": r["kind"], "조각 수": r["n_patches"], "평균 신호": _fmt(r["mean_signal"]),
                "잡음 분산": _fmt(r["noise_var"]), "고역/저역 NPS 비": _fmt(r["hf_lf_ratio"], 3),
                "Nyquist (lp/mm)": _fmt(r["nyquist"]),
                "스파이크 주파수 (fx, fy /mm)": ", ".join(f"({a}, {b})" for a, b in r["spikes"][:6]) or "없음"}

        def plot(fig):
            ax = fig.add_subplot(121)
            ext = [r["fx"][0], r["fx"][-1], r["fy"][-1], r["fy"][0]]
            ax.imshow(np.log10(r["nps2d"] + 1e-12), extent=ext, cmap="viridis")
            ax.set_title("2D NPS (log)")
            ax.set_xlabel("fx (/mm)")
            ax2 = fig.add_subplot(122)
            ax2.plot(r["f"], r["nps"], color="#4a9eff")
            ax2.set_xlabel("f (/mm)")
            ax2.set_title("radial NPS")
        self.ctx.results("NPS", rows, plot, note=note + "NPS 단위: 신호² · mm²")
        return r


# ═══ 8. NEQ ═══

class NEQTool(IQTool):
    title = "NEQ (Noise Equivalent Quanta)"
    help = ("NEQ(f) = S² · MTF(f)² / NPS(f). MTF 도구와 NPS 도구를 먼저 실행하세요 (마지막 결과 사용). "
            "입사 양자 q(/mm²)를 넣으면 DQE = NEQ / q.")

    def build(self):
        self.q = spin(0, 0, 1e9, 0, suffix=" /mm²")
        self.form.addRow("입사 양자 q (선택):", self.q)
        self.button("⚛ NEQ 계산", self.run)

    def run(self):
        last = _last(self.ctx)
        if "mtf" not in last or "nps" not in last:
            raise ValueError("먼저 MTF와 NPS를 계산하세요.")
        r = iq.neq(last["mtf"], last["nps"], self.q.value() or None)
        f, n = r["f"], r["neq"]
        rows = {"평균 신호 S": _fmt(r["signal"])}
        for target in (0.1, 0.25, 0.5, 1.0):
            if f[0] <= target <= f[-1]:
                rows[f"NEQ @ {target} lp/mm"] = _fmt(float(np.interp(target, f, n)), 4)
        if "dqe" in r:
            rows["DQE @ 최저 주파수"] = _fmt(float(r["dqe"][0]), 4)

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.plot(f, n, color="#4a9eff", label="NEQ")
            ax.set_xlabel("f (lp/mm)")
            ax.set_ylabel("NEQ (/mm²)")
            if "dqe" in r:
                ax2 = ax.twinx()
                ax2.plot(f, r["dqe"], color="#fc4", label="DQE")
                ax2.set_ylabel("DQE")
        self.ctx.results("NEQ", rows, plot, note="MTF와 NPS는 같은 장비·조건의 영상이어야 합니다.")
        return r


# ═══ 9. 공간 분해능 ═══

class ResolutionTool(IQTool):
    title = "Resolution (공간분해능)"
    help = ("점 광원: 가장 밝은 점(ROI가 있으면 그 안)에서 가로·세로 FWHM. 선 광원: 선을 가로지르는 직선의 "
            "프로파일 FWHM. 바 패턴: 막대 묶음을 가로지르는 직선 → 묶음별 lp/mm·변조도, 분해되는 최대 lp/mm.")

    def build(self):
        self.series_row()
        self.min_mod = spin(10, 1, 90, 0, suffix=" %")
        self.form.addRow("분해 기준 변조도:", self.min_mod)
        self.button("• 점 광원 FWHM", self.point)
        self.button("— 선 광원 FWHM (마지막 직선)", self.line)
        self.button("▥ 바 패턴 (마지막 직선)", self.bars)

    def point(self):
        s, k, arr, px, valid = self.current()
        rois = self.user_rois()
        center = None
        if rois:
            from ..roi_tools import shape_params
            cx, cy, _w, _h = shape_params(rois[-1], px)
            center = (cx - 0.5, cy - 0.5)
        r = iq.point_source(arr, px, center)
        self.markers(s, k, [("pt", r["x"], r["y"], "#ff5050")])
        rows = {"FWHM 가로 (mm)": _fmt(r["horizontal"]["fwhm"]), "FWHM 세로 (mm)": _fmt(r["vertical"]["fwhm"]),
                "FWTM 가로 (mm)": _fmt(r["horizontal"]["fwtm"]), "FWTM 세로 (mm)": _fmt(r["vertical"]["fwtm"]),
                "위치 (x, y)": f"{r['x']:.1f}, {r['y']:.1f}"}

        def plot(fig):
            ax = fig.add_subplot(111)
            for name, c in (("horizontal", "#4a9eff"), ("vertical", "#fc4")):
                t, v = r[name + "_profile"]
                ax.plot(t - t[len(t) // 2], v, color=c, label=name)
            ax.set_xlabel("mm")
            ax.legend(fontsize=7)
        self.ctx.results("점 광원 FWHM", rows, plot)
        return r

    def _line_profile(self):
        s, k, arr, px, valid = self.current()
        if self.ctx.series() is not s:
            raise ValueError("시리즈를 화면에 띄우고 그 위에 선을 그리세요.")
        p0, p1 = self.ctx.last_lines(1)[0]
        t, v, u = iq.profile_along(arr, p0, p1)
        mm = math.hypot(u[0] * px[1], u[1] * px[0])
        return t * mm, v

    def line(self):
        t, v = self._line_profile()
        r = iq.fwhm(t, v)

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.plot(t, v, color="#4a9eff")
            if r["left"] is not None:
                half = r["base"] + (r["peak"] - r["base"]) / 2
                ax.plot([r["left"], r["right"]], [half, half], color="#f66")
            ax.set_xlabel("mm")
        self.ctx.results("선 광원 FWHM", {"FWHM (mm)": _fmt(r["fwhm"]), "FWTM (mm)": _fmt(r["fwtm"]),
                                        "봉우리": _fmt(r["peak"]), "기준선": _fmt(r["base"])}, plot)
        return r

    def bars(self):
        t, v = self._line_profile()
        r = iq.bar_pattern(t, v, self.min_mod.value() / 100)
        rows = {"header": ["묶음", "lp/mm", "주기 (mm)", "변조도 (%)", "막대 수", "분해"],
                "rows": [[i + 1, _fmt(g["lp_mm"]), _fmt(g["period_mm"]), _fmt(g["modulation"] * 100, 1),
                          g["bars"], "예" if g["modulation"] * 100 >= self.min_mod.value() else "아니오"]
                         for i, g in enumerate(r["groups"])]}

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.plot(t, v, color="#4a9eff")
            for g in r["groups"]:
                ax.axvspan(g["start_mm"], g["end_mm"], color="#fc4", alpha=0.15)
            ax.set_xlabel("mm")
        self.ctx.results(f"바 패턴 — 분해 한계 {r['limit_lp_mm']:.2f} lp/mm", rows, plot)
        return r


# ═══ 10. 아티팩트 ═══

class ArtifactTool(IQTool):
    title = "Artifact 검출 (링 · 지퍼 · 밴딩)"
    help = ("링: 물체 중심 기준 극좌표 변환 → 반지름 프로파일에서 모든 각도에 걸친 줄무늬. "
            "지퍼: 영상 전체를 가로지르는 한 열/행의 튀는 값 (주기 성분은 주파수 분석). "
            "밴딩: 물체 안 행·열 평균의 주기 성분 진폭 (%).")

    def build(self):
        self.series_row()
        self.k = spin(4, 2, 20, 1, suffix=" σ")
        self.form.addRow("링 검출 기준:", self.k)
        self.button("◎ 링 아티팩트", self.rings)
        self.button("┃ 지퍼 아티팩트", self.zipper)
        self.button("≡ 밴딩 아티팩트", self.banding)

    def rings(self):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        r = iq.ring_artifacts(arr, (fit["cx"], fit["cy"]), 0.95 * fit["r"], valid, self.k.value())
        pxm = (px[0] + px[1]) / 2
        self.markers(s, k, [("circle", fit["cx"] + 0.5, fit["cy"] + 0.5, ring["r_px"], "#ff5050")
                            for ring in r["rings"][:10]])
        rows = {"header": ["반지름 (mm)", "진폭", "진폭 (%)", "각도 일치율"],
                "rows": [[_fmt(x["r_px"] * pxm, 1), _fmt(x["amp"]), _fmt(x["amp_pct"], 2),
                          _fmt(x["consistency"], 2)] for x in r["rings"]]}

        def plot(fig):
            ax = fig.add_subplot(211)
            ax.imshow(r["polar"], aspect="auto", cmap="gray",
                      extent=[r["r"][0] * pxm, r["r"][-1] * pxm, 360, 0])
            ax.set_ylabel("angle (°)")
            ax2 = fig.add_subplot(212)
            ax2.plot(r["r"] * pxm, r["resid"], color="#4a9eff")
            ax2.axhline(self.k.value() * r["sigma"], color="#f66", ls="--", lw=1)
            ax2.axhline(-self.k.value() * r["sigma"], color="#f66", ls="--", lw=1)
            ax2.set_xlabel("radius (mm)")
        self.ctx.results(f"링 아티팩트 — {len(r['rings'])}개", rows, plot,
                         note="없으면 표가 비어 있습니다. 극좌표 영상에서 세로 줄 = 링")
        return r

    def zipper(self):
        s, k, arr, px, valid = self.current()
        r = iq.zipper(arr, valid)
        items, table = [], []
        h, w = arr.shape
        for name, res in r.items():
            for line in res["lines"]:
                if name.startswith("vertical"):
                    items.append(("line", line["pos"] + 0.5, 0, line["pos"] + 0.5, h, "#ff5050"))
                else:
                    items.append(("line", 0, line["pos"] + 0.5, w, line["pos"] + 0.5, "#ff5050"))
                table.append([name, line["pos"], _fmt(line["z"], 1), _fmt(line["amp"]), line["width"]])
            if res["periodic"]:
                table.append([name + " 주기 성분", "-", "-", f"주기 {res['periodic']['period_px']:.1f} px", "-"])
        self.markers(s, k, items)

        def plot(fig):
            for i, (name, res) in enumerate(r.items()):
                ax = fig.add_subplot(2, 1, i + 1)
                ax.plot(res["z"], color="#4a9eff")
                ax.axhline(8, color="#f66", ls="--", lw=1)
                ax.axhline(-8, color="#f66", ls="--", lw=1)
                ax.set_ylabel("z")
                ax.set_title(name, fontsize=8)
        self.ctx.results(f"지퍼 아티팩트 — {sum(len(v['lines']) for v in r.values())}줄",
                         {"header": ["방향", "위치 (px)", "z", "진폭", "폭 (px)"], "rows": table}, plot)
        return r

    def banding(self):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        r = iq.banding(arr, fit, px, valid)
        rows = {"header": ["방향", "SD (%)", "최대−최소 (%)", "주 성분 진폭 (%)", "주기 (mm)"],
                "rows": [[name, _fmt(v["std_pct"], 2), _fmt(v["p2p_pct"], 2), _fmt(v["peak_amp_pct"], 2),
                          _fmt(v["period_mm"], 1)] for name, v in r.items()]}

        def plot(fig):
            ax = fig.add_subplot(111)
            for name, v in r.items():
                ax.plot(v["profile"] * 100, label=name)
            ax.set_ylabel("%")
            ax.legend(fontsize=7)
        self.ctx.results("밴딩 아티팩트", rows, plot, note="주 성분 진폭이 1 % 이상이면 눈에 띄는 밴딩")
        return r


# ═══ 자동 평가 ═══

def history_path():
    override = os.environ.get("DABBAVIEW_IQ_HISTORY")
    if override:
        return override
    from ..library import library_path
    return os.path.join(os.path.dirname(library_path()), "iq_history.json")


def load_history(path=None):
    try:
        with open(path or history_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(record, path=None):
    path = path or history_path()
    items = [r for r in load_history(path) if (r.get("date"), r.get("name")) != (record["date"], record["name"])]
    items.append(record)
    items.sort(key=lambda r: (r.get("date", ""), r.get("name", "")))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    return items


AUTO_METRICS = [("snr", "SNR"), ("cnr", "CNR (물체/배경)"), ("nema_uniformity", "NEMA 균일도 (%)"),
                ("piu", "PIU (%)"), ("psg", "고스팅 PSG (%)"), ("mtf50", "MTF50 (lp/mm)"),
                ("mtf10", "MTF10 (lp/mm)"), ("noise", "잡음 σ")]


class AutoIQTool(IQTool):
    title = "Auto IQ Assessment (자동 화질 평가)"
    help = ("팬텀 영상 한 장으로 SNR · CNR · 균일도(NEMA·PIU) · 고스팅 · MTF(팬텀 가장자리)를 한 번에 "
            "측정하고, 이전 결과와 비교합니다. PDF 보고서와 날짜별 추세 그래프를 만들 수 있습니다.")

    def build(self):
        self.series_row()
        self.name_hint = QLabel("")
        self.name_hint.setStyleSheet("color: #999;")
        self.form.addRow(self.name_hint)
        self.button("▶ Auto IQ Assessment", self.run)
        self.button("📄 PDF 보고서…", self.export_pdf)
        self.button("💾 추세 기록에 저장", self.save)
        self.button("📈 추세 (날짜별 그래프)…", self.trend)
        self.result = None

    def run(self):
        s, k, arr, px, valid = self.current()
        fit = self.fit(arr, valid)
        out, anns = {}, []
        sig = iq.signal_roi(fit)
        sig["name"] = "Auto signal"
        bgs = iq.background_rois(arr, fit["mask"], valid)
        rayleigh = s.modality == "MR" or getattr(s, "source_format", "") == "image"
        errors = {}
        try:
            snr = iq.snr_single(arr, sig, bgs, valid, rayleigh)
            out.update(snr=snr["snr"], noise=snr["noise"], signal=snr["signal"],
                       cnr=abs(snr["signal"] - snr["bg_mean"]) / snr["noise"] if snr["noise"] else float("nan"))
            anns += [sig] + bgs
        except ValueError as e:
            errors["SNR/CNR"] = str(e)
        try:
            u5 = iq.uniformity_five(arr, iq.nema_five(fit), valid)
            out["nema_uniformity"] = u5["uniformity"]
        except ValueError as e:
            errors["NEMA"] = str(e)
        try:
            p = iq.piu(arr, fit, px, valid)
            out["piu"] = p["piu"]
            anns += p["rois"][1:]
        except ValueError as e:
            errors["PIU"] = str(e)
        try:
            g = iq.ghosting(arr, fit, px, valid, sig)
            out["psg"] = g["psg"]
            anns += g["rois"][1:]
        except ValueError as e:
            errors["Ghosting"] = str(e)
        mtfs = []
        for side in ("right", "left", "bottom"):
            try:
                a, b = iq.auto_edge_line(fit, px, side)
                mtfs.append(iq.mtf_edge(arr, a, b, px, width=min(40, 0.3 * fit["r"])))
            except ValueError:
                continue
        good = [m for m in mtfs if math.isfinite(m["mtf50"])]
        if good:
            out["mtf50"] = float(np.median([m["mtf50"] for m in good]))
            out["mtf10"] = float(np.nanmedian([m["mtf10"] for m in good]))
            _last(self.ctx)["mtf"] = good[0]
            anns.append({"type": "roi", "pts": good[0]["rect"], "name": "Auto MTF"})
        else:
            errors["MTF"] = "가장자리 에지를 찾지 못함"
        self.place(s, k, arr, px, anns, "auto", "#50c8ff")
        name = f"{s.description or 'series'}"
        date = _date_of(s)
        prev = [r for r in load_history() if r.get("name") == name and r.get("date") != date]
        prev = prev[-1] if prev else None
        self.result = {"date": date, "name": name, "values": out, "errors": errors,
                       "series": s, "k": k, "px": px, "mtf": good[0] if good else None,
                       "saved": datetime.datetime.now().isoformat(timespec="seconds")}
        rows = []
        for key, label in AUTO_METRICS:
            v = out.get(key)
            p_ = (prev or {}).get("values", {}).get(key)
            delta = (v - p_) if v is not None and p_ is not None else None
            rows.append([label, _fmt(v, 3) if v is not None else "-", _fmt(p_, 3) if p_ is not None else "-",
                         (f"{delta:+.3g}" if delta is not None else "-")])
        for key, msg in errors.items():
            rows.append([key, "실패", "", msg])
        self.ctx.results(f"Auto IQ — {name} ({date})",
                         {"header": ["항목", "현재", f"이전 ({prev['date']})" if prev else "이전", "변화"],
                          "rows": rows},
                         (lambda fig: _mtf_plot(fig, good[0])) if good else None,
                         note=("MR 크기 영상: Rayleigh 보정" if rayleigh else "") +
                         ("  ⚠ 배경 SD < 1 (8비트 양자화) → SNR 과대" if out.get("noise", 9) * (RAY if rayleigh else 1) < 1
                          and arr.max() <= 255 else "") +
                         ("  · 화면 캡처: 픽셀 크기 FOV 가정" if valid is not None else ""))
        return self.result

    def save(self):
        if not self.result:
            raise ValueError("먼저 Auto IQ Assessment를 실행하세요.")
        rec = {k: self.result[k] for k in ("date", "name", "values", "saved")}
        items = save_history(rec)
        self.ctx.status(f"IQ 기록 저장 — 전체 {len(items)}개")

    def export_pdf(self):
        if not self.result:
            raise ValueError("먼저 Auto IQ Assessment를 실행하세요.")
        path, _ = QFileDialog.getSaveFileName(self, "IQ 보고서", os.path.expanduser(
            f"~/Documents/IQ_{self.result['date']}.pdf"), "PDF (*.pdf)")
        if not path:
            return None
        write_pdf(path, self.result, load_history(), self.ctx.main._annotation_store)
        self.ctx.status(f"IQ 보고서 저장: {path}")
        self.last_report = path
        return path

    def trend(self):
        IQTrendDialog(self).exec_()


def _date_of(series):
    raw = str(getattr(series.slices[0], "StudyDate", "") or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    from .tools_acr import date_from_path
    return date_from_path(getattr(series, "source_path", "") or "") or datetime.date.today().isoformat()


def _mtf_plot(fig, r):
    ax = fig.add_subplot(111)
    ax.plot(r["f"], r["mtf"], color="#4a9eff")
    ax.axvline(r["nyquist"], color="#aaa", ls="--", lw=1)
    for level, key in ((0.5, "mtf50"), (0.1, "mtf10")):
        if math.isfinite(r[key]):
            ax.plot([r[key]], [level], "o", color="#fc4")
    ax.set_xlim(0, min(r["f"][-1], 1.5 * r["nyquist"]))
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("lp/mm")
    ax.set_ylabel("MTF")


def write_pdf(path, result, history, store=None):
    """IQ 보고서: 값 표 · MTF 그래프 · 추세 · ROI 그린 영상"""
    import tempfile
    from xml.sax.saxutils import escape

    from matplotlib.figure import Figure
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from ..annotations import image_key
    from ..library_export import _pdf_font
    from .tools_acr import render_snapshot
    font = _pdf_font()
    base = ParagraphStyle("b", fontName=font, fontSize=9, leading=12)
    h1 = ParagraphStyle("h", parent=base, fontSize=15, leading=20, alignment=1, spaceAfter=6)
    with tempfile.TemporaryDirectory() as tmp:
        story = [Paragraph("영상 화질 평가 (Image Quality Assessment)", h1),
                 Paragraph(escape(f"시리즈: {result['name']}   ·   검사일: {result['date']}   ·   "
                                  f"작성: {result['saved']}"), base), Spacer(1, 3 * mm)]
        prev = [r for r in history if r.get("name") == result["name"] and r.get("date") != result["date"]]
        prev = prev[-1] if prev else None
        data = [["항목", "현재", f"이전 ({prev['date']})" if prev else "이전", "변화"]]
        for key, label in AUTO_METRICS:
            v = result["values"].get(key)
            p = (prev or {}).get("values", {}).get(key)
            data.append([label, f"{v:.4g}" if v is not None else "-", f"{p:.4g}" if p is not None else "-",
                         f"{v - p:+.3g}" if v is not None and p is not None else "-"])
        t = Table([[Paragraph(escape(str(c)), base) for c in row] for row in data],
                  colWidths=[55 * mm, 35 * mm, 45 * mm, 35 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F0"))]))
        story += [t, Spacer(1, 4 * mm)]
        imgs = []
        if result.get("mtf"):
            fig = Figure(figsize=(4, 3), dpi=120)
            _mtf_plot(fig, result["mtf"])
            fig.tight_layout()
            p = os.path.join(tmp, "mtf.png")
            fig.savefig(p)
            imgs.append(Image(p, width=80 * mm, height=60 * mm))
        series_hist = [r for r in history if r.get("name") == result["name"]]
        if len(series_hist) >= 2:
            fig = Figure(figsize=(4, 3), dpi=120)
            ax = fig.add_subplot(111)
            for key, label in (("snr", "SNR"), ("piu", "PIU")):
                pts = [(r["date"], r["values"].get(key)) for r in series_hist if r["values"].get(key) is not None]
                if pts:
                    ax.plot([d for d, _ in pts], [v for _, v in pts], "o-", label=label)
            ax.legend(fontsize=7)
            ax.set_title("trend")
            fig.autofmt_xdate()
            fig.tight_layout()
            p = os.path.join(tmp, "trend.png")
            fig.savefig(p)
            imgs.append(Image(p, width=80 * mm, height=60 * mm))
        s, k = result["series"], result["k"]
        if store is not None:
            snap = os.path.join(tmp, "snap.png")
            arr = np.asarray(s.get_pixel_array(k), float)
            render_snapshot(arr.mean(-1) if arr.ndim == 3 else arr, store.items(image_key(s, k)), [], snap)
            imgs.append(Image(snap, width=60 * mm, height=60 * mm))
        if imgs:
            rows = [imgs[i:i + 2] for i in range(0, len(imgs), 2)]
            story.append(Table(rows))
        if result.get("errors"):
            story.append(Paragraph(escape("측정 실패: " + "; ".join(f"{k}: {v}" for k, v in result["errors"].items())), base))
        story.append(Paragraph(escape("DabbaView 자동 측정 (연구·교육용). SNR은 NEMA 단일 영상 방법, "
                                      "PIU·PSG는 ACR 방식, MTF는 팬텀 가장자리 에지 방법."), base))
        SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                          bottomMargin=14 * mm, title="Image Quality Assessment").build(story)
    return path


class IQTrendDialog(QDialog):
    def __init__(self, parent=None, path=None):
        super().__init__(parent)
        self.setWindowTitle("IQ 추세")
        self.resize(780, 560)
        self.history = load_history(path)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.metric = QComboBox()
        for key, label in AUTO_METRICS:
            self.metric.addItem(label, key)
        self.metric.currentIndexChanged.connect(self.redraw)
        top.addWidget(QLabel("항목:"))
        top.addWidget(self.metric, 1)
        layout.addLayout(top)
        from ..analysis import configure_matplotlib
        configure_matplotlib()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(7, 3.4), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout.addWidget(self.canvas, 3)
        table = QTableWidget(len(self.history), 2 + len(AUTO_METRICS))
        table.setHorizontalHeaderLabels(["검사일", "시리즈"] + [l for _k, l in AUTO_METRICS])
        for r, rec in enumerate(self.history):
            table.setItem(r, 0, QTableWidgetItem(rec.get("date", "")))
            table.setItem(r, 1, QTableWidgetItem(rec.get("name", "")))
            for c, (key, _l) in enumerate(AUTO_METRICS, start=2):
                v = rec.get("values", {}).get(key)
                table.setItem(r, c, QTableWidgetItem("-" if v is None else f"{v:.4g}"))
        table.resizeColumnsToContents()
        layout.addWidget(table, 2)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
        self.redraw()

    def redraw(self):
        key = self.metric.currentData()
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        names = sorted({r.get("name", "") for r in self.history})
        for name in names:
            pts = sorted((r["date"], r["values"][key]) for r in self.history
                         if r.get("name") == name and r.get("values", {}).get(key) is not None)
            if pts:
                dates = [datetime.date.fromisoformat(d) for d, _ in pts]
                ax.plot(dates, [v for _, v in pts], "o-", label=name)
        ax.set_title(self.metric.currentText())
        if names:
            ax.legend(fontsize=7)
        self.figure.autofmt_xdate()
        self.figure.tight_layout()
        self.canvas.draw_idle()


# ═══ 뷰포트 표시 (격자점·변위 화살표·링·지퍼 선) ═══

def paint_markers(vp, painter):
    from PyQt5.QtCore import QPointF

    from ..annotations import image_key
    if vp.series is None:
        return
    items = _MARKERS.get(image_key(vp.series, vp.current_slice))
    if not items:
        return
    for m in items:
        kind, color = m[0], m[-1]
        painter.setPen(QPen(QColor(color), 1.3))
        painter.setBrush(Qt.NoBrush)
        if kind == "pt":
            c = vp._image_to_screen_f((m[1], m[2]))
            painter.drawEllipse(c, 2.0, 2.0)
        elif kind in ("arrow", "line"):
            a = vp._image_to_screen_f((m[1], m[2]))
            b = vp._image_to_screen_f((m[3], m[4]))
            painter.drawLine(a, b)
            if kind == "arrow":
                d = b - a
                length = math.hypot(d.x(), d.y())
                if length > 3:
                    ux, uy = d.x() / length, d.y() / length
                    for sgn in (1, -1):
                        painter.drawLine(b, b - QPointF(ux * 5 - sgn * uy * 3, uy * 5 + sgn * ux * 3))
        elif kind == "circle":
            c = vp._image_to_screen_f((m[1], m[2]))
            e = vp._image_to_screen_f((m[1] + m[3], m[2]))
            r = math.hypot(e.x() - c.x(), e.y() - c.y())
            painter.drawEllipse(c, r, r)


TOOLS = [("iq_mtf", MTFTool), ("iq_snr", SNRTool), ("iq_cnr", CNRTool), ("iq_uniformity", UniformityTool),
         ("iq_ghosting", GhostingTool), ("iq_distortion", DistortionTool), ("iq_nps", NPSTool),
         ("iq_neq", NEQTool), ("iq_resolution", ResolutionTool), ("iq_artifact", ArtifactTool),
         ("iq_auto", AutoIQTool)]

__all__ = ["TOOLS", "paint_markers", "QMessageBox"]
