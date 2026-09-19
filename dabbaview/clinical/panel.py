# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Analysis 도크 (오른쪽) - Analysis 메뉴에서 고른 도구 페이지 + 공통 결과 영역(표·그래프·CSV)
"""
import csv
import io
import math
import os

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                             QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
                             QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from . import DISCLAIMER


# ═══ 공통 컨텍스트 (도구가 앱에 접근하는 창구) ═══

class AnalysisContext:
    def __init__(self, main, dock):
        self.main = main
        self.dock = dock
        self._volumes = {}

    # 뷰포트 / 시리즈
    def vp(self):
        return self.main._target_viewport()

    def series(self):
        return self.vp().series

    def slice_index(self):
        return self.vp().current_slice

    def image(self, series=None, k=None):
        series = series or self.series()
        return series.get_pixel_array(self.slice_index() if k is None else k)

    def spacing2d(self, series=None, k=0):
        from .. import dicom_info
        series = series or self.series()
        return dicom_info.pixel_spacing(series.slices[k]) or (1.0, 1.0)

    def series_list(self):
        return self.main._loader.get_series_list()

    def by_uid(self, uid):
        return self.main._loader.get_series_by_uid(uid) if uid else None

    def volume(self, series):
        from ..ai.volume import load_volume
        vol = self._volumes.get(series.series_uid)
        if vol is None or vol.series is not series:
            vol = load_volume(series)
            self._volumes[series.series_uid] = vol
        return vol

    # ROI / 주석
    def roi_mask(self, required=True):
        mask = self.vp().roi_mask()
        if mask is None and required:
            raise ValueError("현재 영상에 ROI가 없습니다. Freehand ROI(8) 또는 타원(E) 도구로 그리세요.")
        return mask

    def roi_by_slice(self, series):
        """시리즈의 슬라이스별 마지막 ROI 마스크 {k: mask}"""
        from ..annotations import image_key
        from ..roi import ellipse_mask, polygon_mask
        store = self.main._annotation_store
        out = {}
        for k in range(series.num_slices):
            for ann in reversed(store.items(image_key(series, k))):
                shape = tuple(int(v) for v in (series.slices[k].Rows, series.slices[k].Columns))
                if ann["type"] == "roi":
                    out[k] = polygon_mask(ann["pts"], shape)
                    break
                if ann["type"] == "ellipse":
                    out[k] = ellipse_mask(ann["pts"][0], ann["pts"][1], shape)
                    break
        return out

    def last_lines(self, n=2):
        """현재 영상의 마지막 거리 측정선 n개 [(p0, p1)]"""
        lines = [a["pts"] for a in self.vp().annotations_here() if a["type"] == "distance"]
        if len(lines) < n:
            raise ValueError(f"현재 영상에 거리 측정선(Dist, 4)이 {n}개 필요합니다.")
        return lines[-n:]

    def add_line(self, series, k, p0, p1, mm, label=""):
        """영상에 측정선 주석 추가 (RECIST 장경·단경 표시)"""
        from ..annotations import image_key
        ann = {"type": "distance", "pts": [tuple(p0), tuple(p1)], "mm": mm}
        if label:
            ann["label"] = label
        self.main._annotation_store.add(image_key(series, k), ann)

    # AI 라벨
    def label_mask(self, series, label_id, required=True):
        case = self.main._seg.case(series)
        mask = None if not case.editable or case.is_empty() else (case.mask == label_id)
        if (mask is None or not mask.any()) and required:
            raise ValueError("이 시리즈에 해당 라벨이 칠해진 곳이 없습니다 "
                             "(AI 패널 → 세그멘트에서 칠하거나 SEG를 여세요).")
        return mask

    def show_mask(self, series, mask, name):
        """결과 영역을 AI 라벨(이름)로 오버레이"""
        label = self.main._labels.ensure(name)
        m = np.zeros(mask.shape, dtype=np.uint8)
        m[np.asarray(mask, bool)] = label["id"]
        self.main._apply_mask(series, m)
        return label

    def landmark_voxel(self, series):
        """마지막 랜드마크 → (k, row, col) (없으면 None)"""
        points = list(self.main._landmarks)
        if not points or series.geometry is None:
            return None
        from ..ai.volume import series_spacing_affine
        _sp, affine = series_spacing_affine(series)
        idx = np.linalg.solve(affine, np.append(np.asarray(points[-1]["position"]), 1.0))
        return idx[2], idx[1], idx[0]

    # 결과 시리즈
    def make_series(self, array, refs, name, source, window=None, modality=None):
        """맵 배열 (Z, H, W) + 위치별 기준 영상 refs[z]=(series, index) → 새 시리즈"""
        from .. import dicom_info
        from ..formats.volume_series import VolumeSeries
        array = np.asarray(array, dtype=np.float32)
        z = array.shape[0]
        first = refs[0][0].slices[refs[0][1]]
        spacing = dicom_info.pixel_spacing(first) or (1.0, 1.0)
        iop = [float(v) for v in getattr(first, "ImageOrientationPatient", [1, 0, 0, 0, 1, 0])]
        row_dir, col_dir = np.array(iop[:3]), np.array(iop[3:])
        origin = np.array([float(v) for v in getattr(first, "ImagePositionPatient", [0, 0, 0])])
        if z > 1:
            last = refs[-1][0].slices[refs[-1][1]]
            end = np.array([float(v) for v in getattr(last, "ImagePositionPatient", [0, 0, z - 1])])
            step = (end - origin) / (z - 1)
        else:
            step = np.cross(row_dir, col_dir) * float(getattr(first, "SliceThickness", 1) or 1)
        affine = np.eye(4)
        affine[:3, 0] = row_dir * spacing[1]
        affine[:3, 1] = col_dir * spacing[0]
        affine[:3, 2] = step
        affine[:3, 3] = origin
        path = f"{source.series_uid}#{name}#{id(array)}"
        series = VolumeSeries(array, affine, str(getattr(first, "PatientName", "")), path,
                              "derived", modality=modality or source.modality,
                              description=name, canonical=False)
        ref = source.slices[0]
        for ds in series.slices:
            for keyword in ("PatientName", "PatientID", "StudyInstanceUID", "StudyDate",
                            "StudyTime", "FrameOfReferenceUID"):
                if keyword in ref:
                    setattr(ds, keyword, getattr(ref, keyword))
            ds.StudyDescription = str(getattr(ref, "StudyDescription", ""))
            if window:
                ds.WindowCenter, ds.WindowWidth = round(window[0], 4), round(window[1], 4)
        self.main.add_derived_series(series, select=False)
        return series

    def open_series(self, series, colormap=None, window=None):
        self.main._select_series(series)
        if window:
            self.vp().set_window(window[0], window[1], user=True)
        if colormap:
            self.main.apply_colormap(colormap)

    # 결과
    def results(self, title, rows, plot=None, note=""):
        self.dock.result.show(title, rows, plot, note)

    def run(self, text, fn, done):
        self.dock.run_task(text, fn, done)

    def status(self, text):
        self.main.statusBar().showMessage(text, 8000)


# ═══ 공통 위젯 ═══

class SeriesPicker(QComboBox):
    """불러온 시리즈 선택 (기본: 현재 시리즈)"""

    def __init__(self, ctx, allow_none=False, none_text="(없음)"):
        super().__init__()
        self.ctx = ctx
        self.allow_none = allow_none
        self.none_text = none_text
        self.setMinimumContentsLength(22)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

    def refresh(self, prefer_current=True):
        current = self.currentData()
        self.blockSignals(True)
        self.clear()
        if self.allow_none:
            self.addItem(self.none_text, None)
        for s in self.ctx.series_list():
            self.addItem(f"{s.modality} · {s.description or '(설명 없음)'} ({s.num_slices})",
                         s.series_uid)
        vp_series = self.ctx.series()
        target = current or (vp_series.series_uid if prefer_current and vp_series else None)
        index = self.findData(target)
        self.setCurrentIndex(max(0, index) if index >= 0 or not self.allow_none else 0)
        self.blockSignals(False)

    def series(self, required=True):
        s = self.ctx.by_uid(self.currentData())
        if s is None and required:
            raise ValueError("시리즈를 고르세요.")
        return s


class MultiSeriesPicker(QListWidget):
    """여러 시리즈 체크 (예: b-value마다 따로 저장된 확산 영상)"""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.setMaximumHeight(110)

    def refresh(self):
        checked = set(self.selected_uids())
        vp_series = self.ctx.series()
        self.clear()
        for s in self.ctx.series_list():
            item = QListWidgetItem(f"{s.modality} · {s.description} ({s.num_slices})")
            item.setData(Qt.UserRole, s.series_uid)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            on = s.series_uid in checked or (not checked and vp_series is not None
                                             and s.series_uid == vp_series.series_uid)
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.addItem(item)

    def selected_uids(self):
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())
                if self.item(i).checkState() == Qt.Checked]

    def series(self):
        out = [self.ctx.by_uid(u) for u in self.selected_uids()]
        out = [s for s in out if s is not None]
        if not out:
            raise ValueError("시리즈를 하나 이상 체크하세요.")
        return out


class LabelPicker(QComboBox):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    def refresh(self):
        current = self.currentData()
        self.clear()
        for label in self.ctx.main._labels:
            self.addItem(f"{label['id']}. {label['name']}", label["id"])
        self.setCurrentIndex(max(0, self.findData(current)))


def spin(value, lo, hi, decimals=1, step=None, suffix=""):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setValue(value)
    if step:
        w.setSingleStep(step)
    if suffix:
        w.setSuffix(suffix)
    return w


def ispin(value, lo, hi, suffix=""):
    w = QSpinBox()
    w.setRange(lo, hi)
    w.setValue(value)
    if suffix:
        w.setSuffix(suffix)
    return w


# ═══ 도구 기반 ═══

class Tool(QWidget):
    """도구 페이지 기본 - build()에서 self.form에 입력, self.button()으로 실행 버튼"""

    title = ""
    help = ""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._pickers = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        head = QLabel(f"<b>{self.title}</b>")
        layout.addWidget(head)
        if self.help:
            text = QLabel(self.help)
            text.setWordWrap(True)
            text.setStyleSheet("color: #999;")
            layout.addWidget(text)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.buttons = QVBoxLayout()
        layout.addLayout(self.buttons)
        self.build()
        layout.addStretch()
        for combo in self.findChildren(QComboBox):   # 긴 시리즈 이름이 패널 폭을 넓히지 않게
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)

    def build(self):
        pass

    def series_picker(self, label="시리즈:", **kw):
        p = SeriesPicker(self.ctx, **kw)
        self._pickers.append(p)
        self.form.addRow(label, p)
        return p

    def multi_picker(self, label="시리즈:"):
        p = MultiSeriesPicker(self.ctx)
        self._pickers.append(p)
        self.form.addRow(label, p)
        return p

    def label_picker(self, label="AI 라벨:"):
        p = LabelPicker(self.ctx)
        self._pickers.append(p)
        self.form.addRow(label, p)
        return p

    def button(self, text, slot, tip=""):
        b = QPushButton(text)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(lambda: self._guard(slot))
        self.buttons.addWidget(b)
        return b

    def _guard(self, slot):
        try:
            slot()
        except ValueError as e:
            QMessageBox.information(self, self.title, str(e))

    def on_show(self):
        for p in self._pickers:
            p.refresh()


# ═══ 결과 영역 ═══

class ResultView(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.title = QLabel("결과")
        self.title.setStyleSheet("font-weight: bold;")
        top.addWidget(self.title, 1)
        copy = QPushButton("복사")
        copy.clicked.connect(self.copy)
        save = QPushButton("CSV")
        save.clicked.connect(self.save_csv)
        top.addWidget(copy)
        top.addWidget(save)
        layout.addLayout(top)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #9ab;")
        layout.addWidget(self.note)
        self.table = QTableWidget(0, 2)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        from ..analysis import configure_matplotlib
        configure_matplotlib()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(4, 3), dpi=100, facecolor="#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(230)
        self.canvas.setVisible(False)
        layout.addWidget(self.canvas, 2)
        self._rows = []
        self._header = []

    def show(self, title, rows, plot=None, note=""):
        """rows: dict 또는 [(이름, 값)] 또는 {'header': [...], 'rows': [[...]]}"""
        self.title.setText(title)
        self.note.setText(note)
        if isinstance(rows, dict) and "header" in rows:
            header, data = rows["header"], rows["rows"]
        else:
            items = rows.items() if isinstance(rows, dict) else rows
            header, data = ["항목", "값"], [[k, v] for k, v in items]
        self._header, self._rows = header, data
        self.table.setColumnCount(len(header))
        self.table.setHorizontalHeaderLabels(header)
        self.table.setRowCount(len(data))
        for r, row in enumerate(data):
            for c, value in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(_fmt(value)))
        self.figure.clear()
        if plot is not None:
            plot(self.figure)
            for ax in self.figure.axes:
                _style_axes(ax)
            self.figure.tight_layout()
            self.canvas.draw_idle()
        self.canvas.setVisible(plot is not None)

    def copy(self):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter="\t")
        w.writerow(self._header)
        for row in self._rows:
            w.writerow([_fmt(v) for v in row])
        QApplication.clipboard().setText(buf.getvalue())

    def save_csv(self):
        if not self._rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "결과 CSV", "analysis.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["# " + self.title.text()])
                w.writerow(self._header)
                for row in self._rows:
                    w.writerow([_fmt(v) for v in row])
            if self.figure.axes:
                self.figure.savefig(os.path.splitext(path)[0] + ".png", dpi=150,
                                    facecolor=self.figure.get_facecolor())


def _fmt(value):
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(value):
            return "∞" if value > 0 else ("-∞" if value < 0 else "-")
        return f"{value:.4g}" if abs(value) < 1e5 else f"{value:.3e}"
    return str(value)


def _style_axes(ax):
    if getattr(ax, "name", "") == "polar":
        return
    ax.set_facecolor("#141414")
    for spine in ax.spines.values():
        spine.set_color("#777")
    ax.tick_params(colors="#bbb", labelsize=8)
    ax.xaxis.label.set_color("#bbb")
    ax.yaxis.label.set_color("#bbb")
    ax.title.set_color("#ddd")
    legend = ax.get_legend()
    if legend:
        legend.get_frame().set_facecolor("#222")
        for t in legend.get_texts():
            t.set_color("#ddd")


# ═══ 도크 ═══

class AnalysisDock(QDockWidget):
    def __init__(self, main):
        super().__init__("Analysis", main)
        self.setObjectName("ClinicalAnalysisDock")
        self.main = main
        self.ctx = AnalysisContext(main, self)
        self._tools = {}
        self._worker = None
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        self.header = QLabel("Analysis 메뉴에서 도구를 고르세요.")
        self.header.setStyleSheet("font-size: 13px; font-weight: bold; padding: 4px;")
        layout.addWidget(self.header)
        splitter = QSplitter(Qt.Vertical)
        self.stack = QStackedWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.stack)
        splitter.addWidget(scroll)
        self.result = ResultView()
        splitter.addWidget(self.result)
        splitter.setSizes([420, 420])
        layout.addWidget(splitter, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.task_label = QLabel()
        self.task_label.setStyleSheet("color: #9ab;")
        layout.addWidget(self.task_label)
        disclaimer = QLabel(DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: #886; font-size: 10px;")
        layout.addWidget(disclaimer)
        self.setWidget(body)
        self.setMinimumWidth(380)

    def open_tool(self, category, key, cls):
        tool = self._tools.get(key)
        if tool is None:
            tool = cls(self.ctx)
            self._tools[key] = tool
            self.stack.addWidget(tool)
        self.header.setText(f"{category} ▸ {cls.title}")
        self.stack.setCurrentWidget(tool)
        tool.on_show()
        self.show()
        self.raise_()
        return tool

    def run_task(self, text, fn, done):
        from ..ai.panel import TaskWorker
        if self._worker is not None:
            QMessageBox.information(self, "Analysis", "다른 계산이 진행 중입니다.")
            return
        worker = TaskWorker(fn, self)
        worker.progress.connect(lambda t, f: self.task_label.setText(t))
        worker.succeeded.connect(lambda r: (self._finish(), done(r)))
        worker.failed.connect(lambda m: (self._finish(), self.task_label.setText(f"실패: {m}"),
                                         QMessageBox.warning(self, "Analysis", m)))
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        self.progress.setVisible(True)
        self.task_label.setText(text)
        worker.start()

    def _finish(self):
        self._worker = None
        self.progress.setVisible(False)
        self.task_label.setText("")


__all__ = ["AnalysisDock", "AnalysisContext", "Tool", "SeriesPicker", "MultiSeriesPicker",
           "LabelPicker", "spin", "ispin", "QCheckBox", "QComboBox", "QLineEdit", "QHBoxLayout",
           "QPushButton", "QLabel", "QListWidget"]
