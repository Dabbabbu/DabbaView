# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Analysis ▸ Diffusion / Perfusion / Spectroscopy 도구 페이지"""
import numpy as np
from PyQt5.QtWidgets import QComboBox, QFileDialog, QLabel, QLineEdit

from . import models as M
from . import mrs as MR
from .data import build_stack, detect, parse_values, position_key
from .panel import Tool, ispin, spin


def _stack_for(tool, series_list, kind, text):
    override = parse_values(text) if text.strip() else None
    stack = build_stack(series_list, kind, override)
    if stack.kind == "index" and kind != "index":
        raise ValueError(f"{kind} 값을 DICOM에서 찾지 못했습니다. 값을 직접 입력하세요.")
    stack.values = np.asarray(stack.values, dtype=float)
    return stack


def _current_z(ctx, stack):
    """뷰포트의 현재 슬라이스가 stack의 몇 번째 위치인지 (없으면 가운데)"""
    s = ctx.series()
    if s is not None:
        key = position_key(s.slices[ctx.slice_index()])
        if key in stack.positions:
            return stack.positions.index(key)
    return len(stack.positions) // 2


class ParamTool(Tool):
    """다중 파라미터 입력 공통 (시리즈 체크 + 값 자동/수동)"""

    kind = "b"
    param_label = "b-value:"

    def build_params(self):
        self.series = self.multi_picker("입력 시리즈:")
        self.values = QLineEdit()
        self.values.setPlaceholderText("비우면 DICOM에서 자동 (예: 0, 50, 100, 800)")
        self.form.addRow(self.param_label, self.values)
        self.detected = QLabel()
        self.form.addRow(self.detected)

    def detect(self):
        vals = sorted({v for s in self.series.series() for v in detect(s, self.kind)})
        self.detected.setText("감지: " + (", ".join(f"{v:g}" for v in vals) or "없음"))

    def stack(self):
        return _stack_for(self, self.series.series(), self.kind, self.values.text())

    def roi_curve(self, stack):
        """현재 ROI의 파라미터별 평균 신호 (현재 위치)"""
        mask = self.ctx.roi_mask()
        z = _current_z(self.ctx, stack)
        if stack.array.shape[2:] != mask.shape:
            raise ValueError("ROI를 입력 시리즈 영상 위에 그리세요.")
        return np.array([stack.array[p, z][mask].mean() for p in range(stack.shape[0])])


# ═══ Diffusion ═══

class ADCMapTool(ParamTool):
    title = "ADC Map 생성 (다중 b-value → 단일 지수 피팅)"
    help = ("b-value가 다른 확산 영상(한 시리즈 또는 b마다 따로 저장된 시리즈 여러 개)으로 "
            "S = S0·exp(−b·ADC)를 픽셀마다 피팅해 ADC 맵(×10⁻⁶ mm²/s)을 만듭니다.")

    def build(self):
        self.build_params()
        self.noise = spin(5, 0, 50, 0, suffix=" %")
        self.form.addRow("배경 제외:", self.noise)
        self.button("🔍 b-value 확인", self.detect)
        self.button("🗺 ADC 맵 생성", self.run)

    def run(self):
        series = self.series.series()
        text = self.values.text()
        noise = self.noise.value() / 100

        def task(progress, cancelled):
            stack = _stack_for(self, series, "b", text)
            if len(set(stack.values)) < 2:
                raise ValueError("b-value가 2개 이상 필요합니다.")
            mask = stack.array[np.argmin(stack.values)] > noise * stack.array.max()
            adc, _s0 = M.fit_adc(stack.array, stack.values, mask)
            return stack, adc * 1e6

        def done(result):
            stack, adc = result
            refs = [stack.ref(0, z) for z in range(stack.shape[1])]
            new = self.ctx.make_series(adc, refs, f"ADC map ({series[0].description})",
                                       series[0], window=(1500, 3000), modality="MR")
            self.ctx.open_series(new, colormap="Gray", window=(1500, 3000))
            vals = adc[adc > 0]
            self.ctx.results("ADC map", {"b-values": ", ".join(f"{v:g}" for v in stack.values),
                                         "위치 수": adc.shape[0],
                                         "중앙값 ADC (×10⁻⁶ mm²/s)": float(np.median(vals)) if vals.size else 0,
                                         "새 시리즈": new.description})
        self.ctx.run("ADC 피팅 중...", task, done)


