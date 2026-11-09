# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
2D MPR (Multi-Planar Reconstruction) 뷰어
Axial, Sagittal, Coronal 3평면 재구성 + Oblique(사선) 회전
원본 시리즈가 어느 방향(Axial · Sagittal · Coronal)으로 찍혔든 IOP/IPP로 환자 좌표계를
읽어 세 뷰가 실제 해부학적 평면이 되게 한다 (anatomical_frames 참고).

좌표계: 볼륨 mm 좌표 X = (k * Δk, row * Δrow, col * Δcol)
  - 세 평면이 공유하는 중심점 C(mm)를 지나고, 평면마다 직교 기저 (u, v, n)을 가짐
    u = 화면 오른쪽, v = 화면 아래쪽, n = 평면 법선
  - 한 평면에서 크로스헤어를 회전하면 나머지 두 평면이 그 평면의 법선을 축으로
    함께 회전 → 세 평면은 항상 서로 직교
  - 각 평면 영상은 scipy.ndimage.affine_transform으로 볼륨에서 임의 각도 추출
"""
import math
import time

import numpy as np
from scipy.ndimage import affine_transform
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
                              QPushButton, QComboBox, QSpinBox)
from PyQt5.QtCore import Qt, pyqtSignal, QPointF, QRectF
from PyQt5.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont, QBrush

from .geometry import build_series_geometry
from .phases import phase_map

PLANES = ("Axial", "Sagittal", "Coronal")

PLANE_COLORS = {
    "Axial": QColor(255, 100, 100),     # 빨강
    "Sagittal": QColor(100, 255, 100),  # 초록
    "Coronal": QColor(100, 100, 255),   # 파랑
}

_E_K = np.array([1.0, 0.0, 0.0])
_E_ROW = np.array([0.0, 1.0, 0.0])
_E_COL = np.array([0.0, 0.0, 1.0])

# 표준 평면 기저 (u, v, n) - 볼륨 인덱스 축 (k, row, col) 기준
# 위치 정보가 없어 원본 슬라이스를 Axial로 가정할 때 쓰는 기본값
BASE_FRAMES = {
    "Axial": (_E_COL, _E_ROW, _E_K),           # x = col, y = row
    "Sagittal": (_E_ROW, -_E_K, _E_COL),       # x = row, y = -k (상하반전)
    "Coronal": (_E_COL, -_E_K, _E_ROW),        # x = col, y = -k (상하반전)
}

# 환자 좌표계(LPS: +x = Left, +y = Posterior, +z = Superior) 기준 표준 평면 기저 (u, v, n)
#   Axial:    화면 오른쪽 = L, 아래 = P, 넘김 = S (방사선 관례: 발 쪽에서 올려다봄)
#   Sagittal: 화면 오른쪽 = P, 아래 = I, 넘김 = L (앞쪽이 화면 왼쪽)
#   Coronal:  화면 오른쪽 = L, 아래 = I, 넘김 = P
PATIENT_FRAMES = {
    "Axial": (np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0])),
    "Sagittal": (np.array([0, 1.0, 0]), np.array([0, 0, -1.0]), np.array([1.0, 0, 0])),
    "Coronal": (np.array([1.0, 0, 0]), np.array([0, 0, -1.0]), np.array([0, 1.0, 0])),
}


def volume_axes_patient(geometry):
    """볼륨 인덱스 축 (k, row, col)이 환자 좌표계에서 향하는 단위 벡터 (3, 3) — 행 = 축

    geometry.row_dirs는 열 인덱스(col) 증가 방향, col_dirs는 행 인덱스(row) 증가 방향.
    k 방향은 슬라이스 법선 중 origins가 증가하는 쪽.
    """
    if geometry is None or geometry.num_slices < 2:
        return None
    n = geometry.normals[0]
    sign = 1.0 if float((geometry.origins[-1] - geometry.origins[0]) @ n) >= 0 else -1.0
    return np.array([sign * n, geometry.col_dirs[0], geometry.row_dirs[0]], dtype=float)


def anatomical_frames(axes):
    """환자 좌표계 표준 평면을 볼륨 축에 가장 가까운 축으로 스냅한 기저 {plane: (u, v, n)}

    원본 시리즈가 Sagittal · Coronal로 찍혔어도 Axial/Sagittal/Coronal 뷰가 실제 해부학적
    평면이 되게 한다. 축에 스냅하므로 표준 평면은 그대로 복셀 격자를 따라(보간 없이) 추출된다.
    축이 겹치면(심하게 기울어진 oblique 촬영) None → 기본 BASE_FRAMES 사용.
    """
    if axes is None:
        return None
    frames = {}
    for plane, vectors in PATIENT_FRAMES.items():
        basis, used = [], set()
        for p in vectors:
            comp = axes @ p                       # 각 볼륨 축에 투영한 성분
            axis = int(np.argmax(np.abs(comp)))
            if axis in used:
                return None
            used.add(axis)
            e = np.zeros(3)
            e[axis] = 1.0 if comp[axis] >= 0 else -1.0
            basis.append(e)
        frames[plane] = tuple(basis)
    return frames


def source_orientation(axes):
    """원본 슬라이스(k 축 법선)가 어느 표준 평면에 가장 가까운지 + 기울기(°)"""
    if axes is None:
        return None, 0.0
    n = axes[0]
    axis = int(np.argmax(np.abs(n)))
    tilt = math.degrees(math.acos(min(1.0, abs(float(n[axis])))))
    return ("Sagittal", "Coronal", "Axial")[axis], tilt

MAX_OUTPUT_SIZE = 1024  # Oblique 재구성 영상 한 변 최대 픽셀
HANDLE_RADIUS = 6       # 회전 핸들 반지름 (px)
FINE_ROTATION_GAIN = 0.25  # Shift+드래그 시 회전 감도


def rotation_matrix(axis, angle_rad):
    """단위 벡터 axis를 축으로 angle_rad 회전하는 3x3 행렬 (로드리게스 공식)"""
    axis = np.asarray(axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    K = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + math.sin(angle_rad) * K + (1 - math.cos(angle_rad)) * (K @ K)


def _is_axis_aligned(vec, tol=1e-6):
    return float(np.max(np.abs(vec))) > 1 - tol


class MPRSliceView(QWidget):
    """단일 MPR 평면 뷰 (표시 + 마우스 입력만 담당, 재구성은 MPRWidget)"""

    point_picked = pyqtSignal(str, float, float)   # plane, 영상 픽셀 (x, y)
    scrolled = pyqtSignal(str, int)                # plane, 방향 (+1/-1)
    rotation_started = pyqtSignal(str)             # plane
    rotation_changed = pyqtSignal(str, float, bool)  # plane, 시작 대비 각도(°), 미세조절

    def __init__(self, plane_name="Axial", parent=None):
        super().__init__(parent)
        self.plane_name = plane_name   # 슬롯 키 (색 · 신호용)
        self.display_name = plane_name  # 화면에 적는 이름 (촬영 평면 모드에서는 SAX · 4CH 등)
        self.setMinimumSize(256, 256)
        self.setMouseTracking(True)

        self._image = None   # 재구성된 float32 2D 영상
        self._pixmap = None
        self._window_center = 400.0
        self._window_width = 2000.0
        # 표시 영상의 (세로 픽셀 간격 / 가로 픽셀 간격) - mm 기준 비율로 표시
        self._pixel_aspect = 1.0

        # 크로스헤어 중심 (영상 픽셀 좌표) + 다른 평면 교선 [(plane, (dx, dy))]
        self._center = None
        self._lines = []
        self._info = []   # 좌상단 표시 문자열 목록
        self._show_crosshair = True

        # 마우스 상태
        self._mode = None  # None / "move" / "rotate"
        self._rot_start = 0.0
        self._rot_prev = 0.0
        self._rot_accum = 0.0
        self._rot_fine = False

    # ─── 외부에서 설정 ───

    def set_image(self, image, pixel_aspect):
        self._image = image
        self._pixel_aspect = pixel_aspect if pixel_aspect and pixel_aspect > 0 else 1.0
        self._update_pixmap()
        self.update()

    def set_window(self, center, width):
        self._window_center = center
        self._window_width = max(1, width)
        self._update_pixmap()
        self.update()

    def set_label(self, text):
        self.display_name = text or self.plane_name
        self.update()

    def set_overlay(self, center, lines, info):
        self._center = center
        self._lines = lines
        self._info = info
        self.update()

    def _update_pixmap(self):
        if self._image is None:
            self._pixmap = None
            return
        wc, ww = self._window_center, self._window_width
        low = wc - ww / 2
        img = np.clip((self._image - low) / max(ww, 1) * 255, 0, 255)
        img = np.ascontiguousarray(img.astype(np.uint8))
        h, w = img.shape
        qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
        self._pixmap = QPixmap.fromImage(qimg)

    # ─── 좌표 변환 ───

    def _image_rect(self):
        """영상을 물리적 비율(mm)로 위젯 중앙에 맞춘 영역 (x, y, w, h)"""
        pw, ph = self._pixmap.width(), self._pixmap.height() * self._pixel_aspect
        scale = min(self.width() / pw, self.height() / ph)
        w, h = max(1, int(pw * scale)), max(1, int(ph * scale))
        return (self.width() - w) // 2, (self.height() - h) // 2, w, h

    def _image_to_screen(self, px, py):
        x, y, w, h = self._image_rect()
        return (x + (px + 0.5) / self._pixmap.width() * w,
                y + (py + 0.5) / self._pixmap.height() * h)

    def _screen_to_image(self, sx, sy):
        x, y, w, h = self._image_rect()
        return ((sx - x) / w * self._pixmap.width() - 0.5,
                (sy - y) / h * self._pixmap.height() - 0.5)

    def _screen_center(self):
        if self._pixmap is None or self._center is None:
            return None
        return self._image_to_screen(*self._center)

    def _screen_direction(self, d):
        """영상 픽셀 방향 → 화면 단위 벡터"""
        x, y, w, h = self._image_rect()
        dx = d[0] * w / self._pixmap.width()
        dy = d[1] * h / self._pixmap.height()
        norm = math.hypot(dx, dy)
        return (dx / norm, dy / norm) if norm > 1e-9 else (1.0, 0.0)

    def _handle_positions(self):
        """회전 핸들 화면 위치 [(plane, x, y)] - 각 교선 양 끝"""
        center = self._screen_center()
        if center is None:
            return []
        x, y, w, h = self._image_rect()
        cx, cy = center
        r = 0.42 * min(w, h)
        handles = []
        for plane, d in self._lines:
            dx, dy = self._screen_direction(d)
            for s in (1, -1):
                sx, sy = s * dx, s * dy
                # 크로스헤어가 가장자리에 가까워도 핸들이 영상 안에 남도록
                edge = min((x + w - cx) / sx if sx > 1e-9 else
                           (x - cx) / sx if sx < -1e-9 else math.inf,
                           (y + h - cy) / sy if sy > 1e-9 else
                           (y - cy) / sy if sy < -1e-9 else math.inf)
                dist = min(r, max(24.0, edge - 2 * HANDLE_RADIUS))
                handles.append((plane, cx + dist * sx, cy + dist * sy))
        return handles

    def _hit_handle(self, pos):
        for _, hx, hy in self._handle_positions():
            if math.hypot(pos.x() - hx, pos.y() - hy) <= HANDLE_RADIUS + 4:
                return True
        return False

    def _mouse_angle(self, pos):
        cx, cy = self._screen_center()
        return math.degrees(math.atan2(pos.y() - cy, pos.x() - cx))

    # ─── 그리기 ───

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._pixmap:
            # 뷰포트에 맞추기 (mm 비율 유지)
            x, y, w, h = self._image_rect()
            scaled = self._pixmap.scaled(w, h, Qt.IgnoreAspectRatio,
                                         Qt.SmoothTransformation)
            painter.drawPixmap(x, y, scaled)

            center = self._screen_center()
            if self._show_crosshair and center is not None:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.save()
                painter.setClipRect(x, y, w, h)
                cx, cy = center
                reach = 2 * (w + h)
                for plane, d in self._lines:
                    dx, dy = self._screen_direction(d)
                    painter.setPen(QPen(PLANE_COLORS[plane], 1, Qt.DashLine))
                    painter.drawLine(QPointF(cx - reach * dx, cy - reach * dy),
                                     QPointF(cx + reach * dx, cy + reach * dy))
                painter.restore()
                # 회전 핸들 (교선 끝)
                for plane, hx, hy in self._handle_positions():
                    color = PLANE_COLORS[plane]
                    painter.setPen(QPen(color.lighter(130), 1.5))
                    painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
                    painter.drawEllipse(QRectF(hx - HANDLE_RADIUS, hy - HANDLE_RADIUS,
                                               2 * HANDLE_RADIUS, 2 * HANDLE_RADIUS))
                painter.setBrush(Qt.NoBrush)

        # 라벨 + 각도 오버레이
        color = PLANE_COLORS.get(self.plane_name, QColor(255, 255, 255))
        painter.setPen(color)
        painter.setFont(QFont("Arial", 12, QFont.Bold))
        painter.drawText(10, 22, self.display_name)
        painter.setFont(QFont("Arial", 10))
        for i, text in enumerate(self._info):
            painter.drawText(10, 40 + i * 16, text)

        painter.end()

    # ─── 마우스 ───

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            return
        # 한 칸마다 한 장, 빠르게 연달아 굴리면 가속, 트랙패드는 모이는 만큼 (2D 뷰와 같음)
        if not hasattr(self, "_wheel_accel"):
            from .mouse_feel import WheelAccel
            self._wheel_accel = WheelAccel()
        steps = self._wheel_accel.feed(delta, time.monotonic() * 1000.0)
        for _ in range(min(abs(steps), 20)):
            self.scrolled.emit(self.plane_name, -1 if steps > 0 else 1)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self._pixmap is None:
            return
        if self._hit_handle(event.pos()):
            self._mode = "rotate"
            self._begin_rotation(event)
        else:
            self._mode = "move"
            self._emit_point(event.pos())

    def _begin_rotation(self, event):
        self._rot_fine = bool(event.modifiers() & Qt.ShiftModifier)
        self._rot_start = self._rot_prev = self._mouse_angle(event.pos())
        self._rot_accum = 0.0
        self.rotation_started.emit(self.plane_name)
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._mode == "move":
            self._emit_point(event.pos())
        elif self._mode == "rotate":
            fine = bool(event.modifiers() & Qt.ShiftModifier)
            if fine != self._rot_fine:
                # 드래그 도중 Shift 전환 → 현재 상태를 기준으로 다시 시작
                self._begin_rotation(event)
                return
            angle = self._mouse_angle(event.pos())
            step = (angle - self._rot_prev + 180) % 360 - 180  # ±180 경계 넘김 보정
            self._rot_prev = angle
            self._rot_accum += step
            self.rotation_changed.emit(self.plane_name, self._rot_accum, fine)
        elif self._pixmap is not None:
            self.setCursor(Qt.OpenHandCursor if self._hit_handle(event.pos())
                           else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._mode == "rotate":
            self.setCursor(Qt.OpenHandCursor if self._hit_handle(event.pos())
                           else Qt.ArrowCursor)
        self._mode = None

    def _emit_point(self, pos):
        px, py = self._screen_to_image(pos.x(), pos.y())
        px = min(max(px, 0), self._pixmap.width() - 1)
        py = min(max(py, 0), self._pixmap.height() - 1)
        self.point_picked.emit(self.plane_name, px, py)




# ─── 촬영 평면(심장 2CH · 4CH · SAX 등) 이름 ───

CARDIAC_NAMES = (
    ("4CH", ("4ch", "4-ch", "4 ch", "four", "hla", "horizontal")),
    ("2CH", ("2ch", "2-ch", "2 ch", "two", "vla", "vertical")),
    ("3CH", ("3ch", "3-ch", "3 ch", "three", "lvot")),
    ("SAX", ("sax", "short", "sa_", "sa ", "short axis", "shortaxis")),
    ("LAX", ("lax", "long")),
)


def view_short_name(series, fallback="Series"):
    """SeriesDescription에서 심장 뷰 이름(4CH · 2CH · 3CH · SAX · LAX)을 뽑고, 없으면 설명 앞부분"""
    desc = str(getattr(series, "description", "") or "")
    low = desc.lower()
    for name, keys in CARDIAC_NAMES:
        if any(k in low for k in keys):
            return name
    desc = desc.strip()
    return (desc[:12] + "…") if len(desc) > 13 else (desc or fallback)


def cardiac_rank(series):
    """평면 후보 우선순위: 4CH → 2CH → 3CH → 그 밖의 LAX → 나머지"""
    name = view_short_name(series, "")
    order = {"4CH": 0, "2CH": 1, "3CH": 2, "LAX": 3}
    return order.get(name, 4)


def _series_geometry(series):
    g = getattr(series, "geometry", None)
    if g is None and getattr(series, "slices", None):
        g = build_series_geometry(series.slices)
    return g


def _unit(v):
    v = np.asarray(v, dtype=float)
    norm = np.linalg.norm(v)
    return v / norm if norm > 1e-12 else v


def volume_indices(series, phase=0):
    """볼륨에 쌓을 슬라이스 번호(공간 순서). cine · perfusion처럼 위치마다 여러 위상이면 한 위상만

    → (indices, PhaseMap 또는 None)
    """
    pm = phase_map(series)
    order = series.spatial_order()
    if pm is None:
        return list(order), None
    phase = max(0, min(pm.n_phases - 1, int(phase)))
    chosen = {pm.slice_at(pos, phase) for pos in range(pm.n_positions)}
    chosen.discard(None)
    return [i for i in order if i in chosen], pm


def num_positions(series):
    """시리즈의 공간 위치 수 (위상이 있으면 위치 수, 없으면 슬라이스 수)"""
    pm = phase_map(series)
    return pm.n_positions if pm is not None else int(getattr(series, "num_slices", 0) or 0)


MODE_ANATOMICAL = "anatomical"   # Axial · Sagittal · Coronal (환자 좌표계)
MODE_ACQUIRED = "acquired"       # 촬영 평면: 스택(SAX 등) + 같이 불러온 2CH · 4CH 평면

MODE_LABELS = {MODE_ANATOMICAL: "해부학적 (Axial · Sagittal · Coronal)",
               MODE_ACQUIRED: "촬영 평면 (SAX · 2CH · 4CH 등)"}


class MPRWidget(QWidget):
    """3평면 MPR 뷰어 (Axial + Sagittal + Coronal + 정보)

    볼륨 인덱스: volume[k, row, col]
      - k: 정렬된 슬라이스 순서, row/col: 원본 슬라이스의 행/열

    평면 기준 두 가지:
      - 해부학적: 원본 촬영 방향에 상관없이 Axial · Sagittal · Coronal
      - 촬영 평면: 스택 자체 평면(SAX 등)과, 같은 좌표계로 같이 불러온 다른 방향 시리즈
        (2CH · 4CH …)의 평면으로 재구성. 심장 MRI처럼 이중 사선(double-oblique) 촬영용.
        2CH · 4CH 같은 한 장짜리 cine을 고르면 같은 검사의 스택(SAX)에서 볼륨을 가져온다.
    """

    reference_point_selected = pyqtSignal(object)  # 환자 좌표 (mm)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = None
        self._series = None          # 볼륨을 만든 시리즈 (스택)
        self._selected = None        # 사용자가 고른 시리즈 (2CH 한 장짜리면 _series와 다름)
        self._related = []           # 같이 불러온 다른 시리즈 (평면 후보)
        self._geometry = None  # 볼륨에 들어간 슬라이스들의 SeriesGeometry
        self._spacing = np.ones(3)          # (Δk, Δrow, Δcol) mm
        self._center = np.zeros(3)          # 세 평면 공유 중심점 (볼륨 mm 좌표)
        self._frames = {}                   # plane → (u, v, n)
        self._angles = {}                   # plane → 해당 뷰에서 회전한 누적 각도(°)
        self._specs = {}                    # plane → 마지막 재구성 정보
        self._cval = 0.0
        self._rot_snapshot = None
        self._base_frames = BASE_FRAMES        # 이 볼륨의 표준 평면 기저
        self._labels = {p: p for p in PLANES}  # 슬롯 → 화면 이름
        self._source_plane = None              # 원본 슬라이스가 가장 가까운 표준 평면
        self._source_tilt = 0.0
        self._mode = MODE_ANATOMICAL
        self._plane_sources = []               # 촬영 평면 모드에서 쓴 (이름, 시리즈) 목록
        self._phase_map = None
        self._phase = 0
        self._updating_ui = False
        self._reset_frames()
        self._init_ui()
        self._connect_signals()

    def _init_ui(self):
        layout = QGridLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)

        self.axial_view = MPRSliceView("Axial")
        self.sagittal_view = MPRSliceView("Sagittal")
        self.coronal_view = MPRSliceView("Coronal")
        self._views = {"Axial": self.axial_view, "Sagittal": self.sagittal_view,
                       "Coronal": self.coronal_view}

        # 정보 패널
        self.info_panel = QWidget()
        self.info_panel.setStyleSheet("background-color: #1a1a1a;")
        info_layout = QVBoxLayout(self.info_panel)

        # 평면 기준 선택 (해부학적 / 촬영 평면)
        mode_row = QHBoxLayout()
        mode_caption = QLabel("평면 기준")
        mode_caption.setStyleSheet("color: #aaa; font-size: 12px;")
        mode_row.addWidget(mode_caption)
        self._mode_combo = QComboBox()
        for key in (MODE_ANATOMICAL, MODE_ACQUIRED):
            self._mode_combo.addItem(MODE_LABELS[key], key)
        self._mode_combo.setToolTip(
            "해부학적: 촬영 방향에 상관없이 Axial · Sagittal · Coronal\n"
            "촬영 평면: 스택 평면(SAX 등) + 같이 불러온 2CH · 4CH 등 다른 방향 시리즈의 평면")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo, 1)
        info_layout.addLayout(mode_row)

        # 위상 (cine) 선택
        phase_row = QHBoxLayout()
        self._phase_caption = QLabel("위상")
        self._phase_caption.setStyleSheet("color: #aaa; font-size: 12px;")
        phase_row.addWidget(self._phase_caption)
        self._phase_spin = QSpinBox()
        self._phase_spin.setMinimum(1)
        self._phase_spin.setToolTip("cine처럼 한 위치에 여러 위상이 있으면 한 위상으로 볼륨을 만듭니다")
        self._phase_spin.valueChanged.connect(self._on_phase_changed)
        phase_row.addWidget(self._phase_spin)
        self._phase_total = QLabel("")
        self._phase_total.setStyleSheet("color: #aaa; font-size: 12px;")
        phase_row.addWidget(self._phase_total, 1)
        info_layout.addLayout(phase_row)
        self._phase_widgets = (self._phase_caption, self._phase_spin, self._phase_total)
        for w in self._phase_widgets:
            w.hide()

        self._source_label = QLabel()
        self._source_label.setStyleSheet("color: #ddd; font-size: 12px;")
        self._source_label.setAlignment(Qt.AlignTop)
        self._source_label.setWordWrap(True)
        info_layout.addWidget(self._source_label)

        self._info_label = QLabel("MPR Viewer\n\n"
                                   "마우스 휠: 슬라이스 이동\n"
                                   "좌클릭 드래그: 크로스헤어 이동\n"
                                   "크로스헤어 끝 ○ 드래그: 각도 회전 (Oblique)\n"
                                   "Shift + ○ 드래그: 미세 회전 (1° 단위)\n\n"
                                   "각 뷰의 크로스헤어 선은 다른 평면과의\n"
                                   "교선이며, 평면 색으로 표시됩니다.\n\n"
                                   "Sync Cursor를 켜면 같은 좌표계\n"
                                   "(Frame of Reference)의 다른 뷰와\n"
                                   "크로스헤어가 연동됩니다.")
        self._info_label.setStyleSheet("color: #aaa; font-size: 12px;")
        self._info_label.setAlignment(Qt.AlignTop)
        info_layout.addWidget(self._info_label)

        self._angle_label = QLabel()
        self._angle_label.setStyleSheet("color: #ddd;")
        self._angle_label.setFont(QFont("Menlo", 11))
        self._angle_label.setAlignment(Qt.AlignTop)
        info_layout.addWidget(self._angle_label)

        self._reset_button = QPushButton("각도 리셋")
        self._reset_button.setToolTip("Oblique 회전을 기준 3평면으로 되돌립니다")
        self._reset_button.clicked.connect(self.reset_angles)
        info_layout.addWidget(self._reset_button, 0, Qt.AlignLeft)
        info_layout.addStretch(1)

        # 2x2 그리드
        layout.addWidget(self.axial_view, 0, 0)
        layout.addWidget(self.sagittal_view, 0, 1)
        layout.addWidget(self.coronal_view, 1, 0)
        layout.addWidget(self.info_panel, 1, 1)
        self._update_angle_label()

    def _connect_signals(self):
        for view in self._views.values():
            view.point_picked.connect(self._on_point_picked)
            view.scrolled.connect(self._on_scrolled)
            view.rotation_started.connect(self._on_rotation_started)
            view.rotation_changed.connect(self._on_rotation_changed)

    # ─── 평면 기하 ───

    def _reset_frames(self):
        self._frames = {p: tuple(b.copy() for b in self._base_frames[p]) for p in PLANES}
        self._angles = {p: 0.0 for p in PLANES}

    def _set_labels(self, labels):
        self._labels = dict(labels)
        if hasattr(self, "_views"):
            for plane, view in self._views.items():
                view.set_label(self._labels.get(plane, plane))

    def _set_base_frames(self):
        """현재 평면 기준(모드)에 맞춰 세 뷰의 표준 기저를 정한다"""
        axes = volume_axes_patient(self._geometry)
        self._source_plane, self._source_tilt = source_orientation(axes)
        self._plane_sources = []
        frames = None
        if self._mode == MODE_ACQUIRED and axes is not None:
            frames = self._acquired_frames(axes)
        if frames is None:
            self._mode = MODE_ANATOMICAL
            frames = anatomical_frames(axes)
            self._set_labels({p: p for p in PLANES})
        self._base_frames = frames if frames is not None else BASE_FRAMES
        self._sync_mode_combo()
        self._update_source_label()

    def _find_plane_sources(self):
        """촬영 평면 모드에서 쓸 다른 방향 시리즈 (최대 2개): 같은 좌표계 · 스택과 평행하지 않음"""
        g = self._geometry
        if g is None:
            return []
        candidates = []
        pool = list(self._related)
        if self._selected is not None and self._selected is not self._series:
            pool.insert(0, self._selected)      # 사용자가 고른 2CH · 4CH를 먼저
        for s in pool:
            if s is None or s is self._series:
                continue
            sg = _series_geometry(s)
            if sg is None or not g.is_linkable_with(sg):
                continue
            n = sg.normals[0]
            if abs(float(n @ g.normals[0])) >= 0.9:
                continue                        # 스택과 거의 같은 평면 → 새 정보 없음
            candidates.append((0 if s is self._selected else 1, cardiac_rank(s), s, sg))
        candidates.sort(key=lambda c: (c[0], c[1]))
        picked = []
        for _, _, s, sg in candidates:
            if any(abs(float(sg.normals[0] @ pg.normals[0])) >= 0.9 for _, pg in picked):
                continue                        # 이미 고른 평면과 거의 같은 방향
            picked.append((s, sg))
            if len(picked) == 2:
                break
        return picked

    def _acquired_frames(self, axes):
        """스택 평면 + 같이 불러온 시리즈 평면을 볼륨 축 기준 기저로"""
        g = self._geometry

        def to_vol(p):
            return _unit(axes @ np.asarray(p, dtype=float))

        stack_u, stack_v = to_vol(g.row_dirs[0]), to_vol(g.col_dirs[0])
        stack_n = _E_K.copy()
        frames = {"Axial": (stack_u, stack_v, stack_n)}
        labels = {"Axial": view_short_name(self._series, "Stack")}
        sources = self._find_plane_sources()
        self._plane_sources = [(view_short_name(s), s) for s, _ in sources]
        slots = ["Sagittal", "Coronal"]
        for (s, sg), slot in zip(sources, slots):
            frames[slot] = (to_vol(sg.row_dirs[0]), to_vol(sg.col_dirs[0]), to_vol(sg.normals[0]))
            labels[slot] = view_short_name(s)
        used = set(frames)
        if len(frames) == 2:
            # 한 평면만 있으면 스택 · 그 평면 둘 다에 수직인 평면을 셋째로 (예: 4CH만 → 2CH 비슷)
            other_slot = [s for s in slots if s in used][0]
            _, _, n_other = frames[other_slot]
            n3 = _unit(np.cross(stack_n, n_other))
            u3 = _unit(n_other if abs(float(n_other @ n3)) < 0.99 else stack_u)
            u3 = _unit(u3 - float(u3 @ n3) * n3)
            v3 = _unit(np.cross(n3, u3))
            if float(v3 @ stack_n) > 0:        # 스택 진행(k) 방향이 화면 위로 오게
                v3 = -v3
            slot = [s for s in slots if s not in used][0]
            frames[slot] = (u3, v3, n3)
            labels[slot] = labels[other_slot] + "⊥"
        elif len(frames) == 1:
            # 다른 방향 시리즈가 없으면 스택의 행 · 열 방향으로 자른 두 장축 평면
            frames["Sagittal"] = (stack_v, -stack_n, stack_u)
            frames["Coronal"] = (stack_u, -stack_n, stack_v)
            labels["Sagittal"], labels["Coronal"] = "LAX 1", "LAX 2"
        self._set_labels(labels)
        return {p: tuple(np.asarray(x, dtype=float) for x in frames[p]) for p in PLANES}

    def _is_source_plane(self, plane):
        """plane의 표준 법선이 원본 슬라이스 스택(k 축)인지"""
        n = self._base_frames[plane][2]
        return _is_axis_aligned(n) and int(np.argmax(np.abs(n))) == 0

    def _update_source_label(self):
        if self._volume is None or self._geometry is None:
            self._source_label.setText("" if self._volume is None else
                                       "위치 정보(IOP/IPP)가 없어 원본 스택을 Axial로 표시")
            return
        lines = []
        if self._selected is not None and self._selected is not self._series:
            lines.append(f"볼륨: {view_short_name(self._series)} 스택 "
                         f"({self._volume.shape[0]}장) — 고른 시리즈는 한 장짜리라 "
                         f"같은 검사의 스택에서 재구성")
        if self._source_plane is not None:
            text = f"원본 촬영: {self._source_plane}"
            if self._source_tilt >= 0.5:
                text += f" (기울기 {self._source_tilt:.1f}°)"
            lines.append(text)
        if self._mode == MODE_ACQUIRED:
            src = [p for p in PLANES if self._is_source_plane(p)]
            if src:
                lines.append(f"{self._labels[src[0]]} 뷰 = 원본 슬라이스")
            if self._plane_sources:
                lines.append("평면 기준 시리즈: " + " · ".join(
                    f"{name} ← {str(getattr(s, 'description', '') or '')[:24]}"
                    for name, s in self._plane_sources))
            else:
                lines.append("같은 좌표계의 다른 방향 시리즈가 없어 스택의 행 · 열 방향으로 자름")
        else:
            src = next((p for p in PLANES if self._is_source_plane(p)), None)
            if src is not None:
                others = " · ".join(p for p in PLANES if p != src)
                lines.append(f"{src} 뷰 = 원본 슬라이스, {others}는 재구성")
            if self._base_frames is BASE_FRAMES:
                lines.append("(축을 나눌 수 없는 oblique 촬영 → 원본을 Axial로 표시)")
        self._source_label.setText("\n".join(lines))

    def _sync_mode_combo(self):
        self._updating_ui = True
        try:
            idx = self._mode_combo.findData(self._mode)
            if idx >= 0:
                self._mode_combo.setCurrentIndex(idx)
        finally:
            self._updating_ui = False

    def _sync_phase_ui(self):
        pm = self._phase_map
        self._updating_ui = True
        try:
            for w in self._phase_widgets:
                w.setVisible(pm is not None)
            if pm is not None:
                self._phase_spin.setMaximum(pm.n_phases)
                self._phase_spin.setValue(self._phase + 1)
                self._phase_total.setText(f"/ {pm.n_phases}  ({pm.unit})")
        finally:
            self._updating_ui = False

    def _box_corners(self):
        """볼륨 복셀 중심 범위의 8개 꼭짓점 (mm)"""
        ext = (np.array(self._volume.shape) - 1) * self._spacing
        return np.array([[a, b, c] for a in (0, ext[0]) for b in (0, ext[1])
                         for c in (0, ext[2])])

    def _box_center(self):
        return (np.array(self._volume.shape) - 1) * self._spacing / 2

    def _clamp(self, point):
        ext = (np.array(self._volume.shape) - 1) * self._spacing
        return np.minimum(np.maximum(point, 0), ext)

    def _normal_step(self, n):
        """법선 방향 슬라이스 간격 (mm) - 축 정렬이면 해당 축 복셀 간격"""
        return 1.0 / float(np.linalg.norm(n / self._spacing))

    def _is_standard(self, plane):
        u, v, _ = self._frames[plane]
        return _is_axis_aligned(u) and _is_axis_aligned(v)

    def _tilt(self, plane):
        """기준 법선 대비 기울어진 각도 (°)"""
        cos = abs(float(self._frames[plane][2] @ self._base_frames[plane][2]))
        return math.degrees(math.acos(min(1.0, cos)))

    # ─── 재구성 ───

    def _render(self, plane):
        """plane 평면을 볼륨에서 affine 변환으로 추출해 뷰에 설정"""
        u, v, n = self._frames[plane]
        sp = self._spacing
        standard = self._is_standard(plane)
        if standard:
            su = sp[int(np.argmax(np.abs(u)))]
            sv = sp[int(np.argmax(np.abs(v)))]
        else:
            su = sv = float(sp.min())

        origin = self._box_center()
        rel = self._box_corners() - origin
        half_u = float(np.max(np.abs(rel @ u)))
        half_v = float(np.max(np.abs(rel @ v)))
        if not standard:
            # 너무 큰 영상은 간격을 늘려 크기 제한
            su = sv = max(su, 2 * max(half_u, half_v) / (MAX_OUTPUT_SIZE - 1))
        nu = int(round(2 * half_u / su)) + 1
        nv = int(round(2 * half_v / sv)) + 1

        # 평면 중심 = 볼륨 중심을 C를 지나는 평면에 투영 (크로스헤어 이동 시 화면 고정)
        plane_center = origin + float((self._center - origin) @ n) * n
        if standard:
            axis = int(np.argmax(np.abs(n)))
            plane_center[axis] = round(plane_center[axis] / sp[axis]) * sp[axis]
        p0 = plane_center - (nu - 1) * su / 2 * u - (nv - 1) * sv / 2 * v

        # 출력 좌표 (0, i, j) → 볼륨 인덱스 = matrix @ (0, i, j) + offset
        matrix = np.column_stack([n / sp, v * sv / sp, u * su / sp])
        offset = p0 / sp
        image = affine_transform(self._volume, matrix, offset=offset,
                                 output_shape=(1, nv, nu), output=np.float32,
                                 order=0 if standard else 1, mode='constant',
                                 cval=self._cval, prefilter=False)[0]

        self._specs[plane] = {"p0": p0, "u": u, "v": v, "n": n,
                              "su": su, "sv": sv, "nu": nu, "nv": nv,
                              "t": float((plane_center - origin) @ n)}
        self._views[plane].set_image(image, sv / su)

    def _render_all(self):
        for plane in PLANES:
            self._render(plane)
        self._update_overlays()

    def _update_overlays(self):
        origin = self._box_center()
        rel = self._box_corners() - origin
        for plane in PLANES:
            spec = self._specs.get(plane)
            if spec is None:
                continue
            u, v, n = spec["u"], spec["v"], spec["n"]
            su, sv = spec["su"], spec["sv"]
            d = self._center - spec["p0"]
            center = (float(d @ u) / su, float(d @ v) / sv)

            lines = []
            for other in PLANES:
                if other == plane:
                    continue
                direction = np.cross(self._frames[other][2], n)
                if np.linalg.norm(direction) < 1e-6:
                    continue                    # 평행한 평면은 교선 없음
                lines.append((other, (float(direction @ u) / su,
                                      float(direction @ v) / sv)))

            # 법선 방향 슬라이스 번호
            step = self._normal_step(n)
            t = rel @ n
            t_min, t_max = float(t.min()), float(t.max())
            total = int(round((t_max - t_min) / step)) + 1
            index = int(round((spec["t"] - t_min) / step)) + 1
            info = [f"Slice: {min(max(index, 1), total)}/{total}"]
            if self._geometry is not None and abs(self._tilt(plane)) < 0.05:
                info.append("원본" if self._is_source_plane(plane) else "재구성")
            angle, tilt = self._angles[plane], self._tilt(plane)
            if abs(angle) >= 0.05:
                info.append(f"Angle: {angle:+.1f}°")
            if tilt >= 0.05:
                info.append(f"Tilt: {tilt:.1f}° (Oblique)")
            self._views[plane].set_overlay(center, lines, info)
        self._update_angle_label()

    def _update_angle_label(self):
        rows = []
        for plane in PLANES:
            rows.append(f"{self._labels.get(plane, plane):<9} 회전 {self._angles.get(plane, 0.0):+6.1f}°  "
                        f"기울기 {self._tilt(plane):5.1f}°")
        self._angle_label.setText("현재 각도\n" + "\n".join(rows))

    # ─── 뷰 입력 처리 ───

    def _on_point_picked(self, plane, px, py):
        spec = self._specs.get(plane)
        if self._volume is None or spec is None:
            return
        point = spec["p0"] + px * spec["su"] * spec["u"] + py * spec["sv"] * spec["v"]
        self._center = self._clamp(point)
        # 선택한 평면은 그대로, 나머지 두 평면만 C를 지나도록 재구성
        for other in PLANES:
            if other != plane:
                self._render(other)
        self._update_overlays()
        self._emit_reference_point()

    def _on_scrolled(self, plane, direction):
        if self._volume is None:
            return
        n = self._frames[plane][2]
        self._center = self._clamp(self._center + direction * self._normal_step(n) * n)
        # 다른 두 평면은 n 방향을 포함하면 영상이 안 바뀌지만, 촬영 평면 모드처럼
        # 서로 직교하지 않을 때는 바뀌므로 같이 다시 그림
        self._render(plane)
        if self._mode == MODE_ACQUIRED:
            for other in PLANES:
                if other != plane and abs(float(self._frames[other][2] @ n)) > 1e-6:
                    self._render(other)
        self._update_overlays()

    def _on_rotation_started(self, plane):
        self._rot_snapshot = ({p: tuple(x.copy() for x in f)
                               for p, f in self._frames.items()},
                              self._angles[plane])

    def _on_rotation_changed(self, plane, delta, fine):
        if self._volume is None or self._rot_snapshot is None:
            return
        frames0, angle0 = self._rot_snapshot
        if fine:
            # 미세 조절: 감도 낮추고 누적 각도를 1° 단위로 스냅
            target = round(angle0 + delta * FINE_ROTATION_GAIN)
        else:
            target = angle0 + delta
        applied = target - angle0
        u, v, _ = frames0[plane]
        # 화면 기준 회전 (u → v 방향)이 되도록 u × v를 축으로 사용
        rot = rotation_matrix(np.cross(u, v), math.radians(applied))
        for other in PLANES:
            if other != plane:
                self._frames[other] = tuple(rot @ x for x in frames0[other])
        self._angles[plane] = (target + 180) % 360 - 180
        for other in PLANES:
            if other != plane:
                self._render(other)
        self._update_overlays()

    def reset_angles(self):
        """Oblique 회전 해제 → 기준 3평면 (중심점 유지)"""
        self._reset_frames()
        self._rot_snapshot = None
        if self._volume is not None:
            self._render_all()
        else:
            self._update_angle_label()

    def _on_mode_changed(self, _index):
        if self._updating_ui:
            return
        self.set_mode(self._mode_combo.currentData())

    def set_mode(self, mode):
        """평면 기준 바꾸기 (해부학적 ↔ 촬영 평면). 중심점은 유지"""
        if mode not in (MODE_ANATOMICAL, MODE_ACQUIRED):
            return
        self._mode = mode
        if self._volume is None:
            self._sync_mode_combo()
            return
        self._set_base_frames()
        self._reset_frames()
        self._rot_snapshot = None
        self._render_all()

    @property
    def mode(self):
        return self._mode

    def _on_phase_changed(self, value):
        if self._updating_ui or self._phase_map is None or self._series is None:
            return
        self.set_phase(int(value) - 1)

    def set_phase(self, phase):
        """cine 위상 바꾸기: 같은 위치 · 평면 · 각도 그대로 두고 볼륨만 다시 쌓음"""
        if self._series is None or self._phase_map is None:
            return
        phase = max(0, min(self._phase_map.n_phases - 1, int(phase)))
        if phase == self._phase and self._volume is not None:
            return
        center, frames, angles = self._center.copy(), dict(self._frames), dict(self._angles)
        if self._build_volume(self._series, phase):
            self._center = self._clamp(center)
            self._frames, self._angles = frames, angles
            self._sync_phase_ui()
            self._render_all()

    @property
    def phase(self):
        return self._phase

    def _emit_reference_point(self):
        point = self.voxel_to_patient(*(self._center / self._spacing))
        if point is not None:
            self.reference_point_selected.emit(point)

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
            self._center = self._clamp(np.array(voxel, dtype=float) * self._spacing)
            self._render_all()

    def clear_reference_point(self):
        pass

    def show_cursor3d(self, point):
        """다른 뷰의 3D Cursor 위치로 세 평면 이동 (항상 표시 가능)"""
        self.set_reference_point(point)
        return True

    def clear_cursor3d(self):
        pass

    # ─── 볼륨 구성 ───

    def _pick_volume_series(self, series, related):
        """볼륨을 만들 스택 고르기: 고른 시리즈에 위치가 2개 이상이면 그것,
        한 장짜리(2CH · 4CH cine 등)면 같은 좌표계의 가장 두꺼운 스택(SAX)"""
        if series is not None and num_positions(series) >= 2:
            return series
        g = _series_geometry(series) if series is not None else None
        best, best_n = None, 0
        for s in related or []:
            if s is None or s is series:
                continue
            n = num_positions(s)
            if n < 3:
                continue
            sg = _series_geometry(s)
            if sg is None or (g is not None and not g.is_linkable_with(sg)):
                continue
            if g is not None and abs(float(sg.normals[0] @ g.normals[0])) >= 0.9:
                continue                        # 같은 방향 스택이면 고른 평면이 새 정보가 아님
            if n > best_n:
                best, best_n = s, n
        return best

    def _build_volume(self, series, phase=0):
        """series(스택)의 한 위상으로 3D 볼륨 구성. 성공하면 True"""
        indices, pm = volume_indices(series, phase)
        if len(indices) < 2:
            return False
        if pm is None:
            arrays = series.get_all_pixel_arrays()
            picked = [(i, arrays[i]) for i in indices]
        else:
            picked = [(i, series.get_pixel_array(i)) for i in indices]

        slices, kept = [], []
        for i, arr in picked:
            if arr is not None and len(arr.shape) == 2:
                slices.append(arr)
                kept.append(i)
        if not slices:
            return False

        # 크기 통일 (첫 슬라이스 기준)
        ref_shape = slices[0].shape
        keep = [j for j, s in enumerate(slices) if s.shape == ref_shape]
        if len(keep) < 2:
            return False

        self._volume = np.stack([slices[j] for j in keep], axis=0)
        self._series = series
        self._phase_map, self._phase = pm, (max(0, min(pm.n_phases - 1, int(phase))) if pm else 0)
        self._cval = float(self._volume.min())
        # 볼륨에 실제로 들어간 슬라이스만으로 공간 정보 구성
        self._geometry = build_series_geometry(
            [series.slices[kept[j]] for j in keep])

        # 복셀 간격 (Δk, Δrow, Δcol) mm
        if self._geometry is not None:
            d_row, d_col = self._geometry.spacings[0]
            d_slice = self._geometry.slice_spacing() or d_row
            self._spacing = np.array([d_slice, d_row, d_col], dtype=float)
        else:
            self._spacing = np.ones(3)
        return True

    def _initial_center(self):
        """처음 중심점: 평면 기준 시리즈(4CH 등)의 영상 중심을 볼륨에 투영, 없으면 볼륨 가운데"""
        d, h, w = self._volume.shape
        center = np.array([d // 2, h // 2, w // 2], dtype=float) * self._spacing
        if self._mode == MODE_ACQUIRED and self._plane_sources and self._geometry is not None:
            _, s = self._plane_sources[0]
            sg = _series_geometry(s)
            if sg is not None:
                voxel = self.patient_to_voxel(sg.center_point(sg.num_slices // 2))
                if voxel is not None:
                    center = self._clamp(np.array(voxel, dtype=float) * self._spacing)
        return center

    def set_series(self, series, related=None, phase=0):
        """DicomSeries로부터 3D 볼륨 구성

        related: 같이 불러온 다른 시리즈들 — 촬영 평면 모드의 평면 후보(2CH · 4CH)이자,
                 고른 시리즈가 한 장짜리일 때 볼륨으로 쓸 스택(SAX) 후보
        """
        if not series:
            return
        self._selected = series
        self._related = [s for s in (related or []) if s is not None]
        volume_series = self._pick_volume_series(series, self._related)
        if volume_series is None:
            return
        volume_series.sort_slices()
        if not self._build_volume(volume_series, phase):
            return

        wc, ww = volume_series.get_default_window()
        for view in self._views.values():
            view.set_window(wc, ww)

        # 평면 기준 자동 선택: 다른 방향 시리즈가 있거나(2CH · 4CH), 고른 게 한 장짜리거나,
        # 스택이 많이 기울어진 이중 사선 촬영이면 촬영 평면 모드
        axes = volume_axes_patient(self._geometry)
        _, tilt = source_orientation(axes)
        if axes is not None and (self._find_plane_sources() or volume_series is not series
                                 or tilt >= 20.0):
            self._mode = MODE_ACQUIRED
        else:
            self._mode = MODE_ANATOMICAL
        self._set_base_frames()
        self._reset_frames()
        self._rot_snapshot = None
        self._sync_phase_ui()
        self._center = self._initial_center()
        self._render_all()

    def sync_geometry(self):
        return self._geometry

    def set_window(self, center, width):
        for view in self._views.values():
            view.set_window(center, width)
