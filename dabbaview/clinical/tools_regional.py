# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Analysis ▸ Neuro / Oncology / Lung / MSK / Vascular 도구 페이지"""
import datetime

import numpy as np
from PyQt5.QtWidgets import QCheckBox, QComboBox, QLabel
from scipy import ndimage

from . import lesion as LS
from .panel import Tool, spin


def _adc_scale(adc):
    """ADC 단위 → ×10⁻⁶ mm²/s 로 바꾸는 배수 (mm²/s로 저장된 경우 1e6)"""
    med = float(np.median(adc[adc > 0])) if np.any(adc > 0) else 0
    return 1e6 if 0 < med < 0.02 else (1e3 if 0.02 <= med < 20 else 1.0)


def _brain_mask(volume):
    from skimage.filters import threshold_otsu
    vals = volume[volume > 0]
    if vals.size == 0:
        return volume > 0
    mask = volume > threshold_otsu(vals) * 0.5
    return np.stack([ndimage.binary_fill_holes(ndimage.binary_opening(s, iterations=2))
                     for s in mask])


def _resample_to(fixed_series, fixed_vol, moving_vol):
    """moving 볼륨을 fixed 시리즈 격자로 (환자 좌표 기준)"""
    from ..analysis.fusion import FusionLayer
    if moving_vol.array.shape == fixed_vol.array.shape and np.allclose(
            moving_vol.affine_lps, fixed_vol.affine_lps, atol=1e-3):
        return moving_vol.array
    layer = FusionLayer(moving_vol, (0, 1), None)
    out = np.stack([layer.sample_slice(fixed_vol.affine_lps, k, fixed_vol.array.shape[1:])
                    for k in range(fixed_vol.array.shape[0])])
    return np.nan_to_num(out, nan=0.0)


def _study_date(series):
    text = str(getattr(series.slices[0], "StudyDate", "") or "")
    try:
        return datetime.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, IndexError):
        return None


# ═══ Neuro ═══

class ColormapTool(Tool):
    title = "ADC / FA 컬러맵"
    help = "ADC·FA 맵에 컬러맵과 표준 표시 범위를 적용하고 컬러바를 표시합니다 (단위 자동 판별)."

    def build(self):
        self.series = self.series_picker()
        self.kind = QComboBox()
        self.kind.addItems(["ADC", "FA"])
        self.form.addRow("맵:", self.kind)
        self.cmap = QComboBox()
        self.cmap.addItems(["Jet", "Viridis", "Hot", "Magma", "Gray"])
        self.form.addRow("컬러맵:", self.cmap)
        self.button("🎨 적용", self.run)

    def run(self):
        s = self.series.series()
        vol = self.ctx.volume(s).array
        if self.kind.currentText() == "ADC":
            scale = _adc_scale(vol)
            window = (1500 / scale, 3000 / scale)
            unit = "×10⁻⁶ mm²/s" if scale == 1 else ("mm²/s" if scale == 1e6 else "×10⁻³ mm²/s")
        else:
            top = float(np.percentile(vol, 99.9))
            window = (0.5, 1.0) if top <= 1.5 else (500, 1000)
            unit = "FA" if top <= 1.5 else "FA×1000"
        self.ctx.open_series(s, colormap=self.cmap.currentText(), window=window)
        self.ctx.results(f"{self.kind.currentText()} 컬러맵",
                         {"단위(추정)": unit, "표시 범위": f"{window[0] - window[1] / 2:g} ~ "
                                                      f"{window[0] + window[1] / 2:g}"})