class DecayCurveTool(ParamTool):
    title = "b-value 신호 감쇠 곡선"
    help = "입력 영상 위에 ROI를 그리고 실행하세요. b에 따른 평균 신호와 단일 지수/IVIM 피팅 곡선."

    def build(self):
        self.build_params()
        self.button("📉 곡선 그리기 (현재 ROI)", self.run)

    def run(self):
        stack = self.stack()
        y = self.roi_curve(stack)
        b = stack.values
        adc, s0 = M.fit_adc(y[:, None], b)
        rows = {"ADC (×10⁻⁶ mm²/s)": float(adc[0] * 1e6), "S0": float(s0[0])}
        ivim = None
        if len(b) >= 4 and (b < 200).sum() >= 2 and (b >= 200).sum() >= 2:
            ivim = M.ivim_curve_fit(y, b)
            rows.update({"IVIM D (×10⁻³)": ivim["D"] * 1e3, "IVIM f": ivim["f"],
                         "IVIM D* (×10⁻³)": ivim["Dstar"] * 1e3})

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.semilogy(b, y, "o", color="#fc4", label="ROI")
            bb = np.linspace(0, b.max(), 200)
            ax.semilogy(bb, s0[0] * np.exp(-bb * adc[0]), "-", color="#4af", label="mono-exp")
            if ivim:
                ax.semilogy(bb, ivim["S0"] * (ivim["f"] * np.exp(-bb * ivim["Dstar"]) +
                                               (1 - ivim["f"]) * np.exp(-bb * ivim["D"])),
                            "--", color="#f66", label="IVIM")
            ax.set_xlabel("b (s/mm²)")
            ax.set_ylabel("signal")
            ax.legend(fontsize=7)
        self.ctx.results("신호 감쇠", rows, plot)


class IVIMTool(ParamTool):
    title = "IVIM 분석 (D · D* · f)"
    help = ("분할(segmented) 피팅: b ≥ 기준값으로 D, 절편으로 f, 낮은 b로 D*를 픽셀마다 계산해 "
            "맵 3개를 만듭니다. ROI가 있으면 평균 신호를 비선형 bi-exponential로 정밀 피팅합니다.")

    def build(self):
        self.build_params()
        self.threshold = spin(200, 50, 1000, 0, suffix=" s/mm²")
        self.form.addRow("고 b 기준:", self.threshold)
        self.button("🗺 IVIM 맵 생성", self.run)
        self.button("ROI 정밀 피팅", self.roi_fit)

    def run(self):
        series = self.series.series()
        text = self.values.text()
        thr = self.threshold.value()

        def task(progress, cancelled):
            stack = _stack_for(self, series, "b", text)
            maps = M.ivim_segmented(stack.array, stack.values, thr,
                                    stack.array[np.argmin(stack.values)] > 0.05 * stack.array.max())
            return stack, maps

        def done(result):
            stack, maps = result
            refs = [stack.ref(0, z) for z in range(stack.shape[1])]
            made = []
            for key, scale, window in (("D", 1e6, (1000, 2000)), ("Dstar", 1e6, (30000, 60000)),
                                       ("f", 1000, (150, 300))):
                name = {"D": "IVIM D (×10⁻⁶)", "Dstar": "IVIM D* (×10⁻⁶)", "f": "IVIM f (×1000)"}[key]
                made.append(self.ctx.make_series(maps[key] * scale, refs,
                                                 f"{name} ({series[0].description})", series[0],
                                                 window=window, modality="MR"))
            self.ctx.open_series(made[0], colormap="Jet", window=(1000, 2000))
            self.ctx.results("IVIM 맵", {"새 시리즈": ", ".join(m.description for m in made),
                                        "b-values": ", ".join(f"{v:g}" for v in stack.values)})
        self.ctx.run("IVIM 피팅 중...", task, done)

    def roi_fit(self):
        stack = self.stack()
        y = self.roi_curve(stack)
        fit = M.ivim_curve_fit(y, stack.values)
        self.ctx.results("IVIM (ROI)", {"D (×10⁻³ mm²/s)": fit["D"] * 1e3, "f": fit["f"],
                                       "D* (×10⁻³ mm²/s)": fit["Dstar"] * 1e3, "S0": fit["S0"]})


