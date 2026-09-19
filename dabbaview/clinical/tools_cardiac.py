# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Analysis ▸ Cardiac 도구 페이지"""
import math
import re

import numpy as np
from PyQt5.QtWidgets import QComboBox, QLabel, QLineEdit

from ..annotations import instance_key
from . import cardiac as C
from . import models as M
from .data import build_stack, detect, parse_values, position_key, slice_param
from .panel import Tool, ispin, spin


def last_roi_polygon(ctx, remove=False):
    """현재 영상의 마지막 자유곡선/타원 ROI → 다각형 점 목록 (remove=True면 그 ROI 주석을 지움)"""
    items = ctx.vp().annotations_here()
    for ann in reversed(items):
        if ann["type"] == "roi" and len(ann["pts"]) >= 3:
            pts = list(ann["pts"])
        elif ann["type"] == "ellipse":
            (x0, y0), (x1, y1) = ann["pts"]
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
            pts = [(cx + rx * math.cos(a), cy + ry * math.sin(a))
                   for a in np.linspace(0, 2 * math.pi, 64, endpoint=False)]
        else:
            continue
        if remove:
            items.remove(ann)   # 이 ROI만 (다른 주석은 그대로)
            ctx.main._annotation_store.changed.emit()
        return pts
    raise ValueError("현재 영상에 ROI가 없습니다. Freehand ROI(8) 또는 타원(E)으로 윤곽을 그리세요.")


def frames_at_current_position(ctx, series, kind="phase", filter_fn=None):
    """현재 슬라이스와 같은 위치의 영상들 (kind 순서) → (배열 (T,H,W), 값, 영상 번호)"""
    k0 = ctx.slice_index()
    key = position_key(series.slices[k0])
    items = []
    for i, ds in enumerate(series.slices):
        if position_key(ds) != key or (filter_fn and not filter_fn(ds)):
            continue
        items.append((slice_param(ds, kind), int(getattr(ds, "InstanceNumber", i) or i), i))
    if len(items) < 2:
        raise ValueError("현재 위치에 시간(위상)별 영상이 2장 이상 있는 시리즈가 필요합니다.")
    items.sort(key=lambda t: (t[0] if t[0] is not None else 0, t[1]))
    values = [t[0] if t[0] is not None else n for n, t in enumerate(items)]
    frames = np.stack([series.get_pixel_array(i) for _v, _n, i in items]).astype(np.float64)
    return frames, np.asarray(values, dtype=float), [i for *_x, i in items]


# ═══ 윤곽 & 기능 ═══

