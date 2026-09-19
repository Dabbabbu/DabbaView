# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
하단 도킹 패널: 히스토그램 / 라인 프로파일 (matplotlib)
"""
import csv

import numpy as np
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QPushButton, QSpinBox, QTableWidget,
                             QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import measure

STAT_KEYS = ["N", "Mean", "StdDev", "Min", "Max", "Median", "Mode"]


def _canvas():
    from . import configure_matplotlib
    configure_matplotlib()
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=(5, 2.4), dpi=100, facecolor="#1e1e1e")
    canvas = FigureCanvasQTAgg(fig)
    ax = fig.add_subplot(111)
    _style(ax)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.9, bottom=0.2)
    return fig, canvas, ax


def _style(ax):
    ax.set_facecolor("#141414")
    for spine in ax.spines.values():
        spine.set_color("#777")
    ax.tick_params(colors="#bbb", labelsize=8)
    ax.xaxis.label.set_color("#bbb")
    ax.yaxis.label.set_color("#bbb")
    ax.title.set_color("#ddd")


def _stats_table():
    table = QTableWidget(len(STAT_KEYS), 1)
    table.setVerticalHeaderLabels(STAT_KEYS)
    table.horizontalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setFixedWidth(170)
    return table


def _fill_stats(table, stats):
    for row, key in enumerate(STAT_KEYS):
        value = "" if not stats else (f"{stats[key]:,}" if key == "N" else f"{stats[key]:.4g}")
        table.setItem(row, 0, QTableWidgetItem(value))


class AnalysisPlotDock(QDockWidget):
    """히스토그램 + 라인 프로파일 (하단)"""

    def __init__(self, main_window):
        super().__init__("Histogram / Profile", main_window)
        self.setObjectName("AnalysisPlotDock")
        self.main = main_window
        self._hist = None      # (counts, edges, stats, title)
        self._profile = None   # dict
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_histogram(), "📊 Histogram")
        self.tabs.addTab(self._build_profile(), "📈 Line Profile")
        self.setWidget(self.tabs)
        self.visibilityChanged.connect(lambda v: v and self.tabs.currentIndex() == 0
                                       and self.refresh_histogram())

    # ─── 히스토그램 ───

    def _build_histogram(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        controls = QHBoxLayout()
        self._source = QComboBox()
        self._source.addItems(["현재 슬라이스", "전체 볼륨", "ROI (마지막 자유곡선/타원)",
                               "AI 라벨 영역 (현재 라벨)"])
        self._source.currentIndexChanged.connect(self.refresh_histogram)
        self._bins = QSpinBox()
        self._bins.setRange(8, 4096)
        self._bins.setValue(256)
        self._bins.setPrefix("bins ")
        self._bins.valueChanged.connect(self.refresh_histogram)
        self._log = QCheckBox("log")
        self._log.toggled.connect(self._draw_histogram)
        self._window_range = QCheckBox("W/L 범위만")
        self._window_range.setToolTip("현재 윈도(W/L) 안의 값만 표시")
        self._window_range.toggled.connect(self.refresh_histogram)
        refresh = QPushButton("새로 고침")
        refresh.clicked.connect(self.refresh_histogram)
        export = QPushButton("CSV")
        export.setToolTip("히스토그램 구간·개수 + 통계를 CSV로")
        export.clicked.connect(self._export_histogram)
        for w in (self._source, self._bins, self._log, self._window_range, refresh, export):
            controls.addWidget(w)
        controls.addStretch()
        left.addLayout(controls)
        self._hist_fig, self._hist_canvas, self._hist_ax = _canvas()
        left.addWidget(self._hist_canvas, 1)
        layout.addLayout(left, 1)
        self._hist_stats = _stats_table()
        layout.addWidget(self._hist_stats)
        return page

    def _values(self):
        """(값 배열, 제목) - 선택한 범위"""
        vp = self.main._target_viewport()
        series = vp.series
        if series is None:
            return None, "시리즈 없음"
        k = vp.current_slice
        source = self._source.currentIndex()
        arr = series.get_pixel_array(k)
        if source == 1:
            values = np.concatenate([a.ravel() for a in series.get_all_pixel_arrays()
                                     if a is not None])
            return values, f"{series.description} · 전체 {series.num_slices}장"
        if arr is None:
            return None, "픽셀 없음"
        if source == 2:
            mask = vp.roi_mask()
            if mask is None:
                return None, "현재 영상에 ROI가 없습니다 (Freehand ROI / 타원 도구로 그리세요)"
            return arr[mask], f"ROI · slice {k + 1}"
        if source == 3:
            seg = self.main._seg
            case = seg.case(series)
            if not case.editable or case.is_empty():
                return None, "AI 라벨이 없습니다"
            label = seg.active_label
            region = case.mask == label
            if not region.any():
                return None, f"'{seg.labels.name(label)}' 라벨이 칠해진 곳이 없습니다"
            volume = series.get_volume_array()
            return volume[region], f"라벨 '{seg.labels.name(label)}' (3D)"
        return arr, f"{series.description} · slice {k + 1}"

    def refresh_histogram(self, *_):
        if not self.isVisible():
            return
        values, title = self._values()
        if values is None:
            self._hist = None
            _fill_stats(self._hist_stats, None)
            self._hist_ax.clear()
            _style(self._hist_ax)
            self._hist_ax.set_title(title, fontsize=9)
            self._hist_canvas.draw_idle()
            return
        values = np.asarray(values, dtype=np.float64).ravel()
        rng = None
        if self._window_range.isChecked():
            c, w = self.main._target_viewport().window_level
            rng = (c - w / 2, c + w / 2)
        counts, edges = measure.histogram(values, self._bins.value(), rng)
        stats = measure.statistics(values)
        self._hist = (counts, edges, stats, title)
        _fill_stats(self._hist_stats, stats)
        self._draw_histogram()

    def _draw_histogram(self, *_):
        ax = self._hist_ax
        ax.clear()
        _style(ax)
        if self._hist is None:
            self._hist_canvas.draw_idle()
            return
        counts, edges, stats, title = self._hist
        ax.bar(edges[:-1], counts, width=np.diff(edges), align="edge", color="#4a9eff",
               edgecolor="none")
        if self._log.isChecked():
            ax.set_yscale("log")
        if stats:
            ax.axvline(stats["Mean"], color="#ffcc00", lw=1, label=f"Mean {stats['Mean']:.4g}")
            ax.axvline(stats["Median"], color="#ff6b6b", lw=1, ls="--",
                       label=f"Median {stats['Median']:.4g}")
            legend = ax.legend(fontsize=7, facecolor="#222", edgecolor="#555")
            for text in legend.get_texts():
                text.set_color("#ddd")
        ax.set_title(title, fontsize=9)
        ax.set_xlabel("value")
        ax.set_ylabel("count")
        self._hist_canvas.draw_idle()

    def _export_histogram(self):
        if self._hist is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "히스토그램 CSV", "histogram.csv", "CSV (*.csv)")
        if not path:
            return
        counts, edges, stats, title = self._hist
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["# source", title])
            for key in STAT_KEYS:
                w.writerow([f"# {key}", stats[key]])
            w.writerow(["bin_start", "bin_end", "count"])
            for a, b, c in zip(edges[:-1], edges[1:], counts):
                w.writerow([f"{a:.4f}", f"{b:.4f}", int(c)])
        self.main.statusBar().showMessage(f"저장: {path}", 5000)

    # ─── 라인 프로파일 ───

    def _build_profile(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        controls = QHBoxLayout()
        self._profile_hint = QLabel("Profile 도구(Shift+L)로 영상 위에 선을 그으면 여기 그래프가 나타납니다.")
        self._profile_hint.setStyleSheet("color: #999;")
        export = QPushButton("CSV")
        export.clicked.connect(self._export_profile)
        controls.addWidget(self._profile_hint, 1)
        controls.addWidget(export)
        left.addLayout(controls)
        self._prof_fig, self._prof_canvas, self._prof_ax = _canvas()
        left.addWidget(self._prof_canvas, 1)
        layout.addLayout(left, 1)
        self._prof_stats = _stats_table()
        layout.addWidget(self._prof_stats)
        return page

    def show_profile(self, profile):
        self._profile = profile
        if not self.isVisible():
            self.show()
        self.tabs.setCurrentIndex(1)
        ax = self._prof_ax
        ax.clear()
        _style(ax)
        d, v = profile["distances"], profile["values"]
        ax.plot(d, v, color="#ffcc00", lw=1.2)
        ax.fill_between(d, v, v.min(), color="#ffcc00", alpha=0.12)
        ax.set_xlabel("distance (mm)" if profile["calibrated"] else "distance (px)")
        ax.set_ylabel("value")
        ax.set_title(profile["title"], fontsize=9)
        ax.set_xlim(d[0], d[-1])
        self._prof_canvas.draw_idle()
        _fill_stats(self._prof_stats, measure.statistics(v))
        unit = "mm" if profile["calibrated"] else "px"
        self._profile_hint.setText(f"길이 {d[-1]:.2f} {unit} · 점 {len(v)}개")

    def _export_profile(self):
        if self._profile is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "프로파일 CSV", "profile.csv", "CSV (*.csv)")
        if path:
            p = self._profile
            measure.save_profile_csv(path, p["distances"], p["values"],
                                     {"source": p["title"], "from (x,y)": p["p0"], "to (x,y)": p["p1"]})
            self.main.statusBar().showMessage(f"저장: {path}", 5000)