# ═══ Perfusion ═══

class DynamicTool(Tool):
    """동적 시리즈 공통: 시간 축 + AIF 선택"""

    def build_dynamic(self, aif_options):
        self.series = self.series_picker("동적 시리즈:")
        self.interval = spin(0, 0, 60, 2, suffix=" s")
        self.interval.setToolTip("0이면 DICOM 획득 시각")
        self.form.addRow("프레임 간격:", self.interval)
        self.baseline = ispin(3, 1, 30)
        self.form.addRow("기준선 프레임:", self.baseline)
        self.aif_mode = QComboBox()
        self.aif_mode.addItems(aif_options)
        self.form.addRow("AIF:", self.aif_mode)
        self.aif_info = QLabel("저장한 AIF 없음")
        self.form.addRow(self.aif_info)
        self.saved_aif = None

    def stack(self):
        s = self.series.series()
        stack = build_stack([s], "time")
        t = np.asarray(stack.values, dtype=float)
        if self.interval.value() > 0 or stack.kind == "index" or np.ptp(t) == 0:
            t = np.arange(stack.shape[0]) * (self.interval.value() or 1.0)
        return stack, np.asarray(t, dtype=float)

    def save_aif(self, transform):
        stack, t = self.stack()
        z = _current_z(self.ctx, stack)
        mask = self.ctx.roi_mask()
        curves = transform(stack.array[:, z])
        self.saved_aif = curves[:, mask].mean(1)
        self.aif_info.setText(f"AIF 저장: 슬라이스 위치 {z + 1}, 픽셀 {int(mask.sum())}개")


class DCETool(DynamicTool):
    title = "DCE: Tofts 모델 (Ktrans · ve · kep)"
    help = ("상대 조영 증강 (S−S0)/S0 를 농도에 비례한다고 보고 표준 Tofts 모델을 선형화(Murase)해 "
            "픽셀마다 풉니다. AIF: 동맥에 ROI를 그려 저장 / 자동 / Parker 집단 AIF. "
            "T1 매핑 없이 쓰는 근사이므로 절대값보다 상대 비교에 쓰세요.")

    def build(self):
        self.build_dynamic(["자동 (가장 밝고 빠른 픽셀)", "저장한 ROI (동맥)", "Parker 집단 AIF"])
        self.scope = QComboBox()
        self.scope.addItems(["현재 슬라이스", "모든 슬라이스"])
        self.form.addRow("범위:", self.scope)
        self.button("AIF: 현재 ROI 저장", lambda: self.save_aif(self._enh))
        self.button("🗺 Ktrans · ve · kep 맵", self.run)

    def _enh(self, frames):
        return M.relative_enhancement(frames, self.baseline.value())

    def run(self):
        stack, t = self.stack()
        tmin = t / 60.0
        enh = M.relative_enhancement(stack.array, self.baseline.value())
        mode = self.aif_mode.currentIndex()
        z_list = list(range(stack.shape[1])) if self.scope.currentIndex() == 1 else \
            [_current_z(self.ctx, stack)]
        if mode == 1:
            if self.saved_aif is None:
                raise ValueError("동맥 ROI를 그리고 'AIF: 현재 ROI 저장'을 누르세요.")
            aif = self.saved_aif
        elif mode == 2:
            onset = tmin[self.baseline.value()]
            aif = np.where(tmin >= onset, M.parker_aif(np.clip(tmin - onset, 0, None)), 0)
            aif = aif / aif.max() * np.percentile(enh.max(0), 99.5)   # 상대 증강 척도로
        else:
            z0 = z_list[0]
            aif, _ = M.auto_aif(enh[:, z0].reshape(len(t), -1), t)

        def task(progress, cancelled):
            maps = {"Ktrans": [], "ve": [], "kep": []}
            for i, z in enumerate(z_list):
                progress(f"Tofts 피팅 {i + 1}/{len(z_list)}", i / len(z_list))
                mask = stack.array[:, z].mean(0) > 0.05 * stack.array.max()
                res = M.tofts_linear(enh[:, z], aif, tmin, mask)
                for k in maps:
                    maps[k].append(res[k])
            return {k: np.array(v) for k, v in maps.items()}

        def done(maps):
            refs = [stack.ref(0, z) for z in z_list]
            s = self.series.series()
            made = []
            for key, window in (("Ktrans", (0.1, 0.2)), ("ve", (0.3, 0.6)), ("kep", (0.5, 1.0))):
                made.append(self.ctx.make_series(maps[key], refs, f"DCE {key} ({s.description})", s,
                                                 window=window, modality="MR"))
            self.ctx.open_series(made[0], colormap="Jet", window=(0.1, 0.2))
            rows = {"AIF": self.aif_mode.currentText(), "프레임": len(t),
                    "새 시리즈": ", ".join(m.description for m in made)}
            try:
                mask = self.ctx.roi_mask(required=False)
            except ValueError:
                mask = None

            def plot(fig):
                ax = fig.add_subplot(111)
                ax.plot(t, aif, "-", color="#f66", label="AIF")
                if mask is not None and mask.shape == enh.shape[2:]:
                    ax.plot(t, enh[:, _current_z(self.ctx, stack)][:, mask].mean(1), "o-",
                            color="#fc4", label="ROI")
                ax.set_xlabel("time (s)")
                ax.set_ylabel("relative enhancement")
                ax.legend(fontsize=7)
            self.ctx.results("DCE Tofts", rows, plot)
        self.ctx.run("DCE 계산 중...", task, done)