class ContoursTool(Tool):
    title = "LV/RV 윤곽 & 기능 (EF · EDV · ESV · SV · 심근 질량)"
    help = ("단축(SAX) cine에서 슬라이스·위상마다 Freehand ROI(8)로 윤곽을 그리고 종류를 골라 "
            "저장하세요. ED/ES는 내강 부피가 가장 큰/작은 위상으로 자동 결정됩니다 "
            "(Simpson 원반 합산, 심근 질량 = (Epi−Endo) 부피 × 1.05 g/mL).")

    def build(self):
        self.kind = QComboBox()
        for key, (name, _color) in C.CONTOUR_TYPES.items():
            self.kind.addItem(name, key)
        self.form.addRow("윤곽 종류:", self.kind)
        self.hr = ispin(0, 0, 250, " bpm")
        self.hr.setToolTip("0이면 DICOM HeartRate 사용")
        self.form.addRow("심박수:", self.hr)
        self.gap = spin(0, 0, 50, 2, suffix=" mm")
        self.gap.setToolTip("0이면 윤곽 위치 간격으로 자동")
        self.form.addRow("슬라이스 간격:", self.gap)
        self.info = QLabel()
        self.form.addRow(self.info)
        self.button("＋ 현재 ROI → 윤곽 저장", self.capture)
        self.button("이 영상의 윤곽 지우기", self.remove_here)
        self.button("시리즈 윤곽 모두 지우기", self.clear_all)
        self.button("📊 기능 계산 (EF · 용적 · 질량)", self.compute)
        self.ctx.main._contours.changed.connect(self.update_info)

    def on_show(self):
        super().on_show()
        self.update_info()

    def update_info(self):
        s = self.ctx.series()
        items = self.ctx.main._contours.for_series(s.series_uid) if s else []
        counts = {}
        for c in items:
            counts[c["kind"]] = counts.get(c["kind"], 0) + 1
        phases = len({round(c["phase"] or 0, 1) for c in items})
        text = ", ".join(f"{C.CONTOUR_TYPES[k][0]} {v}" for k, v in counts.items()) or "없음"
        self.info.setText(f"현재 시리즈 윤곽: {text} · 위상 {phases}개")

    def capture(self):
        s = self.ctx.series()
        if s is None:
            raise ValueError("시리즈를 여세요.")
        # ROI 주석은 윤곽으로 옮기므로 지움
        pts = last_roi_polygon(self.ctx, remove=True)
        item = self.ctx.main._contours.add(s, self.ctx.slice_index(), self.kind.currentData(), pts)
        self.ctx.status(f"{C.CONTOUR_TYPES[item['kind']][0]} 저장: {item['area_mm2']:.0f} mm², "
                        f"위상 {item['phase']} ms")

    def remove_here(self):
        s = self.ctx.series()
        ds = s.slices[self.ctx.slice_index()]
        self.ctx.main._contours.remove_for_image(instance_key(ds))

    def clear_all(self):
        s = self.ctx.series()
        if s is not None:
            self.ctx.main._contours.clear(s.series_uid)

    def compute(self):
        s = self.ctx.series()
        contours = self.ctx.main._contours.for_series(s.series_uid)
        if not contours:
            raise ValueError("저장한 윤곽이 없습니다.")
        hr = self.hr.value() or float(getattr(s.slices[0], "HeartRate", 0) or 0) or None
        res = C.ventricular_function(contours, self.gap.value() or None, hr)
        rows = [("Slice gap (mm)", res["slice_gap_mm"])]
        for side in ("LV", "RV"):
            for k, v in res.get(side, {}).items():
                rows.append((f"{side} {k}", v))
        by_phase, _gap = C.volumes_by_phase(contours, self.gap.value() or None)

        def plot(fig):
            ax = fig.add_subplot(111)
            for kind, color in (("lv_endo", "#ff6b6b"), ("rv_endo", "#5aa0ff")):
                pts = sorted((p, v[kind][0]) for p, v in by_phase.items() if kind in v)
                if pts:
                    ax.plot(*zip(*pts), "o-", color=color, label=C.CONTOUR_TYPES[kind][0])
            ax.set_xlabel("TriggerTime (ms)")
            ax.set_ylabel("volume (mL)")
            ax.set_title("용적-위상 곡선", fontsize=9)
            ax.legend(fontsize=7)
        self.ctx.results("심실 기능", rows, plot)


# ═══ Bull's Eye ═══