class MismatchTool(Tool):
    title = "DWI–ADC 미스매치 (뇌경색)"
    help = ("DWI(고 b)에서 밝고 ADC가 낮은 곳 = 진성 확산 제한(급성 경색 핵심부), "
            "DWI는 밝지만 ADC가 정상 = T2 shine-through. 결과는 AI 라벨로 표시됩니다.")

    def build(self):
        self.dwi = self.series_picker("DWI (b1000):")
        self.adc = self.series_picker("ADC:")
        self.adc_thr = spin(620, 100, 2000, 0, suffix=" ×10⁻⁶ mm²/s")
        self.form.addRow("ADC 기준 (미만):", self.adc_thr)
        self.dwi_k = spin(2.0, 0.5, 6, 1, suffix=" SD")
        self.form.addRow("DWI 기준 (뇌 평균 +):", self.dwi_k)
        self.min_ml = spin(0.1, 0, 10, 2, suffix=" mL")
        self.form.addRow("최소 병변 크기:", self.min_ml)
        self.button("🧠 계산", self.run)

    def run(self):
        dwi_s, adc_s = self.dwi.series(), self.adc.series()
        dwi_v, adc_v = self.ctx.volume(dwi_s), self.ctx.volume(adc_s)
        adc = _resample_to(dwi_s, dwi_v, adc_v) * _adc_scale(adc_v.array)
        dwi = dwi_v.array
        brain = _brain_mask(dwi)
        mu, sd = dwi[brain].mean(), dwi[brain].std()
        bright = brain & (dwi > mu + self.dwi_k.value() * sd)
        spacing = dwi_v.spacing
        vox_ml = float(np.prod(spacing)) / 1000.0
        bright = _drop_small(bright, self.min_ml.value() / vox_ml)
        restricted = bright & (adc > 0) & (adc < self.adc_thr.value())
        shine = bright & ~restricted & (adc >= self.adc_thr.value())
        self.ctx.show_mask(dwi_s, restricted, "Restricted diffusion")
        self.ctx.show_mask(dwi_s, shine, "T2 shine-through")
        self.ctx.results("DWI–ADC", {
            "DWI 고신호 (mL)": bright.sum() * vox_ml,
            "진성 확산 제한 (DWI↑ ADC↓, mL)": restricted.sum() * vox_ml,
            "T2 shine-through (mL)": shine.sum() * vox_ml,
            "제한 부위 평균 ADC (×10⁻⁶)": float(adc[restricted].mean()) if restricted.any() else 0,
            "DWI 기준값": mu + self.dwi_k.value() * sd},
            note="관류(PWI)–확산 미스매치와는 다릅니다.")


def _drop_small(mask, min_voxels):
    labels, n = ndimage.label(mask)
    if n == 0 or min_voxels <= 1:
        return mask
    sizes = ndimage.sum(mask, labels, range(1, n + 1))
    keep = np.isin(labels, np.nonzero(sizes >= min_voxels)[0] + 1)
    return keep


class FlairLesionTool(Tool):
    title = "FLAIR 병변 볼륨"
    help = "뇌 영역 평균 + k×SD 이상인 고신호 병변을 찾아 개수와 부피를 셉니다 (AI 라벨 'FLAIR lesion')."

    def build(self):
        self.series = self.series_picker()
        self.k = spin(2.5, 0.5, 8, 1, suffix=" SD")
        self.form.addRow("임계값 (평균 +):", self.k)
        self.min_ml = spin(0.02, 0, 5, 3, suffix=" mL")
        self.form.addRow("최소 병변:", self.min_ml)
        self.button("🧠 계산", self.run)

    def run(self):
        s = self.series.series()
        vol = self.ctx.volume(s)
        brain = _brain_mask(vol.array)
        v = vol.array
        thr = v[brain].mean() + self.k.value() * v[brain].std()
        vox_ml = vol.voxel_volume_mm3() / 1000.0
        lesions = _drop_small(brain & (v > thr), self.min_ml.value() / vox_ml)
        labels, n = ndimage.label(lesions)
        sizes = sorted((ndimage.sum(lesions, labels, range(1, n + 1)) * vox_ml).tolist(),
                       reverse=True) if n else []
        self.ctx.show_mask(s, lesions, "FLAIR lesion")
        rows = [("임계값", thr), ("병변 수", n), ("총 부피 (mL)", sum(sizes)),
                ("뇌 부피 (mL)", brain.sum() * vox_ml)]
        rows += [(f"병변 {i + 1} (mL)", v_) for i, v_ in enumerate(sizes[:10])]
        self.ctx.results("FLAIR 병변", rows)


# ═══ Oncology ═══