class DSCTool(DynamicTool):
    title = "DSC: rCBV · rCBF · MTT (sSVD)"
    help = ("ΔR2*(t) = −ln(S/S0)/TE로 바꾼 뒤 AIF로 절단 SVD 디컨볼루션해 CBF, CBV(=∫C/∫AIF), "
            "MTT(=CBV/CBF)를 계산합니다. 정상 백질 ROI로 정규화하면 상대값(rCBV, rCBF)이 됩니다. "
            "sSVD는 CBF를 약간 과소평가(MTT 과대)하는 경향이 있습니다.")

    def build(self):
        self.build_dynamic(["자동", "저장한 ROI (동맥)"])
        self.te = spin(30, 1, 200, 1, suffix=" ms")
        self.form.addRow("TE:", self.te)
        self.button("AIF: 현재 ROI 저장", lambda: self.save_aif(self._dr2))
        self.button("🗺 CBV · CBF · MTT 맵 (현재 슬라이스)", self.run)
        self.button("정규화: 현재 ROI를 정상 백질로", self.normalize)
        self.button("📈 ROI 곡선 + 감마 피팅", self.curve)
        self.last = None

    def on_show(self):
        super().on_show()
        s = self.ctx.series()
        if s is not None and getattr(s.slices[0], "EchoTime", None):
            self.te.setValue(float(s.slices[0].EchoTime))

    def _dr2(self, frames):
        return M.signal_to_delta_r2(frames, self.te.value(), self.baseline.value())

    def run(self):
        stack, t = self.stack()
        z = _current_z(self.ctx, stack)
        dr2 = self._dr2(stack.array[:, z])
        if self.aif_mode.currentIndex() == 1:
            if self.saved_aif is None:
                raise ValueError("동맥 ROI를 그리고 'AIF: 현재 ROI 저장'을 누르세요.")
            aif = self.saved_aif
        else:
            aif, _ = M.auto_aif(dr2.reshape(len(t), -1), t)
        mask = stack.array[:, z].mean(0) > 0.1 * stack.array.max()
        maps = M.dsc_maps(dr2, t, aif, 0.2, mask)
        s = self.series.series()
        refs = [stack.ref(0, z)]
        made = {}
        for key in ("CBV", "CBF", "MTT"):
            v = maps[key]
            window = (float(np.percentile(v[mask], 50)), float(np.percentile(v[mask], 98)) * 1.5 or 1)
            made[key] = self.ctx.make_series(v[None], refs, f"DSC {key} ({s.description})", s,
                                             window=window, modality="MR")
        self.last = (maps, made, stack, z)
        self.ctx.open_series(made["CBV"], colormap="Jet")
        self.ctx.results("DSC 맵", {"AIF": self.aif_mode.currentText(),
                                   "새 시리즈": ", ".join(m.description for m in made.values())})

    def normalize(self):
        if self.last is None:
            raise ValueError("먼저 맵을 만드세요.")
        maps, _made, stack, z = self.last
        mask = self.ctx.roi_mask()
        if mask.shape != maps["CBV"].shape:
            raise ValueError("ROI를 같은 크기의 영상(원본 또는 맵) 위에 그리세요.")
        rows = {}
        for key in ("CBV", "CBF"):
            ref = maps[key][mask].mean()
            rel = maps[key] / ref if ref > 0 else maps[key]
            rows[f"r{key} 기준값"] = float(ref)
            s = self.series.series()
            self.ctx.make_series(rel[None], [stack.ref(0, z)], f"DSC r{key} ({s.description})", s,
                                 window=(1.5, 3.0), modality="MR")
        self.ctx.results("정규화", rows, note="정상 백질 = 1.0 인 rCBV/rCBF 시리즈를 만들었습니다.")

    def curve(self):
        stack, t = self.stack()
        z = _current_z(self.ctx, stack)
        mask = self.ctx.roi_mask()
        y = self._dr2(stack.array[:, z])[:, mask].mean(1)
        _popt, (tf, yf), metrics = M.fit_gamma(t, y)

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.plot(t, y, "o", color="#fc4", label="ΔR2* ROI")
            ax.plot(tf, yf, "-", color="#4af", label="gamma-variate")
            if self.saved_aif is not None:
                ax.plot(t, self.saved_aif, "--", color="#f66", label="AIF")
            ax.set_xlabel("time (s)")
            ax.set_ylabel("ΔR2* (1/s)")
            ax.legend(fontsize=7)
        self.ctx.results("DSC 곡선", metrics, plot)


