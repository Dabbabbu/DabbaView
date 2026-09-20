# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 시리즈 → 동영상(MP4/AVI/GIF) 내보내기
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from PIL import Image

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QLabel, QComboBox, QDoubleSpinBox, QCheckBox,
                             QPushButton, QFileDialog, QProgressDialog,
                             QMessageBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

from .dicom_loader import default_worker_count


# 포맷별 (확장자, OpenCV fourcc 후보). avc1(H.264)는 QuickTime 재생 가능, mp4v는 폴백
FORMATS = {
    'MP4': ('.mp4', ['avc1', 'mp4v']),
    'AVI': ('.avi', ['MJPG', 'XVID']),
    'GIF': ('.gif', []),
}

# (표시 이름, 한 변 크기). None = 원본
RESOLUTIONS = [
    ('원본', None),
    ('256 x 256', 256),
    ('512 x 512', 512),
    ('1024 x 1024', 1024),
]

# 브라우저/뷰어 대부분이 20ms 미만 GIF 프레임을 100ms로 늘려 재생하므로 하한 적용
GIF_MIN_FRAME_MS = 20

# 병렬로 미리 읽어 둘 슬라이스 수 (메모리 사용량 제한)
PREFETCH_CHUNK = 16


def count_frames(series):
    """시리즈 전체 프레임 수 (멀티프레임 DICOM 포함)"""
    total = 0
    for ds in series.slices:
        try:
            total += max(1, int(getattr(ds, 'NumberOfFrames', 1) or 1))
        except (TypeError, ValueError):
            total += 1
    return total


def _split_frames(arr, ds):
    """pixel_array를 2D(그레이) 또는 HxWx3(컬러) 프레임 리스트로 분리"""
    color = int(getattr(ds, 'SamplesPerPixel', 1) or 1) > 1
    if color:
        return [arr] if arr.ndim == 3 else list(arr)
    return [arr] if arr.ndim == 2 else list(arr)


def _estimate_range(series, samples=10):
    """W/L 미적용 시 사용할 전체 시리즈 공통 밝기 범위 추정

    프레임마다 정규화하면 밝기가 깜빡이므로 고르게 뽑은 슬라이스의 분포로 추정.
    """
    n = series.num_slices
    idx = sorted(set(np.linspace(0, n - 1, min(samples, n)).astype(int)))
    values = []
    for i in idx:
        arr = series.get_pixel_array(i)
        if arr is not None:
            values.append(arr[::4, ::4].ravel())
    if not values:
        return 0.0, 255.0
    v = np.concatenate(values)
    low, high = np.percentile(v, [0.5, 99.5])
    if high <= low:
        high = low + 1
    return float(low), float(high)


def _to_uint8(frame, low, high, inverted):
    """밝기 범위를 0~255로 매핑. 컬러 프레임은 그대로 클리핑"""
    if frame.ndim == 3:
        return np.clip(frame, 0, 255).astype(np.uint8)
    img = np.clip((frame - low) / (high - low) * 255, 0, 255).astype(np.uint8)
    if inverted:
        img = 255 - img
    return img


def _fit_square(img, size):
    """종횡비를 유지하며 size x size에 맞추고 남는 영역은 검정으로 채움"""
    h, w = img.shape[:2]
    scale = size / max(h, w)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(img, (nw, nh), interpolation=interp)
    canvas = np.zeros((size, size) + img.shape[2:], dtype=np.uint8)
    y, x = (size - nh) // 2, (size - nw) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas


def _make_even(img):
    """H.264 등 대부분 코덱은 짝수 크기가 필요 → 홀수면 1px 패딩"""
    h, w = img.shape[:2]
    ph, pw = h % 2, w % 2
    if not ph and not pw:
        return img
    pad = ((0, ph), (0, pw)) + ((0, 0),) * (img.ndim - 2)
    return np.pad(img, pad)


class ExportCancelled(Exception):
    pass