class TumorTool(Tool):
    title = "종양 볼륨 + RECIST (장경/단경)"
    help = ("AI 라벨(세그멘테이션)로 부피와 RECIST 장경·단경을 계산하고 가장 긴 슬라이스에 측정선을 그립니다. "
            "라벨이 없으면 마지막 랜드마크(F)에서 값 범위로 자동 분할합니다.")

    def build(self):
        self.series = self.series_picker()
        self.label = self.label_picker()
        self.tol = spin(80, 1, 2000, 0)
        self.tol.setToolTip("자동 분할: 시드 값 ± 이 범위의 연결 영역")
        self.form.addRow("자동 분할 ±:", self.tol)
        self.button("📏 측정 (라벨)", self.measure)
        self.button("🌱 랜드마크에서 자동 분할 후 측정", self.grow)

    def grow(self):
        s = self.series.series()
        seed = self.ctx.landmark_voxel(s)
        if seed is None:
            raise ValueError("Landmark 도구(F)로 병변 중심을 찍으세요.")
        vol = self.ctx.volume(s)
        k, r, c = (int(round(v)) for v in seed)
        value = vol.array[k, r, c]
        region = np.abs(vol.array - value) <= self.tol.value()
        labels, _ = ndimage.label(region)
        mask = labels == labels[k, r, c]
        label = self.ctx.show_mask(s, mask, self.label.currentText().split(". ", 1)[-1] or "Tumor")
        self.label.refresh()
        self.label.setCurrentIndex(max(0, self.label.findData(label["id"])))
        self.measure()

    def measure(self):
        s = self.series.series()
        mask = self.ctx.label_mask(s, self.label.currentData())
        vol = self.ctx.volume(s)
        summary, rec = LS.lesion_summary(mask, vol.spacing, vol.array)
        if rec["slice"] is not None:
            k = rec["slice"]
            self.ctx.add_line(s, k, *rec["long_pts"], rec["long_mm"], "LD")
            if rec["short_pts"]:
                self.ctx.add_line(s, k, *rec["short_pts"], rec["short_mm"], "SA")
            self.ctx.main._select_series(s)
            self.ctx.vp().go_to_slice(k)
        self.ctx.main._last_lesion = (s, summary)
        self.ctx.results("종양 측정", summary, note="RECIST 측정선을 해당 슬라이스에 그렸습니다.")


class FollowUpTool(Tool):
    title = "Follow-up 비교 (크기 변화율 · 배가 시간)"
    help = ("이전 검사와 현재 검사에서 같은 병변을 AI 라벨로 칠한 뒤 비교합니다. "
            "간격은 두 검사의 StudyDate로 계산합니다 (폐결절 Doubling Time 포함).")

    def build(self):
        self.prior = self.series_picker("이전 검사:")
        self.prior_label = self.label_picker("이전 라벨:")
        self.current = self.series_picker("현재 검사:")
        self.current_label = self.label_picker("현재 라벨:")
        self.days = spin(0, 0, 10000, 0, suffix=" 일")
        self.days.setToolTip("0이면 StudyDate 차이")
        self.form.addRow("간격:", self.days)
        self.button("📊 비교", self.run)

    def run(self):
        ps, cs = self.prior.series(), self.current.series()
        pv, cv = self.ctx.volume(ps), self.ctx.volume(cs)
        pm = self.ctx.label_mask(ps, self.prior_label.currentData())
        cm = self.ctx.label_mask(cs, self.current_label.currentData())
        prior, _ = LS.lesion_summary(pm, pv.spacing)
        current, _ = LS.lesion_summary(cm, cv.spacing)
        days = self.days.value()
        if not days:
            a, b = _study_date(ps), _study_date(cs)
            days = (b - a).days if a and b else 0
        res = LS.follow_up(prior, current, days or None)

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.bar(["이전", "현재"], [prior["Volume (mL)"], current["Volume (mL)"]],
                   color=["#888", "#fc4"])
            ax.set_ylabel("volume (mL)")
        self.ctx.results("Follow-up", res, plot)