class TimeCurveTool(DynamicTool):
    title = "시간-신호 곡선 (TIC)"
    help = "현재 ROI의 시간에 따른 평균 신호. 곡선을 여러 개 저장해 겹쳐 보고 감마 피팅 지표를 봅니다."

    def build(self):
        self.build_dynamic(["(사용 안 함)"])
        self.curves = []
        self.button("➕ 현재 ROI 곡선 추가", self.add)
        self.button("곡선 모두 지우기", self.clear)

    def add(self):
        stack, t = self.stack()
        z = _current_z(self.ctx, stack)
        mask = self.ctx.roi_mask()
        y = stack.array[:, z][:, mask].mean(1)
        self.curves.append((f"ROI {len(self.curves) + 1}", t, y))
        rows = []
        for name, tt, yy in self.curves:
            base = yy[:self.baseline.value()].mean()
            dev = yy - base
            i = int(np.argmax(np.abs(dev)))   # 조영 증강(+) 또는 DSC 신호 감소(−)
            rows.append([name, float(yy[i]), float(tt[i]),
                         float(dev[i] / base * 100) if base else 0.0,
                         M.upslope(tt, yy if dev[i] >= 0 else -yy)])

        def plot(fig):
            ax = fig.add_subplot(111)
            for name, tt, yy in self.curves:
                ax.plot(tt, yy, "o-", label=name)
            ax.set_xlabel("time (s)")
            ax.set_ylabel("signal")
            ax.legend(fontsize=7)
        self.ctx.results("시간-신호 곡선", {"header": ["곡선", "Peak", "TTP (s)", "Peak 변화 (%)", "Max slope"],
                                            "rows": rows}, plot)

    def clear(self):
        self.curves = []


# ═══ Spectroscopy ═══