class BullseyeTool(Tool):
    title = "Bull's Eye Plot (AHA 17분절)"
    help = ("ED 위상의 LV Endo·Epi 윤곽이 있는 슬라이스로 17분절을 만듭니다. "
            "RV Endo 윤곽이 있으면 중격 방향을 자동으로, 없으면 화면 왼쪽을 중격으로 봅니다.")

    def build(self):
        self.source = QComboBox()
        self.source.addItems(["벽 두께 (mm)", "맵 시리즈 평균값 (T1/T2 등)", "LGE % (LGE 분석 임계값)"])
        self.form.addRow("값:", self.source)
        self.map_series = self.series_picker("맵 시리즈:", allow_none=True)
        self.button("🎯 Bull's Eye 그리기", self.run)

    def run(self):
        s = self.ctx.series()
        contours = self.ctx.main._contours.for_series(s.series_uid)
        by_phase, _ = C.volumes_by_phase(contours)
        phases = {p: v["lv_endo"][0] for p, v in by_phase.items() if "lv_endo" in v}
        if not phases:
            raise ValueError("LV Endo 윤곽이 없습니다 (윤곽 & 기능 도구).")
        ed = max(phases, key=phases.get)
        at_ed = [c for c in contours if round(c["phase"] or 0, 1) == ed]
        per_pos = {}
        for c in at_ed:
            per_pos.setdefault(round(c["position"], 1), {})[c["kind"]] = c
        rows = [v for _p, v in sorted(per_pos.items()) if "lv_endo" in v and "lv_epi" in v]
        if len(rows) < 3:
            raise ValueError("ED 위상에 Endo·Epi 윤곽이 모두 있는 슬라이스가 3장 이상 필요합니다.")
        # 기저부 = 심외막 면적이 큰 쪽
        if rows[0]["lv_epi"]["area_mm2"] < rows[-1]["lv_epi"]["area_mm2"]:
            rows = rows[::-1]
        mode = self.source.currentIndex()
        slices, unit, cmap = [], "mm", "viridis"
        map_series = self.map_series.series(required=False)
        lge_threshold = getattr(self.ctx.main, "_last_lge_threshold", None)
        for r in rows:
            item = {"endo": r["lv_endo"]["pts"], "epi": r["lv_epi"]["pts"],
                    "spacing": r["lv_endo"]["spacing"],
                    "rv_center": np.mean(r["rv_endo"]["pts"], axis=0) if "rv_endo" in r else None}
            if mode > 0:
                src = map_series if mode == 1 else s
                if src is None:
                    raise ValueError("맵 시리즈를 고르세요.")
                k = _slice_at_position(src, r["lv_endo"]["position"])
                if k is None:
                    raise ValueError("맵 시리즈에 같은 위치의 영상이 없습니다.")
                item["image"] = src.get_pixel_array(k).astype(np.float64)
            slices.append(item)
        if mode == 0:
            values = C.segment_values(slices)
        elif mode == 1:
            values = C.segment_values(slices, value_fn=lambda img, m: float(img[m].mean()))
            unit, cmap = "map", "plasma"
        else:
            if lge_threshold is None:
                raise ValueError("먼저 LGE 분석을 실행하세요 (임계값 필요).")
            values = C.segment_values(slices, value_fn=lambda img, m: float(
                (img[m] >= lge_threshold).mean() * 100))
            unit, cmap = "%", "hot"
        table = {"header": ["분절", "이름", "값"],
                 "rows": [[s_, C.SEGMENT_NAMES[s_], values[s_]] for s_ in range(1, 18)]}
        title = self.source.currentText()
        self.ctx.results("Bull's Eye", table,
                         lambda fig: C.plot_bullseye(fig.add_subplot(111), values, title,
                                                     cmap=cmap, unit=unit))


def _slice_at_position(series, position_mm, tol=1.0):
    for i, ds in enumerate(series.slices):
        if abs(position_key(ds, 0.1) / 10.0 - position_mm) <= tol:
            return i
    return None


# ═══ T1/T2 매핑 ═══

class MappingTool(Tool):
    title = "T1 / T2 / T2* Mapping"
    help = ("같은 위치에서 TI(MOLLI)나 TE(다중 에코)만 다른 영상들로 픽셀별 이완 시간 맵을 만듭니다. "
            "TI/TE는 DICOM에서 읽고(MOLLI는 ImageComments 'TI 123'도 인식), 없으면 직접 입력하세요.")

    def build(self):
        self.series = self.multi_picker("입력 시리즈:")
        self.kind = QComboBox()
        self.kind.addItems(["T1 (MOLLI/ShMOLLI, Look-Locker 보정)", "T2 (다중 에코)",
                            "T2* (다중 에코 GRE)"])
        self.form.addRow("종류:", self.kind)
        self.values = QLineEdit()
        self.values.setPlaceholderText("비우면 자동 (예: 100, 180, 260, 1100 ...)")
        self.form.addRow("TI/TE (ms):", self.values)
        self.detected = QLabel()
        self.form.addRow(self.detected)
        self.noise = spin(5, 0, 50, 0, suffix=" %")
        self.noise.setToolTip("최대 신호의 이 % 미만인 픽셀(배경)은 계산하지 않음")
        self.form.addRow("배경 제외:", self.noise)
        self.button("🔍 파라미터 확인", self.detect)
        self.button("🗺 맵 생성", self.run)
        self.button("ROI 평균 (현재 영상)", self.roi_value)

    def _kind(self):
        return "ti" if self.kind.currentIndex() == 0 else "te"

    def detect(self):
        series = self.series.series()
        vals = sorted({v for s in series for v in detect(s, self._kind())})
        self.detected.setText(f"감지된 값: {', '.join(f'{v:g}' for v in vals) or '없음'}")

    def run(self):
        series = self.series.series()
        kind = self._kind()
        override = parse_values(self.values.text()) if self.values.text().strip() else None
        noise = self.noise.value() / 100.0
        idx = self.kind.currentIndex()

        def task(progress, cancelled):
            stack = build_stack(series, kind, override)
            if stack.kind == "index":
                raise ValueError("TI/TE 값을 DICOM에서 찾지 못했습니다. 직접 입력하세요.")
            data = stack.array
            mask = data.max(0) > noise * data.max()
            maps = []
            for z in range(data.shape[1]):
                progress(f"피팅 {z + 1}/{data.shape[1]}", z / data.shape[1])
                if idx == 0:
                    m, _r2 = M.fit_t1_molli(data[:, z], stack.values, mask[z])
                else:
                    m, _s0 = M.fit_t2(data[:, z], stack.values, mask[z])
                maps.append(m)
            return stack, np.array(maps)

        def done(result):
            stack, maps = result
            name = ["T1 map", "T2 map", "T2* map"][idx]
            window = [(1100, 1600), (60, 100), (35, 70)][idx]
            refs = [stack.ref(0, z) for z in range(stack.shape[1])]
            new = self.ctx.make_series(maps, refs, f"{name} ({series[0].description})",
                                       series[0], window=window, modality="MR")
            self.ctx.open_series(new, colormap="Jet", window=window)
            vals = maps[maps > 0]
            self.ctx.results(name, {"위치 수": maps.shape[0], f"{stack.kind} 값": ", ".join(
                f"{v:g}" for v in stack.values), "중앙값 (ms)": float(np.median(vals)) if vals.size else 0,
                "새 시리즈": new.description},
                note="ROI를 그리고 'ROI 평균'으로 값을 읽으세요.")
        self.ctx.run("맵 계산 중...", task, done)

    def roi_value(self):
        mask = self.ctx.roi_mask()
        img = self.ctx.image()
        vals = img[mask]
        self.ctx.results("ROI 값", {"시리즈": self.ctx.series().description, "Mean": float(vals.mean()),
                                   "SD": float(vals.std()), "Median": float(np.median(vals)),
                                   "Pixels": int(vals.size)})