class ADCHistogramTool(Tool):
    title = "ADC 히스토그램"
    help = "ADC 시리즈 위 AI 라벨(병변) 영역의 ADC 분포 - 백분위수, 왜도, 첨도."

    def build(self):
        self.series = self.series_picker("ADC:")
        self.label = self.label_picker()
        self.button("📊 히스토그램", self.run)

    def run(self):
        from scipy import stats
        s = self.series.series()
        vol = self.ctx.volume(s)
        mask = self.ctx.label_mask(s, self.label.currentData())
        vals = vol.array[mask] * _adc_scale(vol.array)
        rows = {"N": int(vals.size), "Mean": float(vals.mean()), "SD": float(vals.std()),
                "Min": float(vals.min()), "Max": float(vals.max())}
        for p in (10, 25, 50, 75, 90):
            rows[f"P{p}"] = float(np.percentile(vals, p))
        rows["Skewness"] = float(stats.skew(vals))
        rows["Kurtosis"] = float(stats.kurtosis(vals))
        hist, _ = np.histogram(vals, bins=64)
        p = hist[hist > 0] / hist.sum()
        rows["Entropy (bits)"] = float(-(p * np.log2(p)).sum())

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.hist(vals, bins=60, color="#4a9eff")
            ax.axvline(np.percentile(vals, 10), color="#f66", ls="--", label="P10")
            ax.axvline(vals.mean(), color="#fc4", label="mean")
            ax.set_xlabel("ADC (×10⁻⁶ mm²/s)")
            ax.legend(fontsize=7)
        self.ctx.results("ADC 히스토그램", rows, plot)


class SUVTool(Tool):
    title = "SUV 측정 (PET, SUVbw)"
    help = ("PET DICOM의 투여량·체중·시각 태그로 SUVbw를 계산합니다. "
            "영역: AI 라벨(3D) 또는 현재 ROI(2D). MTV 경계는 SUVmax의 % 또는 라벨 그대로.")

    def build(self):
        self.series = self.series_picker("PET:")
        self.source = QComboBox()
        self.source.addItems(["AI 라벨 (3D)", "현재 ROI (현재 슬라이스)"])
        self.form.addRow("영역:", self.source)
        self.label = self.label_picker()
        self.pct = spin(41, 0, 100, 0, suffix=" %")
        self.pct.setToolTip("0이면 영역 전체를 MTV로")
        self.form.addRow("MTV 임계 (SUVmax %):", self.pct)
        self.button("☢️ SUV 계산", self.run)

    def run(self):
        from .. import dicom_info
        s = self.series.series()
        factor = dicom_info.suv_factor(s.slices[0])
        if not factor:
            raise ValueError("SUV 계산에 필요한 PET 태그(투여량, 체중, 시각, 반감기)가 없습니다.")
        vol = self.ctx.volume(s)
        suv = vol.array * factor
        if self.source.currentIndex() == 0:
            mask = self.ctx.label_mask(s, self.label.currentData())
        else:
            mask = np.zeros(suv.shape, bool)
            mask[self.ctx.slice_index()] = self.ctx.roi_mask()
        res = LS.suv_stats(suv, mask, vol.spacing, self.pct.value() or None)
        res["SUV factor"] = factor
        self.ctx.results("SUV", res)


# ═══ Lung ═══

class NoduleTool(Tool):
    title = "폐결절 측정 (장경 · 단경 · 볼륨)"
    help = ("Landmark 도구(F)로 결절 중심을 찍고 실행하세요. 임계값 이상 연결 영역을 반경 안에서 키우고 "
            "열림 연산으로 혈관을 분리합니다. 결과는 AI 라벨 'Nodule'.")

    def build(self):
        self.series = self.series_picker("CT:")
        self.thr = spin(-500, -1000, 500, 0, suffix=" HU")
        self.form.addRow("임계값:", self.thr)
        self.radius = spin(25, 5, 80, 0, suffix=" mm")
        self.form.addRow("최대 반경:", self.radius)
        self.button("🫁 결절 분할·측정", self.run)

    def run(self):
        s = self.series.series()
        seed = self.ctx.landmark_voxel(s)
        if seed is None:
            raise ValueError("Landmark 도구(F)로 결절 중심을 찍으세요.")
        vol = self.ctx.volume(s)
        mask = LS.grow_nodule(vol.array, seed, vol.spacing, self.thr.value(), self.radius.value())
        if not mask.any():
            raise ValueError("시드 위치가 임계값보다 어둡습니다 (결절 안쪽을 찍으세요).")
        self.ctx.show_mask(s, mask, "Nodule")
        summary, rec = LS.lesion_summary(mask, vol.spacing, vol.array)
        vals = vol.array[mask]
        summary["Solid portion (> -300 HU, %)"] = float((vals > -300).mean() * 100)
        if rec["slice"] is not None:
            self.ctx.add_line(s, rec["slice"], *rec["long_pts"], rec["long_mm"], "LD")
            if rec["short_pts"]:
                self.ctx.add_line(s, rec["slice"], *rec["short_pts"], rec["short_mm"], "SA")
        self.ctx.results("폐결절", summary,
                         note="Doubling Time: Lung ▸ Doubling Time에서 이전 검사와 비교하세요.")


