# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Analysis ▸ ACR Phantom QC - ACR 대형 팬텀 7개 검사 자동 분석

Auto Analyze: 영상 찾기 → 검사마다 해당 슬라이스로 가서 ROI·측정선을 주석으로 배치(보이게) →
값 계산 → 결과 표 (적합 초록 / 부적합 빨강). 주석은 ROI Manager에서 보이고 옮길 수 있으며,
옮기면 같은 계산으로 다시 판정한다. 저대조도 스포크 수·분해능은 자동 추정 후 직접 고칠 수 있다.
"""
import datetime
import os

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QPen
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from . import acr, acr_report
from .panel import SeriesPicker, Tool

STEPS = [("find", "0. 영상 찾기 (localizer · T1 · T2)")] + list(acr.TESTS)
GREEN, RED, GREY = "#2e9e4f", "#d64545", "#888888"
PASS_COLOR, FAIL_COLOR = "#3ddc84", "#ff4d4d"     # 영상 위 ROI (적합 / 부적합)
TEST_COLORS = {"geometry": "#4aa3ff", "resolution": "#b18cff", "thickness": "#ff5ad2", "position": "#ff9f40",
               "uniformity": "#50d890", "ghosting": "#f2c200", "low_contrast": "#00c8e0"}
RES_CHOICES = [("-", None), ("1.1 mm", 1.1), ("1.0 mm", 1.0), ("0.9 mm", 0.9)]


def _is_screen_capture(series):
    fmt = getattr(series, "source_format", "")
    return fmt == "image"


def _spacing(series, k, fov_mm):
    """(행, 열) mm. 이미지 파일(간격 정보 없음)은 FOV / 영상 폭"""
    from .. import dicom_info
    ds = series.slices[k]
    sp = dicom_info.pixel_spacing(ds)
    if _is_screen_capture(series) or sp is None:
        cols = int(getattr(ds, "Columns", 0) or np.shape(series.get_pixel_array(k))[1])
        return (fov_mm / cols, fov_mm / cols)
    return sp


def calibrate_capture(series, fov_mm):
    """화면 캡처 시리즈에 FOV 기준 PixelSpacing을 넣어 뷰포트·ROI Manager도 mm로 재게 함"""
    if not _is_screen_capture(series):
        return None
    cols = int(getattr(series.slices[0], "Columns", 0) or 0)
    if not cols:
        return None
    px = round(fov_mm / cols, 6)
    for ds in series.slices:
        ds.PixelSpacing = [px, px]
    return px


def _te(series, k):
    try:
        return float(series.slices[k].EchoTime)
    except (AttributeError, TypeError, ValueError):
        return None


class _AutoPicker(SeriesPicker):
    """기본값 '(자동)' - 현재 보고 있는 시리즈로 바뀌지 않음"""

    def refresh(self, prefer_current=False):
        super().refresh(prefer_current=False)


# ═══ 기준값 편집 (Settings 탭과 도구에서 같이 씀) ═══

CRITERIA_FIELDS = [
    ("loc_nominal", "Localizer 길이 (mm)", 1), ("axial_nominal", "축상 지름 (mm)", 1),
    ("geo_tol", "기하 허용 ± (mm)", 1), ("thk_nominal", "절편 두께 (mm)", 2),
    ("thk_tol", "두께 허용 ± (mm)", 2), ("pos_max", "절편 위치 |차이| ≤ (mm)", 1),
    ("piu_min", "PIU ≥ (%)", 1), ("ghost_max", "고스팅 ≤ (%)", 2),
    ("lc_min", "저대조도 스포크 합 ≥", 0), ("res_max", "분해능 ≤ (mm)", 2),
]
REPORT_FIELDS = [("hospital", "병원"), ("unit", "호기"), ("scanner", "장비명"),
                 ("field", "자장 세기"), ("coil", "코일"), ("tester", "검사자")]


class ACRCriteriaWidget(QWidget):
    def __init__(self, app_settings, parent=None):
        super().__init__(parent)
        self._settings = app_settings
        layout = QVBoxLayout(self)
        presets = QHBoxLayout()
        presets.addWidget(QLabel("기본값:"))
        for name in acr.PRESETS:
            b = QPushButton(f"{name} ACR")
            b.clicked.connect(lambda _=False, n=name: self.apply_preset(n))
            presets.addWidget(b)
        presets.addStretch()
        layout.addLayout(presets)
        crit = QGroupBox("판정 기준")
        form = QFormLayout(crit)
        self.field = QComboBox()
        self.field.addItems(list(acr.PRESETS))
        form.addRow("자장:", self.field)
        self.spins = {}
        for key, label, decimals in CRITERIA_FIELDS:
            w = QDoubleSpinBox()
            w.setDecimals(decimals)
            w.setRange(0, 1000)
            w.setSingleStep(0.1 if decimals else 1)
            self.spins[key] = w
            form.addRow(label + ":", w)
        layout.addWidget(crit)
        rep = QGroupBox("보고서 머리글 · 이미지 파일")
        rform = QFormLayout(rep)
        self.report = {}
        for key, label in REPORT_FIELDS:
            self.report[key] = QLineEdit()
            rform.addRow(label + ":", self.report[key])
        self.fov = QDoubleSpinBox()
        self.fov.setRange(50, 600)
        self.fov.setSuffix(" mm")
        self.fov.setToolTip("DICOM이 아닌 화면 캡처(JPEG·PNG)는 픽셀 크기를 FOV ÷ 영상 폭으로 가정")
        rform.addRow("이미지 FOV:", self.fov)
        layout.addWidget(rep)
        layout.addStretch()
        self.load()

    def load(self):
        c = self._settings.acr_criteria()
        self.field.setCurrentText(c.get("field", "3T"))
        for key, w in self.spins.items():
            w.setValue(float(c[key]))
        for key, value in self._settings.acr_report_info().items():
            self.report[key].setText(value)
        self.fov.setValue(self._settings.acr_fov())

    def apply_preset(self, name):
        c = acr.criteria_with(acr.PRESETS[name])
        self.field.setCurrentText(name)
        for key, w in self.spins.items():
            w.setValue(float(c[key]))

    def values(self):
        out = {key: w.value() for key, w in self.spins.items()}
        out["lc_min"] = int(out["lc_min"])
        out["field"] = self.field.currentText()
        return out

    def save(self):
        self._settings.set_acr_criteria(self.values())
        self._settings.set_acr_report_info({k: w.text().strip() for k, w in self.report.items()})
        self._settings.set_acr_fov(self.fov.value())


class CriteriaDialog(QDialog):
    def __init__(self, app_settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ACR Phantom QC 기준값")
        layout = QVBoxLayout(self)
        self.editor = ACRCriteriaWidget(app_settings)
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(lambda: (self.editor.save(), self.accept()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ═══ 보고서 내보내기 / 추세 ═══

class ExportDialog(QDialog):
    def __init__(self, info, default_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ACR QC 보고서 내보내기")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self.fields = {}
        for key, label in [("date", "검사일 (YYYY-MM-DD)")] + REPORT_FIELDS:
            self.fields[key] = QLineEdit(info.get(key, ""))
            form.addRow(label + ":", self.fields[key])
        self.format = QComboBox()
        for key, text in (("pdf", "PDF (.pdf)"), ("docx", "Word (.docx)"), ("xlsx", "Excel (.xlsx)")):
            self.format.addItem(text, key)
        form.addRow("형식:", self.format)
        row = QHBoxLayout()
        stem = f"ACR_QC_{info.get('date') or datetime.date.today().isoformat()}"
        self.path = QLineEdit(os.path.join(default_dir, stem + ".pdf"))
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("저장:", row)
        self.snapshots = QCheckBox("ROI를 그린 영상 캡처 넣기 (PDF · Word)")
        self.snapshots.setChecked(True)
        form.addRow(self.snapshots)
        self.procedure = QCheckBox("측정 과정 단계별 영상 넣기 (두께 · 균일도 · 고스팅, W/L 조절 화면)")
        self.procedure.setChecked(True)
        form.addRow(self.procedure)
        self.evidence = QCheckBox("증빙 영상 44장 넣기 (콘솔 수동 캡처와 1:1, 날짜·장비·검사자 표시)")
        self.evidence.setChecked(True)
        form.addRow(self.evidence)
        self.evidence_files = QCheckBox("증빙 영상을 보고서 옆 폴더에도 JPG로 저장")
        self.evidence_files.setChecked(True)
        form.addRow(self.evidence_files)
        self.history = QCheckBox("추세 기록에도 저장")
        self.history.setChecked(True)
        form.addRow(self.history)
        self.format.currentIndexChanged.connect(self._ext)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _ext(self):
        root, _ = os.path.splitext(self.path.text())
        self.path.setText(root + "." + self.format.currentData())

    def _browse(self):
        ext = self.format.currentData()
        path, _ = QFileDialog.getSaveFileName(self, "보고서 저장", self.path.text(), f"*.{ext}")
        if path:
            self.path.setText(path)

    def info(self):
        return {k: w.text().strip() for k, w in self.fields.items()}


class TrendDialog(QDialog):
    def __init__(self, criteria, parent=None, history_path=None):
        super().__init__(parent)
        self.setWindowTitle("ACR QC 추세")
        self.resize(820, 620)
        self.criteria = criteria
        self.path = history_path
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.metric = QComboBox()
        for key, label, *_rest in acr_report.TREND_METRICS:
            self.metric.addItem(label, key)
        self.metric.currentIndexChanged.connect(self.redraw)
        top.addWidget(QLabel("항목:"))
        top.addWidget(self.metric, 1)
        png = QPushButton("PNG 저장")
        png.clicked.connect(self.save_png)
        top.addWidget(png)
        layout.addLayout(top)
        from ..analysis import configure_matplotlib
        configure_matplotlib()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(7, 3.6), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout.addWidget(self.canvas, 3)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["검사일", "호기", "장비", "종합", "저장 시각"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table, 2)
        bottom = QHBoxLayout()
        delete = QPushButton("선택 기록 삭제")
        delete.clicked.connect(self.delete_selected)
        bottom.addWidget(delete)
        bottom.addStretch()
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.reload()

    def reload(self):
        self.history = acr_report.load_history(self.path)
        self.table.setRowCount(len(self.history))
        for r, rec in enumerate(self.history):
            ok = rec.get("overall")
            for c, v in enumerate((rec.get("date", ""), rec.get("unit", ""), rec.get("scanner", ""),
                                   acr_report.JUDGE.get(ok, "-"), rec.get("saved", ""))):
                item = QTableWidgetItem(str(v))
                if c == 3 and ok is not None:
                    item.setForeground(QBrush(QColor(GREEN if ok else RED)))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.redraw()

    def redraw(self):
        self.figure.clear()
        acr_report.plot_trend(self.figure, self.history, self.metric.currentData(), self.criteria)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows or QMessageBox.question(self, "ACR QC 추세", f"기록 {len(rows)}개를 지울까요?") \
                != QMessageBox.Yes:
            return
        for r in rows:
            rec = self.history[r]
            acr_report.delete_record(rec.get("date"), rec.get("unit"), rec.get("scanner"), self.path)
        self.reload()

    def save_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "추세 그래프", "acr_trend.png", "PNG (*.png)")
        if path:
            self.figure.savefig(path, dpi=150)


# ═══ 도구 ═══

class ACRTool(Tool):
    title = "ACR Phantom QC"
    help = ("ACR 대형 팬텀 7개 검사를 자동 분석합니다 (기준: Settings → ACR QC, 기본 3.0T). "
            "ROI는 영상에 주석으로 놓이며 ROI Manager에서 보고 옮길 수 있습니다. 옮기면 다시 계산됩니다. "
            "JPEG 등 화면 캡처는 FOV로 픽셀 크기를 가정합니다.")

    def build(self):
        self.sets = None           # {"LOC": (series, k)|None, "T1": [(series, k)]*11, "T2": ...}
        self.roles = {}            # "T1|ACR PIU large" → (series_uid, k, ann_id)
        self.lc_detail = {}        # seq → [low_contrast 결과 4개]
        self.res_detail = {}       # seq → resolution 결과
        self.markers = {}          # 영상 키 → [(종류, ...)] (원판·구멍 배열 표시)
        self.values, self.rows = {}, []
        self.step_errors = {}
        self.site_sets = []        # 사이트 시퀀스 (ACR T1·T2 외 11장 세트) - 증빙 영상용
        self._imgs = {}
        self._calibrated = {}      # 화면 캡처 시리즈 uid → 넣은 픽셀 크기
        self._busy = False
        self._recompute_timer = QTimer(self)
        self._recompute_timer.setSingleShot(True)
        self._recompute_timer.setInterval(350)
        self._recompute_timer.timeout.connect(self.recompute)
        self._pickers_seq = {}
        for seq, label in (("T1", "T1 시리즈:"), ("T2", "T2 시리즈:"), ("LOC", "Localizer:")):
            row = QHBoxLayout()
            picker = _AutoPicker(self.ctx, allow_none=True, none_text="(자동)")
            start = QSpinBox()
            start.setRange(1, 9999)
            start.setPrefix("첫 영상 ")
            row.addWidget(picker, 1)
            row.addWidget(start)
            stride = None
            if seq != "LOC":
                stride = QSpinBox()
                stride.setRange(1, 4)
                stride.setPrefix("간격 ")
                row.addWidget(stride)
            self._pickers.append(picker)
            self._pickers_seq[seq] = (picker, start, stride)
            self.form.addRow(label, row)
        self.fov = QDoubleSpinBox()
        self.fov.setRange(50, 600)
        self.fov.setSuffix(" mm")
        self.fov.setValue(self.ctx.main._app_settings.acr_fov())
        self.fov.setToolTip("화면 캡처(JPEG·PNG)만: 픽셀 크기 = FOV ÷ 영상 폭")
        self.fov.valueChanged.connect(lambda _v: (self._imgs.clear(), self._calibrated.clear()))
        self.form.addRow("이미지 FOV:", self.fov)

        self.button("🔍 영상 자동 찾기", self.find_images,
                    "불러온 영상에서 localizer, T1 slice 1–11, T2 slice 1–11을 찾아 위 칸을 채움")
        self.go = self.button("▶ Auto Analyze (7개 검사)", self.auto_analyze)
        self.button("↻ 수동 수정 후 다시 계산", self.recompute,
                    "ROI·측정선을 옮기거나 스포크 수를 고친 뒤 판정 다시 계산")
        self.auto = QCheckBox("ROI를 옮기면 자동으로 다시 계산")
        self.auto.setChecked(True)
        self.buttons.addWidget(self.auto)
        self.show_markers = QCheckBox("저대조도 원판 · 분해능 배열 표시")
        self.show_markers.setChecked(True)
        self.show_markers.toggled.connect(lambda _on: self._repaint())
        self.buttons.addWidget(self.show_markers)

        self.steps = QListWidget()
        self.steps.setMaximumHeight(150)
        for key, text in STEPS:
            item = QListWidgetItem("○ " + text)
            item.setData(Qt.UserRole, key)
            self.steps.addItem(item)
        self.steps.itemDoubleClicked.connect(self._go_to_step)
        self.buttons.addWidget(QLabel("진행 (두 번 클릭하면 해당 영상으로):"))
        self.buttons.addWidget(self.steps)

        manual = QGroupBox("사용자 확인 · 수정")
        grid = QGridLayout(manual)
        grid.addWidget(QLabel("저대조도 스포크"), 0, 0)
        for c, s in enumerate(("S8", "S9", "S10", "S11")):
            grid.addWidget(QLabel(s), 0, c + 1, alignment=Qt.AlignCenter)
        grid.addWidget(QLabel("합계"), 0, 5)
        self.lc_spins, self.lc_total = {}, {}
        self.res_combos = {}
        for r, seq in enumerate(("T1", "T2"), start=1):
            grid.addWidget(QLabel(seq), r, 0)
            spins = []
            for c in range(4):
                sp = QSpinBox()
                sp.setRange(0, 10)
                sp.valueChanged.connect(self._manual_changed)
                spins.append(sp)
                grid.addWidget(sp, r, c + 1)
            self.lc_spins[seq] = spins
            self.lc_total[seq] = QLabel("-")
            grid.addWidget(self.lc_total[seq], r, 5)
        grid.addWidget(QLabel("분해능 (UL / LR)"), 3, 0, 1, 2)
        for r, seq in enumerate(("T1", "T2"), start=4):
            grid.addWidget(QLabel(seq), r, 0)
            combos = []
            for c in range(2):
                cb = QComboBox()
                for text, value in RES_CHOICES:
                    cb.addItem(text, value)
                cb.currentIndexChanged.connect(self._manual_changed)
                combos.append(cb)
                grid.addWidget(cb, r, 1 + 2 * c, 1, 2)
            self.res_combos[seq] = combos
        self.buttons.addWidget(manual)
        self.buttons.addWidget(QLabel("ACR 7개 검사 요약 (클릭하면 해당 영상으로):"))
        self.dashboard = QTableWidget(len(acr.TESTS), 4)
        self.dashboard.setHorizontalHeaderLabels(["검사", "T1", "T2", "기준"])
        self.dashboard.verticalHeader().setVisible(False)
        self.dashboard.setEditTriggers(QTableWidget.NoEditTriggers)
        self.dashboard.setSelectionMode(QTableWidget.NoSelection)
        self.dashboard.setWordWrap(False)
        for r, (_key, name) in enumerate(acr.TESTS):
            item = QTableWidgetItem(name)
            item.setForeground(QBrush(QColor(TEST_COLORS[_key])))
            self.dashboard.setItem(r, 0, item)
        self.dashboard.setMinimumHeight(self.dashboard.verticalHeader().defaultSectionSize() * 8 + 6)
        self.dashboard.cellClicked.connect(self._dashboard_clicked)
        self.dashboard.resizeColumnsToContents()
        self.buttons.addWidget(self.dashboard)
        self.show_overlay = QCheckBox("영상 위에 ACR 검사 항목 · 판정 표시")
        self.show_overlay.setChecked(True)
        self.show_overlay.toggled.connect(lambda _on: self._repaint())
        self.buttons.addWidget(self.show_overlay)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-weight: bold; padding: 4px;")
        self.buttons.addWidget(self.summary)
        self.button("🖼 증빙 영상 44장 보기…", self.show_evidence,
                    "콘솔 수동 캡처와 같은 44장(+ 분해능 2장)을 만들어 휠 · 화살표로 넘겨 봄")
        self.button("📄 Export Report… (PDF · Word · Excel)", self.export_report)
        self.button("💾 추세 기록에 저장", self.save_history)
        self.button("📈 추세 (날짜별 그래프)…", self.show_trend)
        self.button("⚙ 기준값…", self.edit_criteria)
        self.button("🗑 ACR ROI 지우기", self.clear_annotations)
        store = self.ctx.main._annotation_store
        store.changed.connect(self._store_changed)
        _TOOLS.append(self)

    # ─── 영상 ───
    def criteria(self):
        return self.ctx.main._app_settings.acr_criteria()

    def img(self, series, k):
        key = (series.series_uid, k)
        img = self._imgs.get(key)
        if img is None:
            if _is_screen_capture(series) and series.series_uid not in self._calibrated:
                self._calibrated[series.series_uid] = calibrate_capture(series, self.fov.value())
            arr = np.asarray(series.get_pixel_array(k), dtype=float)
            if arr.ndim == 3:
                arr = arr.mean(-1)
            capture = _is_screen_capture(series) and arr.max() <= 255
            img = acr.Img(arr, _spacing(series, k, self.fov.value()), ref=(series, k),
                          screen_capture=capture, te=_te(series, k))
            self._imgs[key] = img
        return img

    def _candidate_series(self):
        out = []
        for s in self.ctx.series_list():
            if s.num_slices < 1 or getattr(s, "is_derived", False):
                continue
            try:
                shape = np.shape(s.get_pixel_array(0))
            except Exception:  # noqa: BLE001 - 읽을 수 없는 시리즈는 건너뜀
                continue
            if len(shape) < 2 or min(shape[:2]) < 96 or s.num_slices > 600:
                continue
            out.append(s)
        return out

    def _set_step(self, key, state, detail=""):
        for i in range(self.steps.count()):
            item = self.steps.item(i)
            if item.data(Qt.UserRole) == key:
                text = dict(STEPS)[key]
                mark = {"run": "▶", "ok": "✓", "fail": "✗", "wait": "○", "info": "•"}[state]
                item.setText(f"{mark} {text}" + (f" — {detail}" if detail else ""))
                item.setForeground(QBrush(QColor({"ok": GREEN, "fail": RED, "run": "#3b82f6"}
                                                 .get(state, GREY))))
                self.steps.scrollToItem(item)

    # ─── 1. 영상 찾기 ───
    def find_images(self, then=None):
        if self._busy:
            return
        series_list = self._candidate_series()
        if not series_list:
            raise ValueError("먼저 ACR 팬텀 영상(DICOM 또는 이미지 폴더)을 여세요.")
        self._busy = True
        self._set_step("find", "run")
        refs = [(s, k) for s in series_list for k in range(s.num_slices)]

        def task(report, cancelled):
            images = []
            for i, (s, k) in enumerate(refs):
                images.append(self.img(s, k))
                if i % 10 == 0:
                    report(f"영상 읽는 중 {i + 1}/{len(refs)}", (i + 1) / len(refs) / 2)
            return acr.find_sets(images, progress=lambda i, n: report(
                f"팬텀 슬라이스 분류 {i}/{n}", 0.5 + i / n / 2) if i % 10 == 0 else None)

        def done(found):
            self._busy = False
            self.sets = {"LOC": found["LOC"].ref if found["LOC"] is not None else None,
                         "T1": [img.ref for img in found["T1"]],
                         "T2": [img.ref for img in found["T2"]] if found["T2"] else None}
            self.site_sets = [[img.ref for img in imgs] for imgs in found.get("extra", [])[:2]]
            self._fill_pickers()
            detail = "T1 " + self._describe(self.sets["T1"]) + (
                ", T2 " + self._describe(self.sets["T2"]) if self.sets["T2"] else ", T2 없음")
            self._set_step("find", "ok", detail)
            if then:
                then()

        def failed(message):
            self._busy = False
            self._set_step("find", "fail", message)

        self._run(task, done, failed, "ACR 팬텀 영상 찾는 중…")

    def _run(self, task, done, failed, text):
        from ..ai.panel import TaskWorker
        dock = self.ctx.dock
        worker = TaskWorker(task, dock)
        worker.progress.connect(lambda t, f: dock.task_label.setText(t))
        worker.succeeded.connect(lambda r: (dock._finish(), done(r)))
        worker.failed.connect(lambda m: (dock._finish(), failed(m)))
        worker.finished.connect(worker.deleteLater)
        dock._worker = worker
        dock.progress.setVisible(True)
        dock.task_label.setText(text)
        worker.start()

    @staticmethod
    def _describe(refs):
        s, k = refs[0]
        stride = refs[1][1] - refs[0][1] if len(refs) > 1 and refs[1][0] is s else 1
        return f"'{s.description}' {k + 1}번째부터" + (f" {stride}장 간격" if stride != 1 else "")

    def _fill_pickers(self):
        for seq in ("T1", "T2", "LOC"):
            picker, start, stride = self._pickers_seq[seq]
            picker.refresh()
            value = self.sets.get(seq)
            if not value:
                picker.setCurrentIndex(0)
                continue
            refs = [value] if seq == "LOC" else value
            s, k = refs[0]
            picker.setCurrentIndex(max(0, picker.findData(s.series_uid)))
            start.setValue(k + 1)
            if stride is not None:
                stride.setValue(refs[1][1] - refs[0][1] if len(refs) > 1 else 1)

    def _sets_from_pickers(self):
        """위 칸에서 고른 값 (자동 찾기 결과를 사용자가 고친 경우 포함)"""
        out = {}
        for seq in ("T1", "T2", "LOC"):
            picker, start, stride = self._pickers_seq[seq]
            s = picker.series(required=False)
            if s is None:
                out[seq] = None
                continue
            k0 = start.value() - 1
            if seq == "LOC":
                out[seq] = (s, min(k0, s.num_slices - 1))
                continue
            step = stride.value()
            ks = [k0 + step * n for n in range(11)]
            if ks[-1] >= s.num_slices:
                raise ValueError(f"{seq}: '{s.description}'에 {ks[-1] + 1}번째 영상이 없습니다.")
            out[seq] = [(s, k) for k in ks]
        if not out["T1"]:
            raise ValueError("T1 시리즈를 고르거나 '영상 자동 찾기'를 하세요.")
        return out

    # ─── 2. 자동 분석 ───
    def auto_analyze(self):
        if self._busy:
            return
        if self.sets is None and all(p[0].currentData() is None for p in self._pickers_seq.values()):
            self.find_images(then=self._analyze)
            return
        self._analyze()

    def _analyze(self):
        try:
            self.sets = self._sets_from_pickers()
        except ValueError as e:
            QMessageBox.information(self, self.title, str(e))
            return
        self.clear_annotations(confirm=False)
        self.lc_detail, self.res_detail, self.markers = {}, {}, {}
        for key, _t in acr.TESTS:
            self._set_step(key, "wait")
        self._queue = list(self._plan())
        self.step_errors = {}
        self._busy = True
        self.go.setEnabled(False)
        QTimer.singleShot(50, self._next_step)

    def _plan(self):
        s = self.sets
        seqs = [q for q in ("T1", "T2") if s.get(q)]
        yield "geometry", lambda: self._place("geometry", "T1", [
            *([(s["LOC"], lambda im: acr.place_geometry(im, "LOC"))] if s.get("LOC") else []),
            (s["T1"][0], lambda im: acr.place_geometry(im, "S1")),
            (s["T1"][4], lambda im: acr.place_geometry(im, "S5"))])
        yield "resolution", lambda: [self._resolution(q) for q in seqs]
        yield "thickness", lambda: [self._place("thickness", q, [(s[q][0], acr.place_thickness)])
                                    for q in seqs]
        yield "position", lambda: [self._place("position", q, [
            (s[q][0], lambda im: acr.place_position(im, "S1")),
            (s[q][10], lambda im: acr.place_position(im, "S11"))]) for q in seqs]
        yield "uniformity", lambda: [self._place("uniformity", q, [(s[q][6], acr.place_uniformity)])
                                     for q in seqs]
        yield "ghosting", lambda: [self._place("ghosting", q, [(s[q][6], acr.place_ghosting)])
                                   for q in seqs]
        yield "low_contrast", lambda: [self._low_contrast(q) for q in seqs]

    def _next_step(self):
        if not self._queue:
            self._busy = False
            self.go.setEnabled(True)
            self.recompute()
            return
        key, fn = self._queue.pop(0)
        self._set_step(key, "run")
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            fn()
            self._set_step(key, "info", "배치 완료")
        except Exception as e:  # noqa: BLE001 - 한 검사가 실패해도 나머지는 진행
            self.step_errors[key] = str(e)
            self._set_step(key, "fail", str(e))
        QTimer.singleShot(250, self._next_step)   # 한 단계씩 보이게

    def _show(self, ref):
        series, k = ref
        main = self.ctx.main
        if main._current_series is not series:
            main._select_series(series)
        main._slice_slider.setValue(k)
        main._viewport.go_to_slice(k, user=False)

    def _place(self, test, seq, jobs):
        from ..annotations import image_key
        from ..roi_tools import recompute
        store = self.ctx.main._annotation_store
        for ref, fn in jobs:
            series, k = ref
            img = self.img(series, k)
            self._show(ref)
            for ann in fn(img):
                role = f"{seq}|{ann['name']}"
                ann["name"] = ann["name"].replace("ACR ", f"ACR {seq} ", 1) \
                    if not ann["name"].startswith("ACR Geo") else ann["name"]
                ann["acr_role"] = role
                recompute(ann, img.a, img.px)
                store.add(image_key(series, k), ann)
                self.roles[role] = (series.series_uid, k, ann["id"])
            self._repaint()

    def _resolution(self, seq):
        from ..annotations import image_key
        ref = self.sets[seq][0]
        img = self.img(*ref)
        self._show(ref)
        res = acr.resolution(img)
        self.res_detail[seq] = res
        marks = [("box", *res["box"], "#9aa", "")]
        for arr in res["arrays"]:
            for key in ("ul", "lr"):
                marks.append(("box", *arr[f"{key}_box"], GREEN if arr[key] else RED,
                              f"{arr['size']:.1f} {key.upper()}"))
        self.markers.setdefault(image_key(*ref), []).extend(marks)
        combos = self.res_combos[seq]
        for cb, value in zip(combos, (res["ul"], res["lr"])):
            cb.blockSignals(True)
            cb.setCurrentIndex(max(0, cb.findData(value)) if value is not None else 0)
            cb.blockSignals(False)
        if res.get("note"):
            raise ValueError(res["note"])

    def _low_contrast(self, seq):
        from ..annotations import image_key
        refs = self.sets[seq][7:11]
        prior = None
        if seq == "T2" and "T1" in self.lc_detail:   # 같은 위치 → T1에서 찾은 회전으로 시작
            prior = self.lc_detail["T1"][3]["rotation"]
        imgs = [self.img(*r) for r in refs]
        self._show(refs[3])
        out = acr.low_contrast_set(imgs, prior)
        self.lc_detail[seq] = out
        for ref, o in zip(refs, out):
            self.markers[image_key(*ref)] = [
                ("disk", d["x"], d["y"], d["r"], GREEN if d["visible"] else RED) for d in o["disks"]]
        for sp, o in zip(self.lc_spins[seq], out):
            sp.blockSignals(True)
            sp.setValue(o["spokes"])
            sp.blockSignals(False)
        self._repaint()

    # ─── 3. 계산 · 판정 ───
    def _roles_now(self):
        """주석 (사용자가 옮겼으면 옮긴 모양) → {역할: (Img, 주석)}"""
        store = self.ctx.main._annotation_store
        out = {}
        for role, (uid, k, ann_id) in list(self.roles.items()):
            _key, ann = store.find(ann_id)
            series = self.ctx.by_uid(uid)
            if ann is None or series is None:
                continue
            out[role] = (self.img(series, k), ann)
        return out

    def _manual_values(self):
        lc, res = {}, {}
        for seq in ("T1", "T2"):
            if self.sets and self.sets.get(seq) and seq in self.lc_detail:
                lc[seq] = [sp.value() for sp in self.lc_spins[seq]]
            if self.sets and self.sets.get(seq) and seq in self.res_detail:
                res[seq] = tuple(cb.currentData() for cb in self.res_combos[seq])
        for seq, spins in self.lc_spins.items():
            self.lc_total[seq].setText(str(sum(sp.value() for sp in spins)) if seq in lc else "-")
        return lc, res

    def recompute(self):
        if self._busy or not self.roles and not self.lc_detail:
            return
        lc, res = self._manual_values()
        self.values = acr.compute(self._roles_now(), None, lc, res)
        self.rows = acr.evaluate(self.values, self.criteria())
        for key, _t in acr.TESTS:
            judged = [r[4] for r in self.rows if r[0] == key and r[4] is not None]
            error = getattr(self, "step_errors", {}).get(key)
            if any(r[0] == key for r in self.rows):
                ok = all(judged) if judged else None
                detail = "; ".join(f"{r[1]} {r[2]}" for r in self.rows if r[0] == key)
                if error:
                    detail += f" (일부 자동 배치 실패: {error})"
                self._set_step(key, "ok" if ok and not error else "fail" if ok is False or error
                               else "info", detail)
            elif error:
                self._set_step(key, "fail", error)
        self._show_results()
        self._color_rois()
        self._fill_dashboard()
        self._repaint()

    def verdict(self, test, seq):
        """(test, seq) 판정 True/False/None"""
        for t, s_, _m, _c, ok in self.rows:
            if t == test and s_ == seq:
                return ok
        return None

    def _color_rois(self):
        """ROI · 측정선 색 = 판정 (적합 초록 / 부적합 빨강). 원래 색은 acr_color에 (보고서 그림용)"""
        store = self.ctx.main._annotation_store
        with store.group():   # 되돌리기 한 번으로
            for role, (_uid, _k, ann_id) in list(self.roles.items()):
                _key, ann = store.find(ann_id)
                if ann is None:
                    continue
                ok = self.verdict(ann.get("acr"), role.split("|")[0])
                original = ann.get("acr_color") or ann.get("color")
                color = original if ok is None else PASS_COLOR if ok else FAIL_COLOR
                if ann.get("color") != color or ann.get("acr_color") != original:
                    store.update(ann_id, color=color, acr_color=original)

    def _fill_dashboard(self):
        crit = {r[0]: r[3] for r in self.rows}
        for r, (key, _name) in enumerate(acr.TESTS):
            for col, seq in ((1, "T1"), (2, "T2")):
                row = next((x for x in self.rows if x[0] == key and x[1] == seq), None)
                if row is None:
                    item = QTableWidgetItem("-")
                else:
                    mark = {True: "✓ ", False: "✗ ", None: "· "}[row[4]]
                    item = QTableWidgetItem(mark + _short(key, row[2]))
                    item.setToolTip(f"{acr.TEST_NAMES[key]} {seq}: {row[2]} (기준 {row[3]})")
                    if row[4] is not None:
                        item.setForeground(QBrush(QColor(PASS_COLOR if row[4] else FAIL_COLOR)))
                self.dashboard.setItem(r, col, item)
            self.dashboard.setItem(r, 3, QTableWidgetItem(crit.get(key, "")))
        self.dashboard.resizeColumnsToContents()

    def _dashboard_clicked(self, row, col):
        key = acr.TESTS[row][0]
        seq = "T2" if col == 2 else "T1"
        ref = self.test_ref(key, seq) or self.test_ref(key, "T1")
        if ref:
            self._show(ref)

    def test_ref(self, key, seq):
        """검사를 대표하는 영상 (series, k)"""
        if not self.sets or not self.sets.get(seq):
            return None
        if key == "geometry":
            return self.sets["T1"][4]
        n = {"resolution": 0, "thickness": 0, "position": 0, "uniformity": 6, "ghosting": 6,
             "low_contrast": 7}[key]
        return self.sets[seq][n]

    def slice_tests(self, series, k):
        """이 영상이 쓰이는 ACR 검사 → (시퀀스 이름, slice 번호, [(test, 설명)])"""
        if not self.sets:
            return None
        uid = series.series_uid
        loc = self.sets.get("LOC")
        if loc and loc[0].series_uid == uid and loc[1] == k:
            return "Localizer", None, [("geometry", "Localizer 길이")]
        groups = [(seq, self.sets.get(seq)) for seq in ("T1", "T2")]
        groups += [(f"사이트 {i + 1}", refs) for i, refs in enumerate(getattr(self, "site_sets", []) or [])]
        for seq, refs in groups:
            for n, (s, kk) in enumerate(refs or []):
                if s.series_uid != uid or kk != k:
                    continue
                site = seq.startswith("사이트")
                tests = []
                if n == 0:
                    if seq == "T1":
                        tests.append(("geometry", "slice 1 직경"))
                    tests += [("thickness", "경사판 길이"), ("position", "쐐기 S1")]
                    if not site:
                        tests.append(("resolution", "구멍 배열"))
                elif n == 4 and seq == "T1":
                    tests.append(("geometry", "slice 5 직경 · 대각선"))
                elif n == 6 and not site:
                    tests += [("uniformity", "PIU"), ("ghosting", "배경 ROI")]
                if n >= 7:
                    tests.append(("low_contrast", f"slice {n + 1}"))
                if n == 10 and not site:
                    tests.append(("position", "쐐기 S11"))
                if site:
                    tests = [(t, d + " (참고)") for t, d in tests if t in ("thickness", "low_contrast")]
                return seq, n + 1, tests
        return None

    def _show_results(self):
        header = ["검사", "시퀀스", "측정값", "기준", "판정"]
        data = [[acr.TEST_NAMES[t], s, m, c, acr_report.JUDGE[ok]] for t, s, m, c, ok in self.rows]
        overall = acr.overall(self.rows)
        data.append(["종합 판정", "", "", "", acr_report.JUDGE[overall]])
        note = ("화면 캡처(JPEG) 분석: 픽셀 크기는 FOV 가정, 신호는 표시 값 (L=W/2이면 비율은 유효)."
                if self._any_capture() else "")
        self.ctx.results("ACR Phantom QC — " + ("적합" if overall else "부적합" if overall is False
                                                  else "판정 없음"),
                         {"header": header, "rows": data}, note=note)
        table = self.ctx.dock.result.table
        for r, row in enumerate(data):
            ok = {"적합": True, "부적합": False}.get(row[4])
            if ok is None:
                continue
            for c in range(len(header)):
                item = table.item(r, c)
                if item is not None:
                    item.setBackground(QBrush(QColor("#1f4d2c" if ok else "#5a1f1f")))
                    if c == 4:
                        item.setForeground(QBrush(QColor("#7ee29a" if ok else "#ff8a8a")))
        table.resizeColumnsToContents()
        self.summary.setText({True: "✓ 종합: 적합", False: "✗ 종합: 부적합 (빨간 항목 확인)",
                              None: ""}[overall])
        self.summary.setStyleSheet(f"font-weight: bold; padding: 4px; color: "
                                   f"{GREEN if overall else RED if overall is False else GREY};")

    def _any_capture(self):
        return any(_is_screen_capture(s) for s, _k in (self.sets or {}).get("T1") or [])

    def _manual_changed(self, *_):
        if not self._busy:
            self.recompute()

    def _store_changed(self):
        if self.roles and self.auto.isChecked() and not self._busy:
            self._recompute_timer.start()

    # ─── 보조 ───
    def _go_to_step(self, item):
        key = item.data(Qt.UserRole)
        if not self.sets:
            return
        where = {"geometry": ("T1", 4), "resolution": ("T1", 0), "thickness": ("T1", 0),
                 "position": ("T1", 10), "uniformity": ("T1", 6), "ghosting": ("T1", 6),
                 "low_contrast": ("T1", 10)}.get(key)
        if where and self.sets.get(where[0]):
            self._show(self.sets[where[0]][where[1]])

    def _repaint(self):
        for vp in self.ctx.main._all_viewports():
            vp.update()

    def clear_annotations(self, confirm=True):
        store = self.ctx.main._annotation_store
        items = [(key, ann) for key, ann in store.all_items() if ann.get("acr")]
        if confirm and items and QMessageBox.question(
                self, self.title, f"ACR 자동 ROI·측정선 {len(items)}개를 지울까요?") != QMessageBox.Yes:
            return
        for key, ann in items:
            store.remove(key, ann["id"])
        self.roles = {}
        self.markers = {}
        self._repaint()

    def report_info(self):
        info = dict(self.ctx.main._app_settings.acr_report_info())
        info["date"] = self._scan_date()
        c = self.criteria()
        if not info.get("field"):
            info["field"] = c.get("field", "")
        if self.sets and self.sets.get("T1"):
            ds = self.sets["T1"][0][0].slices[0]
            model = " ".join(str(getattr(ds, k, "") or "") for k in ("Manufacturer", "ManufacturerModelName")).strip()
            if model and not _is_screen_capture(self.sets["T1"][0][0]):
                info["scanner"] = model
            station = str(getattr(ds, "StationName", "") or "")
            if station and not info.get("unit"):
                info["unit"] = station
        return info

    def _scan_date(self):
        if not self.sets or not self.sets.get("T1"):
            return datetime.date.today().isoformat()
        series = self.sets["T1"][0][0]
        raw = str(getattr(series.slices[0], "StudyDate", "") or "")
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        path = getattr(series, "source_path", "") or ""
        return date_from_path(path) or datetime.date.today().isoformat()

    def _snapshots(self, folder):
        """주요 영상 4장에 ROI·표시를 그린 PNG → [(제목, 경로)]"""
        from ..annotations import image_key
        out = []
        if not self.sets or not self.sets.get("T1"):
            return out
        store = self.ctx.main._annotation_store
        picks = [("T1 slice 1 (두께·위치·분해능)", self.sets["T1"][0]),
                 ("T1 slice 5 (기하)", self.sets["T1"][4]),
                 ("T1 slice 7 (균일도·고스팅)", self.sets["T1"][6]),
                 ("T1 slice 11 (저대조도)", self.sets["T1"][10])]
        for i, (title, (series, k)) in enumerate(picks):
            key = image_key(series, k)
            path = os.path.join(folder, f"acr_snap_{i}.png")
            render_snapshot(self.img(series, k).a, store.items(key), self.markers.get(key, []), path)
            out.append((title, path))
        return out

    def _procedure(self, folder, images=True):
        """콘솔 수동 절차와 같은 순서의 측정 과정 → [{"title", "images": [(설명, PNG)], "lines": [...]}]"""
        roles = self._roles_now()
        out = []
        for seq in ("T1", "T2"):
            for fn in (_thickness_steps, _uniformity_steps, _ghosting_steps):
                try:
                    step = fn(seq, roles, self.values, self.criteria(), folder if images else None)
                except Exception:  # noqa: BLE001 - 과정 그림 하나가 실패해도 보고서는 만듦
                    step = None
                if step:
                    out.append(step)
        return out

    def _evidence(self, folder, info):
        """증빙 영상 44장 (+ 분해능 2장) → [(번호, 검사, 내용, 비고, 경로)]"""
        from PyQt5.QtWidgets import QApplication
        from .acr_evidence import Evidence
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return Evidence(self, info).build(folder, progress=lambda i, n: (
                self.ctx.status(f"증빙 영상 만드는 중 {i}/{n}"), QApplication.processEvents()))
        finally:
            QApplication.restoreOverrideCursor()

    def show_evidence(self):
        if not self.rows:
            QMessageBox.information(self, self.title, "먼저 Auto Analyze를 실행하세요.")
            return
        import tempfile
        folder = tempfile.mkdtemp(prefix="dabbaview-acr-evidence-")
        items = self._evidence(folder, self.report_info())
        EvidenceViewer(items, self).exec_()

    def export_report(self):
        if not self.rows:
            raise ValueError("먼저 Auto Analyze를 실행하세요.")
        import tempfile
        info = self.report_info()
        dialog = ExportDialog(info, os.path.expanduser("~/Documents"), self)
        if dialog.exec_() != QDialog.Accepted:
            return
        info = dialog.info()
        path = dialog.path.text().strip()
        fmt = dialog.format.currentData()
        with tempfile.TemporaryDirectory() as tmp:
            snaps = self._snapshots(tmp) if dialog.snapshots.isChecked() and fmt != "xlsx" else []
            steps = self._procedure(tmp, images=fmt != "xlsx") if dialog.procedure.isChecked() else []
            evidence = []
            if dialog.evidence.isChecked() or dialog.evidence_files.isChecked():
                evidence = self._evidence(os.path.join(tmp, "evidence"), info)
                if dialog.evidence_files.isChecked():
                    folder = os.path.splitext(path)[0] + "_증빙영상"
                    os.makedirs(folder, exist_ok=True)
                    import shutil
                    for item in evidence:
                        shutil.copy2(item[4], os.path.join(folder, os.path.basename(item[4])))
                if not dialog.evidence.isChecked():
                    evidence = []
            acr_report.write_report(fmt, path, info, self.rows, self._any_capture(), snaps, steps, evidence)
        if dialog.history.isChecked():
            acr_report.save_record(acr_report.make_record(info, self.values, self.rows))
        self.ctx.status(f"ACR QC 보고서 저장: {path}")
        box = QMessageBox(self)
        box.setWindowTitle(self.title)
        box.setText(f"보고서를 저장했습니다.\n{path}")
        open_button = box.addButton("열기", QMessageBox.AcceptRole)
        box.addButton("닫기", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is open_button:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.last_report = path

    def save_history(self):
        if not self.rows:
            raise ValueError("먼저 Auto Analyze를 실행하세요.")
        info = self.report_info()
        items = acr_report.save_record(acr_report.make_record(info, self.values, self.rows))
        self.ctx.status(f"ACR QC 기록 저장 ({info['date']}) — 전체 {len(items)}개")

    def show_trend(self):
        TrendDialog(self.criteria(), self).exec_()

    def edit_criteria(self):
        if CriteriaDialog(self.ctx.main._app_settings, self).exec_() == QDialog.Accepted:
            self.fov.setValue(self.ctx.main._app_settings.acr_fov())
            self.recompute()


def _short(key, text):
    """대시보드 칸에 들어갈 짧은 값"""
    if key == "geometry":
        return text.split(",")[0] + " …" if "," in text else text
    return text if len(text) <= 26 else text[:25] + "…"


class EvidenceViewer(QDialog):
    """증빙 영상 44장 (+ 2장): 왼쪽 목록, 오른쪽 영상. 휠 · 화살표 · PageUp/Down으로 넘김"""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        from PyQt5.QtGui import QPixmap
        from PyQt5.QtWidgets import QSplitter
        self._pixmap = QPixmap
        self.items = items
        self.setWindowTitle(f"ACR 증빙 영상 ({len(items)}장)")
        self.resize(1100, 820)
        layout = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        for number, test, desc, _note, _p in items:
            label = f"{number:02d}" if isinstance(number, int) else number
            self.list.addItem(f"{label}  {test} — {desc}")
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(500, 500)
        self.image.setStyleSheet("background: #1b1d22;")
        split.addWidget(self.list)
        split.addWidget(self.image)
        split.setSizes([330, 770])
        layout.addWidget(split, 1)
        self.caption = QLabel("")
        layout.addWidget(self.caption)
        self.list.currentRowChanged.connect(self._show)
        self.list.setCurrentRow(0)

    def _show(self, row):
        if not 0 <= row < len(self.items):
            return
        pm = self._pixmap(self.items[row][4])
        self.image.setPixmap(pm.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.caption.setText(f"{row + 1}/{len(self.items)} · {self.items[row][3]} "
                             "(휠 · ↑↓ · PageUp/Down으로 넘김)")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show(self.list.currentRow())

    def wheelEvent(self, event):
        step = -1 if event.angleDelta().y() > 0 else 1
        self.list.setCurrentRow(min(max(0, self.list.currentRow() + step), len(self.items) - 1))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_PageDown, Qt.Key_Right, Qt.Key_Space):
            self.list.setCurrentRow(min(self.list.currentRow() + 1, len(self.items) - 1))
        elif event.key() in (Qt.Key_PageUp, Qt.Key_Left):
            self.list.setCurrentRow(max(self.list.currentRow() - 1, 0))
        else:
            super().keyPressEvent(event)


def date_from_path(path):
    """'0531_1_20260911' 처럼 앞의 MMDD가 촬영일, 뒤 YYYYMMDD가 저장일이면 촬영일로"""
    import re
    for part in reversed(os.path.normpath(path).split(os.sep)):
        m = re.match(r"^(\d{2})(\d{2})_.*?(20\d{2})\d{4}", part)
        if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
            return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        found = acr.date_from_text(part)
        if found:
            return found
    return ""


def render_snapshot(arr, anns, markers, path, size=360):
    """영상 + ROI·측정선·표시 → PNG (보고서용)"""
    from PIL import Image, ImageDraw
    a = np.asarray(arr, dtype=float)
    lo, hi = np.percentile(a, [1, 99.5])
    g = np.clip((a - lo) / max(1e-6, hi - lo) * 255, 0, 255).astype(np.uint8)
    im = Image.fromarray(g).convert("RGB")
    d = ImageDraw.Draw(im)
    for ann in anns:
        color = ann.get("acr_color") or ann.get("color") or "#ffff00"
        pts = [(x - 0.5, y - 0.5) for x, y in ann["pts"]]
        if ann["type"] == "distance":
            d.line(pts[:2], fill=color, width=1)
        elif ann["type"] == "ellipse":
            (x0, y0), (x1, y1) = pts[:2]
            d.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], outline=color)
        elif ann["type"] == "rect":
            (x0, y0), (x1, y1) = pts[:2]
            d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], outline=color)
    for m in markers:
        if m[0] == "disk":
            _k, x, y, r, color = m
            d.ellipse([x - r, y - r, x + r, y + r], outline=color)
        elif m[0] == "box":
            _k, x0, y0, x1, y1, color, _label = m
            d.rectangle([x0, y0, x1, y1], outline=color)
    im.resize((size, size)).save(path)
    return path


def render_step(arr, anns, path, window=None, crop=None, labels=(), width=420):
    """측정 과정 한 장: window=(L, W) - W ≤ 1이면 콘솔 W 1처럼 L 기준 흑백. crop=(x0, y0, x1, y1)
    labels=[(x, y, 글, 색)] (영상 좌표). → PNG 경로"""
    from PIL import Image, ImageDraw, ImageFont
    a = np.asarray(arr, dtype=float)
    if window is None:
        lo, hi = np.percentile(a, [1, 99.5])
    else:
        level, win = window
        lo, hi = level - max(win, 1e-6) / 2, level + max(win, 1e-6) / 2
    if window is not None and window[1] <= 1:
        g = np.where(a > window[0], 255, 0).astype(np.uint8)
    else:
        g = np.clip((a - lo) / max(1e-6, hi - lo) * 255, 0, 255).astype(np.uint8)
    x0, y0, x1, y1 = crop or (0, 0, a.shape[1], a.shape[0])
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(a.shape[1], int(x1)), min(a.shape[0], int(y1))
    scale = width / (x1 - x0)
    im = Image.fromarray(g[y0:y1, x0:x1]).convert("RGB").resize((width, max(1, int(round((y1 - y0) * scale)))))
    d = ImageDraw.Draw(im)

    def P(x, y):
        return ((x - x0) * scale, (y - y0) * scale)
    for ann in anns:
        color = ann.get("acr_color") or ann.get("color") or "#ffff00"
        pts = [P(x - 0.5, y - 0.5) for x, y in ann["pts"]]
        if ann["type"] == "distance":
            d.line(pts[:2], fill=color, width=3)
        elif ann["type"] in ("ellipse", "rect"):
            (ax, ay), (bx, by) = pts[:2]
            box = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
            (d.ellipse if ann["type"] == "ellipse" else d.rectangle)(box, outline=color, width=3)
    font = None
    from ..library_export import korean_font_path
    fp = korean_font_path()
    if fp:
        try:
            font = ImageFont.truetype(fp, 15)
        except OSError:
            font = None
    for x, y, text, color in labels:
        px_, py_ = P(x, y)
        tw = d.textlength(text, font=font) if font else 7 * len(text)
        px_ = min(max(2, px_ - tw / 2), im.width - tw - 2)
        py_ = min(max(2, py_), im.height - 20)
        d.rectangle([px_ - 3, py_ - 1, px_ + tw + 3, py_ + 18], fill=(0, 0, 0))
        d.text((px_, py_), text, fill=color, font=font)
    im.save(path)
    return path


def _centre(ann):
    (x0, y0), (x1, y1) = ann["pts"][:2]
    return (x0 + x1) / 2, (y0 + y1) / 2


def _judge(ok):
    return {True: "적합", False: "부적합", None: "-"}[ok]


def _thickness_steps(seq, roles, values, c, folder):
    v = values.get(("thickness", seq))
    need = [f"{seq}|ACR ST {n}" for n in ("ROI top", "ROI bottom", "top", "bottom")]
    if not v or "level" not in v or not all(n in roles for n in need):
        return None
    img, rt = roles[need[0]]
    rb, lt, lb = (roles[n][1] for n in need[1:])
    m_t, m_b, level = v["roi_top"], v["roi_bottom"], v["level"]
    ok = abs(v["thickness"] - c["thk_nominal"]) <= c["thk_tol"]
    lines = [f"① slice 1의 위·아래 경사판 가운데에 작은 ROI: 위 {v['roi_top_mm2']:.1f} mm² 평균 {m_t:.2f}, "
             f"아래 {v['roi_bottom_mm2']:.1f} mm² 평균 {m_b:.2f}",
             f"② 기준 레벨 = (위 평균 + 아래 평균) / 4 = ({m_t:.2f} + {m_b:.2f}) / 4 = {level:.2f}  "
             f"(두 평균의 절반) → W 1 / L {level:.2f}",
             f"③ 그 W/L에서 밝게 남은 경사판 길이: 위 {v['top']:.1f} mm, 아래 {v['bottom']:.1f} mm",
             f"④ 두께 = 0.2 × 위 × 아래 / (위 + 아래) = 0.2 × {v['top']:.1f} × {v['bottom']:.1f} / "
             f"({v['top']:.1f} + {v['bottom']:.1f}) = {v['thickness']:.2f} mm",
             f"판정: 기준 {c['thk_nominal']:g} ± {c['thk_tol']:g} mm → {_judge(ok)}"]
    images = []
    if folder:
        fit = img.fit()
        ys = [p[1] for a in (rt, rb, lt, lb) for p in a["pts"]]
        crop = (fit["cx"] - 0.8 * fit["r"], min(ys) - 0.32 * fit["r"],
                fit["cx"] + 0.8 * fit["r"], max(ys) + 0.32 * fit["r"])
        above, below = min(ys) - 0.24 * fit["r"], max(ys) + 0.1 * fit["r"]
        mean2 = (m_t + m_b) / 2
        win = (0.73 * mean2, 0.44 * mean2)   # 콘솔 동영상: 경사판이 보이게 좁힌 W/L (W 188 / L 315 ≈ 평균 430)
        (tx, ty), (bx, by) = _centre(rt), _centre(rb)
        images.append((f"① 경사판 ROI (W {win[1]:.0f} / L {win[0]:.0f})",
                       render_step(img.a, [rt, rb], os.path.join(folder, f"st_{seq}_1.png"), win, crop,
                                   [(tx, above, f"위 평균 {m_t:.1f}", "#ff5ad2"),
                                    (bx, below, f"아래 평균 {m_b:.1f}", "#ff5ad2")])))
        (l1, l2), (b1, b2) = lt["pts"][:2], lb["pts"][:2]
        images.append((f"② W 1 / L {level:.1f} → 경사판 길이",
                       render_step(img.a, [lt, lb], os.path.join(folder, f"st_{seq}_2.png"), (level, 1), crop,
                                   [((l1[0] + l2[0]) / 2, above, f"위 {v['top']:.1f} mm", "#00e5ff"),
                                    ((b1[0] + b2[0]) / 2, below, f"아래 {v['bottom']:.1f} mm", "#00e5ff")])))
    return {"title": f"절편 두께 ({seq}) — {v['thickness']:.2f} mm, {_judge(ok)}", "images": images, "lines": lines}


def _uniformity_steps(seq, roles, values, c, folder):
    v = values.get(("uniformity", seq))
    need = [f"{seq}|ACR PIU {n}" for n in ("large", "min", "max")]
    if not v or not all(n in roles for n in need):
        return None
    img, big = roles[need[0]]
    lo, hi = roles[need[1]][1], roles[need[2]][1]
    m_lo, m_hi, m_big = v["min"], v["max"], v["large"]
    ok = v["piu"] >= c["piu_min"]
    (lx, ly), (hx, hy) = _centre(lo), _centre(hi)
    area_big = acr.roi_area_mm2(img, big)
    area_s = acr.roi_area_mm2(img, lo)
    lines = [f"① slice 7 가운데에 큰 ROI {area_big / 100:.0f} cm² ({area_big:.0f} mm²), 평균 {m_big:.2f}",
             "② W 1로 좁히고 L을 올려 큰 ROI 안에서 가장 어두운 곳만 남김 → 그곳에 1 cm² ROI "
             f"({area_s:.0f} mm², 위치 x {lx:.0f}, y {ly:.0f}): Low = {m_lo:.2f}",
             "③ L을 내려 가장 밝은 곳만 남김 → 그곳에 1 cm² ROI "
             f"(위치 x {hx:.0f}, y {hy:.0f}): High = {m_hi:.2f}",
             "   (자동: 큰 ROI 안 모든 위치의 1 cm² 평균을 계산해 가장 낮은·높은 곳을 고름)",
             f"④ PIU = 100 × (1 - (High - Low) / (High + Low)) = 100 × (1 - ({m_hi:.2f} - {m_lo:.2f}) / "
             f"({m_hi:.2f} + {m_lo:.2f})) = {v['piu']:.1f} %",
             f"판정: 기준 ≥ {c['piu_min']:g} % → {_judge(ok)}"]
    images = []
    if folder:
        fit = img.fit()
        pad = 0.08 * fit["r"]
        (bx0, by0), (bx1, by1) = big["pts"][:2]
        crop = (min(bx0, bx1) - pad, min(by0, by1) - pad, max(bx0, bx1) + pad, max(by0, by1) + pad)
        l_dark = m_lo + 0.3 * (m_big - m_lo)
        l_bright = m_hi - 0.3 * (m_hi - m_big)
        images.append(("① 큰 ROI (기본 W/L)",
                       render_step(img.a, [big], os.path.join(folder, f"piu_{seq}_1.png"), None, crop,
                                   [(_centre(big)[0], _centre(big)[1], f"평균 {m_big:.1f}", "#50ff78")], 300)))
        images.append((f"② W 1 / L {l_dark:.1f}: 가장 어두운 곳",
                       render_step(img.a, [big, lo], os.path.join(folder, f"piu_{seq}_2.png"), (l_dark, 1), crop,
                                   [(lx, ly + 0.08 * fit["r"], f"Low {m_lo:.1f}", "#5aa0ff")], 300)))
        images.append((f"③ W 1 / L {l_bright:.1f}: 가장 밝은 곳",
                       render_step(img.a, [big, hi], os.path.join(folder, f"piu_{seq}_3.png"), (l_bright, 1), crop,
                                   [(hx, hy + 0.08 * fit["r"], f"High {m_hi:.1f}", "#ff5050")], 300)))
    return {"title": f"영상 균일도 PIU ({seq}) — {v['piu']:.1f} %, {_judge(ok)}", "images": images, "lines": lines}


def _ghosting_steps(seq, roles, values, c, folder):
    v = values.get(("ghosting", seq))
    sides = ("top", "bottom", "left", "right")
    names = [f"{seq}|ACR Ghost {s}" for s in sides]
    big_name = f"{seq}|ACR Ghost large" if f"{seq}|ACR Ghost large" in roles else f"{seq}|ACR PIU large"
    if not v or not all(n in roles for n in names) or big_name not in roles:
        return None
    img, big = roles[big_name]
    anns = [roles[n][1] for n in names]
    ok = v["ratio"] <= c["ghost_max"] if seq == "T1" else None
    kor = {"top": "위", "bottom": "아래", "left": "왼쪽", "right": "오른쪽"}
    area = acr.roi_area_mm2(img, anns[0])
    lines = [f"① slice 7 큰 ROI 평균 {v['large']:.2f}",
             f"② 팬텀 바깥 위·아래·왼쪽·오른쪽에 타원 ROI (≈{area:.0f} mm², 긴 축이 영상 가장자리와 나란히): "
             + ", ".join(f"{kor[s]} {v[s]:.2f}" for s in sides),
             f"③ 고스팅 = |(위 + 아래) - (왼쪽 + 오른쪽)| / (2 × 큰 ROI) × 100 = |({v['top']:.2f} + {v['bottom']:.2f}) - "
             f"({v['left']:.2f} + {v['right']:.2f})| / (2 × {v['large']:.2f}) × 100 = {v['ratio']:.2f} %",
             f"판정: 기준 ≤ {c['ghost_max']:g} %" + (f" → {_judge(ok)}" if seq == "T1" else " (ACR 판정은 T1만, T2는 참고)")]
    images = []
    if folder:
        win = (0.025 * v["large"], 0.05 * v["large"])   # 배경의 고스트가 보이게 아주 좁게
        labels = [(*_centre(a), f"{v[s]:.1f}", "#ffd200") for a, s in zip(anns, sides)]
        images.append((f"배경 ROI (W {win[1]:.0f} / L {win[0]:.0f})",
                       render_step(img.a, [big] + anns, os.path.join(folder, f"ghost_{seq}.png"), win, None, labels, 360)))
    return {"title": f"고스팅 ({seq}) — {v['ratio']:.2f} %" + (f", {_judge(ok)}" if seq == "T1" else ""),
            "images": images, "lines": lines}


# ═══ 뷰포트 표시 (원판 · 구멍 배열) ═══

_TOOLS = []


def paint_badges(tool, vp, painter):
    """영상 위 가운데: 이 슬라이스가 쓰이는 ACR 검사 배지 (검사 색 · 판정 · 값)"""
    from PyQt5.QtCore import QRectF
    from PyQt5.QtGui import QFont, QFontMetrics
    info = tool.slice_tests(vp.series, vp.current_slice)
    if not info:
        return
    seq, n, tests = info
    head = f"ACR {seq}" + (f" · slice {n}" if n else "")
    chips = [(head, "#2b3445", None)]
    for key, desc in tests:
        ok = tool.verdict(key, seq) if seq in ("T1", "T2") else None
        row = next((r for r in tool.rows if r[0] == key and r[1] == seq), None)
        mark = {True: " ✓", False: " ✗", None: ""}[ok]
        value = ""
        v = tool.values.get((key, "T1" if key == "geometry" else seq)) or {}
        if key == "geometry" and v:   # 이 영상에서 잰 직경만
            names = ["LOC"] if n is None else [f"S{n} {d}" for d in ("V", "H", "D1", "D2")]
            value = " " + ", ".join(f"{nm} {v[nm]:.1f}" for nm in names if nm in v) + " mm"
        elif key == "position" and v and n:
            which = "S1" if n == 1 else "S11"
            value = f" {which} {v[which]:+.1f} mm" if which in v else ""
        elif key == "low_contrast" and seq in tool.lc_detail and n and n >= 8:
            value = f" spoke {tool.lc_detail[seq][n - 8]['spokes']}"
        elif row is not None:
            value = " " + _short(key, row[2])
        chips.append((f"{acr.TEST_NAMES[key].split('. ', 1)[-1]} · {desc}{value}{mark}",
                      TEST_COLORS[key], ok))
    font = QFont(painter.font())
    font.setPointSizeF(max(9.0, font.pointSizeF()))
    font.setBold(True)
    painter.save()
    painter.setFont(font)
    fm = QFontMetrics(font)
    pad, gap, h = 7, 5, fm.height() + 6
    widths = [fm.horizontalAdvance(t) + 2 * pad for t, _c, _ok in chips]
    rows, cur, width = [[]], 0, vp.width() - 20
    for i, w in enumerate(widths):   # 좁으면 줄 바꿈
        if rows[-1] and cur + w > width:
            rows.append([])
            cur = 0
        rows[-1].append(i)
        cur += w + gap
    y = 30.0
    for line in rows:
        total = sum(widths[i] for i in line) + gap * (len(line) - 1)
        x = (vp.width() - total) / 2
        for i in line:
            text, color, ok = chips[i]
            rect = QRectF(x, y, widths[i], h)
            fill = QColor(color)
            fill.setAlpha(215)
            painter.setPen(QPen(QColor(PASS_COLOR if ok else FAIL_COLOR), 2) if ok is not None else Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor("#ffffff") if i == 0 else QColor("#10141c"))
            painter.drawText(rect, Qt.AlignCenter, text)
            x += widths[i] + gap
        y += h + 4
    painter.restore()


def paint_markers(vp, painter):
    from PyQt5.QtCore import QPointF
    from ..annotations import image_key
    if not _TOOLS:
        return
    tool = _TOOLS[-1]
    if vp.series is None:
        return
    if tool.show_overlay.isChecked() and tool.rows:
        try:
            paint_badges(tool, vp, painter)
        except Exception:  # noqa: BLE001 - 표시 실패로 뷰포트 그리기를 막지 않음
            pass
    if not tool.show_markers.isChecked():
        return
    marks = tool.markers.get(image_key(vp.series, vp.current_slice))
    if not marks:
        return
    for m in marks:
        if m[0] == "disk":
            _k, x, y, r, color = m
            c = vp._image_to_screen_f((x + 0.5, y + 0.5))
            edge = vp._image_to_screen_f((x + 0.5 + r, y + 0.5))
            rad = ((edge.x() - c.x()) ** 2 + (edge.y() - c.y()) ** 2) ** 0.5
            painter.setPen(QPen(QColor(color), 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(c, rad, rad)
        elif m[0] == "box":
            _k, x0, y0, x1, y1, color, label = m
            p0, p1 = vp._image_to_screen_f((x0, y0)), vp._image_to_screen_f((x1, y1))
            painter.setPen(QPen(QColor(color), 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(int(min(p0.x(), p1.x())), int(min(p0.y(), p1.y())),
                             int(abs(p1.x() - p0.x())), int(abs(p1.y() - p0.y())))
            if label:
                vp._draw_text_shadow(painter, int(min(p0.x(), p1.x())), int(min(p0.y(), p1.y())) - 2, label)
    _ = QPointF


__all__ = ["ACRTool", "ACRCriteriaWidget", "CriteriaDialog", "TrendDialog", "paint_markers"]
