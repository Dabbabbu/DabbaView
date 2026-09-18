"""
DICOM 이미지 뷰포트 위젯

- 윈도잉 (좌클릭 드래그 / 가운데 버튼), 팬 (우클릭 드래그), 줌 (Ctrl+휠)
- 슬라이스 스크롤 (휠), 시네 재생
- 회전 / 상하·좌우 반전 / 흑백 반전
- 측정: 거리, 각도, Freehand ROI(통계), Freehand 면적, 2D 화살표, 3D 커서
- 돋보기 렌즈, 픽셀 값(HU/SI/SUV) 렌즈, GE 스타일 오버레이 + 스케일 바
- 크로스 레퍼런스 (Sync Cursor)

좌표계
- 이미지 좌표 (x, y): 픽셀 i는 [i, i+1) 구간 (중심 i + 0.5)
- 화면 좌표: 이미지 좌표에 표시 변환(줌·팬·회전·반전·픽셀 비율)을 적용한 위치
  측정/주석은 이미지 좌표로 저장하므로 회전·반전 후에도 영상에 붙어 있음
"""
import math
import os

import numpy as np
from PyQt5.QtWidgets import QWidget, QInputDialog
from PyQt5.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QTimer, pyqtSignal
from PyQt5.QtGui import (QImage, QPixmap, QPainter, QPen, QColor, QFont,
                         QFontDatabase, QTransform, QPainterPath, QPolygonF,
                         QBrush)

from . import dicom_info
from .roi import roi_statistics, polygon_area_mm2, polygon_perimeter_mm


LOGO_PATH = os.path.join(os.path.dirname(__file__), "resources", "logo.png")

# 화면 방향 연산 (화면 좌표, y축 아래 방향 기준)
ROTATE_RIGHT = np.array([[0, -1], [1, 0]])   # 시계 방향 90°
ROTATE_LEFT = np.array([[0, 1], [-1, 0]])    # 반시계 방향 90°
FLIP_H = np.array([[-1, 0], [0, 1]])
FLIP_V = np.array([[1, 0], [0, -1]])

MAGNIFY_LEVELS = (2.0, 3.0, 4.0)

COLOR_DISTANCE = QColor(255, 255, 0)
COLOR_ANGLE = QColor(0, 255, 255)
COLOR_ROI = QColor(255, 140, 0)
COLOR_AREA = QColor(120, 220, 255)
COLOR_ARROW = QColor(255, 80, 200)
COLOR_CURSOR3D = QColor(255, 60, 60)