def export_series_video(series, filepath, fmt, duration, size=None,
                        window=None, inverted=False,
                        progress_callback=None, cancel_event=None):
    """시리즈를 동영상 파일로 내보냄

    fmt: 'MP4' | 'AVI' | 'GIF'
    duration: 전체 재생 시간(초). FPS = 프레임 수 / duration
    size: 출력 한 변 크기(정사각형), None이면 원본 크기
    window: (center, width)면 해당 W/L 적용, None이면 시리즈 밝기 범위 자동 추정
    progress_callback(current, total): 호출한 스레드에서 실행됨
    반환: 실제 사용한 FPS
    """
    series.sort_slices()
    total = count_frames(series)
    if total == 0:
        raise ValueError("내보낼 슬라이스가 없습니다.")
    fps = total / duration

    if window is not None:
        wc, ww = window
        ww = max(ww, 1)
        low, high = wc - ww / 2, wc + ww / 2
    else:
        low, high = _estimate_range(series)

    # GIF는 프레임 간격 하한이 있으므로 초과하는 FPS면 프레임을 솎아 재생 시간 유지
    gif_step = 1
    if fmt == 'GIF':
        frame_ms = duration * 1000 / total
        if frame_ms < GIF_MIN_FRAME_MS:
            gif_step = int(np.ceil(GIF_MIN_FRAME_MS / frame_ms))

    writer = None
    last = None  # 직전에 쓴 프레임 (읽기 실패 시 반복용)
    gif_frames = []
    out_size = None
    done = 0

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled()

    try:
        n = series.num_slices
        with ThreadPoolExecutor(max_workers=default_worker_count()) as ex:
            for start in range(0, n, PREFETCH_CHUNK):
                check_cancel()
                chunk = range(start, min(start + PREFETCH_CHUNK, n))
                datasets = list(ex.map(series.get_full_dataset, chunk))
                for ds in datasets:
                    try:
                        arr = ds.pixel_array.astype(np.float64)
                        arr = (arr * float(getattr(ds, 'RescaleSlope', 1))
                               + float(getattr(ds, 'RescaleIntercept', 0)))
                        frames = _split_frames(arr, ds)
                    except Exception:
                        frames = [None] * max(1, int(getattr(ds, 'NumberOfFrames', 1) or 1))

                    for frame in frames:
                        check_cancel()
                        done += 1
                        if frame is None:
                            # 읽기 실패 프레임은 직전 프레임 반복(재생 시간 유지)
                            if last is not None:
                                if fmt == 'GIF':
                                    if (done - 1) % gif_step == 0:
                                        gif_frames.append(last)
                                else:
                                    writer.write(last)
                            if progress_callback:
                                progress_callback(done, total)
                            continue

                        img = _to_uint8(frame, low, high, inverted)
                        if size:
                            img = _fit_square(img, size)
                        img = _make_even(img)

                        if out_size is None:
                            out_size = (img.shape[1], img.shape[0])
                        elif (img.shape[1], img.shape[0]) != out_size:
                            # 원본 크기 모드에서 크기가 다른 슬라이스는 첫 프레임 크기로 맞춤
                            img = cv2.resize(img, out_size,
                                             interpolation=cv2.INTER_AREA)

                        if fmt == 'GIF':
                            last = Image.fromarray(img)
                            if (done - 1) % gif_step == 0:
                                gif_frames.append(last)
                        else:
                            bgr = (cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                                   if img.ndim == 2 else
                                   cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
                            if writer is None:
                                writer = _open_writer(filepath, fmt, fps, out_size)
                            writer.write(bgr)
                            last = bgr

                        if progress_callback:
                            progress_callback(done, total)

        check_cancel()
        if fmt == 'GIF':
            if not gif_frames:
                raise ValueError("읽을 수 있는 프레임이 없습니다.")
            # GIF 지연은 10ms 단위 → 프레임마다 20/30ms 등을 섞어 총 재생 시간을 맞춤
            n = len(gif_frames)
            total_ms = max(duration * 1000, n * GIF_MIN_FRAME_MS)
            ticks = [round(i * total_ms / n / 10) * 10 for i in range(n + 1)]
            durations = [max(GIF_MIN_FRAME_MS, b - a)
                         for a, b in zip(ticks, ticks[1:])]
            gif_frames[0].save(filepath, save_all=True,
                               append_images=gif_frames[1:],
                               duration=durations, loop=0, optimize=False)
            return n * 1000 / sum(durations)
        if writer is None:
            raise ValueError("읽을 수 있는 프레임이 없습니다.")
        return fps
    except BaseException:
        if writer is not None:
            writer.release()
            writer = None
        if os.path.exists(filepath):
            os.remove(filepath)  # 불완전한 파일 제거
        raise
    finally:
        if writer is not None:
            writer.release()


def _open_writer(filepath, fmt, fps, frame_size):
    for fourcc in FORMATS[fmt][1]:
        writer = cv2.VideoWriter(filepath, cv2.VideoWriter_fourcc(*fourcc),
                                 fps, frame_size, True)
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError(f"{fmt} 인코더를 열 수 없습니다.")


class VideoExportWorker(QThread):
    """동영상 인코딩을 백그라운드에서 수행"""

    progress = pyqtSignal(int, int)
    finished_export = pyqtSignal(bool, str)  # success, message

    def __init__(self, series, filepath, options, parent=None):
        super().__init__(parent)
        self._series = series
        self._filepath = filepath
        self._options = options
        self._cancel_event = threading.Event()
        self._last_percent = -1

    def cancel(self):
        self._cancel_event.set()

    def _on_progress(self, current, total):
        percent = current * 100 // total
        if percent != self._last_percent:
            self._last_percent = percent
            self.progress.emit(current, total)

    def run(self):
        try:
            fps = export_series_video(
                self._series, self._filepath,
                progress_callback=self._on_progress,
                cancel_event=self._cancel_event, **self._options)
            self.finished_export.emit(
                True, f"Exported: {self._filepath} ({fps:.1f} fps)")
        except ExportCancelled:
            self.finished_export.emit(False, "")
        except Exception as e:
            self.finished_export.emit(False, str(e))


class VideoExportDialog(QDialog):
    """동영상 내보내기 옵션 다이얼로그"""

    def __init__(self, series, window_center, window_width, inverted,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export as Video")
        self.setMinimumWidth(380)
        self._series = series
        self._wc = window_center
        self._ww = window_width
        self._inverted = inverted
        self._frames = count_frames(series)
        self._worker = None
        self._progress = None
        self._init_ui()
        self._update_fps()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(f"{self._series.modality}: {self._series.description}\n"
                      f"{self._frames} frames")
        layout.addWidget(info)

        form = QFormLayout()

        self._format_combo = QComboBox()
        self._format_combo.addItems(list(FORMATS))
        self._format_combo.currentIndexChanged.connect(self._update_fps)
        form.addRow("Format:", self._format_combo)

        self._duration_spin = QDoubleSpinBox()
        self._duration_spin.setRange(0.1, 3600)
        self._duration_spin.setDecimals(1)
        self._duration_spin.setSuffix(" s")
        # 기본값: 약 10fps가 되는 재생 시간 (1~60초)
        self._duration_spin.setValue(min(60, max(1, round(self._frames / 10))))
        self._duration_spin.valueChanged.connect(self._update_fps)
        form.addRow("Duration:", self._duration_spin)

        self._fps_label = QLabel()
        self._fps_label.setWordWrap(True)
        form.addRow("FPS:", self._fps_label)

        self._resolution_combo = QComboBox()
        for name, _ in RESOLUTIONS:
            self._resolution_combo.addItem(name)
        self._resolution_combo.setCurrentIndex(2)  # 512 x 512
        form.addRow("Resolution:", self._resolution_combo)

        wl_text = f"현재 W/L 적용 (W: {self._ww:.0f}  L: {self._wc:.0f})"
        if self._inverted:
            wl_text += " + 반전"
        self._wl_check = QCheckBox(wl_text)
        self._wl_check.setChecked(True)
        self._wl_check.setToolTip("해제하면 시리즈 전체 밝기 범위로 자동 매핑")
        form.addRow("Windowing:", self._wl_check)

        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        export_btn = QPushButton("Export...")
        export_btn.setDefault(True)
        export_btn.clicked.connect(self._export)
        btn_layout.addWidget(export_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _update_fps(self):
        duration = self._duration_spin.value()
        frame_ms = duration * 1000 / self._frames
        text = f"{self._frames / duration:.2f} fps  ({self._frames} frames / {duration:g} s)"
        if (self._format_combo.currentText() == 'GIF'
                and frame_ms < GIF_MIN_FRAME_MS):
            step = int(np.ceil(GIF_MIN_FRAME_MS / frame_ms))
            text += (f"\nGIF는 최대 {1000 / GIF_MIN_FRAME_MS:.0f} fps → "
                     f"{step}프레임마다 1장만 사용")
        self._fps_label.setText(text)

    def _export(self):
        fmt = self._format_combo.currentText()
        ext = FORMATS[fmt][0]
        from .batch_video import safe_name   # 일괄 내보내기와 같은 이름 규칙 (단일은 번호 없음)
        default_name = safe_name(self._series.description)
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Video", default_name + ext, f"{fmt} (*{ext})")
        if not filepath:
            return
        if not filepath.lower().endswith(ext):
            filepath += ext

        apply_wl = self._wl_check.isChecked()
        options = {
            'fmt': fmt,
            'duration': self._duration_spin.value(),
            'size': RESOLUTIONS[self._resolution_combo.currentIndex()][1],
            'window': (self._wc, self._ww) if apply_wl else None,
            'inverted': self._inverted and apply_wl,
        }

        self._progress = QProgressDialog("Encoding video...", "Cancel",
                                         0, 100, self)
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)

        self._worker = VideoExportWorker(self._series, filepath, options, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_export.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._progress.canceled.connect(self._worker.cancel)
        self._worker.start()

    def _on_progress(self, current, total):
        if self._progress is not None:
            self._progress.setLabelText(
                f"Encoding video... ({current}/{total})")
            self._progress.setValue(current * 100 // total)

    def _on_finished(self, success, message):
        self._worker = None
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        if success:
            self.result_message = message
            self.accept()
        elif message:
            QMessageBox.critical(self, "Export Failed", message)

    def reject(self):
        # 인코딩 중 닫으면 먼저 취소하고 스레드 종료를 기다림
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait()
        super().reject()
