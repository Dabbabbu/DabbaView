"""
2D MPR (Multi-Planar Reconstruction) 뷰어
Axial, Sagittal, Coronal 3평면 재구성
"""
import numpy as np
from PyQt5.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                              QGridLayout, QFrame)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont

from .geometry import build_series_geometry


class MPRSliceView(QWidget):
    """단일 MPR 평면 뷰"""

    crosshair_moved = pyqtSignal(str, int, int)  # plane, x, y

    def __init__(self, plane_name="Axial", parent=None):
        super().__init__(parent)
        self.plane_name = plane_name
        self.setMinimumSize(256, 256)
        self.setMouseTracking(True)

        self._volume = None  # 3D numpy array
        self._slice_index = 0
        self._max_index = 0
        self._window_center = 400.0
        self._window_width = 2000.0
        self._pixmap = None
        # 표시 영상의 (세로 픽셀 간격 / 가로 픽셀 간격) - mm 기준 비율로 표시
        self._pixel_aspect = 1.0

        # 크로스헤어 위치 (0~1 정규화)
        self._crosshair_x = 0.5
        self._crosshair_y = 0.5
        self._show_crosshair = True

        # 마우스 상태
        self._dragging = False
        self._last_pos = None

        # 색상
        self._colors = {
            "Axial": QColor(255, 100, 100),     # 빨강
            "Sagittal": QColor(100, 255, 100),   # 초록
            "Coronal": QColor(100, 100, 255),    # 파랑
        }

    def set_volume(self, volume, wc=400, ww=2000):
        """3D 볼륨 데이터 설정"""
        self._volume = volume
        self._window_center = wc
        self._window_width = ww
        if volume is not None:
            if self.plane_name == "Axial":
                self._max_index = volume.shape[0] - 1
            elif self.plane_name == "Sagittal":
                self._max_index = volume.shape[2] - 1
            elif self.plane_name == "Coronal":
                self._max_index = volume.shape[1] - 1
            self._slice_index = self._max_index // 2
        self._update_image()

    def set_slice(self, index):
        """슬라이스 인덱스 설정"""
        self._slice_index = max(0, min(self._max_index, index))
        self._update_image()
        self.update()

    def set_window(self, center, width):
        self._window_center = center
        self._window_width = max(1, width)
        self._update_image()
        self.update()

    def set_pixel_aspect(self, aspect):
        self._pixel_aspect = aspect if aspect and aspect > 0 else 1.0
        self.update()

    def _image_rect(self):
        """영상을 물리적 비율(mm)로 위젯 중앙에 맞춘 영역 (x, y, w, h)"""
        pw, ph = self._pixmap.width(), self._pixmap.height() * self._pixel_aspect
        scale = min(self.width() / pw, self.height() / ph)
        w, h = max(1, int(pw * scale)), max(1, int(ph * scale))
        return (self.width() - w) // 2, (self.height() - h) // 2, w, h

    def set_crosshair(self, x, y):
        """크로스헤어 위치 설정 (0~1)"""
        self._crosshair_x = max(0, min(1, x))
        self._crosshair_y = max(0, min(1, y))
        self.update()

    def _get_slice(self):
        """현재 평면의 슬라이스 추출"""
        if self._volume is None:
            return None
        idx = self._slice_index
        if self.plane_name == "Axial":
            return self._volume[idx, :, :]
        elif self.plane_name == "Sagittal":
            return self._volume[:, :, idx]
        elif self.plane_name == "Coronal":
            return self._volume[:, idx, :]
        return None

    def _update_image(self):
        """슬라이스를 QPixmap으로 변환"""
        slice_data = self._get_slice()
        if slice_data is None:
            self._pixmap = None
            return

        # 윈도잉
        wc, ww = self._window_center, self._window_width
        low = wc - ww / 2
        img = np.clip((slice_data - low) / max(ww, 1) * 255, 0, 255)
        img = img.astype(np.uint8)

        # Sagittal/Coronal은 상하 반전
        if self.plane_name in ("Sagittal", "Coronal"):
            img = np.flipud(img)

        # Sagittal/Coronal 단면과 flipud 결과는 비연속 뷰 → QImage에 넘기기 전 연속화
        img = np.ascontiguousarray(img)
        h, w = img.shape
        qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
        self._pixmap = QPixmap.fromImage(qimg)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._pixmap:
            # 뷰포트에 맞추기 (mm 비율 유지)
            x, y, w, h = self._image_rect()
            scaled = self._pixmap.scaled(w, h, Qt.IgnoreAspectRatio,
                                         Qt.SmoothTransformation)
            painter.drawPixmap(x, y, scaled)

            # 크로스헤어
            if self._show_crosshair:
                cx = x + int(self._crosshair_x * w)
                cy = y + int(self._crosshair_y * h)
                color = self._colors.get(self.plane_name, QColor(255, 255, 0))
                pen = QPen(color, 1, Qt.DashLine)
                painter.setPen(pen)
                painter.drawLine(cx, y, cx, y + h)
                painter.drawLine(x, cy, x + w, cy)

        # 라벨
        color = self._colors.get(self.plane_name, QColor(255, 255, 255))
        painter.setPen(color)
        painter.setFont(QFont("Arial", 12, QFont.Bold))
        painter.drawText(10, 22, self.plane_name)
        painter.setFont(QFont("Arial", 10))
        painter.drawText(10, 40,
                         f"Slice: {self._slice_index + 1}/{self._max_index + 1}")

        painter.end()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta > 0:
            self.set_slice(self._slice_index - 1)
        else:
            self.set_slice(self._slice_index + 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._update_crosshair_from_mouse(event.pos())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._update_crosshair_from_mouse(event.pos())

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def _update_crosshair_from_mouse(self, pos):
        if not self._pixmap:
            return
        x_off, y_off, w, h = self._image_rect()

        nx = (pos.x() - x_off) / w
        ny = (pos.y() - y_off) / h
        self._crosshair_x = max(0, min(1, nx))
        self._crosshair_y = max(0, min(1, ny))
        self.crosshair_moved.emit(
            self.plane_name,
            int(self._crosshair_x * 1000),
            int(self._crosshair_y * 1000))
        self.update()


class MPRWidget(QWidget):
    """3평면 MPR 뷰어 (Axial + Sagittal + Coronal + 정보)

    볼륨 인덱스: volume[k, row, col]
      - k: 정렬된 슬라이스 순서, row/col: 원본 슬라이스의 행/열
    화면 정규화 좌표 (0~1):
      - Axial:    x = col, y = row           (슬라이스 = k)
      - Sagittal: x = row, y = 1 - k (상하반전) (슬라이스 = col)
      - Coronal:  x = col, y = 1 - k (상하반전) (슬라이스 = row)
    """

    reference_point_selected = pyqtSignal(object)  # 환자 좌표 (mm)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = None
        self._series = None
        self._geometry = None  # 볼륨에 들어간 슬라이스들의 SeriesGeometry
        self._voxel = (0.0, 0.0, 0.0)  # (k, row, col)
        self._init_ui()
        self._connect_signals()

    def _init_ui(self):
        layout = QGridLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)

        self.axial_view = MPRSliceView("Axial")
        self.sagittal_view = MPRSliceView("Sagittal")
        self.coronal_view = MPRSliceView("Coronal")

        # 정보 패널
        self.info_panel = QWidget()
        self.info_panel.setStyleSheet("background-color: #1a1a1a;")
        info_layout = QVBoxLayout(self.info_panel)
        self._info_label = QLabel("MPR Viewer\n\n"
                                   "마우스 휠: 슬라이스 이동\n"
                                   "좌클릭 드래그: 크로스헤어 이동\n\n"
                                   "각 뷰의 크로스헤어가\n"
                                   "다른 뷰의 슬라이스 위치를\n"
                                   "나타냅니다.\n\n"
                                   "Sync Cursor를 켜면 같은 좌표계\n"
                                   "(Frame of Reference)의 다른 뷰와\n"
                                   "크로스헤어가 연동됩니다.")
        self._info_label.setStyleSheet("color: #aaa; font-size: 12px;")
        self._info_label.setAlignment(Qt.AlignTop)
        info_layout.addWidget(self._info_label)

        # 2x2 그리드
        layout.addWidget(self.axial_view, 0, 0)
        layout.addWidget(self.sagittal_view, 0, 1)
        layout.addWidget(self.coronal_view, 1, 0)
        layout.addWidget(self.info_panel, 1, 1)

    def _connect_signals(self):
        self.axial_view.crosshair_moved.connect(self._on_crosshair)
        self.sagittal_view.crosshair_moved.connect(self._on_crosshair)
        self.coronal_view.crosshair_moved.connect(self._on_crosshair)

    def _on_crosshair(self, plane, x_norm, y_norm):
        """크로스헤어 이동 시 복셀 위치 갱신 → 다른 뷰 + Sync Cursor 전파"""
        if self._volume is None:
            return

        x = x_norm / 1000.0
        y = y_norm / 1000.0
        d, h, w = self._volume.shape

        if plane == "Axial":
            k = self.axial_view._slice_index
            row, col = y * h - 0.5, x * w - 0.5
        elif plane == "Sagittal":
            col = self.sagittal_view._slice_index
            row, k = x * h - 0.5, (1 - y) * d - 0.5
        else:  # Coronal
            row = self.coronal_view._slice_index
            col, k = x * w - 0.5, (1 - y) * d - 0.5

        self._set_voxel(k, row, col)
        point = self.voxel_to_patient(*self._voxel)
        if point is not None:
            self.reference_point_selected.emit(point)

    def _set_voxel(self, k, row, col):
        """복셀 위치로 세 평면의 슬라이스와 크로스헤어를 맞춤"""
        d, h, w = self._volume.shape
        k = min(max(k, 0), d - 1)
        row = min(max(row, 0), h - 1)
        col = min(max(col, 0), w - 1)
        self._voxel = (k, row, col)

        self.axial_view.set_slice(int(round(k)))
        self.sagittal_view.set_slice(int(round(col)))
        self.coronal_view.set_slice(int(round(row)))

        self.axial_view.set_crosshair((col + 0.5) / w, (row + 0.5) / h)
        self.sagittal_view.set_crosshair((row + 0.5) / h, 1 - (k + 0.5) / d)
        self.coronal_view.set_crosshair((col + 0.5) / w, 1 - (k + 0.5) / d)

    # ─── 환자 좌표 변환 (크로스 레퍼런스) ───

    def voxel_to_patient(self, k, row, col):
        g = self._geometry
        if g is None:
            return None
        k0 = int(np.floor(k))
        k1 = min(k0 + 1, g.num_slices - 1)
        frac = k - k0
        p0 = g.pixel_to_patient(k0, col, row)
        p1 = g.pixel_to_patient(k1, col, row)
        return p0 + (p1 - p0) * frac

    def patient_to_voxel(self, point):
        g = self._geometry
        if g is None:
            return None
        index, _ = g.nearest_slice(point)
        col, row, _ = g.patient_to_pixel(index, point)
        # 슬라이스 법선 방향 위치로 k를 소수점까지 보간
        positions = g.origins @ g.normals[0]
        p = float(np.asarray(point, dtype=float) @ g.normals[0])
        idx = np.arange(g.num_slices, dtype=float)
        if positions[-1] < positions[0]:
            positions, idx = positions[::-1], idx[::-1]
        k = float(np.interp(p, positions, idx))
        return k, row, col

    @property
    def series(self):
        return self._series

    def set_sync_cursor_enabled(self, enabled):
        pass  # MPR 자체 크로스헤어는 항상 표시

    def set_reference_point(self, point, navigate=True):
        """다른 뷰의 기준점으로 세 평면 이동"""
        if self._volume is None:
            return
        voxel = self.patient_to_voxel(point)
        if voxel is not None:
            self._set_voxel(*voxel)

    def clear_reference_point(self):
        pass

    def set_series(self, series):
        """DicomSeries로부터 3D 볼륨 구성"""
        if not series or series.num_slices < 2:
            return

        series.sort_slices()
        slices, kept = [], []
        for i, arr in enumerate(series.get_all_pixel_arrays()):
            if arr is not None and len(arr.shape) == 2:
                slices.append(arr)
                kept.append(i)

        if not slices:
            return

        # 크기 통일 (첫 슬라이스 기준)
        ref_shape = slices[0].shape
        keep = [j for j, s in enumerate(slices) if s.shape == ref_shape]
        if len(keep) < 2:
            return

        self._volume = np.stack([slices[j] for j in keep], axis=0)
        self._series = series
        # 볼륨에 실제로 들어간 슬라이스만으로 공간 정보 구성
        self._geometry = build_series_geometry(
            [series.slices[kept[j]] for j in keep])

        # 표시 비율: Axial = 행/열 간격, Sagittal = 슬라이스/행 간격, Coronal = 슬라이스/열 간격
        if self._geometry is not None:
            d_row, d_col = self._geometry.spacings[0]
            d_slice = self._geometry.slice_spacing() or d_row
            self.axial_view.set_pixel_aspect(d_row / d_col)
            self.sagittal_view.set_pixel_aspect(d_slice / d_row)
            self.coronal_view.set_pixel_aspect(d_slice / d_col)
        else:
            for view in (self.axial_view, self.sagittal_view, self.coronal_view):
                view.set_pixel_aspect(1.0)

        wc, ww = series.get_default_window()
        self.axial_view.set_volume(self._volume, wc, ww)
        self.sagittal_view.set_volume(self._volume, wc, ww)
        self.coronal_view.set_volume(self._volume, wc, ww)
        d, h, w = self._volume.shape
        self._set_voxel(d // 2, h // 2, w // 2)

    def sync_geometry(self):
        return self._geometry

    def set_window(self, center, width):
        self.axial_view.set_window(center, width)
        self.sagittal_view.set_window(center, width)
        self.coronal_view.set_window(center, width)
