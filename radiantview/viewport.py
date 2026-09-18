"""
DICOM 이미지 뷰포트 위젯
- 윈도잉 (좌클릭 드래그)
- 줌 (휠 스크롤 + Ctrl)
- 팬 (우클릭 드래그)
- 슬라이스 스크롤 (휠 스크롤)
- 거리 측정 (Shift+클릭)
"""
import math
import os
import numpy as np
from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, QPoint, QRect, QTimer, pyqtSignal
from PyQt5.QtGui import (QImage, QPixmap, QPainter, QPen, QColor,
                          QFont, QCursor)


LOGO_PATH = os.path.join(os.path.dirname(__file__), "resources", "logo.png")


class DicomViewport(QWidget):
    """DICOM 영상을 표시하고 조작하는 뷰포트 위젯"""

    _logo = None  # 시작 화면 로고 (모든 뷰포트가 공유)

    slice_changed = pyqtSignal(int, int)  # current, total
    window_changed = pyqtSignal(float, float)  # center, width
    zoom_changed = pyqtSignal(float)  # zoom factor
    measurement_completed = pyqtSignal(float)  # distance in mm
    reference_point_selected = pyqtSignal(object)  # 환자 좌표 (mm, ndarray)

    # 도구 모드
    TOOL_WINDOW = 0
    TOOL_PAN = 1
    TOOL_ZOOM = 2
    TOOL_MEASURE = 3
    TOOL_ANGLE = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(512, 512)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # 시리즈 데이터
        self._series = None
        self._current_slice = 0

        # 윈도잉
        self._window_center = 400.0
        self._window_width = 2000.0

        # 변환
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._rotation = 0  # 0, 90, 180, 270

        # 마우스 상태
        self._mouse_pressed = False
        self._mouse_button = Qt.NoButton
        self._last_mouse_pos = QPoint()

        # 도구
        self._current_tool = self.TOOL_WINDOW

        # 측정
        self._measurements = []  # [(x1,y1,x2,y2,distance_mm), ...]
        self._measuring = False
        self._measure_start = None
        self._measure_current = None

        # 각도 측정
        self._angle_points = []
        self._angle_measurements = []  # [(p1,p2,p3,angle_deg), ...]

        # 시네 재생
        self._cine_timer = QTimer(self)
        self._cine_timer.timeout.connect(self._cine_next_frame)
        self._cine_playing = False
        self._cine_fps = 15

        # 캐시된 QPixmap
        self._cached_pixmap = None
        self._cache_valid = False

        # 오버레이 정보 표시
        self._show_overlay = True

        # 반전
        self._inverted = False

        # 크로스 레퍼런스 (Sync Cursor)
        self._sync_cursor_enabled = False
        self._ref_point = None       # 환자 좌표 (mm)
        self._placing_cursor = False

        # 테두리 강조 (활성 뷰포트 / 드롭 대상)
        self._highlight = None       # (QColor, width)

    def set_series(self, series):
        """표시할 시리즈 설정"""
        self._series = series
        self._current_slice = 0
        self._ref_point = None
        self._measurements.clear()
        self._angle_measurements.clear()
        self._cache_valid = False

        if series:
            wc, ww = series.get_default_window()
            self._window_center = wc
            self._window_width = ww
            self.window_changed.emit(wc, ww)
            self.slice_changed.emit(0, series.num_slices)

        self._fit_to_window()
        self.update()

    def _fit_to_window(self):
        """영상을 뷰포트에 맞춤"""
        if not self._series or self._series.num_slices == 0:
            return
        arr = self._series.get_pixel_array(self._current_slice)
        if arr is None:
            return

        h, w = arr.shape[:2]
        vw, vh = self.width(), self.height()
        self._zoom = min(vw / w, vh / h) * 0.95
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._cache_valid = False

    def set_tool(self, tool):
        """현재 도구 설정"""
        self._current_tool = tool
        self._measuring = False
        self._measure_start = None
        self._angle_points.clear()
        cursors = {
            self.TOOL_WINDOW: Qt.ArrowCursor,
            self.TOOL_PAN: Qt.OpenHandCursor,
            self.TOOL_ZOOM: Qt.SizeVerCursor,
            self.TOOL_MEASURE: Qt.CrossCursor,
            self.TOOL_ANGLE: Qt.CrossCursor,
        }
        self.setCursor(cursors.get(tool, Qt.ArrowCursor))

    # ─── 이벤트 처리 ───

    def mousePressEvent(self, event):
        self._mouse_pressed = True
        self._mouse_button = event.button()
        self._last_mouse_pos = event.pos()

        if self._cursor_mode_active() and event.button() == Qt.LeftButton:
            # Sync Cursor: 좌클릭/드래그로 기준점 지정 (윈도잉은 가운데 버튼)
            self._placing_cursor = True
            self._place_cursor(event.pos())

        elif self._current_tool == self.TOOL_MEASURE and event.button() == Qt.LeftButton:
            img_pos = self._screen_to_image(event.pos())
            if img_pos:
                if not self._measuring:
                    self._measuring = True
                    self._measure_start = img_pos
                    self._measure_current = img_pos
                else:
                    self._finish_measurement(img_pos)

        elif self._current_tool == self.TOOL_ANGLE and event.button() == Qt.LeftButton:
            img_pos = self._screen_to_image(event.pos())
            if img_pos:
                self._angle_points.append(img_pos)
                if len(self._angle_points) == 3:
                    self._finish_angle_measurement()
                self.update()

        elif self._current_tool == self.TOOL_PAN:
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if not self._mouse_pressed:
            return

        dx = event.pos().x() - self._last_mouse_pos.x()
        dy = event.pos().y() - self._last_mouse_pos.y()

        if self._placing_cursor and self._mouse_button == Qt.LeftButton:
            self._place_cursor(event.pos())

        elif self._current_tool == self.TOOL_WINDOW and self._mouse_button == Qt.LeftButton:
            # 윈도잉: 좌우=Width, 상하=Center
            self._window_width = max(1, self._window_width + dx * 4)
            self._window_center += dy * 4
            self._cache_valid = False
            self.window_changed.emit(self._window_center, self._window_width)

        elif self._current_tool == self.TOOL_PAN and self._mouse_button == Qt.LeftButton:
            self._pan_x += dx
            self._pan_y += dy
            self._cache_valid = False

        elif self._current_tool == self.TOOL_ZOOM and self._mouse_button == Qt.LeftButton:
            factor = 1.0 + dy * 0.005
            self._zoom = max(0.1, min(20.0, self._zoom * factor))
            self._cache_valid = False
            self.zoom_changed.emit(self._zoom)

        elif self._mouse_button == Qt.RightButton:
            # 우클릭은 항상 팬
            self._pan_x += dx
            self._pan_y += dy
            self._cache_valid = False

        elif self._mouse_button == Qt.MiddleButton:
            # 중간 버튼: 윈도잉
            self._window_width = max(1, self._window_width + dx * 4)
            self._window_center += dy * 4
            self._cache_valid = False
            self.window_changed.emit(self._window_center, self._window_width)

        if self._measuring:
            self._measure_current = self._screen_to_image(event.pos())

        self._last_mouse_pos = event.pos()
        self.update()

    def mouseReleaseEvent(self, event):
        self._mouse_pressed = False
        self._mouse_button = Qt.NoButton
        self._placing_cursor = False
        if self._current_tool == self.TOOL_PAN:
            self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        modifiers = event.modifiers()

        if modifiers & Qt.ControlModifier:
            # Ctrl+휠: 줌
            factor = 1.1 if delta > 0 else 0.9
            self._zoom = max(0.1, min(20.0, self._zoom * factor))
            self._cache_valid = False
            self.zoom_changed.emit(self._zoom)
        else:
            # 휠: 슬라이스 스크롤
            if self._series:
                if delta > 0:
                    self._go_to_slice(self._current_slice - 1)
                else:
                    self._go_to_slice(self._current_slice + 1)
        self.update()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_R:
            self._fit_to_window()
            self.update()
        elif key == Qt.Key_I:
            self._inverted = not self._inverted
            self._cache_valid = False
            self.update()
        elif key == Qt.Key_Delete:
            if self._measurements:
                self._measurements.pop()
                self.update()
            if self._angle_measurements:
                self._angle_measurements.pop()
                self.update()
        elif key == Qt.Key_Space:
            self.toggle_cine()

    # ─── 슬라이스 이동 ───

    def _go_to_slice(self, index):
        if not self._series:
            return
        index = max(0, min(self._series.num_slices - 1, index))
        if index != self._current_slice:
            self._current_slice = index
            self._cache_valid = False
            self.slice_changed.emit(index, self._series.num_slices)

    def go_to_slice(self, index):
        self._go_to_slice(index)
        self.update()

    # ─── 시네 재생 ───

    def toggle_cine(self):
        if self._cine_playing:
            self.stop_cine()
        else:
            self.start_cine()

    def start_cine(self):
        if not self._series or self._series.num_slices <= 1:
            return
        self._cine_playing = True
        self._cine_timer.start(int(1000 / self._cine_fps))

    def stop_cine(self):
        self._cine_playing = False
        self._cine_timer.stop()

    def set_cine_fps(self, fps):
        self._cine_fps = max(1, min(60, fps))
        if self._cine_playing:
            self._cine_timer.setInterval(int(1000 / self._cine_fps))

    def _cine_next_frame(self):
        if not self._series:
            return
        next_slice = (self._current_slice + 1) % self._series.num_slices
        self._go_to_slice(next_slice)
        self.update()

    # ─── 크로스 레퍼런스 (Sync Cursor) ───

    def set_sync_cursor_enabled(self, enabled):
        self._sync_cursor_enabled = enabled
        if not enabled:
            self._placing_cursor = False
            self.clear_reference_point()

    def sync_geometry(self):
        return self._series.geometry if self._series else None

    def _cursor_mode_active(self):
        # 측정/팬/줌 도구 사용 중에는 해당 도구 동작 유지
        return (self._sync_cursor_enabled and self._series is not None
                and self._current_tool == self.TOOL_WINDOW)

    def _place_cursor(self, screen_pos):
        """클릭 위치를 환자 좌표로 변환해 기준점 지정 후 시그널 전송"""
        geom = self._series.geometry if self._series else None
        img_pos = self._screen_to_image(screen_pos)
        if geom is None or img_pos is None:
            return
        # 화면→이미지 좌표는 픽셀 i가 [i, i+1) 구간 → 픽셀 중심 기준으로 0.5 보정
        col = img_pos[0] - 0.5
        row = img_pos[1] - 0.5
        point = geom.pixel_to_patient(self._current_slice, col, row)
        self._ref_point = point
        self.update()
        self.reference_point_selected.emit(point)

    def set_reference_point(self, point, navigate=True):
        """다른 뷰에서 지정한 환자 좌표를 표시 (가장 가까운 슬라이스로 이동)"""
        geom = self._series.geometry if self._series else None
        if geom is None:
            self.clear_reference_point()
            return
        if navigate:
            index, _ = geom.nearest_slice(point)
            self._go_to_slice(index)
        self._ref_point = np.asarray(point, dtype=float)
        self.update()

    def clear_reference_point(self):
        if self._ref_point is not None:
            self._ref_point = None
            self.update()

    def reference_pixel(self):
        """현재 슬라이스에서 기준점의 (col, row, 평면까지 거리 mm). 없으면 None"""
        geom = self._series.geometry if self._series else None
        if self._ref_point is None or geom is None:
            return None
        return geom.patient_to_pixel(self._current_slice, self._ref_point)

    def _draw_reference_cursor(self, painter, img_w, img_h):
        ref = self.reference_pixel()
        if ref is None:
            return
        col, row, dist = ref
        if not (-0.5 <= col <= img_w - 0.5 and -0.5 <= row <= img_h - 0.5):
            return  # 현재 영상 범위 밖

        spacing = self._series.geometry.slice_spacing()
        tolerance = (spacing / 2 if spacing else 1.0) + 0.5
        in_plane = abs(dist) <= tolerance

        center = self._image_to_screen((col + 0.5, row + 0.5))
        top_left = self._image_to_screen((0, 0))
        bottom_right = self._image_to_screen((img_w, img_h))
        gap = 8
        color = QColor(0, 255, 120) if in_plane else QColor(255, 170, 0)
        pen = QPen(color, 1, Qt.SolidLine if in_plane else Qt.DashLine)
        painter.setPen(pen)
        cx, cy = center.x(), center.y()
        painter.drawLine(top_left.x(), cy, cx - gap, cy)
        painter.drawLine(cx + gap, cy, bottom_right.x(), cy)
        painter.drawLine(cx, top_left.y(), cx, cy - gap)
        painter.drawLine(cx, cy + gap, cx, bottom_right.y())
        painter.drawEllipse(center, 3, 3)
        if not in_plane:
            painter.setFont(QFont("Arial", 10))
            painter.drawText(cx + 8, cy - 8, f"Δ {dist:+.1f} mm")

    def set_highlight(self, color=None, width=2):
        """테두리 강조 (None이면 해제)"""
        self._highlight = (QColor(color), width) if color else None
        self.update()

    def _draw_highlight(self, painter):
        if not self._highlight:
            return
        color, width = self._highlight
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.NoBrush)
        half = width // 2
        painter.drawRect(self.rect().adjusted(half, half, -half - 1 + width % 2,
                                              -half - 1 + width % 2))

    # ─── 측정 ───

    def _screen_to_image(self, screen_pos):
        """화면 좌표를 이미지 좌표로 변환"""
        if not self._series:
            return None
        arr = self._series.get_pixel_array(self._current_slice)
        if arr is None:
            return None

        h, w = arr.shape[:2]
        cx = self.width() / 2 + self._pan_x
        cy = self.height() / 2 + self._pan_y
        img_x = (screen_pos.x() - cx) / self._zoom + w / 2
        img_y = (screen_pos.y() - cy) / self._zoom + h / 2
        return (img_x, img_y)

    def _image_to_screen(self, img_pos):
        """이미지 좌표를 화면 좌표로 변환"""
        if not self._series:
            return QPoint()
        arr = self._series.get_pixel_array(self._current_slice)
        if arr is None:
            return QPoint()

        h, w = arr.shape[:2]
        cx = self.width() / 2 + self._pan_x
        cy = self.height() / 2 + self._pan_y
        sx = (img_pos[0] - w / 2) * self._zoom + cx
        sy = (img_pos[1] - h / 2) * self._zoom + cy
        return QPoint(int(sx), int(sy))

    def _finish_measurement(self, end_pos):
        """거리 측정 완료"""
        if not self._measure_start or not self._series:
            return
        sx, sy = self._series.get_pixel_spacing(self._current_slice)
        dx = (end_pos[0] - self._measure_start[0]) * sx
        dy = (end_pos[1] - self._measure_start[1]) * sy
        distance = math.sqrt(dx * dx + dy * dy)
        self._measurements.append(
            (self._measure_start[0], self._measure_start[1],
             end_pos[0], end_pos[1], distance)
        )
        self._measuring = False
        self._measure_start = None
        self._measure_current = None
        self.measurement_completed.emit(distance)
        self.update()

    def _finish_angle_measurement(self):
        """각도 측정 완료"""
        if len(self._angle_points) != 3:
            return
        p1, p2, p3 = self._angle_points
        # p2가 꼭짓점
        v1 = (p1[0] - p2[0], p1[1] - p2[1])
        v2 = (p3[0] - p2[0], p3[1] - p2[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        mag1 = math.sqrt(v1[0]**2 + v1[1]**2)
        mag2 = math.sqrt(v2[0]**2 + v2[1]**2)
        if mag1 == 0 or mag2 == 0:
            self._angle_points.clear()
            return
        cos_angle = max(-1, min(1, dot / (mag1 * mag2)))
        angle = math.degrees(math.acos(cos_angle))
        self._angle_measurements.append((p1, p2, p3, angle))
        self._angle_points.clear()
        self.update()

    # ─── 렌더링 ───

    def _apply_window(self, arr):
        """윈도잉 적용하여 8비트 이미지로 변환"""
        wc = self._window_center
        ww = self._window_width
        low = wc - ww / 2
        high = wc + ww / 2
        img = np.clip((arr - low) / max(ww, 1) * 255, 0, 255).astype(np.uint8)
        if self._inverted:
            img = 255 - img
        return img

    def _render_image(self):
        """현재 슬라이스를 QPixmap으로 렌더링"""
        if self._cache_valid and self._cached_pixmap:
            return self._cached_pixmap

        if not self._series:
            self._cached_pixmap = None
            return None

        arr = self._series.get_pixel_array(self._current_slice)
        if arr is None:
            self._cached_pixmap = None
            return None

        # 윈도잉 적용
        img_8bit = self._apply_window(arr)

        # RGB 변환인지 그레이스케일인지
        if len(img_8bit.shape) == 2:
            h, w = img_8bit.shape
            bytes_per_line = w
            qimg = QImage(img_8bit.data, w, h, bytes_per_line,
                          QImage.Format_Grayscale8)
        else:
            h, w, ch = img_8bit.shape
            if ch == 3:
                bytes_per_line = 3 * w
                qimg = QImage(img_8bit.data, w, h, bytes_per_line,
                              QImage.Format_RGB888)
            else:
                h, w = img_8bit.shape[:2]
                bytes_per_line = w
                qimg = QImage(img_8bit.data, w, h, bytes_per_line,
                              QImage.Format_Grayscale8)

        pixmap = QPixmap.fromImage(qimg)
        self._cached_pixmap = pixmap
        self._cache_valid = True
        return pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 배경
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        pixmap = self._render_image()
        if not pixmap:
            self._draw_start_screen(painter)
            self._draw_highlight(painter)
            painter.end()
            return

        # 이미지 그리기 (줌 + 팬)
        w, h = pixmap.width(), pixmap.height()
        cx = self.width() / 2 + self._pan_x
        cy = self.height() / 2 + self._pan_y
        target_w = int(w * self._zoom)
        target_h = int(h * self._zoom)
        x = int(cx - target_w / 2)
        y = int(cy - target_h / 2)

        scaled = pixmap.scaled(target_w, target_h,
                               Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
        painter.drawPixmap(x, y, scaled)

        # 측정선 그리기
        self._draw_measurements(painter)

        # 진행 중인 측정
        if self._measuring and self._measure_start and self._measure_current:
            pen = QPen(QColor(255, 255, 0), 2, Qt.DashLine)
            painter.setPen(pen)
            p1 = self._image_to_screen(self._measure_start)
            p2 = self._image_to_screen(self._measure_current)
            painter.drawLine(p1, p2)

        # 진행 중인 각도 측정
        if self._angle_points:
            pen = QPen(QColor(0, 255, 255), 2, Qt.DashLine)
            painter.setPen(pen)
            for i in range(len(self._angle_points) - 1):
                p1 = self._image_to_screen(self._angle_points[i])
                p2 = self._image_to_screen(self._angle_points[i + 1])
                painter.drawLine(p1, p2)

        # 크로스 레퍼런스 십자선
        self._draw_reference_cursor(painter, w, h)

        # 오버레이 정보
        if self._show_overlay:
            self._draw_overlay(painter)

        self._draw_highlight(painter)
        painter.end()

    def _draw_start_screen(self, painter):
        """영상이 없을 때: 로고 + 열기 안내"""
        if DicomViewport._logo is None:
            DicomViewport._logo = QPixmap(LOGO_PATH)
        logo = DicomViewport._logo

        message = "DICOM 파일을 열어주세요"
        hint = "File → Open (Ctrl+O)  ·  폴더 열기 Ctrl+Shift+O  ·  드래그 앤 드롭"
        w, h = self.width(), self.height()

        # 작은 뷰포트(Multi View 칸 등)에서는 안내 문구만
        if h < 320 or w < 360 or logo.isNull():
            painter.setPen(QColor(110, 110, 110))
            font = QFont()
            font.setPointSize(12)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter,
                             f"{message}\n(Ctrl+O)")
            return

        logo_size = int(min(128, h * 0.25))
        title_h, msg_h, hint_h, gap = 34, 26, 22, 16
        block_h = logo_size + gap + title_h + gap + msg_h + hint_h
        top = (h - block_h) // 2

        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(QRect((w - logo_size) // 2, top,
                                 logo_size, logo_size), logo)
        y = top + logo_size + gap

        font = QFont()
        font.setPointSize(22)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(220, 220, 220))
        painter.drawText(QRect(0, y, w, title_h), Qt.AlignCenter, "RadiantView")
        y += title_h + gap

        font = QFont()
        font.setPointSize(14)
        painter.setFont(font)
        painter.setPen(QColor(170, 170, 170))
        painter.drawText(QRect(0, y, w, msg_h), Qt.AlignCenter, message)
        y += msg_h

        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QColor(110, 110, 110))
        painter.drawText(QRect(0, y, w, hint_h), Qt.AlignCenter, hint)

    def _draw_measurements(self, painter):
        """측정 결과 그리기"""
        # 거리 측정
        pen = QPen(QColor(255, 255, 0), 2)
        painter.setPen(pen)
        font = QFont("Arial", 10, QFont.Bold)
        painter.setFont(font)

        for (x1, y1, x2, y2, dist) in self._measurements:
            p1 = self._image_to_screen((x1, y1))
            p2 = self._image_to_screen((x2, y2))
            painter.drawLine(p1, p2)
            # 끝점 마커
            painter.drawEllipse(p1, 3, 3)
            painter.drawEllipse(p2, 3, 3)
            # 거리 텍스트
            mid = QPoint((p1.x() + p2.x()) // 2, (p1.y() + p2.y()) // 2 - 10)
            painter.drawText(mid, f"{dist:.1f} mm")

        # 각도 측정
        pen = QPen(QColor(0, 255, 255), 2)
        painter.setPen(pen)
        for (p1, p2, p3, angle) in self._angle_measurements:
            sp1 = self._image_to_screen(p1)
            sp2 = self._image_to_screen(p2)
            sp3 = self._image_to_screen(p3)
            painter.drawLine(sp1, sp2)
            painter.drawLine(sp2, sp3)
            painter.drawEllipse(sp2, 3, 3)
            painter.drawText(sp2.x() + 10, sp2.y() - 10, f"{angle:.1f}°")

    def _draw_overlay(self, painter):
        """오버레이 정보 (환자명, W/L, 슬라이스 등)"""
        if not self._series:
            return

        painter.setPen(QColor(255, 255, 255))
        font = QFont("Consolas", 11)
        painter.setFont(font)
        margin = 10
        line_h = 18

        # 좌상단: 환자 정보
        y = margin + line_h
        painter.drawText(margin, y, f"{self._series.patient_name}")
        y += line_h
        painter.drawText(margin, y, f"{self._series.study_description}")
        y += line_h
        painter.drawText(margin, y, f"{self._series.study_date}")
        y += line_h
        painter.drawText(margin, y, f"{self._series.description}")

        # 우상단: 윈도잉 정보
        right = self.width() - margin
        y = margin + line_h
        text = f"W: {self._window_width:.0f}  L: {self._window_center:.0f}"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        painter.drawText(right - tw, y, text)

        # 좌하단: 줌
        y = self.height() - margin
        painter.drawText(margin, y, f"Zoom: {self._zoom:.1%}")

        # 우하단: 슬라이스 정보
        text = f"Im: {self._current_slice + 1}/{self._series.num_slices}"
        tw = fm.horizontalAdvance(text)
        painter.drawText(right - tw, y, text)

        # 시네 재생 표시
        if self._cine_playing:
            painter.setPen(QColor(0, 255, 0))
            painter.drawText(self.width() // 2 - 30,
                             self.height() - margin,
                             f"▶ {self._cine_fps} fps")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._series:
            self._cache_valid = False

    # ─── 공개 API ───

    def set_window(self, center, width):
        self._window_center = center
        self._window_width = max(1, width)
        self._cache_valid = False
        self.window_changed.emit(self._window_center, self._window_width)
        self.update()

    def reset_view(self):
        self._fit_to_window()
        self.update()

    def rotate_cw(self):
        self._rotation = (self._rotation + 90) % 360
        self._cache_valid = False
        self.update()

    def flip_horizontal(self):
        self._cache_valid = False
        self.update()

    def clear_measurements(self):
        self._measurements.clear()
        self._angle_measurements.clear()
        self.update()

    @property
    def current_slice(self):
        return self._current_slice

    @property
    def series(self):
        return self._series