# ═══ LGE ═══

class LGETool(Tool):
    title = "LGE 경색 분석 (n-SD / FWHM)"
    help = ("LGE 시리즈의 슬라이스마다 LV Endo·Epi 윤곽(윤곽 & 기능 도구)을 저장한 뒤 실행합니다. "
            "n-SD: 정상(remote) 심근에 그린 현재 ROI 평균 + n×SD, FWHM: 심근 최대 신호의 50%.")

    def build(self):
        self.method = QComboBox()
        self.method.addItems(["n-SD (remote ROI)", "FWHM"])
        self.form.addRow("방법:", self.method)
        self.nsd = spin(5, 1, 10, 1)
        self.form.addRow("n (SD):", self.nsd)
        self.button("🩸 LGE 계산", self.run)

    def run(self):
        s = self.ctx.series()
        contours = self.ctx.main._contours.for_series(s.series_uid)
        per_image = {}
        for c in contours:
            per_image.setdefault(c["index"], {})[c["kind"]] = c
        from ..roi import polygon_mask
        images, masks, indices = [], [], []
        for k, cs in sorted(per_image.items()):
            if "lv_endo" in cs and "lv_epi" in cs:
                img = s.get_pixel_array(k).astype(np.float64)
                shape = img.shape
                myo = polygon_mask(cs["lv_epi"]["pts"], shape) & ~polygon_mask(cs["lv_endo"]["pts"], shape)
                images.append(img)
                masks.append(myo)
                indices.append(k)
        if not images:
            raise ValueError("Endo·Epi 윤곽이 모두 있는 LGE 슬라이스가 없습니다.")
        remote = None
        method = "fwhm" if self.method.currentIndex() == 1 else "nsd"
        if method == "nsd":
            remote = self.ctx.image()[self.ctx.roi_mask()]
        spacing = self.ctx.spacing2d(s)
        positions = sorted({round(contours[0]["position"], 1)} | {round(c["position"], 1) for c in contours})
        gap = float(np.median(np.diff(positions))) if len(positions) > 1 else float(
            getattr(s.slices[0], "SliceThickness", 8) or 8)
        res, infarct = C.lge_quantify(images, masks, spacing, gap, method, self.nsd.value(), remote)
        self.ctx.main._last_lge_threshold = res["threshold"]
        full = np.zeros((s.num_slices,) + images[0].shape, bool)
        for k, m in zip(indices, infarct):
            full[k] = m
        self.ctx.show_mask(s, full, "LGE")
        self.ctx.results("LGE 정량", res, note="경색 영역은 AI 라벨 'LGE'로 표시했습니다.")


# ═══ Phase Contrast ═══