class EmphysemaTool(Tool):
    title = "폐기종 정량 (LAA%)"
    help = "자동 폐 분할 후 −950 HU 미만 저음영 영역 비율(LAA%-950), Perc15 등을 계산합니다."

    def build(self):
        self.series = self.series_picker("CT:")
        self.show = QCheckBox("LAA 영역을 AI 라벨로 표시")
        self.show.setChecked(True)
        self.form.addRow(self.show)
        self.button("🫁 계산", self.run)

    def run(self):
        s = self.series.series()
        vol = self.ctx.volume(s)
        show = self.show.isChecked()

        def task(progress, cancelled):
            progress("폐 분할 중...", 0.3)
            lung = LS.lung_mask(vol.array)
            return lung, LS.emphysema(vol.array, lung, vol.spacing)

        def done(result):
            lung, res = result
            if show:
                self.ctx.show_mask(s, lung & (vol.array < -950), "LAA-950")
            vals = vol.array[lung]

            def plot(fig):
                ax = fig.add_subplot(111)
                ax.hist(vals, bins=np.arange(-1024, -300, 10), color="#4a9eff")
                ax.axvline(-950, color="#f66", ls="--", label="-950 HU")
                ax.set_xlabel("HU")
                ax.legend(fontsize=7)
            self.ctx.results("폐기종", res, plot)
        self.ctx.run("폐기종 계산 중...", task, done)


class GGOTool(Tool):
    title = "GGO 정량 (간유리 음영)"
    help = "폐 안에서 지정한 HU 범위(기본 −700 ~ −300)의 비율과 부피."

    def build(self):
        self.series = self.series_picker("CT:")
        self.lo = spin(-700, -1000, 0, 0, suffix=" HU")
        self.hi = spin(-300, -1000, 200, 0, suffix=" HU")
        self.form.addRow("하한:", self.lo)
        self.form.addRow("상한:", self.hi)
        self.button("🫁 계산", self.run)

    def run(self):
        s = self.series.series()
        vol = self.ctx.volume(s)
        lo, hi = self.lo.value(), self.hi.value()

        def task(progress, cancelled):
            lung = LS.lung_mask(vol.array)
            return lung, LS.ggo(vol.array, lung, vol.spacing, lo, hi)

        def done(result):
            lung, res = result
            self.ctx.show_mask(s, lung & (vol.array >= lo) & (vol.array <= hi), "GGO")
            self.ctx.results("GGO", res)
        self.ctx.run("GGO 계산 중...", task, done)


# ═══ MSK ═══

class JointAngleTool(Tool):
    title = "관절 각도 (두 선 사이 각도)"
    help = ("거리 측정(Dist, 4)으로 뼈의 축을 두 선으로 그리고 실행하세요. "
            "두 선 사이 각도와 각 선의 수평 기준 각도를 계산합니다. 세 점 각도는 Angle(5) 도구.")

    def build(self):
        self.button("📐 각도 계산 (마지막 두 선)", self.run)

    def run(self):
        l1, l2 = self.ctx.last_lines(2)
        sp = self.ctx.spacing2d()
        self.ctx.results("관절 각도", {
            "두 선 사이 각도 (°)": LS.angle_between_lines(l1, l2, sp),
            "선 1 수평 기준 (°)": LS.line_angle_deg(*l1, sp),
            "선 2 수평 기준 (°)": LS.line_angle_deg(*l2, sp)})


