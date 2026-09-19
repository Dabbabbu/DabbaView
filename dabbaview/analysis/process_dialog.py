# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Process 메뉴 필터 대화상자 - 파라미터 · 2D/3D · 미리보기 → 새 시리즈로 적용
"""
import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                             QLabel, QMessageBox, QProgressBar, QPushButton, QVBoxLayout)

from . import derived_series
from .processing import FILTERS, apply_filter, describe


class _Worker(QThread):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.cancelled = False

    def run(self):
        try:
            self.done.emit(self._fn(lambda i, n: self.progress.emit(i, n),
                                    lambda: self.cancelled))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


def _pixmap(arr, window, size=260):
    center, width = window
    img = np.clip((arr - (center - width / 2)) / max(width, 1e-6) * 255, 0, 255).astype(np.uint8)
    img = np.ascontiguousarray(img)
    h, w = img.shape
    pix = QPixmap.fromImage(QImage(img.data, w, h, w, QImage.Format_Grayscale8).copy())
    return pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class FilterDialog(QDialog):
    def __init__(self, main_window, key):
        super().__init__(main_window)
        self.main = main_window
        self.key = key
        self._worker = None
        name, params = FILTERS[key]
        self.setWindowTitle(name)
        vp = main_window._target_viewport()
        self.series = vp.series
        self.k = vp.current_slice
        self.window = vp.window_level

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"대상: {self.series.description} ({self.series.num_slices}장) "
                                "→ 결과는 새 시리즈 (원본 보존)"))
        form = QFormLayout()
        self._spins = {}
        for pname, label, default, lo, hi, decimals in params:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(decimals)
            spin.setValue(default)
            spin.setSingleStep(1 if decimals == 0 else 0.5)
            spin.valueChanged.connect(self._preview)
            self._spins[pname] = spin
            form.addRow(label + ":", spin)
        self._mode = QComboBox()
        self._mode.addItem("2D (슬라이스마다)", "2d")
        if key != "canny":
            self._mode.addItem("3D (볼륨 전체)", "3d")
        form.addRow("적용 방식:", self._mode)
        self._scope = QComboBox()
        self._scope.addItem("모든 슬라이스", "all")
        self._scope.addItem("현재 슬라이스만 (나머지는 원본)", "current")
        form.addRow("범위:", self._scope)
        layout.addLayout(form)

        previews = QHBoxLayout()
        self._before = QLabel()
        self._after = QLabel()
        for title, label in (("원본", self._before), ("미리보기 (현재 슬라이스, 2D)", self._after)):
            box = QVBoxLayout()
            box.addWidget(QLabel(title))
            box.addWidget(label)
            previews.addLayout(box)
        layout.addLayout(previews)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self._apply_btn = QPushButton("적용 → 새 시리즈")
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self._apply)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self._cancel)
        buttons.addWidget(self._apply_btn)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self._slice = np.asarray(self.series.get_pixel_array(self.k), dtype=np.float32)
        self._before.setPixmap(_pixmap(self._slice, self.window))
        self._preview()

    def params(self):
        return {k: s.value() for k, s in self._spins.items()}

    def _preview(self, *_):
        out = apply_filter(self._slice[None], self.key, self.params(), "2d")[0]
        window = self.window
        if self.key in ("sobel", "canny"):
            lo, hi = np.percentile(out, [1, 99.5])
            window = ((lo + hi) / 2, max(hi - lo, 1))
        self._after.setPixmap(_pixmap(out, window))

    def _apply(self):
        series, key, params = self.series, self.key, self.params()
        mode = self._mode.currentData()
        slices = [self.k] if self._scope.currentData() == "current" else None

        def task(progress, cancelled):
            from ..ai.volume import load_volume
            volume = load_volume(series)
            return apply_filter(volume.array, key, params, mode, slices, progress, cancelled)

        worker = _Worker(task)
        worker.progress.connect(lambda i, n: (self._progress.setMaximum(n),
                                              self._progress.setValue(i)))
        worker.done.connect(self._done)
        worker.failed.connect(lambda m: (self._reset(), QMessageBox.warning(self, "Process", m)))
        self._worker = worker
        self._progress.setVisible(True)
        self._apply_btn.setEnabled(False)
        worker.start()

    def _done(self, array):
        self._reset()
        if array is None:
            return
        name = f"{describe(self.key, self.params())} - {self.series.description}"
        result = derived_series(array, self.series, name, self.key)
        if self.key in ("sobel", "canny"):
            lo, hi = np.percentile(array[array.shape[0] // 2], [1, 99.5])
            for ds in result.slices:
                ds.WindowCenter, ds.WindowWidth = round(float((lo + hi) / 2), 3), \
                    round(float(max(hi - lo, 1)), 3)
        self.main.add_derived_series(result, select=True)
        self.accept()

    def _reset(self):
        if self._worker is not None:
            self._worker.wait(2000)
        self._worker = None
        self._progress.setVisible(False)
        self._apply_btn.setEnabled(True)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancelled = True
            self._worker.wait(10000)
        self.reject()