def detect_venc(ds):
    for tag in ((0x0018, 0x9197),):
        elem = ds.get(tag)
        if elem is not None:
            try:
                return float(elem.value)
            except (TypeError, ValueError):
                pass
    m = re.search(r"_v(\d+)", str(getattr(ds, "SequenceName", "")))
    return float(m.group(1)) if m else None


class FlowTool(Tool):
    title = "Phase Contrast 유량 (유량 · 역류량 · Qp/Qs)"
    help = ("위상(속도) 영상 cine에서 혈관 단면에 ROI를 그리고 실행하세요. "
            "같은 위치의 모든 위상에 같은 ROI를 적용합니다. 대동맥·폐동맥을 각각 저장하면 Qp/Qs를 계산합니다.")

    def build(self):
        self.venc = spin(150, 1, 1000, 0, suffix=" cm/s")
        self.form.addRow("VENC:", self.venc)
        self.mapping = QComboBox()
        self.mapping.addItems(["부호 있는 12비트 (−4096 ~ 4096, Siemens)",
                               "부호 없는 12비트 (0 ~ 4096)", "값이 이미 cm/s (Philips 등)"])
        self.form.addRow("위상 값:", self.mapping)
        self.vessel = QComboBox()
        self.vessel.addItems(["Aorta (Qs)", "Pulmonary artery (Qp)", "기타"])
        self.form.addRow("혈관:", self.vessel)
        self.phase_only = QComboBox()
        self.phase_only.addItems(["위상 영상만 (ImageType P/PHASE)", "위치의 모든 영상"])
        self.form.addRow("영상:", self.phase_only)
        self.invert = QComboBox()
        self.invert.addItems(["방향 그대로", "방향 반대 (−)"])
        self.form.addRow("유량 방향:", self.invert)
        self.button("💧 유량 계산", self.run)
        self.saved = {}

    def on_show(self):
        super().on_show()
        s = self.ctx.series()
        if s is not None:
            venc = detect_venc(s.slices[0])
            if venc:
                self.venc.setValue(venc)

    def run(self):
        s = self.ctx.series()
        roi = self.ctx.roi_mask()

        def is_phase(ds):
            t = "\\".join(str(v) for v in getattr(ds, "ImageType", []))
            return "\\P" in t or "PHASE" in t.upper() or "VELOCITY" in t.upper()
        filt = is_phase if self.phase_only.currentIndex() == 0 and any(
            is_phase(ds) for ds in s.slices) else None
        frames, trig, _idx = frames_at_current_position(self.ctx, s, "phase", filt)
        rng = [(-4096, 4096), (0, 4096), None][self.mapping.currentIndex()]
        vel = M.phase_to_velocity(frames, self.venc.value(), rng)
        if self.invert.currentIndex() == 1:
            vel = -vel
        sp = self.ctx.spacing2d(s)
        res = M.pc_flow(vel, roi, sp[0] * sp[1], trig)
        name = self.vessel.currentText()
        self.saved[name] = res["net_ml"]
        rows = [("Area (cm²)", res["area_cm2"]), ("Forward volume (mL)", res["forward_ml"]),
                ("Backward volume (mL)", res["backward_ml"]), ("Net volume (mL)", res["net_ml"]),
                ("Regurgitant fraction (%)", res["regurgitant_fraction"]),
                ("Peak velocity (cm/s)", res["peak_velocity"]),
                ("Cardiac output (L/min)", res["cardiac_output_l_min"]), ("RR (s)", res["rr_s"])]
        qp, qs = self.saved.get("Pulmonary artery (Qp)"), self.saved.get("Aorta (Qs)")
        if qp and qs:
            rows.append(("Qp/Qs", qp / qs))

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.plot(res["t"] * 1000, res["flow"], "o-", color="#4ac")
            ax.axhline(0, color="#666", lw=0.8)
            ax.set_xlabel("time (ms)")
            ax.set_ylabel("flow (mL/s)")
            ax.set_title(f"{name} 유량 곡선", fontsize=9)
        self.ctx.results(f"Phase Contrast: {name}", rows, plot)


# ═══ 스트레인 ═══

