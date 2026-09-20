# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
일괄 동영상 내보내기 - 불러온 시리즈를 골라 한 번에 MP4 · AVI · GIF로

파일 이름: {SeriesDescription}_{SeriesNumber}.mp4 (같은 이름이 있으면 뒤에 _2, _3 …)
"""
import os
import re
import threading

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
                             QVBoxLayout)

from .video_exporter import ExportCancelled, count_frames, export_series_video

SIZES = [("원본 크기", None), ("512 × 512", 512), ("1024 × 1024", 1024)]
FORMATS = ["MP4", "AVI", "GIF"]


def safe_name(text):
    text = re.sub(r"[\\/:*?\"<>|]+", "_", str(text or "").strip())
    return re.sub(r"\s+", " ", text)[:60] or "series"


def output_path(folder, series, fmt):
    """{설명}_{시리즈번호}.{확장자} (겹치면 _2, _3 …)"""
    number = series.series_number if series.series_number is not None else ""
    stem = f"{safe_name(series.description)}_{number}".strip("_") or "series"
    ext = fmt.lower()
    path = os.path.join(folder, f"{stem}.{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}_{n}.{ext}")
        n += 1
    return path


class BatchWorker(QThread):
    """시리즈를 하나씩 내보내며 진행 상황을 알림"""

    progress = pyqtSignal(int, int, str)        # 끝난 개수, 전체, 지금 시리즈 이름
    item_done = pyqtSignal(str, bool, str)      # 경로, 성공 여부, 메시지
    finished_all = pyqtSignal(int, int, list)   # 성공, 실패, [실패 메시지]

    def __init__(self, jobs, options, parent=None):
        super().__init__(parent)
        self._jobs = jobs          # [(series, 저장 경로)]
        self._options = options
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        ok = fail = 0
        errors = []
        for i, (series, path) in enumerate(self._jobs):
            if self._cancel.is_set():
                break
            self.progress.emit(i, len(self._jobs), series.description or "")
            try:
                export_series_video(series, path, cancel_event=self._cancel, **self._options)
                ok += 1
                self.item_done.emit(path, True, "")
            except ExportCancelled:
                break
            except Exception as e:  # noqa: BLE001 - 한 시리즈가 실패해도 나머지는 계속
                fail += 1
                errors.append(f"{series.description or series.series_uid[-8:]}: {e}")
                self.item_done.emit(path, False, str(e))
        self.progress.emit(len(self._jobs), len(self._jobs), "")
        self.finished_all.emit(ok, fail, errors)


class BatchVideoDialog(QDialog):
    """여러 시리즈를 한 번에 동영상으로"""

    def __init__(self, series_list, window=None, inverted=False, default_dir="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("일괄 동영상 내보내기")
        self.setMinimumWidth(620)
        self._series_list = [s for s in series_list if s.num_slices > 1]
        self._window = window
        self._inverted = inverted
        self._worker = None
        self.result_message = ""
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("내보낼 시리즈를 고르세요 (2장 이상인 시리즈만 나옵니다):"))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        for s in self._series_list:
            item = QListWidgetItem(f"{s.series_number or '-'}  {s.description or '(설명 없음)'}"
                                   f"   ·   {count_frames(s)}장")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list.addItem(item)
        self.list.setMinimumHeight(190)
        layout.addWidget(self.list)

        row = QHBoxLayout()
        all_on = QPushButton("전체 선택")
        all_on.clicked.connect(lambda: self._set_all(Qt.Checked))
        all_off = QPushButton("전체 해제")
        all_off.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        self.count_label = QLabel("")
        row.addWidget(all_on)
        row.addWidget(all_off)
        row.addWidget(self.count_label, 1)
        layout.addLayout(row)
        self.list.itemChanged.connect(lambda *_: self._update_count())

        opts = QHBoxLayout()
        opts.addWidget(QLabel("형식:"))
        self.format = QComboBox()
        self.format.addItems(FORMATS)
        opts.addWidget(self.format)
        opts.addWidget(QLabel("재생 시간:"))
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.5, 120.0)
        self.duration.setValue(5.0)
        self.duration.setSuffix(" 초")
        self.duration.setToolTip("시리즈마다 이 시간으로 맞춥니다 (FPS = 장수 ÷ 시간)")
        opts.addWidget(self.duration)
        opts.addWidget(QLabel("해상도:"))
        self.size = QComboBox()
        for text, value in SIZES:
            self.size.addItem(text, value)
        opts.addWidget(self.size)
        opts.addStretch(1)
        layout.addLayout(opts)

        self.use_window = QCheckBox("현재 화면의 W/L 적용 (끄면 시리즈마다 자동 밝기)")
        self.use_window.setChecked(window is not None)
        self.use_window.setEnabled(window is not None)
        layout.addWidget(self.use_window)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("저장 폴더:"))
        self.folder = QLineEdit(default_dir or os.path.expanduser("~/Documents"))
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        self.status = QLabel("")
        layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        layout.addWidget(self.bar)

        self.buttons = QDialogButtonBox()
        self.export_button = self.buttons.addButton("내보내기", QDialogButtonBox.AcceptRole)
        self.buttons.addButton("닫기", QDialogButtonBox.RejectRole)
        self.export_button.clicked.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_count()

    # ─── 목록 ───
    def _set_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)
        self._update_count()

    def _checked_series(self):
        return [s for i, s in enumerate(self._series_list)
                if self.list.item(i).checkState() == Qt.Checked]

    def _update_count(self):
        picked = self._checked_series()
        frames = sum(count_frames(s) for s in picked)
        self.count_label.setText(f"선택 {len(picked)} / {len(self._series_list)} 시리즈 · 총 {frames}장")
        self.export_button.setEnabled(bool(picked))

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "저장할 폴더", self.folder.text())
        if folder:
            self.folder.setText(folder)

    # ─── 내보내기 ───
    def _start(self):
        picked = self._checked_series()
        folder = self.folder.text().strip()
        if not picked:
            return
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                QMessageBox.warning(self, "일괄 동영상 내보내기", f"폴더를 만들 수 없습니다:\n{e}")
                return
        fmt = self.format.currentText()
        jobs = [(s, output_path(folder, s, fmt)) for s in picked]
        options = {"fmt": fmt, "duration": self.duration.value(),
                   "size": self.size.currentData(),
                   "window": self._window if self.use_window.isChecked() else None,
                   "inverted": self._inverted}
        self._worker = BatchWorker(jobs, options, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_all.connect(self._on_finished)
        self.bar.setRange(0, len(jobs))
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.export_button.setEnabled(False)
        self.list.setEnabled(False)
        self._worker.start()

    def _on_progress(self, done, total, name):
        self.bar.setValue(done)
        if done < total:
            self.status.setText(f"{done + 1}/{total} 시리즈 변환 중…  {name}")

    def _on_finished(self, ok, fail, errors):
        self.list.setEnabled(True)
        self.export_button.setEnabled(True)
        self._worker = None
        folder = self.folder.text().strip()
        self.result_message = f"일괄 동영상 내보내기: 성공 {ok}개" + (f" · 실패 {fail}개" if fail else "") \
                              + f"  ·  {folder}"
        self.status.setText(self.result_message)
        if errors:
            QMessageBox.warning(self, "일괄 동영상 내보내기",
                                f"{ok}개를 만들었고 {fail}개가 실패했습니다.\n\n" + "\n".join(errors[:5]))
        self._update_count()

    def reject(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(3000)
        super().reject()