class MRSTool(Tool):
    title = "MR Spectroscopy (NAA · Cho · Cr · Lac)"
    help = ("DICOM MR Spectroscopy 또는 Siemens .rda 파일을 열어 스펙트럼을 표시하고 "
            "대사물질 피크를 찾아 면적과 비율(Cho/Cr, NAA/Cr, Cho/NAA)을 계산합니다.")

    def build(self):
        self.source = QLabel("스펙트럼: 없음")
        self.source.setWordWrap(True)
        self.form.addRow(self.source)
        self.lb = spin(3, 0, 20, 1, suffix=" Hz")
        self.form.addRow("선폭 가중:", self.lb)
        self.phase = QComboBox()
        self.phase.addItems(["자동 위상 보정", "수동"])
        self.form.addRow("위상:", self.phase)
        self.phase_deg = spin(0, -360, 360, 0, suffix=" °")
        self.form.addRow("수동 위상:", self.phase_deg)
        self.voxel = ispin(0, -1, 10000)
        self.voxel.setToolTip("다중 복셀(CSI)일 때 복셀 번호, -1 = 평균")
        self.form.addRow("복셀:", self.voxel)
        self.button("📂 MRS 파일 열기 (.dcm / .rda)", self.open_file)
        self.button("현재 시리즈에서 읽기", self.from_series)
        self.button("📈 분석", self.run)
        self.spectrum = None

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "MRS 파일", "",
                                              "MRS (*.dcm *.rda *.IMA *.ima);;All Files (*)")
        if not path:
            return
        if path.lower().endswith(".rda"):
            self.spectrum = MR.read_rda(path)
        else:
            import pydicom
            self.spectrum = MR.read_dicom(pydicom.dcmread(path, force=True), self.voxel.value())
        self.source.setText(f"스펙트럼: {path}")
        self.run()

    def from_series(self):
        s = self.ctx.series()
        if s is None:
            raise ValueError("시리즈가 없습니다.")
        ds = s.get_full_dataset(0)
        if ds is None or not MR.is_mrs_dataset(ds):
            raise ValueError("현재 시리즈는 MR Spectroscopy DICOM이 아닙니다. 파일 열기를 쓰세요.")
        self.spectrum = MR.read_dicom(ds, self.voxel.value())
        self.source.setText(f"스펙트럼: {s.description}")
        self.run()

    def run(self):
        if self.spectrum is None:
            raise ValueError("MRS 파일을 여세요.")
        auto = self.phase.currentIndex() == 0
        ppm, real, deg = MR.process(self.spectrum, self.lb.value(), 2, auto, self.phase_deg.value())
        if auto:
            self.phase_deg.setValue(deg)
        peaks, ratios = MR.quantify(ppm, real)
        rows = [[name, p["ppm"], p["height"], p["area"], p["SNR"]] for name, p in peaks.items()]
        rows += [[k, "", "", v, ""] for k, v in ratios.items()]

        def plot(fig):
            ax = fig.add_subplot(111)
            sel = (ppm > 0.2) & (ppm < 4.4)
            ax.plot(ppm[sel], real[sel], color="#9cf", lw=1)
            for name, p in peaks.items():
                if name in ("NAA", "Cr", "Cho", "Lac", "mI"):
                    ax.annotate(name, (p["ppm"], p["height"]), textcoords="offset points",
                                xytext=(0, 6), ha="center", fontsize=7, color="#fc4")
            ax.invert_xaxis()
            ax.set_xlabel("ppm")
            ax.set_title(f"{self.spectrum.tf:.1f} MHz · 위상 {deg:.0f}°", fontsize=9)
        self.ctx.results("MRS", {"header": ["피크/비율", "ppm", "높이", "면적(비율)", "SNR"], "rows": rows},
                         plot)


DIFFUSION = [("adc_map", ADCMapTool), ("decay", DecayCurveTool), ("ivim", IVIMTool)]
PERFUSION = [("dce", DCETool), ("dsc", DSCTool), ("tic", TimeCurveTool)]
SPECTROSCOPY = [("mrs", MRSTool)]