class StrainTool(Tool):
    title = "Basic Strain (Feature Tracking)"
    help = ("cine의 한 위치에서 ED 프레임에 LV Endo·Epi 윤곽을 저장하고, 그 영상을 보면서 실행하세요. "
            "OpenCV 광학 흐름으로 윤곽 점을 추적해 원주(GCS)·방사(GRS) 스트레인을 계산합니다.")

    def build(self):
        self.points = ispin(48, 16, 180)
        self.form.addRow("추적 점 수:", self.points)
        self.button("📈 스트레인 계산", self.run)

    def run(self):
        s = self.ctx.series()
        ds = s.slices[self.ctx.slice_index()]
        here = {c["kind"]: c for c in self.ctx.main._contours.for_image(instance_key(ds))}
        if "lv_endo" not in here or "lv_epi" not in here:
            raise ValueError("현재 영상(ED)에 LV Endo와 LV Epi 윤곽을 저장하세요.")
        frames, trig, indices = frames_at_current_position(self.ctx, s, "phase")
        start = indices.index(self.ctx.slice_index())
        order = list(range(start, len(indices))) + list(range(0, start))
        frames, trig = frames[order], trig[order]
        res = C.track_strain(frames, here["lv_endo"]["pts"], here["lv_epi"]["pts"],
                             self.ctx.spacing2d(s), self.points.value())
        rows = {"Peak GCS (%)": res["peak GCS (%)"], "Peak GRS (%)": res["peak GRS (%)"],
                "Frames": len(frames)}

        def plot(fig):
            ax = fig.add_subplot(111)
            x = np.arange(len(frames))
            ax.plot(x, res["GCS"], "o-", color="#ff6b6b", label="GCS (원주)")
            ax.plot(x, res["GRS"], "o-", color="#5aa0ff", label="GRS (방사)")
            ax.axhline(0, color="#666", lw=0.8)
            ax.set_xlabel("frame (ED부터)")
            ax.set_ylabel("strain (%)")
            ax.legend(fontsize=7)
            ax.set_title("Feature tracking strain", fontsize=9)
        self.ctx.results("스트레인", rows, plot,
                         note="기본 광학 흐름 추적입니다. 전용 FT 소프트웨어와 값이 다를 수 있습니다.")


# ═══ 관류 시간-신호 곡선 ═══

class PerfusionCurveTool(Tool):
    title = "관류 시간-신호 곡선 (Perfusion TIC)"
    help = ("동적(first-pass) 관류 시리즈에서 현재 위치의 시간-신호 곡선을 봅니다. "
            "LV 혈액풀 ROI를 먼저 '기준 ROI로 저장'하면 상대 upslope(심근/LV)도 계산합니다.")

    def build(self):
        self.interval = spin(0, 0, 10, 2, suffix=" s")
        self.interval.setToolTip("0이면 DICOM 획득 시각 사용")
        self.form.addRow("프레임 간격:", self.interval)
        self.button("기준 ROI로 저장 (LV 혈액풀)", self.save_reference)
        self.button("📈 곡선 그리기 (현재 ROI)", self.run)
        self.reference = None

    def _curve(self, mask):
        s = self.ctx.series()
        frames, t, _ = frames_at_current_position(self.ctx, s, "time")
        if self.interval.value() > 0 or np.ptp(t) == 0:
            t = np.arange(len(frames)) * (self.interval.value() or 1.0)
        return t - t[0], np.array([f[mask].mean() for f in frames])

    def save_reference(self):
        self.reference = self._curve(self.ctx.roi_mask())
        self.ctx.status("LV 혈액풀 기준 곡선을 저장했습니다.")

    def run(self):
        t, y = self._curve(self.ctx.roi_mask())
        base = y[:3].mean()
        rows = {"Baseline": base, "Peak": float(y.max()), "TTP (s)": float(t[np.argmax(y)]),
                "Upslope": M.upslope(t, y)}
        if self.reference is not None:
            ref_up = M.upslope(*self.reference)
            rows["Relative upslope (myo/LV)"] = rows["Upslope"] / ref_up if ref_up else float("nan")

        def plot(fig):
            ax = fig.add_subplot(111)
            ax.plot(t, y, "o-", color="#fc4", label="ROI")
            if self.reference is not None:
                ax.plot(*self.reference, "o-", color="#f66", label="LV 혈액풀")
            ax.set_xlabel("time (s)")
            ax.set_ylabel("signal")
            ax.legend(fontsize=7)
        self.ctx.results("관류 곡선", rows, plot)


TOOLS = [("contours", ContoursTool), ("bullseye", BullseyeTool), ("mapping", MappingTool),
         ("lge", LGETool), ("flow", FlowTool), ("strain", StrainTool),
         ("perfusion_curve", PerfusionCurveTool)]
