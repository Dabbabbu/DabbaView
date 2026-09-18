# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
2D MPR (Multi-Planar Reconstruction) 뷰어
Axial, Sagittal, Coronal 3평면 재구성 + Oblique(사선) 회전

좌표계: 볼륨 mm 좌표 X = (k * Δk, row * Δrow, col * Δcol)
  - 세 평면이 공유하는 중심점 C(mm)를 지나고, 평면마다 직교 기저 (u, v, n)을 가짐
    u = 화면 오른쪽, v = 화면 아래쪽, n = 평면 법선
  - 한 평면에서 크로스헤어를 회전하면 나머지 두 평면이 그 평면의 법선을 축으로
    함께 회전 → 세 평면은 항상 서로 직교
  - 각 평면 영상은 scipy.ndimage.affine_transform으로 볼륨에서 임의 각도 추출
"""
import math

import numpy as np
from scipy.ndimage import affine_transform
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QGridLayout,
                              QPushButton)
from PyQt5.QtCore import Qt, pyqtSignal, QPointF, QRectF
from PyQt5.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont, QBrush

from .geometry import build_series_geometry

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
BASE_FRAMES = {
    "Axial": (_E_COL, _E_ROW, _E_K),           # x = col, y = row
    "Sagittal": (_E_ROW, -_E_K, _E_COL),       # x = row, y = -k (상하반전)
    "Coronal": (_E_COL, -_E_K, _E_ROW),        # x = col, y = -k (상하반전)
}

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
        self.plane_name = plane_name
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
        painter.drawText(10, 22, self.plane_name)
        painter.setFont(QFont("Arial", 10))
        for i, text in enumerate(self._info):
            painter.drawText(10, 40 + i * 16, text)

        painter.end()

    # ─── 마우스 ───

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.scrolled.emit(self.plane_name, -1 if delta > 0 else 1)

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


class MPRWidget(QWidget):
    """3평면 MPR 뷰어 (Axial + Sagittal + Coronal + 정보)

    볼륨 인덱스: volume[k, row, col]
      - k: 정렬된 슬라이스 순서, row/col: 원본 슬라이스의 행/열
    """

    reference_point_selected = pyqtSignal(object)  # 환자 좌표 (mm)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = None
        self._series = None
        self._geometry = None  # 볼륨에 들어간 슬라이스들의 SeriesGeometry
        self._spacing = np.ones(3)          # (Δk, Δrow, Δcol) mm
        self._center = np.zeros(3)          # 세 평면 공유 중심점 (볼륨 mm 좌표)
        self._frames = {}                   # plane → (u, v, n)
        self._angles = {}                   # plane → 해당 뷰에서 회전한 누적 각도(°)
        self._specs = {}                    # plane → 마지막 재구성 정보
        self._cval = 0.0
        self._rot_snapshot = None
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
        self._reset_button.setToolTip("Oblique 회전을 표준 Axial/Sagittal/Coronal로 되돌립니다")
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
        self._frames = {p: tuple(b.copy() for b in BASE_FRAMES[p]) for p in PLANES}
        self._angles = {p: 0.0 for p in PLANES}

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
        """표준 법선 대비 기울어진 각도 (°)"""
        cos = abs(float(self._frames[plane][2] @ BASE_FRAMES[plane][2]))
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
                lines.append((other, (float(direction @ u) / su,
                                      float(direction @ v) / sv)))

            # 법선 방향 슬라이스 번호
            step = self._normal_step(n)
            t = rel @ n
            t_min, t_max = float(t.min()), float(t.max())
            total = int(round((t_max - t_min) / step)) + 1
            index = int(round((spec["t"] - t_min) / step)) + 1
            info = [f"Slice: {min(max(index, 1), total)}/{total}"]
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
            rows.append(f"{plane:<9} 회전 {self._angles.get(plane, 0.0):+6.1f}°  "
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
        # 다른 두 평면은 n 방향을 포함하므로 영상 변화 없음
        self._render(plane)
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
        """Oblique 회전 해제 → 표준 3평면 (중심점 유지)"""
        self._reset_frames()
        self._rot_snapshot = None
        if self._volume is not None:
            self._render_all()
        else:
            self._update_angle_label()

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
        """다른 뷰의 3D Cursor 위치로 세 평면 이동"""
        self.set_reference_point(point)

    def clear_cursor3d(self):
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

        wc, ww = series.get_default_window()
        for view in self._views.values():
            view.set_window(wc, ww)
        self._reset_frames()
        d, h, w = self._volume.shape
        self._center = np.array([d // 2, h // 2, w // 2], dtype=float) * self._spacing
        self._render_all()

    def sync_geometry(self):
        return self._geometry

    def set_window(self, center, width):
        for view in self._views.values():
            view.set_window(center, width)