class MuscleTool(Tool):
    title = "근육 단면적 · 지방 침윤"
    help = ("근육 윤곽을 Freehand ROI(8)로 그리고 실행하세요. 단면적(cm²)과 지방 범위 픽셀 비율. "
            "CT 기본 −190 ~ −30 HU, MR은 'Otsu 자동'으로 밝은(지방) 픽셀을 나눕니다.")

    def build(self):
        self.mode = QComboBox()
        self.mode.addItems(["CT (HU 범위)", "MR (Otsu 자동)"])
        self.form.addRow("영상:", self.mode)
        self.lo = spin(-190, -1000, 3000, 0)
        self.hi = spin(-30, -1000, 3000, 0)
        self.form.addRow("지방 하한:", self.lo)
        self.form.addRow("지방 상한:", self.hi)
        self.button("💪 계산", self.run)

    def run(self):
        roi = self.ctx.roi_mask()
        img = self.ctx.image()
        sp = self.ctx.spacing2d()
        vals = img[roi]
        if self.mode.currentIndex() == 0:
            lo, hi = self.lo.value(), self.hi.value()
        else:
            lo, hi = LS.otsu_threshold(vals), float(vals.max())
        ff = LS.fat_fraction(img, roi, lo, hi)
        csa = roi.sum() * sp[0] * sp[1] / 100.0
        self.ctx.results("근육", {"CSA (cm²)": csa, "Mean": float(vals.mean()),
                                 "SD": float(vals.std()), "지방 범위": f"{lo:g} ~ {hi:g}",
                                 "지방 침윤 (%)": ff, "Lean CSA (cm²)": csa * (1 - ff / 100)})


class CobbTool(Tool):
    title = "Cobb Angle"
    help = "기존 Cobb 도구(B)를 켭니다: 첫 번째 종판선 드래그 → 두 번째 종판선 드래그."

    def build(self):
        self.button("📐 Cobb 도구 켜기 (B)", self.run)

    def run(self):
        from ..viewport import DicomViewport
        action = self.ctx.main._tool_actions.get(DicomViewport.TOOL_COBB)
        if action:
            action.trigger()


# ═══ Vascular ═══

class VesselTool(Tool):
    title = "혈관 직경 · 면적 · 협착률"
    help = ("혈관 단면에 타원(E)/Freehand ROI를 그려 면적·직경을 재고 '정상'/'협착'으로 저장하면 "
            "협착률((정상−협착)/정상×100)을 계산합니다. 거리 측정선(4)의 길이도 직경으로 쓸 수 있습니다.")

    def build(self):
        self.use = QComboBox()
        self.use.addItems(["ROI (면적·직경)", "마지막 거리 측정선 (직경)"])
        self.form.addRow("측정:", self.use)
        self.info = QLabel("정상: - / 협착: -")
        self.form.addRow(self.info)
        self.button("📏 측정", self.measure)
        self.button("정상 부위로 저장", lambda: self.save("normal"))
        self.button("협착 부위로 저장", lambda: self.save("stenotic"))
        self.button("🩺 협착률 계산", self.compute)
        self.values = {}
        self.last = None

    def _measure(self):
        sp = self.ctx.spacing2d()
        if self.use.currentIndex() == 0:
            m = LS.vessel_metrics(self.ctx.roi_mask(), sp)
            return m, {"diameter": m["Equivalent diameter (mm)"], "area": m["Area (mm²)"]}
        (p0, p1), = self.ctx.last_lines(1)
        d = float(np.hypot((p1[0] - p0[0]) * sp[1], (p1[1] - p0[1]) * sp[0]))
        return {"Diameter (mm)": d}, {"diameter": d, "area": np.pi * (d / 2) ** 2}

    def measure(self):
        rows, self.last = self._measure()
        self.ctx.results("혈관", rows)

    def save(self, which):
        _rows, self.last = self._measure()
        self.values[which] = self.last
        n, st = self.values.get("normal"), self.values.get("stenotic")
        self.info.setText(f"정상: {n['diameter']:.2f} mm" if n else "정상: -")
        self.info.setText(self.info.text() + (f" / 협착: {st['diameter']:.2f} mm" if st else " / 협착: -"))

    def compute(self):
        n, st = self.values.get("normal"), self.values.get("stenotic")
        if not n or not st:
            raise ValueError("정상 부위와 협착 부위를 각각 저장하세요.")
        rows = LS.stenosis(n["diameter"], st["diameter"], "diameter")
        rows.update({"Stenosis by area (%)": LS.stenosis(n["area"], st["area"], "area")
                     ["Stenosis by area (%)"],
                     "Normal diameter (mm)": n["diameter"], "Stenotic diameter (mm)": st["diameter"]})
        self.ctx.results("협착률", rows, note="NASCET 방식은 원위부 정상 내강을 '정상'으로 저장하세요.")