class DicomViewport(QWidget):
    """DICOM 영상을 표시하고 조작하는 뷰포트 위젯"""

    _logo = None  # 시작 화면 로고 (모든 뷰포트가 공유)

    slice_changed = pyqtSignal(int, int)  # current, total
    window_changed = pyqtSignal(float, float)  # center, width
    zoom_changed = pyqtSignal(float)  # zoom factor
    measurement_completed = pyqtSignal(float)  # distance in mm
    reference_point_selected = pyqtSignal(object)  # 환자 좌표 (mm, ndarray)
    cursor_info = pyqtSignal(str)  # 마우스 위치의 좌표/픽셀 값 (상태바용)
    status_message = pyqtSignal(str)  # 측정 결과 등

    # 도구 모드
    TOOL_WINDOW = 0
    TOOL_PAN = 1
    TOOL_ZOOM = 2
    TOOL_MEASURE = 3
    TOOL_ANGLE = 4
    TOOL_CURSOR3D = 5
    TOOL_MAGNIFY = 6
    TOOL_ROI = 7
    TOOL_AREA = 8
    TOOL_ARROW = 9

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

        # 표시 변환
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._orient = np.eye(2, dtype=int)  # 회전/반전 누적 (화면 기준)

        # 마우스 상태
        self._mouse_pressed = False
        self._mouse_button = Qt.NoButton
        self._last_mouse_pos = QPoint()
        self._hover_pos = None

        # 도구
        self._current_tool = self.TOOL_WINDOW

        # 주석 (측정 포함): dict 목록, 각 항목은 그린 슬라이스에서만 표시
        self._annotations = []
        self._draft = None  # 그리는 중인 주석

        # 돋보기
        self._magnify_level = MAGNIFY_LEVELS[0]
        self._magnifying = False

        # 픽셀 값 렌즈 (커서 옆 HU/SI/SUV 표시)
        self._value_lens = False

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

    # ─── 시리즈 ───

    def set_series(self, series):
        """표시할 시리즈 설정"""
        self._series = series
        self._current_slice = 0
        self._ref_point = None
        self._annotations.clear()
        self._draft = None
        self._orient = np.eye(2, dtype=int)
        self._cache_valid = False

        if series:
            wc, ww = series.get_default_window()
            self._window_center = wc
            self._window_width = ww
            self.window_changed.emit(wc, ww)
            self.slice_changed.emit(0, series.num_slices)

        self._fit_to_window()
        self.update()

    def current_dataset(self):
        """현재 슬라이스의 메타데이터 Dataset"""
        if not self._series or not self._series.slices:
            return None
        return self._series.slices[self._current_slice]

    def _current_array(self):
        if not self._series or self._series.num_slices == 0:
            return None
        return self._series.get_pixel_array(self._current_slice)

    def _spacing(self):
        """(행 간격, 열 간격) mm. 정보가 없으면 None"""
        ds = self.current_dataset()
        return dicom_info.pixel_spacing(ds) if ds is not None else None

    # ─── 표시 변환 ───

    def _display_matrix(self, zoom=None):
        """이미지 → 화면 선형 변환 (2x2): 방향 × 확대 × 픽셀 비율"""
        sp = self._spacing()
        aspect = sp[0] / sp[1] if sp else 1.0
        z = self._zoom if zoom is None else zoom
        return self._orient @ np.diag([z, z * aspect])

    def _display_transform(self):
        """이미지 좌표 → 화면 좌표 QTransform (없으면 None)"""
        arr = self._current_array()
        if arr is None:
            return None
        h, w = arr.shape[:2]
        a = self._display_matrix()
        center = np.array([self.width() / 2 + self._pan_x,
                           self.height() / 2 + self._pan_y])
        b = center - a @ np.array([w / 2, h / 2])
        # QTransform: x' = m11*x + m21*y + dx,  y' = m12*x + m22*y + dy
        return QTransform(a[0, 0], a[1, 0], a[0, 1], a[1, 1], b[0], b[1])

    def _fit_to_window(self):
        """영상을 뷰포트에 맞춤 (회전·픽셀 비율 반영)"""
        arr = self._current_array()
        if arr is None:
            return
        h, w = arr.shape[:2]
        extent = np.abs(self._display_matrix(zoom=1.0)) @ np.array([w, h])
        vw, vh = self.width(), self.height()
        self._zoom = min(vw / extent[0], vh / extent[1]) * 0.95
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._cache_valid = False
        self.zoom_changed.emit(self._zoom)

    def _screen_to_image(self, screen_pos):
        """화면 좌표를 이미지 좌표로 변환"""
        t = self._display_transform()
        if t is None:
            return None
        inv, ok = t.inverted()
        if not ok:
            return None
        p = inv.map(QPointF(screen_pos))
        return (p.x(), p.y())

    def _image_to_screen_f(self, img_pos):
        t = self._display_transform()
        if t is None:
            return QPointF()
        return t.map(QPointF(img_pos[0], img_pos[1]))

    def _image_to_screen(self, img_pos):
        """이미지 좌표를 화면 좌표로 변환"""
        return self._image_to_screen_f(img_pos).toPoint()

    def _image_screen_rect(self):
        """화면상 영상 영역 (90° 단위 회전이므로 축 정렬 사각형)"""
        arr = self._current_array()
        t = self._display_transform()
        if arr is None or t is None:
            return QRectF()
        h, w = arr.shape[:2]
        return t.mapRect(QRectF(0, 0, w, h))

    # ─── 회전 / 반전 ───

    def _apply_orientation(self, op):
        # 화면 중심 기준으로 돌리므로 팬 오프셋도 같이 변환
        self._orient = op @ self._orient
        self._pan_x, self._pan_y = (op @ np.array([self._pan_x, self._pan_y])).tolist()
        self.update()

    def rotate_right(self):
        self._apply_orientation(ROTATE_RIGHT)

    def rotate_left(self):
        self._apply_orientation(ROTATE_LEFT)

    rotate_cw = rotate_right

    def flip_horizontal(self):
        self._apply_orientation(FLIP_H)

    def flip_vertical(self):
        self._apply_orientation(FLIP_V)

    def toggle_invert(self):
        self._inverted = not self._inverted
        self._cache_valid = False
        self.update()

    # ─── 도구 ───

    def set_tool(self, tool):
        """현재 도구 설정"""
        self._current_tool = tool
        self._draft = None
        self._magnifying = False
        cursors = {
            self.TOOL_WINDOW: Qt.ArrowCursor,
            self.TOOL_PAN: Qt.OpenHandCursor,
            self.TOOL_ZOOM: Qt.SizeVerCursor,
        }
        self.setCursor(cursors.get(tool, Qt.CrossCursor))
        self.update()

    def set_value_lens(self, enabled):
        self._value_lens = enabled
        self.update()

    # ─── 이벤트 처리 ───

    def mousePressEvent(self, event):
        self._mouse_pressed = True
        self._mouse_button = event.button()
        self._last_mouse_pos = event.pos()
        if event.button() != Qt.LeftButton:
            return

        tool = self._current_tool
        img_pos = self._screen_to_image(event.pos())

        if self._cursor_mode_active():
            # Sync Cursor: 좌클릭/드래그로 기준점 지정 (윈도잉은 가운데 버튼)
            self._placing_cursor = True
            self._place_cursor(event.pos())

        elif tool == self.TOOL_MEASURE and img_pos:
            if self._draft is None:
                self._draft = {"type": "distance", "pts": [img_pos, img_pos]}
            else:
                self._draft["pts"][1] = img_pos
                self._finish_distance()

        elif tool == self.TOOL_ANGLE and img_pos:
            if self._draft is None:
                self._draft = {"type": "angle", "pts": [img_pos, img_pos]}
            else:
                self._draft["pts"][-1] = img_pos
                if len(self._draft["pts"]) == 3:
                    self._finish_angle()
                else:
                    self._draft["pts"].append(img_pos)

        elif tool in (self.TOOL_ROI, self.TOOL_AREA) and img_pos:
            kind = "roi" if tool == self.TOOL_ROI else "area"
            self._draft = {"type": kind, "pts": [img_pos]}

        elif tool == self.TOOL_ARROW and img_pos:
            # 누른 곳 = 화살촉, 드래그한 끝 = 꼬리
            self._draft = {"type": "arrow", "pts": [img_pos, img_pos]}

        elif tool == self.TOOL_CURSOR3D and img_pos:
            self._place_cursor3d(img_pos)

        elif tool == self.TOOL_MAGNIFY:
            self._magnifying = True

        elif tool == self.TOOL_PAN:
            self.setCursor(Qt.ClosedHandCursor)

        self.update()

    def mouseMoveEvent(self, event):
        pos = event.pos()
        self._hover_pos = pos
        self._emit_cursor_info(pos)
        img_pos = self._screen_to_image(pos)

        # 클릭→클릭 방식 도구는 버튼을 누르지 않아도 미리보기 갱신
        if self._draft and self._draft["type"] in ("distance", "angle") and img_pos:
            self._draft["pts"][-1] = img_pos

        if not self._mouse_pressed:
            self.update()
            return

        dx = pos.x() - self._last_mouse_pos.x()
        dy = pos.y() - self._last_mouse_pos.y()
        left = self._mouse_button == Qt.LeftButton
        tool = self._current_tool

        if self._placing_cursor and left:
            self._place_cursor(pos)

        elif left and self._draft and self._draft["type"] in ("roi", "area") and img_pos:
            last = self._image_to_screen_f(self._draft["pts"][-1])
            if math.hypot(pos.x() - last.x(), pos.y() - last.y()) >= 2:
                self._draft["pts"].append(img_pos)

        elif left and self._draft and self._draft["type"] == "arrow" and img_pos:
            self._draft["pts"][1] = img_pos

        elif left and tool == self.TOOL_CURSOR3D and img_pos:
            self._place_cursor3d(img_pos)

        elif left and tool == self.TOOL_MAGNIFY:
            pass  # 렌즈는 hover 위치를 따라감

        elif tool == self.TOOL_WINDOW and left:
            # 윈도잉: 좌우=Width, 상하=Center
            self._adjust_window(dx, dy)

        elif tool == self.TOOL_PAN and left:
            self._pan_x += dx
            self._pan_y += dy

        elif tool == self.TOOL_ZOOM and left:
            factor = 1.0 + dy * 0.005
            self._zoom = max(0.1, min(20.0, self._zoom * factor))
            self.zoom_changed.emit(self._zoom)

        elif self._mouse_button == Qt.RightButton:
            # 우클릭은 항상 팬
            self._pan_x += dx
            self._pan_y += dy

        elif self._mouse_button == Qt.MiddleButton:
            # 중간 버튼: 윈도잉
            self._adjust_window(dx, dy)

        self._last_mouse_pos = pos
        self.update()

    def mouseReleaseEvent(self, event):
        self._mouse_pressed = False
        self._mouse_button = Qt.NoButton
        self._placing_cursor = False
        self._magnifying = False
        if self._draft and self._draft["type"] in ("roi", "area"):
            self._finish_freehand()
        elif self._draft and self._draft["type"] == "arrow":
            self._finish_arrow()
        if self._current_tool == self.TOOL_PAN:
            self.setCursor(Qt.OpenHandCursor)
        self.update()

    def leaveEvent(self, event):
        self._hover_pos = None
        self.cursor_info.emit("")
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        modifiers = event.modifiers()

        if self._magnifying:
            # 돋보기 사용 중 휠: 배율 2x ↔ 3x ↔ 4x
            i = MAGNIFY_LEVELS.index(self._magnify_level)
            i = min(len(MAGNIFY_LEVELS) - 1, i + 1) if delta > 0 else max(0, i - 1)
            self._magnify_level = MAGNIFY_LEVELS[i]
        elif modifiers & Qt.ControlModifier:
            # Ctrl+휠: 줌
            factor = 1.1 if delta > 0 else 0.9
            self._zoom = max(0.1, min(20.0, self._zoom * factor))
            self.zoom_changed.emit(self._zoom)
        elif self._series:
            # 휠: 슬라이스 스크롤
            self._go_to_slice(self._current_slice + (-1 if delta > 0 else 1))
        self.update()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_R:
            self.reset_view()
        elif key == Qt.Key_I:
            self.toggle_invert()
        elif key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_last_annotation()
        elif key == Qt.Key_Escape:
            self._draft = None
            self.update()
        elif key == Qt.Key_Space:
            self.toggle_cine()

    def _adjust_window(self, dx, dy):
        self._window_width = max(1, self._window_width + dx * 4)
        self._window_center += dy * 4
        self._cache_valid = False
        self.window_changed.emit(self._window_center, self._window_width)

    # ─── 슬라이스 이동 ───

    def _go_to_slice(self, index):
        if not self._series:
            return
        index = max(0, min(self._series.num_slices - 1, index))
        if index != self._current_slice:
            self._current_slice = index
            self._cache_valid = False
            if self._draft and self._draft["type"] == "angle":
                self._draft = None
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

    # ─── 마우스 위치 정보 (상태바 / 픽셀 값 렌즈) ───

    def pixel_info(self, img_pos):
        """이미지 좌표의 픽셀 정보 dict (영상 밖이면 None)

        keys: col, row, value, label, patient (환자 좌표 mm 또는 None)
        """
        arr = self._current_array()
        if arr is None or img_pos is None:
            return None
        col, row = int(math.floor(img_pos[0])), int(math.floor(img_pos[1]))
        h, w = arr.shape[:2]
        if not (0 <= col < w and 0 <= row < h):
            return None
        ds = self.current_dataset()
        label, factor = dicom_info.value_label(ds)
        raw = arr[row, col]
        value = None if np.ndim(raw) else float(raw) * factor
        geom = self._series.geometry
        patient = (geom.pixel_to_patient(self._current_slice,
                                         img_pos[0] - 0.5, img_pos[1] - 0.5)
                   if geom is not None else None)
        return {"col": col, "row": row, "value": value, "label": label,
                "patient": patient}

    @staticmethod
    def format_value(info):
        if info is None or info["value"] is None:
            return ""
        value = info["value"]
        if info["label"] == "HU":
            text = f"{value:.0f}"
        elif info["label"] == "SUVbw":
            text = f"{value:.2f}"
        else:
            text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{info['label']}: {text}"

    def _emit_cursor_info(self, pos):
        info = self.pixel_info(self._screen_to_image(pos))
        if info is None:
            self.cursor_info.emit("")
            return
        parts = []
        if info["patient"] is not None:
            parts.append(dicom_info.patient_position_text(info["patient"]) + " mm")
        value = self.format_value(info)
        if value:
            parts.append(value)
        parts.append(f"[{info['col']}, {info['row']}]")
        self.cursor_info.emit("   |   ".join(parts))

    # ─── 3D 커서 ───

    def _place_cursor3d(self, img_pos):
        """3D 커서는 뷰포트당 하나: 새로 찍으면 이전 위치를 대체"""
        info = self.pixel_info(img_pos)
        if info is None:
            return
        self._annotations = [a for a in self._annotations if a["type"] != "cursor3d"]
        self._annotations.append({"type": "cursor3d", "slice": self._current_slice,
                                  "pts": [img_pos], "patient": info["patient"],
                                  "value": self.format_value(info)})
        if info["patient"] is not None:
            text = dicom_info.patient_position_text(info["patient"])
            self.status_message.emit(f"3D Cursor: {text} mm")
            # Sync Cursor가 켜져 있으면 다른 뷰에도 전파 (컨트롤러가 판단)
            self.reference_point_selected.emit(np.asarray(info["patient"]))

    # ─── 측정 완료 처리 ───

    def _mm_vector(self, p1, p2):
        sp = self._spacing() or (1.0, 1.0)
        return ((p2[0] - p1[0]) * sp[1], (p2[1] - p1[1]) * sp[0])

    def _finish_distance(self):
        p1, p2 = self._draft["pts"]
        dx, dy = self._mm_vector(p1, p2)
        distance = math.hypot(dx, dy)
        self._annotations.append({"type": "distance", "slice": self._current_slice,
                                  "pts": [p1, p2], "mm": distance})
        self._draft = None
        self.measurement_completed.emit(distance)

    def _finish_angle(self):
        p1, p2, p3 = self._draft["pts"]  # p2가 꼭짓점
        v1, v2 = self._mm_vector(p2, p1), self._mm_vector(p2, p3)
        mag1, mag2 = math.hypot(*v1), math.hypot(*v2)
        self._draft = None
        if mag1 == 0 or mag2 == 0:
            return
        cos_angle = max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / (mag1 * mag2)))
        angle = math.degrees(math.acos(cos_angle))
        self._annotations.append({"type": "angle", "slice": self._current_slice,
                                  "pts": [p1, p2, p3], "deg": angle})
        self.status_message.emit(f"Angle: {angle:.1f}°")

    def _finish_freehand(self):
        draft, self._draft = self._draft, None
        pts = draft["pts"]
        if len(pts) < 3:
            return
        sp = self._spacing() or (1.0, 1.0)
        ann = {"type": draft["type"], "slice": self._current_slice, "pts": pts,
               "calibrated": self._spacing() is not None}
        if draft["type"] == "roi":
            ds = self.current_dataset()
            label, factor = dicom_info.value_label(ds)
            ann["stats"] = roi_statistics(self._current_array(), pts, sp, factor)
            ann["label"] = label
        else:
            ann["area"] = polygon_area_mm2(pts, sp)
            ann["perimeter"] = polygon_perimeter_mm(pts, sp)
        self._annotations.append(ann)
        self.status_message.emit(" | ".join(self._annotation_text(ann)))

    def _finish_arrow(self):
        draft, self._draft = self._draft, None
        head, tail = draft["pts"]
        a, b = self._image_to_screen_f(head), self._image_to_screen_f(tail)
        if math.hypot(a.x() - b.x(), a.y() - b.y()) < 8:
            return  # 너무 짧은 드래그는 무시
        text, ok = QInputDialog.getText(self, "2D Arrow", "라벨 (비워두면 화살표만):")
        if not ok:
            return
        self._annotations.append({"type": "arrow", "slice": self._current_slice,
                                  "pts": [head, tail], "text": text.strip()})

    def delete_last_annotation(self):
        """현재 슬라이스의 마지막 주석 삭제"""
        for i in range(len(self._annotations) - 1, -1, -1):
            if self._annotations[i]["slice"] == self._current_slice:
                del self._annotations[i]
                break
        self.update()

    def clear_measurements(self):
        self._annotations.clear()
        self._draft = None
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
        point = geom.pixel_to_patient(self._current_slice,
                                      img_pos[0] - 0.5, img_pos[1] - 0.5)
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

    # ─── 테두리 강조 ───

    def set_highlight(self, color=None, width=2):
        """테두리 강조 (None이면 해제)"""
        self._highlight = (QColor(color), width) if color else None
        self.update()

    # ─── 렌더링 ───

    def _apply_window(self, arr):
        """윈도잉 적용하여 8비트 이미지로 변환"""
        wc = self._window_center
        ww = self._window_width
        low = wc - ww / 2
        img = np.clip((arr - low) / max(ww, 1) * 255, 0, 255).astype(np.uint8)
        if self._inverted:
            img = 255 - img
        return img

    def _render_image(self):
        """현재 슬라이스를 QPixmap으로 렌더링 (변환은 그릴 때 적용)"""
        if self._cache_valid and self._cached_pixmap:
            return self._cached_pixmap

        arr = self._current_array()
        if arr is None:
            self._cached_pixmap = None
            return None

        img_8bit = np.ascontiguousarray(self._apply_window(arr))
        if img_8bit.ndim == 3 and img_8bit.shape[2] == 3:
            h, w, _ = img_8bit.shape
            qimg = QImage(img_8bit.data, w, h, 3 * w, QImage.Format_RGB888)
        else:
            if img_8bit.ndim == 3:
                img_8bit = np.ascontiguousarray(img_8bit[..., 0])
            h, w = img_8bit.shape
            qimg = QImage(img_8bit.data, w, h, w, QImage.Format_Grayscale8)

        self._cached_pixmap = QPixmap.fromImage(qimg)
        self._cache_valid = True
        return self._cached_pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        pixmap = self._render_image()
        transform = self._display_transform() if pixmap else None
        if not pixmap or transform is None:
            self._draw_start_screen(painter)
            self._draw_highlight(painter)
            painter.end()
            return

        # 영상 (줌·팬·회전·반전·픽셀 비율을 한 번에)
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setTransform(transform)
        painter.drawPixmap(0, 0, pixmap)
        painter.restore()

        self._draw_annotations(painter)
        self._draw_draft(painter)
        self._draw_reference_cursor(painter)

        if self._show_overlay:
            self._draw_overlay(painter)
            self._draw_scale_bar(painter)

        if self._magnifying and self._hover_pos is not None:
            self._draw_magnifier(painter, pixmap, transform)
        elif self._value_lens and self._hover_pos is not None:
            self._draw_value_lens(painter)

        self._draw_highlight(painter)
        painter.end()

    # ─── 주석 그리기 ───

    def _annotation_text(self, ann):
        """주석 라벨 문구 목록"""
        kind = ann["type"]
        unit2 = "mm²" if ann.get("calibrated", True) else "px²"
        if kind == "distance":
            return [f"{ann['mm']:.1f} mm"]
        if kind == "angle":
            return [f"{ann['deg']:.1f}°"]
        if kind == "area":
            return [f"Area {ann['area']:.1f} {unit2}",
                    f"Perim {ann['perimeter']:.1f} mm"]
        if kind == "roi":
            s, label = ann["stats"], ann.get("label", "")
            lines = [f"Area {s['area_mm2']:.1f} {unit2}"]
            if "mean" in s:
                digits = 2 if label == "SUVbw" else 1
                lines += [f"Mean {s['mean']:.{digits}f}  SD {s['std']:.{digits}f}",
                          f"Min {s['min']:.{digits}f}  Max {s['max']:.{digits}f}",
                          f"{label}  n={s['pixels']}"]
            return lines
        if kind == "cursor3d":
            lines = []
            if ann["patient"] is not None:
                lines.append(dicom_info.patient_position_text(ann["patient"]))
            if ann.get("value"):
                lines.append(ann["value"])
            return lines
        if kind == "arrow":
            return [ann["text"]] if ann["text"] else []
        return []

    def _draw_label(self, painter, anchor, lines, color, occupied=None):
        """반투명 배경 위에 여러 줄 라벨

        occupied: 이미 그린 라벨 영역 목록 - 겹치면 아래로 밀어서 그림
        """
        if not lines:
            return
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()
        w = max(fm.horizontalAdvance(l) for l in lines) + 8
        h = fm.height() * len(lines) + 4
        x = min(max(2, anchor.x()), self.width() - w - 2)
        y = min(max(2, anchor.y()), self.height() - h - 2)
        if occupied is not None:
            rect = QRectF(x, y, w, h)
            moved = True
            while moved:
                moved = False
                for other in occupied:
                    if rect.intersects(other):
                        rect.moveTop(other.bottom() + 2)
                        moved = True
            y = rect.top()
            occupied.append(QRectF(x, y, w, h))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(QRectF(x, y, w, h), 3, 3)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(color)
        for i, line in enumerate(lines):
            painter.drawText(QPointF(x + 4, y + 2 + fm.ascent() + i * fm.height()), line)

    def _screen_polygon(self, pts):
        return QPolygonF([self._image_to_screen_f(p) for p in pts])

    def _draw_arrow_shape(self, painter, head, tail, color):
        painter.setPen(QPen(color, 2))
        painter.drawLine(tail, head)
        angle = math.atan2(head.y() - tail.y(), head.x() - tail.x())
        size = 12
        wing = math.radians(25)
        left = QPointF(head.x() - size * math.cos(angle - wing),
                       head.y() - size * math.sin(angle - wing))
        right = QPointF(head.x() - size * math.cos(angle + wing),
                        head.y() - size * math.sin(angle + wing))
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([head, left, right]))
        painter.setBrush(Qt.NoBrush)

    def _draw_annotations(self, painter):
        occupied = []
        for ann in self._annotations:
            if ann["slice"] != self._current_slice:
                continue
            kind = ann["type"]
            pts = [self._image_to_screen_f(p) for p in ann["pts"]]
            lines = self._annotation_text(ann)

            if kind == "distance":
                painter.setPen(QPen(COLOR_DISTANCE, 2))
                painter.drawLine(pts[0], pts[1])
                painter.drawEllipse(pts[0], 3, 3)
                painter.drawEllipse(pts[1], 3, 3)
                mid = (pts[0] + pts[1]) / 2
                self._draw_label(painter, QPoint(int(mid.x()) + 6, int(mid.y()) - 22),
                                 lines, COLOR_DISTANCE, occupied)
            elif kind == "angle":
                painter.setPen(QPen(COLOR_ANGLE, 2))
                painter.drawLine(pts[0], pts[1])
                painter.drawLine(pts[1], pts[2])
                painter.drawEllipse(pts[1], 3, 3)
                self._draw_label(painter, QPoint(int(pts[1].x()) + 10,
                                                 int(pts[1].y()) - 24),
                                 lines, COLOR_ANGLE, occupied)
            elif kind in ("roi", "area"):
                color = COLOR_ROI if kind == "roi" else COLOR_AREA
                poly = self._screen_polygon(ann["pts"])
                fill = QColor(color)
                fill.setAlpha(40)
                painter.setPen(QPen(color, 2))
                painter.setBrush(fill)
                painter.drawPolygon(poly)
                painter.setBrush(Qt.NoBrush)
                box = poly.boundingRect()
                self._draw_label(painter, QPoint(int(box.right()) + 6, int(box.top())),
                                 lines, color, occupied)
            elif kind == "arrow":
                self._draw_arrow_shape(painter, pts[0], pts[1], COLOR_ARROW)
                self._draw_label(painter, QPoint(int(pts[1].x()) + 4,
                                                 int(pts[1].y()) + 4),
                                 lines, COLOR_ARROW, occupied)
            elif kind == "cursor3d":
                p = pts[0]
                painter.setPen(QPen(COLOR_CURSOR3D, 2))
                painter.drawLine(QPointF(p.x() - 10, p.y()), QPointF(p.x() - 3, p.y()))
                painter.drawLine(QPointF(p.x() + 3, p.y()), QPointF(p.x() + 10, p.y()))
                painter.drawLine(QPointF(p.x(), p.y() - 10), QPointF(p.x(), p.y() - 3))
                painter.drawLine(QPointF(p.x(), p.y() + 3), QPointF(p.x(), p.y() + 10))
                self._draw_label(painter, QPoint(int(p.x()) + 12, int(p.y()) + 6),
                                 lines, COLOR_CURSOR3D, occupied)

    def _draw_draft(self, painter):
        draft = self._draft
        if not draft:
            return
        kind = draft["type"]
        pts = [self._image_to_screen_f(p) for p in draft["pts"]]
        if kind == "distance":
            painter.setPen(QPen(COLOR_DISTANCE, 2, Qt.DashLine))
            painter.drawLine(pts[0], pts[1])
        elif kind == "angle":
            painter.setPen(QPen(COLOR_ANGLE, 2, Qt.DashLine))
            for a, b in zip(pts, pts[1:]):
                painter.drawLine(a, b)
        elif kind in ("roi", "area"):
            painter.setPen(QPen(COLOR_ROI if kind == "roi" else COLOR_AREA, 2,
                                Qt.DashLine))
            painter.drawPolyline(QPolygonF(pts))
        elif kind == "arrow":
            self._draw_arrow_shape(painter, pts[0], pts[1], COLOR_ARROW)

    def _draw_reference_cursor(self, painter):
        ref = self.reference_pixel()
        arr = self._current_array()
        if ref is None or arr is None:
            return
        img_h, img_w = arr.shape[:2]
        col, row, dist = ref
        if not (-0.5 <= col <= img_w - 0.5 and -0.5 <= row <= img_h - 0.5):
            return  # 현재 영상 범위 밖

        spacing = self._series.geometry.slice_spacing()
        tolerance = (spacing / 2 if spacing else 1.0) + 0.5
        in_plane = abs(dist) <= tolerance

        center = self._image_to_screen_f((col + 0.5, row + 0.5))
        rect = self._image_screen_rect()
        gap = 8
        color = QColor(0, 255, 120) if in_plane else QColor(255, 170, 0)
        painter.setPen(QPen(color, 1, Qt.SolidLine if in_plane else Qt.DashLine))
        cx, cy = center.x(), center.y()
        painter.drawLine(QPointF(rect.left(), cy), QPointF(cx - gap, cy))
        painter.drawLine(QPointF(cx + gap, cy), QPointF(rect.right(), cy))
        painter.drawLine(QPointF(cx, rect.top()), QPointF(cx, cy - gap))
        painter.drawLine(QPointF(cx, cy + gap), QPointF(cx, rect.bottom()))
        painter.drawEllipse(center, 3, 3)
        if not in_plane:
            painter.setFont(QFont("Arial", 10))
            painter.drawText(QPointF(cx + 8, cy - 8), f"Δ {dist:+.1f} mm")

    # ─── 렌즈 ───

    def _draw_magnifier(self, painter, pixmap, transform):
        """마우스 주변을 원형 렌즈로 확대 (휠로 2x/3x/4x)"""
        c = QPointF(self._hover_pos)
        radius = max(40, min(120, min(self.width(), self.height()) // 4))
        k = self._magnify_level
        lens = QTransform()
        lens.translate(c.x(), c.y())
        lens.scale(k, k)
        lens.translate(-c.x(), -c.y())

        path = QPainterPath()
        path.addEllipse(c, radius, radius)
        painter.save()
        painter.setClipPath(path)
        painter.fillPath(path, QColor(0, 0, 0))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setTransform(transform * lens)
        painter.drawPixmap(0, 0, pixmap)
        painter.restore()

        painter.setPen(QPen(QColor(230, 230, 230), 2))
        painter.drawEllipse(c, radius, radius)
        painter.setFont(QFont("Arial", 10, QFont.Bold))
        painter.drawText(QPointF(c.x() - 12, c.y() + radius - 8), f"{k:.0f}x")

    def _draw_value_lens(self, painter):
        """커서 옆에 픽셀 값 (HU / SI / SUV)"""
        info = self.pixel_info(self._screen_to_image(self._hover_pos))
        text = self.format_value(info)
        if not text:
            return
        pos = self._hover_pos
        self._draw_label(painter, QPoint(pos.x() + 16, pos.y() + 14), [text],
                         QColor(255, 255, 120))

    # ─── 오버레이 ───

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

    def _overlay_font(self):
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(11 if min(self.width(), self.height()) >= 450 else 9)
        return font

    def _draw_text_shadow(self, painter, x, y, text):
        """밝은 영상 위에서도 읽히도록 검은 외곽선 + 밝은 글자"""
        painter.setPen(QColor(0, 0, 0, 230))
        for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1), (1, 1)):
            painter.drawText(QPointF(x + ox, y + oy), text)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(QPointF(x, y), text)

    def _draw_overlay(self, painter):
        """GE 스타일 네 모서리 정보"""
        ds = self.current_dataset()
        if ds is None:
            return
        corners = dicom_info.overlay_corners(
            ds, self._current_slice, self._series.num_slices,
            window=(self._window_center, self._window_width))

        painter.setFont(self._overlay_font())
        fm = painter.fontMetrics()
        line_h = fm.height()
        margin = 8
        right = self.width() - margin

        y = margin + fm.ascent()
        for line in corners["tl"]:
            self._draw_text_shadow(painter, margin, y, line)
            y += line_h

        y = margin + fm.ascent()
        for line in corners["tr"]:
            self._draw_text_shadow(painter, right - fm.horizontalAdvance(line), y, line)
            y += line_h

        y = self.height() - margin - fm.descent()
        for line in reversed(corners["bl"]):
            self._draw_text_shadow(painter, margin, y, line)
            y -= line_h

        if self._cine_playing:
            painter.setPen(QColor(0, 255, 0))
            painter.drawText(QPointF(self.width() / 2 - 30, self.height() - margin),
                             f"▶ {self._cine_fps} fps")

    def _mm_per_screen_px(self):
        """화면 가로 1px당 mm (Pixel Spacing 없으면 None)"""
        sp = self._spacing()
        if sp is None:
            return None
        p0 = self._screen_to_image(QPoint(0, 0))
        p1 = self._screen_to_image(QPoint(100, 0))
        if p0 is None or p1 is None:
            return None
        dx, dy = self._mm_vector(p0, p1)
        return math.hypot(dx, dy) / 100

    def _draw_scale_bar(self, painter):
        """우하단 스케일 바 (cm 눈금)"""
        mm_per_px = self._mm_per_screen_px()
        if not mm_per_px:
            return
        length_mm = dicom_info.nice_scale_length_mm(self.width() * 0.3 * mm_per_px)
        if not length_mm:
            return
        length_px = length_mm / mm_per_px
        margin = 10
        x1 = self.width() - margin
        x0 = x1 - length_px
        y = self.height() - margin - 16
        tick_mm = 10 if length_mm >= 20 else (1 if length_mm <= 10 else 5)

        for color, offset in ((QColor(0, 0, 0, 200), 1), (QColor(235, 235, 235), 0)):
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(x0 + offset, y + offset),
                             QPointF(x1 + offset, y + offset))
            n_ticks = int(round(length_mm / tick_mm))
            for i in range(n_ticks + 1):
                tx = x0 + i * tick_mm / mm_per_px + offset
                tick = 6 if i in (0, n_ticks) else 3
                painter.drawLine(QPointF(tx, y - tick + offset), QPointF(tx, y + offset))

        label = f"{length_mm / 10:g}cm" if length_mm >= 10 else f"{length_mm:g}mm"
        painter.setFont(self._overlay_font())
        fm = painter.fontMetrics()
        self._draw_text_shadow(painter, x1 - fm.horizontalAdvance(label),
                               y + 4 + fm.ascent(), label)

    def _draw_highlight(self, painter):
        if not self._highlight:
            return
        color, width = self._highlight
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.NoBrush)
        half = width // 2
        painter.drawRect(self.rect().adjusted(half, half, -half - 1 + width % 2,
                                              -half - 1 + width % 2))

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
        """회전/반전을 원래대로 하고 화면에 맞춤"""
        self._orient = np.eye(2, dtype=int)
        self._fit_to_window()
        self.update()

    def capture(self):
        """현재 화면(오버레이·측정 포함) 캡처. 테두리 강조와 렌즈는 제외"""
        saved = (self._highlight, self._hover_pos, self._magnifying)
        self._highlight, self._hover_pos, self._magnifying = None, None, False
        try:
            return self.grab()
        finally:
            self._highlight, self._hover_pos, self._magnifying = saved

    @property
    def current_slice(self):
        return self._current_slice

    @property
    def series(self):
        return self._series