class CPRTool(Tool):
    title = "Curved MPR (중심선 따라 펼치기)"
    help = ("Landmark 도구(F)로 혈관 중심을 따라 순서대로 점을 찍고 실행하세요 (2개 이상). "
            "중심선에 수직인 단면 시리즈와 길이 방향 CPR 영상을 새 시리즈로 만듭니다.")

    def build(self):
        self.series = self.series_picker()
        self.width = spin(40, 10, 150, 0, suffix=" mm")
        self.form.addRow("단면 폭:", self.width)
        self.step = spin(0.5, 0.1, 3, 2, suffix=" mm")
        self.form.addRow("간격:", self.step)
        self.button("🩸 Curved MPR 생성", self.run)

    def run(self):
        s = self.series.series()
        pts = [p["position"] for p in self.ctx.main._landmarks]
        if len(pts) < 2:
            raise ValueError("랜드마크를 혈관을 따라 2개 이상 찍으세요.")
        vol = self.ctx.volume(s)
        width, step = self.width.value(), self.step.value()

        def task(progress, cancelled):
            progress("중심선을 따라 샘플링 중...", 0.3)
            return LS.curved_mpr(vol.array, vol.affine_lps, pts, width, step)

        def done(result):
            cross, cpr, length = result
            from ..formats.volume_series import VolumeSeries
            aff = np.diag([step, step, step, 1.0])
            ref = s.slices[0]

            def add(array, name):
                series = VolumeSeries(array, aff, str(getattr(ref, "PatientName", "")),
                                      f"{s.series_uid}#{name}#{id(array)}", "derived",
                                      modality=s.modality, description=name, canonical=False)
                for ds in series.slices:
                    for kw in ("PatientName", "PatientID", "StudyInstanceUID", "StudyDate"):
                        if kw in ref:
                            setattr(ds, kw, getattr(ref, kw))
                    ds.WindowCenter, ds.WindowWidth = s.get_default_window()
                self.ctx.main.add_derived_series(series, select=False)
                return series
            add(cross, f"CPR cross-sections ({s.description})")
            longi = add(cpr.T[None], f"CPR longitudinal ({s.description})")
            self.ctx.open_series(longi)
            self.ctx.results("Curved MPR", {"중심선 길이 (mm)": length, "단면 수": cross.shape[0],
                                            "단면 크기 (mm)": f"{width:g} × {width:g}"})
        self.ctx.run("Curved MPR 생성 중...", task, done)


class AneurysmTool(Tool):
    title = "동맥류 크기"
    help = "AI 라벨로 칠한 동맥류(또는 대동맥)의 최대 3D 직경, 최대 축상 직경(장경/단경), 부피."

    def build(self):
        self.series = self.series_picker()
        self.label = self.label_picker()
        self.button("📏 측정", self.run)

    def run(self):
        s = self.series.series()
        vol = self.ctx.volume(s)
        mask = self.ctx.label_mask(s, self.label.currentData())
        summary, rec = LS.lesion_summary(mask, vol.spacing)
        if rec["slice"] is not None:
            self.ctx.add_line(s, rec["slice"], *rec["long_pts"], rec["long_mm"], "Dmax")
        self.ctx.results("동맥류", {"Max 3D diameter (mm)": summary["Max 3D diameter (mm)"],
                                   "Max axial diameter (mm)": summary["Long axis (mm)"],
                                   "Perpendicular diameter (mm)": summary["Short axis (mm)"],
                                   "Volume (mL)": summary["Volume (mL)"],
                                   "Slice": summary["RECIST slice"]})


NEURO = [("colormap", ColormapTool), ("mismatch", MismatchTool), ("flair", FlairLesionTool)]
ONCOLOGY = [("tumor", TumorTool), ("followup", FollowUpTool), ("adc_hist", ADCHistogramTool),
            ("suv", SUVTool)]
LUNG = [("nodule", NoduleTool), ("doubling", FollowUpTool), ("emphysema", EmphysemaTool),
        ("ggo", GGOTool)]
MSK = [("joint", JointAngleTool), ("muscle", MuscleTool), ("cobb", CobbTool)]
VASCULAR = [("vessel", VesselTool), ("cpr", CPRTool), ("aneurysm", AneurysmTool)]
