# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 이미지 뷰포트 위젯

- 윈도잉 (좌클릭 드래그 / 가운데 버튼), 팬 (우클릭 드래그), 줌 (Ctrl+휠)
- 슬라이스 스크롤 (휠), 시네 재생
- 회전 / 상하·좌우 반전 / 흑백 반전
- 측정: 거리, 각도, Cobb 각, Freehand/타원 ROI(통계), 면적, 화살표, 텍스트, 3D 커서
- 마우스 매핑(설정 가능): 좌=도구, 우=W/L, 가운데=Pan, Ctrl+좌=Zoom,
  휠=슬라이스, Ctrl+휠=Zoom, Shift+휠=빠른 이동, 더블클릭=Fit / W/L 리셋
- Key Image 표시, Reference Line (다른 뷰포트 슬라이스 위치)
- 돋보기 렌즈, 픽셀 값(HU/SI/SUV) 렌즈, GE 스타일 오버레이 + 스케일 바
- 크로스 레퍼런스 (Sync Cursor)

좌표계
- 이미지 좌표 (x, y): 픽셀 i는 [i, i+1) 구간 (중심 i + 0.5)
- 화면 좌표: 이미지 좌표에 표시 변환(줌·팬·회전·반전·픽셀 비율)을 적용한 위치
  측정/주석은 이미지 좌표로 저장하므로 회전·반전 후에도 영상에 붙어 있음
"""
import math
import time
import os

import numpy as np
from PyQt5.QtWidgets import QWidget, QInputDialog
from PyQt5.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QTimer, pyqtSignal
from PyQt5.QtGui import (QImage, QPixmap, QPainter, QPen, QColor, QFont,
                         QFontDatabase, QTransform, QPainterPath, QPolygonF,
                         QBrush)

from . import __version__, dicom_info
from .annotations import ROI_TYPES, AnnotationStore, image_key
from .annotation_edit import AnnotationEditMixin, fmt_area, fmt_length
from . import mouse_feel
from .slice_bar import SliceScrollBar, WIDTH as SLICE_BAR_W
from . import roi_tools
from .app_settings import MouseBindings
from .geometry import reference_line
from .roi import polygon_area_mm2, polygon_perimeter_mm, cobb_angle
from .text_dialog import TextAnnotationDialog


LOGO_PATH = os.path.join(os.path.dirname(__file__), "resources", "logo.png")

# 화면 방향 연산 (화면 좌표, y축 아래 방향 기준)
ROTATE_RIGHT = np.array([[0, -1], [1, 0]])   # 시계 방향 90°
ROTATE_LEFT = np.array([[0, 1], [-1, 0]])    # 반시계 방향 90°
FLIP_H = np.array([[-1, 0], [0, 1]])
FLIP_V = np.array([[1, 0], [0, -1]])

MAGNIFY_LEVELS = (2.0, 3.0, 4.0)

# Ctrl 조합: macOS에서 ControlModifier = ⌘, MetaModifier = 실제 Control 키 → 둘 다 허용
CTRL_MODIFIERS = Qt.ControlModifier | Qt.MetaModifier

COLOR_DISTANCE = QColor(255, 255, 0)
COLOR_ANGLE = QColor(0, 255, 255)
COLOR_ROI = QColor(255, 140, 0)
COLOR_AREA = QColor(120, 220, 255)
COLOR_ARROW = QColor(255, 80, 200)
COLOR_CURSOR3D = QColor(255, 60, 60)          # 직접 찍은 3D Cursor
COLOR_CURSOR3D_LINKED = QColor(60, 220, 120)  # 다른 시리즈에서 대응된 3D Cursor
COLOR_ELLIPSE = QColor(80, 255, 120)
COLOR_COBB = QColor(255, 200, 60)
COLOR_REFLINE = QColor(255, 230, 0)
COLOR_KEY = QColor(255, 215, 0)


class DicomViewport(AnnotationEditMixin, QWidget):
    """DICOM 영상을 표시하고 조작하는 뷰포트 위젯"""

    _logo = None  # 시작 화면 로고 (모든 뷰포트가 공유)

    slice_changed = pyqtSignal(int, int)  # current, total
    window_changed = pyqtSignal(float, float)  # center, width
    zoom_changed = pyqtSignal(float)  # zoom factor
    measurement_completed = pyqtSignal(float)  # distance in mm
    reference_point_selected = pyqtSignal(object)  # 환자 좌표 (mm, ndarray)
    cursor3d_placed = pyqtSignal(object)  # 3D Cursor 위치 (mm) - Crosslink와 무관하게 연동
    cursor_info = pyqtSignal(str)  # 마우스 위치의 좌표/픽셀 값 (상태바용)
    status_message = pyqtSignal(str)  # 측정 결과 등
    scrolled = pyqtSignal(int)  # 사용자가 슬라이스를 넘김 (동기화 스크롤용)
    cine_state_changed = pyqtSignal(bool)  # 시네 재생 중이면 True (Play/Stop 버튼 표시)
    stepped = pyqtSignal(str, int)         # 방향키 이동 ("sequence"|"position"|"phase", +1/-1) - 함께 고른 칸 동기화
    window_adjusted = pyqtSignal(float, float)  # 사용자가 W/L 변경 (동기화 윈도잉용)
    profile_measured = pyqtSignal(object)  # 라인 프로파일 결과 dict (하단 패널 그래프)
    selection_changed = pyqtSignal(list)   # 선택한 주석 id 목록 (ROI Manager 동기화)
    select_tool_requested = pyqtSignal()   # Esc: 점 찍기 등 도구에서 Select로 돌아가기

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
    TOOL_SELECT = 10   # Selector: 선택만 (좌클릭은 뷰포트 활성화)
    TOOL_ELLIPSE = 11
    TOOL_TEXT = 12
    TOOL_COBB = 13
    TOOL_LANDMARK = 14   # 랜드마크/Fiducial 점 찍기
    TOOL_PROFILE = 15    # 라인 프로파일
    TOOL_RECT = 16       # 사각형 ROI
    TOOL_PATH = 17       # 다중 점 경로 길이

    # 좌클릭 드래그가 '그리기'인 도구 (더블클릭을 Fit으로 해석하지 않음)
    DRAWING_TOOLS = (TOOL_MEASURE, TOOL_ANGLE, TOOL_ROI, TOOL_AREA, TOOL_ARROW,
                     TOOL_ELLIPSE, TOOL_TEXT, TOOL_COBB, TOOL_LANDMARK, TOOL_PROFILE,
                     TOOL_RECT, TOOL_PATH)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(512, 512)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # 조작감 (mouse_feel): 비선형 감도 · 보간(무게감) · Pan 관성 · 휠 가속
        self._smooth = mouse_feel.Smoother()
        self._fling = mouse_feel.Fling()
        self._velocity = mouse_feel.VelocityTracker()
        self._wheel_accel = mouse_feel.WheelAccel()
        self._smooth_anchor = None
        self._last_move_ms = None
        self._motion_last_ms = None
        self._motion_timer = QTimer(self)
        self._motion_timer.setInterval(16)          # 약 60 fps
        self._motion_timer.timeout.connect(self._motion_tick)

        # 영상 오른쪽 슬라이스 막대 (INFINITT 방식) — 칸마다 하나, 끌어서 빠르게 넘김
        self._slice_bar = SliceScrollBar(self)
        self._slice_bar.set_enabled_by_user(DicomViewport.slice_bar_enabled)
        self._slice_bar.slice_requested.connect(lambda i: self.go_to_slice(i))

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
        self._edit_init()

        # 도구 / 마우스 매핑
        self._current_tool = self.TOOL_SELECT
        self._mouse = MouseBindings()
        self._drag_action = None     # 현재 드래그의 동작 ('tool', 'window', ...)
        self._zoom_anchor = None     # 드래그 줌 기준점 (누른 위치)
        self._wl_roi = None          # ROI 자동 W/L 사각형 (이미지 좌표 두 모서리)
        self._show_annotations = True
        self._scroll_accum = 0.0

        # 주석 (측정 포함): 영상(SOPInstanceUID)별 공유 저장소
        self._store = AnnotationStore(self)
        self._store.changed.connect(self.update)
        self._draft = None  # 그리는 중인 주석
        self._cursor3d = None  # 3D 커서 (뷰포트당 하나, 다른 뷰에서 받은 것 포함)

        # Reference Line 원천: 호출하면 [(geometry, index, label)] 반환
        self._reference_sources = None

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
        self._overlay_items = {"phase": True, "orientation": True, "coverage": True}
        # Phase 띠가 넣어 주는 이동 규칙: fn(현재 슬라이스, 방향, "wheel"|"cine") → 갈 슬라이스 | None
        self.slice_navigator = None

        # 반전
        self._inverted = False

        # 크로스 레퍼런스 (Sync Cursor)
        self._sync_cursor_enabled = False
        self._ref_point = None       # 환자 좌표 (mm)
        self._placing_cursor = False

        # 테두리 강조 (활성 뷰포트 / 드롭 대상)
        self._highlight = None       # (QColor, width)

        # AI Research 세그멘테이션 (SegmentationController, 모든 뷰포트 공유)
        self._seg = None

        # 분석: 컬러맵(LUT), 영상 융합, 랜드마크, 라인 프로파일
        self._lut = None             # (256, 3) uint8 또는 None(흑백)
        self._lut_name = "Gray"
        self._fusion = None          # analysis.fusion.FusionLayer
        self._fusion_affine = None   # (series_uid, 기준 시리즈 affine)
        self._landmarks = None       # analysis.landmarks.LandmarkStore (공유)
        self._profile_line = None    # (영상 키, p0, p1) - 마지막 프로파일 선

    # ─── 시리즈 ───

    def set_series(self, series):
        """표시할 시리즈 설정"""
        self.stop_motion()
        self._series = series
        self._current_slice = 0
        self._ref_point = None
        self._draft = None
        self._cursor3d = None
        self._orient = np.eye(2, dtype=int)
        self._cache_valid = False

        if series:
            wc, ww = series.get_default_window()
            self._window_center = wc
            self._window_width = ww
            self.window_changed.emit(wc, ww)
            self.slice_changed.emit(0, series.num_slices)

        self._fit_to_window()
        self.refresh_slice_bar()
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
        self._edit = None
        self._magnifying = False
        self.set_tool_cursor()
        self.update()

    # ─── 분석: 컬러맵 / 융합 / 랜드마크 / 프로파일 ───

    def set_colormap(self, name, lut):
        """컬러맵 적용 (lut=None이면 흑백)"""
        self._lut_name = name or "Gray"
        self._lut = lut
        self._cache_valid = False
        self.update()

    @property
    def colormap_name(self):
        return self._lut_name

    def set_fusion(self, layer):
        """두 번째 시리즈 컬러 오버레이 (None = 끄기)"""
        self._fusion = layer
        self._cache_valid = False
        self.update()

    @property
    def fusion(self):
        return self._fusion

    def refresh_fusion(self):
        self._cache_valid = False
        self.update()

    def _base_affine(self):
        from .ai.volume import series_spacing_affine
        uid = self._series.series_uid
        if self._fusion_affine is None or self._fusion_affine[0] != uid:
            self._fusion_affine = (uid, series_spacing_affine(self._series)[1])
        return self._fusion_affine[1]

    def _colorize(self, gray8):
        """흑백 8비트 → RGB (컬러맵 + 융합)"""
        rgb = (self._lut[gray8] if self._lut is not None
               else np.repeat(gray8[..., None], 3, axis=2))
        layer = self._fusion
        if layer is not None and self._series is not None:
            from .analysis.fusion import composite
            try:
                values = layer.sample_slice(self._base_affine(), self._current_slice,
                                            gray8.shape)
                rgb = composite(rgb, layer, values)
            except Exception as e:  # noqa: BLE001 - 융합 실패해도 기준 영상은 보이도록
                self.status_message.emit(f"Fusion 오류: {e}")
        return np.ascontiguousarray(rgb)

    def _draw_colorbars(self, painter):
        bars = []
        if self._lut is not None:
            bars.append((self._lut, self._window_center, self._window_width, self._lut_name))
        layer = self._fusion
        if layer is not None and layer.lut is not None:
            bars.append((layer.lut, layer.window[0], layer.window[1], "Fusion"))
        x = self._content_right() - 26
        for lut, center, width, name in bars:
            self._draw_colorbar(painter, lut, center, width, name, x)
            x -= 64

    def _draw_colorbar(self, painter, lut, center, width, name, x):
        h = max(80, min(260, int(self.height() * 0.4)))
        top = (self.height() - h) // 2
        strip = np.ascontiguousarray(lut[::-1][:, None, :].repeat(12, axis=1))
        image = QImage(strip.data, 12, 256, 36, QImage.Format_RGB888)
        painter.drawImage(QRect(x, top, 12, h), image)
        painter.setPen(QColor(200, 200, 200))
        painter.drawRect(x, top, 12, h)
        painter.setFont(self._overlay_font())
        lo, hi = center - width / 2, center + width / 2
        fm = painter.fontMetrics()
        for value, y in ((hi, top + fm.ascent()), (center, top + h // 2 + fm.ascent() // 2),
                         (lo, top + h)):
            text = f"{value:.4g}"
            self._draw_text_shadow(painter, x - fm.horizontalAdvance(text) - 4, y, text)
        self._draw_text_shadow(painter, x - 4 - fm.horizontalAdvance(name) + 12, top - 6, name)

    def set_landmark_store(self, store):
        self._landmarks = store
        store.changed.connect(self.update)

    def _add_landmark(self, img_pos):
        if self._landmarks is None:
            return
        info = self.pixel_info(img_pos)
        if info is None:
            return
        position = info["patient"]
        if position is None:
            # 공간 정보가 없는 영상: 픽셀 좌표를 그대로 (x, y, 슬라이스)
            position = (img_pos[0] - 0.5, img_pos[1] - 0.5, float(self._current_slice))
        ds = self.current_dataset()
        point = self._landmarks.add(position, frame_uid=str(getattr(ds, "FrameOfReferenceUID", "")),
                                    series_uid=self._series.series_uid,
                                    study_uid=str(getattr(ds, "StudyInstanceUID", "") or ""),
                                    slice_index=int(self._current_slice),
                                    series_desc=self._series.description or "")
        text = ", ".join(f"{v:.1f}" for v in point["position"])
        self.status_message.emit(f"랜드마크 {point['name']}: ({text}) mm")

    def _landmarks_here(self):
        """현재 슬라이스에 보이는 랜드마크 [(번호, 이름, 영상 좌표)]"""
        store, series = self._landmarks, self._series
        if store is None or not len(store) or series is None:
            return []
        geom = series.geometry
        if geom is None:
            return [(i, p["name"], (p["position"][0] + 0.5, p["position"][1] + 0.5))
                    for i, p in enumerate(store) if p.get("series_uid") == series.series_uid
                    and int(round(p["position"][2])) == self._current_slice]
        spacing = geom.slice_spacing()
        tolerance = (spacing / 2 if spacing else 1.0) + 0.01
        frame = str(getattr(self.current_dataset(), "FrameOfReferenceUID", ""))
        points = list(store)
        return [(i, points[i]["name"], (col + 0.5, row + 0.5))
                for i, col, row, _dist in store.points_near(geom, self._current_slice,
                                                            tolerance, frame)]

    def _finish_profile(self):
        from .analysis.measure import line_profile
        p0, p1 = self._draft["pts"]
        self._draft = None
        arr = self._current_array()
        if arr is None or arr.ndim != 2 or (p0[0] == p1[0] and p0[1] == p1[1]):
            return
        spacing = self._spacing() or (1.0, 1.0)
        distances, values = line_profile(arr, p0, p1, spacing)
        self._profile_line = (self._image_key(), p0, p1)
        self.profile_measured.emit({
            "distances": distances, "values": values, "p0": p0, "p1": p1,
            "calibrated": self._spacing() is not None,
            "title": f"{self._series.description} · slice {self._current_slice + 1}"})

    def roi_mask(self):
        """현재 영상의 마지막 ROI(자유곡선·타원) 마스크 (없으면 None)"""
        from .roi import ellipse_mask, polygon_mask
        arr = self._current_array()
        if arr is None:
            return None
        for ann in reversed(self.annotations_here()):
            if ann["type"] == "roi":
                return polygon_mask(ann["pts"], arr.shape[:2])
            if ann["type"] == "ellipse":
                return ellipse_mask(ann["pts"][0], ann["pts"][1], arr.shape[:2])
            if ann["type"] == "rect":
                return roi_tools.mask_of(ann, arr.shape[:2])
        return None

    _overlay_painters = []   # fn(viewport, painter) - 전문 분석 윤곽 등 (모든 뷰포트 공유)

    @classmethod
    def add_overlay_painter(cls, fn):
        cls._overlay_painters.append(fn)

    # 랜드마크 표시 (View ▸ 📍 랜드마크 표시): 영상을 가리지 않게
    #   "subtle": 가운데를 비운 작은 반투명 표시, 이름은 마우스를 가까이 댈 때만 (기본)
    #   "full": 예전처럼 원 + 십자 + 이름   "hover": 마우스를 가까이 댈 때만   "hidden": 숨김
    LANDMARK_DISPLAYS = ("subtle", "full", "hover", "hidden")
    landmark_display = "subtle"
    LANDMARK_HOVER_PX = 28

    def flash_landmark(self, index, seconds=1.8):
        """목록에서 고른 랜드마크를 잠깐 크게 표시 (어디 있는지 보이게)"""
        self._landmark_flash = (index, time.monotonic() + seconds)
        self.update()
        QTimer.singleShot(int(seconds * 1000) + 50, self.update)

    def _draw_landmark(self, painter, p, name, mode, near, flash):
        color = QColor(120, 255, 120)
        if flash:
            painter.setPen(QPen(QColor(0, 0, 0), 4))
            painter.drawEllipse(p, 14, 14)
            painter.setPen(QPen(QColor(255, 230, 80), 2))
            painter.drawEllipse(p, 14, 14)
        if mode == "full" or flash or (near and mode in ("subtle", "hover")):
            painter.setPen(QPen(QColor(0, 0, 0), 3))
            painter.drawEllipse(p, 5, 5)
            painter.setPen(QPen(color, 1.5))
            painter.drawEllipse(p, 5, 5)
            painter.drawLine(QPointF(p.x() - 8, p.y()), QPointF(p.x() - 3, p.y()))
            painter.drawLine(QPointF(p.x() + 3, p.y()), QPointF(p.x() + 8, p.y()))
            self._draw_text_shadow(painter, int(p.x() + 9), int(p.y() - 6), name)
            return
        if mode != "subtle":
            return
        # 가운데 4px는 비우고 짧은 눈금 네 개만 반투명으로 → 찍은 곳의 영상이 그대로 보임
        color.setAlpha(150)
        painter.setPen(QPen(color, 1.2))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            painter.drawLine(QPointF(p.x() + dx * 4, p.y() + dy * 4),
                             QPointF(p.x() + dx * 8, p.y() + dy * 8))

    def _draw_analysis(self, painter):
        """랜드마크 + 라인 프로파일 선"""
        painter.setFont(self._overlay_font())
        mode = self.landmark_display
        flash_index, until = getattr(self, "_landmark_flash", (None, 0.0))
        flashing = time.monotonic() < until
        hover = getattr(self, "_hover_pos", None) if self.underMouse() else None
        for i, name, img_pos in self._landmarks_here():
            flash = flashing and i == flash_index
            if mode == "hidden" and not flash:
                continue
            p = self._image_to_screen_f(img_pos)
            near = hover is not None and math.hypot(hover.x() - p.x(),
                                                    hover.y() - p.y()) <= self.LANDMARK_HOVER_PX
            self._draw_landmark(painter, p, name, mode, near, flash)
        line = self._profile_line
        if line is not None and line[0] == self._image_key():
            a, b = self._image_to_screen_f(line[1]), self._image_to_screen_f(line[2])
            painter.setPen(QPen(QColor(255, 200, 0), 1.5))
            painter.drawLine(a, b)
            painter.drawEllipse(a, 3, 3)
            painter.drawEllipse(b, 3, 3)

    # ─── 세그멘테이션 (AI Research) ───

    def set_segmentation(self, controller):
        self._seg = controller
        controller.changed.connect(self._on_segmentation_changed)
        controller.tool_changed.connect(lambda _tool: (self.set_tool_cursor(), self.update()))

    def _on_segmentation_changed(self, series_uid):
        if not series_uid or (self._series is not None
                              and self._series.series_uid == series_uid):
            self.update()

    def _seg_active(self):
        """세그멘테이션 도구가 선택되어 좌클릭을 가져가는지"""
        seg = getattr(self, "_seg", None)
        return seg is not None and seg.tool is not None and self._series is not None

    def _draw_segmentation(self, painter, transform):
        """라벨 오버레이 + Threshold 미리보기 + 브러시 커서 (영상 좌표계로 그림)"""
        seg = self._seg
        if seg is None or self._series is None:
            return
        k = self._current_slice
        layers = []
        rgba = seg.overlay_rgba(self._series, k)
        if rgba is not None:
            layers.append(rgba)
        if seg.tool == "threshold":
            preview = seg.threshold_preview(self._series, k)
            if preview is not None and preview.any():
                layer = np.zeros(preview.shape + (4,), dtype=np.uint8)
                layer[preview] = seg.labels.color(seg.active_label) + (90,)
                layers.append(layer)
        brush = (seg.tool in ("brush", "eraser") and self._hover_pos is not None)
        if not layers and not brush:
            return
        painter.save()
        painter.setTransform(transform)
        for layer in layers:
            layer = np.ascontiguousarray(layer)
            h, w = layer.shape[:2]
            painter.drawImage(0, 0, QImage(layer.data, w, h, 4 * w, QImage.Format_RGBA8888))
        if brush:
            img_pos = self._screen_to_image(self._hover_pos)
            if img_pos is not None:
                pen = QPen(QColor(255, 255, 255) if seg.tool == "eraser"
                           else QColor(*seg.labels.color(seg.active_label)), 1.5)
                pen.setCosmetic(True)
                if seg.tool == "eraser":
                    pen.setStyle(Qt.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                r = seg.brush_radius
                painter.drawEllipse(QPointF(img_pos[0], img_pos[1]), r, r)
        painter.restore()

    def set_overlay_items(self, items):
        """위상 인코딩 방향 · 방향 문자 · 스캔 커버리지 선을 각각 켜고 끔 (T 키는 전체)"""
        self._overlay_items.update(items)
        self.update()

    def set_overlay_visible(self, visible):
        """환자 정보 오버레이 + 측정/주석 표시 (T / O)"""
        self._show_overlay = visible
        self._show_annotations = visible
        self.update()

    @property
    def overlay_visible(self):
        return self._show_overlay

    def set_value_lens(self, enabled):
        self._value_lens = enabled
        self.update()

    def set_mouse_bindings(self, bindings):
        """여러 뷰포트가 같은 MouseBindings 객체를 공유 (설정 변경 즉시 반영)"""
        self._mouse = bindings

    def set_annotation_store(self, store):
        self._store.changed.disconnect(self.update)
        self._store = store
        store.changed.connect(self.update)
        self.update()

    @property
    def annotation_store(self):
        return self._store

    def set_reference_source(self, provider):
        """provider() → [(SeriesGeometry, slice index, label)] (None이면 표시 안 함)"""
        self._reference_sources = provider
        self.update()

    # ─── 이벤트 처리 ───

    def _drag_action_for(self, event):
        """눌린 버튼·수정키 → 드래그 동작 (마우스 매핑 설정)"""
        button, mods = event.button(), event.modifiers()
        if button == Qt.LeftButton:
            if mods & Qt.AltModifier:
                return self._mouse.get("alt_left_drag")
            if mods & CTRL_MODIFIERS:
                return self._mouse.get("ctrl_left_drag")
            return self._mouse.get("left_drag")
        if button == Qt.RightButton:
            return self._mouse.get("right_drag")
        if button == Qt.MiddleButton:
            return self._mouse.get("middle_drag")
        return "none"

    # 좌+우 버튼을 함께 누른 채 끌기: 위아래 = 슬라이스, 좌우 = 위상 (빨리 끌수록 많이 넘어감)
    CHORD_PX_PER_SLICE = 6.0     # 천천히 끌 때: 6px마다 한 장 (보통 드래그 넘기기 8px보다 민감)
    CHORD_ACCEL_START = 0.25     # 이 속도(px/ms)를 넘으면 가속 시작
    CHORD_ACCEL_GAIN = 5.0       # 속도가 0.1 px/ms 빨라질 때마다 +0.5배
    CHORD_ACCEL_MAX = 12.0       # 최대 12배

    @staticmethod
    def chord_gain(speed):
        """끄는 속도(px/ms) → 넘김 배율 (천천히 1배 … 급하게 최대 12배)"""
        extra = max(0.0, speed - DicomViewport.CHORD_ACCEL_START) * DicomViewport.CHORD_ACCEL_GAIN
        return min(DicomViewport.CHORD_ACCEL_MAX, 1.0 + extra)

    def _begin_chord_scroll(self, event):
        """먼저 누른 버튼의 동작은 멈추고 '양쪽 버튼 넘기기'로 바꾼다"""
        previous = self._drag_action
        if previous == "seg":
            self._seg.release()
        elif previous == "tool":
            if self._edit is not None:
                self.edit_release()
            if self._draft is not None and not self._draft.get("quick"):
                self._draft = None                # 그리다 만 주석은 버림
        elif previous == "roi_window":
            self._wl_roi = None
        self._placing_cursor = False
        self._magnifying = False
        self._mouse_pressed = True
        self._drag_action = "chord_scroll"
        self._last_mouse_pos = event.pos()
        self._scroll_accum = 0.0
        self._phase_accum = 0.0
        self._chord_time = time.monotonic()
        self.setCursor(Qt.SizeVerCursor)
        self.update()

    def _chord_scroll_move(self, dx, dy):
        """위아래 = 슬라이스 (위상 영상은 위치), 좌우 = 위상 (위상 영상만). 크게 움직인 쪽만 씀"""
        now = time.monotonic()
        dt_ms = max(1.0, (now - getattr(self, "_chord_time", now)) * 1000.0)
        self._chord_time = now
        if self._series is None:
            return
        phases = self._phase_map()
        horizontal = abs(dx) > abs(dy)
        if horizontal and phases is None:
            return                                # 위상이 없는 영상: 좌우는 쓰지 않음
        delta = dx if horizontal else dy
        gain = self.chord_gain(abs(delta) / dt_ms)
        if horizontal:
            self._phase_accum = getattr(self, "_phase_accum", 0.0) \
                + delta / self.CHORD_PX_PER_SLICE * gain
            steps = int(self._phase_accum)
            self._phase_accum -= steps
            for _ in range(min(abs(steps), phases.n_phases)):
                self.step_slice("phase", 1 if steps > 0 else -1)
            return
        self._scroll_accum += delta / self.CHORD_PX_PER_SLICE * gain
        steps = int(self._scroll_accum)
        if not steps:
            return
        self._scroll_accum -= steps
        if phases is not None:                    # 위상 고정한 채 슬라이스 위치만
            for _ in range(min(abs(steps), phases.n_positions)):
                before = self._current_slice
                self.step_slice("position", 1 if steps > 0 else -1)
                if self._current_slice == before:
                    self._scroll_accum = 0.0
                    break
            return
        last = self._series.num_slices - 1
        target = max(0, min(last, self._current_slice + steps))
        if target in (0, last):
            self._scroll_accum = 0.0              # 끝에 닿으면 남은 이동량은 버림
        self._go_to_slice(target, user=True)

    def mousePressEvent(self, event):
        both = Qt.LeftButton | Qt.RightButton
        if (event.buttons() & both) == both and event.button() in (Qt.LeftButton, Qt.RightButton):
            self._begin_chord_scroll(event)
            return
        if (self._seg_active() and event.button() == Qt.LeftButton
                and not event.modifiers() & (Qt.AltModifier | CTRL_MODIFIERS)):
            # 세그멘테이션 도구: 좌클릭 드래그 = 칠하기 (Alt/Ctrl 드래그는 기존 동작)
            self._mouse_pressed = True
            self._mouse_button = event.button()
            self._last_mouse_pos = event.pos()
            self._drag_action = "seg"
            self._seg.press(self._series, self._current_slice,
                            self._screen_to_image(event.pos()))
            self.update()
            return
        self._mouse_pressed = True
        self._mouse_button = event.button()
        self._last_mouse_pos = event.pos()
        self._scroll_accum = 0.0
        self._fling.stop()                 # 미끄러지던 화면은 잡는 순간 멈춤
        self._velocity.reset()
        self._wl_drag_unit = None
        self._last_move_ms = time.monotonic() * 1000.0
        self._drag_action = self._drag_action_for(event)
        self._zoom_anchor = event.pos()
        if self._drag_action == "tool":
            self._tool_press(event)
        elif self._drag_action == "roi_window":
            img_pos = self._screen_to_image(event.pos())
            self._wl_roi = [img_pos, img_pos] if img_pos else None
        elif self._drag_action == "pan":
            self.setCursor(Qt.ClosedHandCursor)
        self.update()

    def _tool_press(self, event):
        """선택한 도구의 좌클릭 동작"""
        tool = self._current_tool
        img_pos = self._screen_to_image(event.pos())
        shift = bool(event.modifiers() & Qt.ShiftModifier)

        if (self._draft is not None and self._draft.get("quick") and img_pos
                and self._draft["type"] == "distance"):
            # Select 도구 더블클릭으로 시작한 빠른 거리 측정: 다음 클릭이 끝점
            end = self.snap_point(self._draft["pts"][0], img_pos) if shift else img_pos
            self._draft["pts"][1] = end
            self._finish_distance()
            return

        if (not self._cursor_mode_active() and img_pos
                and (tool == self.TOOL_SELECT or tool in self.DRAWING_TOOLS)
                and self.edit_press(event, img_pos, tool == self.TOOL_SELECT)):
            return   # 기존 주석 핸들/몸통을 잡음 → 새로 그리지 않고 수정

        if tool in (self.TOOL_MEASURE, self.TOOL_PATH, self.TOOL_ANGLE) and img_pos:
            self._anchor = (self._image_key(), img_pos)   # 실시간 거리 기준점

        if self._cursor_mode_active():
            # Crosslink: 좌클릭/드래그로 기준점 지정
            self._placing_cursor = True
            self._place_cursor(event.pos())

        elif tool == self.TOOL_MEASURE and img_pos:
            if self._draft is None:
                # 클릭→클릭 또는 누른 채 끌어서 놓기
                self._draft = {"type": "distance", "pts": [img_pos, img_pos],
                               "press": (event.pos().x(), event.pos().y())}
            else:
                # Shift: 0/45/90° 스냅
                end = self.snap_point(self._draft["pts"][0], img_pos) if shift else img_pos
                self._draft["pts"][1] = end
                self._finish_distance()

        elif tool == self.TOOL_PATH and img_pos:
            # 다중 점 경로: 클릭마다 점 추가, 더블클릭/Enter로 끝, Esc 취소
            if self._draft is None:
                self._draft = {"type": "path", "pts": [img_pos, img_pos]}
            else:
                end = self.snap_point(self._draft["pts"][-2], img_pos) if shift else img_pos
                self._draft["pts"][-1] = end
                self._draft["pts"].append(end)

        elif tool == self.TOOL_RECT and img_pos:
            self._draft = {"type": "rect", "pts": [img_pos, img_pos], "square": shift}

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

        elif tool == self.TOOL_ELLIPSE and img_pos:
            self._draft = {"type": "ellipse", "pts": [img_pos, img_pos], "circle": shift}

        elif tool == self.TOOL_COBB and img_pos:
            # 1번 선 드래그 → 2번 선 드래그
            if self._draft is None:
                self._draft = {"type": "cobb", "pts": [img_pos, img_pos]}
            else:
                self._draft["pts"] += [img_pos, img_pos]

        elif tool == self.TOOL_ARROW and img_pos:
            # 누른 곳 = 화살촉, 드래그한 끝 = 꼬리
            self._draft = {"type": "arrow", "pts": [img_pos, img_pos]}

        elif tool == self.TOOL_TEXT and img_pos:
            self._add_text_annotation(img_pos)

        elif tool == self.TOOL_LANDMARK and img_pos:
            self._add_landmark(img_pos)

        elif tool == self.TOOL_PROFILE and img_pos:
            self._draft = {"type": "profile", "pts": [img_pos, img_pos]}

        elif tool == self.TOOL_CURSOR3D and img_pos:
            self._place_cursor3d(img_pos)

        elif tool == self.TOOL_MAGNIFY:
            self._magnifying = True

        elif tool == self.TOOL_PAN:
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        pos = event.pos()
        self._hover_pos = pos
        self._emit_cursor_info(pos)
        img_pos = self._screen_to_image(pos)

        # 클릭→클릭 방식 도구는 버튼을 누르지 않아도 미리보기 갱신 (Shift: 각도 스냅)
        if self._draft and self._draft["type"] in ("distance", "angle", "path") and img_pos:
            target = img_pos
            if event.modifiers() & Qt.ShiftModifier and self._draft["type"] in ("distance", "path"):
                target = self.snap_point(self._draft["pts"][-2], img_pos)
            self._draft["pts"][-1] = target

        if not self._mouse_pressed:
            self.update()
            return

        dx = pos.x() - self._last_mouse_pos.x()
        dy = pos.y() - self._last_mouse_pos.y()
        action = self._drag_action

        speed = self._drag_speed(dx, dy)
        if action in ("window", "pan", "zoom"):
            self._feel_drag(action, dx, dy, speed)
        elif action == "roi_window":
            if self._wl_roi is not None and img_pos:
                self._wl_roi[1] = img_pos
        elif action == "chord_scroll":
            self._chord_scroll_move(dx, dy)
        elif action == "scroll":
            # 세로 드래그 8px마다 한 장 — 천천히는 한 장씩, 빠르게 끌면 가속
            self._scroll_accum += dy / 8.0 * self._feel_scale("feel_scroll") * \
                mouse_feel.speed_gain(speed, self._feel("feel_accel"))
            steps = int(self._scroll_accum)
            if steps:
                self._scroll_accum -= steps
                self._go_to_slice(self._current_slice + steps, user=True)
        elif action == "tool" and self._edit is not None:
            self.edit_move(img_pos, bool(event.modifiers() & Qt.ShiftModifier))
        elif action == "tool":
            self._tool_move(pos, img_pos, dx, dy, speed)
        elif action == "seg":
            self._seg.move(img_pos)

        self._last_mouse_pos = pos
        self.update()

    def _tool_move(self, pos, img_pos, dx, dy, speed=0.45):
        tool = self._current_tool
        draft = self._draft
        if self._placing_cursor:
            self._place_cursor(pos)
        elif draft and draft["type"] in ("roi", "area") and img_pos:
            last = self._image_to_screen_f(draft["pts"][-1])
            if math.hypot(pos.x() - last.x(), pos.y() - last.y()) >= 2:
                draft["pts"].append(img_pos)
        elif draft and draft["type"] == "rect" and img_pos:
            if draft.get("square"):
                sp = self._spacing() or (1.0, 1.0)
                x0, y0 = draft["pts"][0]
                side = max(abs(img_pos[0] - x0) * sp[1], abs(img_pos[1] - y0) * sp[0])
                img_pos = (x0 + math.copysign(side / sp[1], img_pos[0] - x0),
                           y0 + math.copysign(side / sp[0], img_pos[1] - y0))
            draft["pts"][1] = img_pos
        elif draft and draft["type"] == "ellipse" and img_pos:
            if draft.get("circle"):
                # Shift: 원 (mm 기준 같은 반지름)
                sp = self._spacing() or (1.0, 1.0)
                x0, y0 = draft["pts"][0]
                rx = abs(img_pos[0] - x0) * sp[1]
                ry = abs(img_pos[1] - y0) * sp[0]
                r = max(rx, ry)
                img_pos = (x0 + math.copysign(r / sp[1], img_pos[0] - x0),
                           y0 + math.copysign(r / sp[0], img_pos[1] - y0))
            draft["pts"][1] = img_pos
        elif draft and draft["type"] in ("arrow", "cobb", "profile") and img_pos:
            draft["pts"][-1] = img_pos
        elif tool == self.TOOL_CURSOR3D and img_pos:
            self._place_cursor3d(img_pos)
        elif tool == self.TOOL_WINDOW:
            self._feel_drag("window", dx, dy, speed)
        elif tool == self.TOOL_PAN:
            self._feel_drag("pan", dx, dy, speed)
        elif tool == self.TOOL_ZOOM:
            self._feel_drag("zoom", dx, dy, speed)

    def mouseReleaseEvent(self, event):
        action = self._drag_action
        if action == "chord_scroll":
            if event.buttons() & (Qt.LeftButton | Qt.RightButton):
                return                            # 한쪽만 뗐음 → 남은 버튼으로 계속 넘김
            self._mouse_pressed = False
            self._mouse_button = Qt.NoButton
            self._drag_action = None
            self.set_tool_cursor()
            self.update()
            return
        self._wl_drag_unit = None
        panning = action == "pan" or (action == "tool" and self._current_tool == self.TOOL_PAN)
        if panning and self._feel("feel_inertia"):
            now = time.monotonic() * 1000.0
            if self._last_move_ms is not None and now - self._last_move_ms < 50:   # 멈췄다 놓으면 관성 없음
                vx, vy = self._velocity.velocity(now)
                scale = self._feel_scale("feel_pan")
                if self._fling.start(vx * scale, vy * scale):
                    self._start_motion()
        self._mouse_pressed = False
        self._mouse_button = Qt.NoButton
        self._drag_action = None
        self._placing_cursor = False
        self._magnifying = False
        if action == "roi_window":
            self._apply_roi_window()
        if action == "seg":
            self._seg.release()
        if action == "tool" and self._edit is not None:
            self.edit_release()
        elif action == "tool" and self._draft:
            kind = self._draft["type"]
            if kind in ("roi", "area"):
                self._finish_freehand()
            elif kind == "ellipse":
                self._finish_ellipse()
            elif kind == "rect":
                self._finish_rect()
            elif kind == "distance" and self._draft.get("press"):
                # 누른 채 끌어서 놓았으면 바로 측정 (그냥 클릭이면 클릭→클릭 방식)
                px, py = self._draft["press"]
                if math.hypot(event.pos().x() - px, event.pos().y() - py) > 6:
                    img_pos = self._screen_to_image(event.pos())
                    if img_pos is not None:
                        shift = bool(event.modifiers() & Qt.ShiftModifier)
                        self._draft["pts"][1] = (self.snap_point(self._draft["pts"][0], img_pos)
                                                 if shift else img_pos)
                        self._finish_distance()
                else:
                    self._draft.pop("press", None)
            elif kind == "arrow":
                self._finish_arrow()
            elif kind == "profile":
                self._finish_profile()
            elif kind == "cobb" and len(self._draft["pts"]) == 4:
                self._finish_cobb()
        if action == "pan" or self._current_tool == self.TOOL_PAN:
            self.set_tool_cursor()  # 잡은 손 모양 → 원래 커서
        self.update()

    def set_tool_cursor(self):
        cursors = {self.TOOL_PAN: Qt.OpenHandCursor, self.TOOL_ZOOM: Qt.SizeVerCursor,
                   self.TOOL_TEXT: Qt.IBeamCursor}
        default = (Qt.ArrowCursor if self._current_tool in (self.TOOL_SELECT, self.TOOL_WINDOW)
                   else Qt.CrossCursor)
        if self._seg_active():
            self.setCursor(Qt.CrossCursor)
            return
        self.setCursor(cursors.get(self._current_tool, default))

    def mouseDoubleClickEvent(self, event):
        """좌 더블클릭: Fit / 우 더블클릭: W/L 리셋 (설정 가능)

        그리기 도구 사용 중 좌 더블클릭은 빠른 두 번째 클릭으로 처리.
        """
        button = event.button()
        if (button == Qt.LeftButton and self._current_tool == self.TOOL_PATH
                and self._draft is not None and self._draft["type"] == "path"):
            self._finish_path()   # 더블클릭 = 경로 끝
            return
        if (button == Qt.LeftButton and self._current_tool == self.TOOL_SELECT
                and not self._cursor_mode_active() and not self._seg_active()):
            img_pos = self._screen_to_image(event.pos())
            ann, _handle = self.hit_annotation(event.pos())
            if img_pos is not None and ann is None and self._inside_image(img_pos):
                # 빠른 거리 측정: 더블클릭 = 시작점, 다음 클릭 = 끝점 (도구 바꾸지 않음)
                self._draft = {"type": "distance", "pts": [img_pos, img_pos], "quick": True}
                self._anchor = (self._image_key(), img_pos)
                self.status_message.emit("빠른 거리 측정: 끝점을 클릭하세요 "
                                         "(Shift: 각도 스냅, Esc: 취소)")
                self.update()
                return
        if button == Qt.LeftButton and (self._current_tool in self.DRAWING_TOOLS
                                        or self._cursor_mode_active()
                                        or self._seg_active()):
            self.mousePressEvent(event)
            return
        key = {Qt.LeftButton: "left_double", Qt.RightButton: "right_double"}.get(button)
        action = self._mouse.get(key) if key else "none"
        if action == "fit":
            self._fit_to_window()
        elif action == "reset_window":
            self.reset_window()
        elif action == "reset_view":
            self.reset_view()
        self.update()

    def leaveEvent(self, event):
        self._hover_pos = None
        self.cursor_info.emit("")
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        # macOS는 Shift+휠을 가로 스크롤(x)로 바꿔 보냄
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            return
        mods = event.modifiers()

        if self._magnifying:
            # 돋보기 사용 중 휠: 배율 2x ↔ 3x ↔ 4x
            i = MAGNIFY_LEVELS.index(self._magnify_level)
            i = min(len(MAGNIFY_LEVELS) - 1, i + 1) if delta > 0 else max(0, i - 1)
            self._magnify_level = MAGNIFY_LEVELS[i]
            self.update()
            return

        if mods & CTRL_MODIFIERS:
            action = self._mouse.get("ctrl_wheel")
        elif mods & Qt.ShiftModifier:
            action = self._mouse.get("shift_wheel")
        else:
            action = self._mouse.get("wheel")

        if action == "zoom":
            # 위로 = 확대, 아래로 = 축소 (커서 아래 지점을 고정). 한 칸 = 1.1배, 부드럽게 따라감
            amount = math.log(1.1) * (delta / 120.0) * self._feel_scale("feel_zoom")
            self._smooth_anchor = event.pos()
            self._smooth.add("zoom", amount)
            self._start_motion()
            return
        if action not in ("scroll", "fast_scroll"):
            self.update()
            return
        # 한 칸(120)마다 한 장 — 트랙패드는 모이는 만큼, 빠르게 연달아 굴리면 가속
        steps = self._wheel_accel.feed(delta, time.monotonic() * 1000.0,
                                       accelerate=bool(self._feel("feel_wheel_accel"))
                                       and action == "scroll")
        if not steps:
            return
        direction = -1 if steps > 0 else 1  # 위로 = 이전 슬라이스
        if action == "scroll":
            index = self._current_slice
            for _ in range(abs(steps)):
                target = self.slice_navigator(index, direction, "wheel") \
                    if self.slice_navigator is not None else None
                index = target if target is not None else index + direction
            self._go_to_slice(index, user=True)
        else:
            step = self._mouse.get("fast_scroll_step")
            self._go_to_slice(self._current_slice + direction * step * abs(steps), user=True)
        self.update()

    def _zoom_by(self, factor, anchor=None):
        """줌. anchor(화면 좌표)가 있으면 그 아래의 영상 지점이 그대로 머물도록 팬 보정"""
        before = self._screen_to_image(anchor) if anchor is not None else None
        # 최대 20배 — 단, 작은 영상은 화면 맞춤만으로 20배를 넘을 수 있으니 확대하다 도로 작아지지 않게
        self._zoom = max(0.1, min(max(20.0, self._zoom), self._zoom * factor))
        if before is not None:
            after = self._image_to_screen_f(before)
            self._pan_x += anchor.x() - after.x()
            self._pan_y += anchor.y() - after.y()
        self.zoom_changed.emit(self._zoom)

    # ─── 조작감: 비선형 감도 · 보간 · 관성 (mouse_feel) ───

    def _feel(self, key):
        value = self._mouse.get(key) if self._mouse is not None else None
        return mouse_feel.FEEL_DEFAULTS[key] if value is None else value

    def _feel_scale(self, key):
        return float(self._feel(key)) / 100.0

    def _drag_speed(self, dx, dy):
        """지난 움직임부터의 속도 (px/ms) — 감도 커브 · 관성에 씀"""
        now = time.monotonic() * 1000.0
        last = self._last_move_ms if self._last_move_ms is not None else now - 16.0
        self._last_move_ms = now
        self._velocity.add(now, dx, dy)
        return math.hypot(dx, dy) / max(4.0, now - last)

    def _feel_drag(self, kind, dx, dy, speed):
        """W/L · Pan · Zoom 드래그를 바로 적용하지 않고 쌓아 두었다가 부드럽게 따라감"""
        if kind == "window":
            # 느리면 정밀, 빠르면 가속. 한 픽셀 = 지금 Width의 1/100 (좁은 창일수록 섬세)
            gain = mouse_feel.speed_gain(speed, self._feel("feel_accel")) * self._feel_scale("feel_wl")
            unit = getattr(self, "_wl_drag_unit", None)
            if unit is None:     # 드래그를 시작할 때의 Width로 고정 (드래그 중에 불어나지 않게)
                unit = self._wl_drag_unit = mouse_feel.wl_unit(
                    self._window_width + self._smooth.pending.get("ww", 0.0))
            self._smooth.add("ww", dx * unit * gain)
            self._smooth.add("wc", dy * unit * gain)
        elif kind == "pan":
            scale = self._feel_scale("feel_pan")     # Pan은 손을 따라 1:1 (가속하면 영상이 손에서 미끄러짐)
            self._smooth.add("px", dx * scale)
            self._smooth.add("py", dy * scale)
        elif kind == "zoom":
            gain = mouse_feel.speed_gain(speed, self._feel("feel_accel")) * self._feel_scale("feel_zoom")
            self._smooth_anchor = self._zoom_anchor
            self._smooth.add("zoom", dy * 0.005 * gain)
        self._start_motion()

    def _start_motion(self):
        """보간 시간이 0이면 바로 적용, 아니면 60fps로 따라감"""
        if float(self._feel("feel_smooth_ms")) <= 0:
            self._apply_motion(self._smooth.flush())
            if self._fling.active():
                self._motion_last_ms = None
                if not self._motion_timer.isActive():
                    self._motion_timer.start()
            return
        if not self._motion_timer.isActive():
            self._motion_last_ms = None
            self._motion_timer.start()

    def _motion_tick(self):
        now = time.monotonic() * 1000.0
        dt = 16.0 if self._motion_last_ms is None else min(64.0, now - self._motion_last_ms)
        self._motion_last_ms = now
        parts = self._smooth.step(dt, float(self._feel("feel_smooth_ms")))
        if self._fling.active() and not self._mouse_pressed:
            fx, fy = self._fling.step(dt)
            parts["px"] = parts.get("px", 0.0) + fx
            parts["py"] = parts.get("py", 0.0) + fy
        self._apply_motion(parts)
        if not self._smooth.active() and not self._fling.active():
            rest = self._smooth.flush()
            if rest:
                self._apply_motion(rest)
            self._motion_timer.stop()

    def _apply_motion(self, parts):
        if not parts:
            return
        wc, ww = parts.get("wc", 0.0), parts.get("ww", 0.0)
        if wc or ww:
            self._window_width = max(1, self._window_width + ww)
            self._window_center += wc
            self._cache_valid = False
            self.window_changed.emit(self._window_center, self._window_width)
            self.window_adjusted.emit(self._window_center, self._window_width)
        self._pan_x += parts.get("px", 0.0)
        self._pan_y += parts.get("py", 0.0)
        zoom = parts.get("zoom", 0.0)
        if zoom:
            self._zoom_by(math.exp(zoom), self._smooth_anchor)
        self.update()

    def settle_motion(self):
        """따라가던 움직임을 즉시 끝냄 (캡처 · 테스트 · 동기화 전에)"""
        self._fling.stop()
        self._apply_motion(self._smooth.flush())
        self._motion_timer.stop()

    def stop_motion(self):
        """움직임을 버림 (다른 시리즈 · 뷰 리셋)"""
        self._smooth.clear()
        self._fling.stop()
        self._motion_timer.stop()

    # ─── ROI 자동 W/L (Ctrl+좌클릭 드래그) ───

    def _apply_roi_window(self):
        roi, self._wl_roi = self._wl_roi, None
        arr = self._current_array()
        if roi is None or arr is None or arr.ndim != 2:
            self.update()
            return
        h, w = arr.shape
        (x0, y0), (x1, y1) = roi
        c0, c1 = sorted((int(math.floor(x0)), int(math.ceil(x1))))
        r0, r1 = sorted((int(math.floor(y0)), int(math.ceil(y1))))
        c0, c1 = max(0, c0), min(w, c1)
        r0, r1 = max(0, r0), min(h, r1)
        if c1 - c0 < 2 or r1 - r0 < 2:
            self.update()
            return  # 너무 작은 사각형 (단순 Ctrl+클릭)
        values = arr[r0:r1, c0:c1]
        if self._mouse.get("roi_window_method") == "mean2sd":
            mean, sd = float(values.mean()), float(values.std())
            low, high = mean - 2 * sd, mean + 2 * sd
        else:
            low, high = float(values.min()), float(values.max())
        width = max(1.0, high - low)
        center = (low + high) / 2
        self.set_window(center, width, user=True)
        self.status_message.emit(
            f"ROI W/L: W {width:.0f}  L {center:.0f}  ({values.size} px, "
            f"{'mean±2SD' if self._mouse.get('roi_window_method') == 'mean2sd' else 'min–max'})")

    def _draw_wl_roi(self, painter):
        if not self._wl_roi:
            return
        a = self._image_to_screen_f(self._wl_roi[0])
        b = self._image_to_screen_f(self._wl_roi[1])
        painter.setPen(QPen(QColor(255, 255, 255), 1, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(a, b).normalized())

    def keyPressEvent(self, event):
        # R/I/Space 등은 메인 윈도우 단축키(QAction)가 처리
        key = event.key()
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            if not self.delete_selected():
                self.delete_last_annotation()
        elif (key in (Qt.Key_Return, Qt.Key_Enter) and self._draft
              and self._draft["type"] == "path"):
            self._finish_path()
        elif key == Qt.Key_Escape and self._seg_active():
            self._seg.set_tool(None)  # 세그멘테이션 도구 해제
        elif key == Qt.Key_Escape:
            if self._draft is None:
                self.clear_cursor3d()
                if self._current_tool not in (self.TOOL_SELECT, self.TOOL_WINDOW):
                    self.select_tool_requested.emit()   # 그리던 게 없으면 Esc = Select로
            self._draft = None
            self.update()
        elif key in (Qt.Key_Up, Qt.Key_Down):
            # ↑↓ 슬라이스 이동. 위상 영상(heart cine 등)은 기본 '슬라이스 위치' (위상 고정)
            # View ▸ ↕ 방향키 메뉴에서 잠시 '전체 순서' · '위상만'으로 바꿀 수 있음
            self.step_slice(DicomViewport.arrow_mode, 1 if key == Qt.Key_Down else -1)
        elif key in (Qt.Key_Left, Qt.Key_Right):
            # ←→ 위상 영상에서만: 이 슬라이스의 위상 (위상이 없는 영상에서는 쓰지 않음)
            if self._phase_map() is not None:
                self.step_slice("phase", 1 if key == Qt.Key_Right else -1)
        else:
            super().keyPressEvent(event)

    # ↑↓ 방향키 동작 (모든 칸 공통, 앱을 다시 켜면 기본값으로) — View ▸ ↕ 방향키
    ARROW_MODES = {"position": "슬라이스 위치", "sequence": "전체 순서", "phase": "위상"}
    arrow_mode = "position"

    def _phase_map(self):
        if self._series is None:
            return None
        from .phases import phase_map
        return phase_map(self._series)

    def step_slice(self, kind, direction, user=True):
        """kind: 'sequence' (전체 순서) | 'position' (슬라이스 위치) | 'phase' (같은 위치의 위상)

        Phase 띠가 없는 뷰포트(Multi View 칸 등)도 시리즈에서 위상 표를 바로 만들어 씀.
        """
        target = self.slice_navigator(self._current_slice, direction, kind) \
            if self.slice_navigator is not None else None
        if target is None and self._series is not None:
            from .phases import phase_map
            phases = phase_map(self._series)
            if phases is not None:
                target = (phases.step_phase(self._current_slice, direction) if kind == "phase"
                          else phases.step_sequence(self._current_slice, direction)
                          if kind == "sequence"
                          else phases.step_position(self._current_slice, direction))
        # 이동 자체는 scrolled를 내보내지 않음 (같은 동작을 stepped로만 전파 → 두 번 움직이지 않게)
        self._go_to_slice(target if target is not None else self._current_slice + direction, user=False)
        self.update()
        if user:
            self.stepped.emit(kind, direction)

    # ─── 슬라이스 이동 ───

    def _go_to_slice(self, index, user=False):
        """user=True: 사용자가 넘긴 경우 → scrolled 시그널 (동기화 스크롤)"""
        if not self._series:
            return
        index = max(0, min(self._series.num_slices - 1, index))
        if index != self._current_slice:
            self._current_slice = index
            self._cache_valid = False
            if self._draft and self._draft["type"] in ("angle", "cobb", "distance"):
                self._draft = None
            self.slice_changed.emit(index, self._series.num_slices)
            self.refresh_slice_bar()
            if user:
                self.scrolled.emit(index)

    def go_to_slice(self, index, user=True):
        self._go_to_slice(index, user=user)
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
        self.cine_state_changed.emit(True)

    def stop_cine(self):
        was = self._cine_playing
        self._cine_playing = False
        self._cine_timer.stop()
        if was:
            self.cine_state_changed.emit(False)

    def set_cine_fps(self, fps):
        self._cine_fps = max(1, min(60, fps))
        if self._cine_playing:
            self._cine_timer.setInterval(int(1000 / self._cine_fps))

    def _cine_next_frame(self):
        if not self._series:
            return
        target = self.slice_navigator(self._current_slice, 1, "cine") if self.slice_navigator is not None else None
        next_slice = target if target is not None else (self._current_slice + 1) % self._series.num_slices
        self._go_to_slice(next_slice, user=True)
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
            x, y, z = info["patient"]
            parts.append(f"x {x:.1f}  y {y:.1f}  z {z:.1f} mm")
            parts.append(dicom_info.patient_position_text(info["patient"]))
        value = self.format_value(info)
        if value:
            parts.append(value)
        parts.append(f"[{info['col']}, {info['row']}]")
        self.cursor_info.emit("   |   ".join(parts))

    # ─── 3D 커서 ───

    def _image_key(self):
        return image_key(self._series, self._current_slice)

    def annotations_here(self):
        """현재 영상의 주석 목록"""
        return self._store.items(self._image_key())

    def _add_annotation(self, ann):
        ann["slice"] = self._current_slice
        self._store.add(self._image_key(), ann)

    def _place_cursor3d(self, img_pos):
        """3D 커서는 뷰포트당 하나: 새로 찍으면 이전 위치를 대체

        공간 정보가 있으면 환자 좌표(mm)로 저장해 같은 좌표계의 다른 뷰에도 표시.
        """
        info = self.pixel_info(img_pos)
        if info is None:
            return
        patient = info["patient"]
        self._cursor3d = {"type": "cursor3d", "key": self._image_key(),
                          "pts": [img_pos], "value": self.format_value(info),
                          "patient": None if patient is None else np.asarray(patient)}
        if patient is not None:
            text = dicom_info.patient_position_text(patient)
            self.status_message.emit(f"3D Cursor: {text} mm   {self._cursor3d['value']}")
            self.cursor3d_placed.emit(np.asarray(patient))
        self.update()

    def show_cursor3d(self, point):
        """다른 뷰에서 찍은 3D Cursor: 가장 가까운 슬라이스로 이동해 표시.

        좌표가 이 시리즈의 스캔 범위 밖(슬라이스 사이 간격을 크게 벗어나거나 영상 밖)이면
        커서를 놓지 않고 False를 반환한다.
        """
        geom = self._series.geometry if self._series else None
        if geom is None:
            self.clear_cursor3d()
            return False
        index, distance = geom.nearest_slice(point)
        spacing = geom.slice_spacing()
        limit = (spacing if spacing else 5.0) + 0.5   # 스캔 범위: 가장 가까운 슬라이스에서 한 칸 안
        col, row, _d = geom.patient_to_pixel(index, point)
        arr = self._series.get_pixel_array(index)
        h, w = np.shape(arr)[:2]
        inside = -0.5 <= col <= w - 0.5 and -0.5 <= row <= h - 0.5
        if abs(distance) > limit or not inside:
            self.clear_cursor3d()
            return False
        self._go_to_slice(index)
        self._cursor3d = {"type": "cursor3d", "key": None, "pts": [],
                          "patient": np.asarray(point, dtype=float), "value": ""}
        self.update()
        return True

    def clear_cursor3d(self):
        if self._cursor3d is not None:
            self._cursor3d = None
            self.update()

    def cursor3d_pixel(self):
        """현재 슬라이스에 투영한 3D Cursor (col, row, 평면까지 거리 mm). 없으면 None"""
        c = self._cursor3d
        geom = self._series.geometry if self._series else None
        if c is None or c["patient"] is None or geom is None:
            return None
        return geom.patient_to_pixel(self._current_slice, c["patient"])

    def _cursor3d_for_drawing(self):
        """이번 화면에 그릴 3D Cursor 주석 dict (없으면 None)"""
        c = self._cursor3d
        if c is None:
            return None
        projected = self.cursor3d_pixel()
        if projected is None:
            # 공간 정보가 없는 영상: 찍은 영상에서만 표시
            return c if c["key"] == self._image_key() else None
        col, row, dist = projected
        arr = self._current_array()
        h, w = arr.shape[:2]
        if not (-0.5 <= col <= w - 0.5 and -0.5 <= row <= h - 0.5):
            return None
        spacing = self._series.geometry.slice_spacing()
        tolerance = (spacing / 2 if spacing else 1.0) + 0.5
        img_pos = (col + 0.5, row + 0.5)
        # 이 시리즈의 해당 위치 픽셀 값 (다른 시리즈면 값이 다름)
        mine = c.get("key") == self._image_key()
        return {"type": "cursor3d", "pts": [img_pos], "patient": c["patient"],
                "value": self.format_value(self.pixel_info(img_pos)),
                # 찍은 영상은 빨강 · 다른 시리즈로 옮겨온 커서는 초록 + 슬라이스까지 거리(Δ)
                "mine": mine, "delta": None if mine else dist,
                "off_plane": abs(dist) > tolerance}

    # ─── 측정 완료 처리 ───

    def _mm_vector(self, p1, p2):
        sp = self._spacing() or (1.0, 1.0)
        return ((p2[0] - p1[0]) * sp[1], (p2[1] - p1[1]) * sp[0])

    def _inside_image(self, img_pos):
        arr = self._current_array()
        if arr is None:
            return False
        return 0 <= img_pos[0] <= arr.shape[1] and 0 <= img_pos[1] <= arr.shape[0]

    def _finish_distance(self):
        p1, p2 = self._draft["pts"][:2]
        dx, dy = self._mm_vector(p1, p2)
        distance = math.hypot(dx, dy)
        self._draft = None
        a, b = self._image_to_screen_f(p1), self._image_to_screen_f(p2)
        if math.hypot(a.x() - b.x(), a.y() - b.y()) < 2:
            self.update()
            return   # 같은 점 (의도치 않은 클릭)
        self._add_annotation({"type": "distance", "pts": [p1, p2], "mm": distance})
        self._anchor = (self._image_key(), p2)
        self.measurement_completed.emit(distance)
        self.status_message.emit(f"Distance {fmt_length(distance, math.hypot(p2[0] - p1[0], p2[1] - p1[1]))}")

    def _finish_path(self):
        """다중 점 경로: 마지막 미리보기 점(마우스 따라다니던 점) 정리 후 총 길이"""
        draft, self._draft = self._draft, None
        pts = list(draft["pts"])
        while len(pts) >= 2 and pts[-1] == pts[-2]:
            pts.pop()
        if len(pts) < 2:
            self.update()
            return
        sp = self._spacing() or (1.0, 1.0)
        total = roi_tools.path_length_mm(pts, sp)
        self._add_annotation({"type": "path", "pts": pts, "mm": total,
                              "calibrated": self._spacing() is not None})
        self._anchor = (self._image_key(), pts[-1])
        self.status_message.emit(f"Path {len(pts)} points: {fmt_length(total)}")

    def _finish_rect(self):
        draft, self._draft = self._draft, None
        p1, p2 = draft["pts"]
        a, b = self._image_to_screen_f(p1), self._image_to_screen_f(p2)
        if abs(a.x() - b.x()) < 4 or abs(a.y() - b.y()) < 4:
            return
        self.add_roi("rect", [p1, p2])

    def add_roi(self, kind, pts, **fields):
        """ROI 주석 추가 (통계 계산 포함) → 주석 dict 반환 (ROI Manager의 정량 ROI 생성에서도 사용)"""
        label, factor = dicom_info.value_label(self.current_dataset())
        ann = {"type": kind, "pts": [tuple(p) for p in pts], "label": label,
               "calibrated": self._spacing() is not None}
        ann.update(fields)
        ann["stats"] = roi_tools.roi_statistics(ann, self._current_array(),
                                                self._spacing() or (1.0, 1.0), factor)
        self._add_annotation(ann)
        self.status_message.emit(" | ".join(self._annotation_text(ann)))
        return ann

    def _finish_angle(self):
        p1, p2, p3 = self._draft["pts"]  # p2가 꼭짓점
        v1, v2 = self._mm_vector(p2, p1), self._mm_vector(p2, p3)
        mag1, mag2 = math.hypot(*v1), math.hypot(*v2)
        self._draft = None
        if mag1 == 0 or mag2 == 0:
            return
        cos_angle = max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / (mag1 * mag2)))
        angle = math.degrees(math.acos(cos_angle))
        self._add_annotation({"type": "angle", "pts": [p1, p2, p3], "deg": angle})
        self.status_message.emit(f"Angle: {angle:.1f}°")

    def _finish_freehand(self):
        draft, self._draft = self._draft, None
        pts = draft["pts"]
        if len(pts) < 3:
            return
        sp = self._spacing() or (1.0, 1.0)
        ann = {"type": draft["type"], "pts": pts,
               "calibrated": self._spacing() is not None}
        if draft["type"] == "roi":
            ds = self.current_dataset()
            label, factor = dicom_info.value_label(ds)
            ann["stats"] = roi_tools.roi_statistics(ann, self._current_array(), sp, factor)
            ann["label"] = label
        else:
            ann["area"] = polygon_area_mm2(pts, sp)
            ann["perimeter"] = polygon_perimeter_mm(pts, sp)
        self._add_annotation(ann)
        self.status_message.emit(" | ".join(self._annotation_text(ann)))

    def _finish_ellipse(self):
        draft, self._draft = self._draft, None
        p1, p2 = draft["pts"]
        a, b = self._image_to_screen_f(p1), self._image_to_screen_f(p2)
        if abs(a.x() - b.x()) < 4 or abs(a.y() - b.y()) < 4:
            return  # 너무 작은 타원은 무시
        sp = self._spacing() or (1.0, 1.0)
        label, factor = dicom_info.value_label(self.current_dataset())
        ann = {"type": "ellipse", "pts": [p1, p2], "label": label,
               "calibrated": self._spacing() is not None}
        if draft.get("circle"):
            ann["circle"] = True
        ann["stats"] = roi_tools.roi_statistics(ann, self._current_array(), sp, factor)
        self._add_annotation(ann)
        self.status_message.emit(" | ".join(self._annotation_text(ann)))

    def _finish_cobb(self):
        draft, self._draft = self._draft, None
        a1, a2, b1, b2 = draft["pts"]
        angle = cobb_angle(a1, a2, b1, b2, self._spacing() or (1.0, 1.0))
        if angle is None:
            return
        self._add_annotation({"type": "cobb", "pts": [a1, a2, b1, b2], "deg": angle})
        self.status_message.emit(f"Cobb angle: {angle:.1f}°")

    def _add_text_annotation(self, img_pos):
        result = TextAnnotationDialog.get_annotation(self)
        if result is None:
            return
        text, size, color = result
        self._add_annotation({"type": "text", "pts": [img_pos], "text": text,
                              "size": size, "color": color})

    def _finish_arrow(self):
        draft, self._draft = self._draft, None
        head, tail = draft["pts"]
        a, b = self._image_to_screen_f(head), self._image_to_screen_f(tail)
        if math.hypot(a.x() - b.x(), a.y() - b.y()) < 8:
            return  # 너무 짧은 드래그는 무시
        text, ok = QInputDialog.getText(self, "2D Arrow", "라벨 (비워두면 화살표만):")
        if not ok:
            return
        self._add_annotation({"type": "arrow", "pts": [head, tail], "text": text.strip()})

    def delete_last_annotation(self):
        """현재 영상의 마지막 주석 삭제"""
        self._store.remove_last(self._image_key())
        self.update()

    def clear_measurements(self):
        """모든 영상의 주석 삭제 (공유 저장소)"""
        self._store.clear()
        self._draft = None
        self._cursor3d = None
        self.update()

    # ─── Key Image ───

    def is_key_image(self):
        return self._store.is_key_image(self._image_key())

    def toggle_key_image(self):
        """현재 영상 Key Image 토글 → 새 상태"""
        if self._series is None:
            return False
        marked = self._store.toggle_key_image(self._series, self._current_slice)
        self.status_message.emit(
            f"Key Image {'marked' if marked else 'unmarked'}: "
            f"{self._series.description} #{self._current_slice + 1}")
        return marked

    # ─── 크로스 레퍼런스 (Sync Cursor) ───

    def set_sync_cursor_enabled(self, enabled):
        self._sync_cursor_enabled = enabled
        if not enabled:
            self._placing_cursor = False
            self.clear_reference_point()

    def sync_geometry(self):
        return self._series.geometry if self._series else None

    # Crosslink가 켜져 있을 때 Select · W/L 좌클릭으로 기준점을 찍던 동작 (예전 방식).
    # 영상을 보려고 누를 때마다 점이 찍혀 불편 → 끔: 위치 찍기는 3D Cursor 도구(6)에서만
    # (3D Cursor는 Crosslink가 켜져 있으면 같은 좌표계의 다른 칸에도 전파됨)
    CLICK_PLACES_REFERENCE = False

    def _cursor_mode_active(self):
        return (self.CLICK_PLACES_REFERENCE and self._sync_cursor_enabled
                and self._series is not None
                and self._current_tool in (self.TOOL_SELECT, self.TOOL_WINDOW))

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
        if img_8bit.ndim == 2 and (self._lut is not None or self._fusion is not None):
            img_8bit = self._colorize(img_8bit)
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

    def _draw_decode_failure(self, painter):
        """디코딩에 실패한 슬라이스: 이유를 보여 주고 다른 슬라이스로 넘길 수 있게"""
        error_fn = getattr(self._series, "decode_error", None)
        reason = (error_fn(self._current_slice) if error_fn else None) or "픽셀 데이터를 읽지 못했습니다."
        ds = self.current_dataset()
        name = os.path.basename(str(getattr(ds, "filename", "") or "")) if ds is not None else ""
        painter.setFont(self._overlay_font())
        painter.setPen(QColor(255, 190, 80))
        text = (f"⚠ 이 영상을 표시할 수 없습니다\n{reason}\n{name}\n\n"
                f"슬라이스 {self._current_slice + 1}/{self._series.num_slices} — 다른 슬라이스는 계속 볼 수 있습니다")
        painter.drawText(self.rect().adjusted(20, 20, -20, -20),
                         Qt.AlignCenter | Qt.TextWordWrap, text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        pixmap = self._render_image()
        transform = self._display_transform() if pixmap else None
        if not pixmap and self._series is not None and self._series.num_slices:
            self._draw_decode_failure(painter)
            self._draw_highlight(painter)
            painter.end()
            return
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
        self._draw_segmentation(painter, transform)

        self._draw_reference_lines(painter)
        if self._show_annotations:
            self._draw_annotations(painter)
            self._draw_draft(painter)
            self.draw_live_readout(painter)
            self._draw_key_marker(painter)
            self._draw_analysis(painter)
            for fn in self._overlay_painters:
                fn(self, painter)
        if self._current_tool == self.TOOL_CURSOR3D:   # 3D Cursor 모드일 때만 십자선·좌표
            self._draw_reference_cursor(painter)
        self._draw_wl_roi(painter)

        if self._show_overlay:
            if self._overlay_items.get("orientation", True):
                self._draw_orientation(painter)
            if self._overlay_items.get("phase", True):
                self._draw_phase(painter)
            self._draw_overlay(painter)
            self._draw_colorbars(painter)
            has_scale_bar = self._draw_scale_bar(painter)
            self._draw_window_bottom_right(painter, has_scale_bar)

        if self._magnifying and self._hover_pos is not None:
            self._draw_magnifier(painter, pixmap, transform)
        elif self._value_lens and self._hover_pos is not None:
            self._draw_value_lens(painter)

        self._draw_highlight(painter)
        painter.end()

    # ─── 주석 그리기 ───

    def _annotation_text(self, ann):
        """주석 라벨 문구 목록 (이름이 있으면 첫 줄, 단위는 측정 설정)"""
        kind = ann["type"]
        calibrated = ann.get("calibrated", True)
        name = [ann["name"]] if ann.get("name") else []
        pts = ann.get("pts") or []
        if kind == "distance":
            px = math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]) if len(pts) >= 2 else None
            return name + [fmt_length(ann["mm"], px)]
        if kind == "path":
            return name + [f"Σ {fmt_length(ann['mm'], roi_tools.path_length_mm(pts, (1.0, 1.0)))}"
                           f"  ({len(pts)} pts)"]
        if kind == "angle":
            return name + [f"{ann['deg']:.1f}°"]
        if kind == "area":
            from .roi import polygon_area_mm2 as _area_px
            px2 = _area_px(pts, (1.0, 1.0))
            area = fmt_area(ann["area"], px2) if calibrated else f"{px2:.0f} px²"
            return name + [f"Area {area}", f"Perim {fmt_length(ann['perimeter'])}"]
        if kind == "cobb":
            return name + [f"Cobb {ann['deg']:.1f}°"]
        if kind in ROI_TYPES:
            s, label = ann["stats"], ann.get("label", "")
            area = fmt_area(s["area_mm2"], s.get("pixels")) if calibrated else f"{s.get('pixels', 0)} px²"
            lines = name + [f"Area {area}"
                            + (f"  Perim {fmt_length(s['perimeter_mm'])}" if "perimeter_mm" in s else "")]
            if "mean" in s:
                digits = 2 if label == "SUVbw" else 1
                lines += [f"Mean {s['mean']:.{digits}f}  SD {s['std']:.{digits}f}",
                          f"Min {s['min']:.{digits}f}  Max {s['max']:.{digits}f}"
                          + (f"  Med {s['median']:.{digits}f}" if "median" in s else ""),
                          f"{label}  n={s['pixels']}"]
            return lines
        if kind == "cursor3d":
            lines = []
            if ann["patient"] is not None:
                lines.append(dicom_info.patient_position_text(ann["patient"]))
            if ann.get("value"):
                lines.append(ann["value"])
            if ann.get("delta") is not None:
                lines.append(f"Δ {ann['delta']:+.1f} mm")
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
        painter.setFont(self.label_font())
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
        items = list(self.annotations_here())
        cursor3d = self._cursor3d_for_drawing() if self._current_tool == self.TOOL_CURSOR3D else None
        if cursor3d is not None:
            items.append(cursor3d)
        for ann in items:
            if not ann.get("visible", True):
                continue
            kind = ann["type"]
            pts = [self._image_to_screen_f(p) for p in ann["pts"]]
            lines = self._annotation_text(ann)
            custom = QColor(ann["color"]) if ann.get("color") else None
            width = 3 if ann.get("id") in self._selected_ids else 2

            if kind == "distance":
                color = custom or COLOR_DISTANCE
                painter.setPen(QPen(color, width))
                painter.drawLine(pts[0], pts[1])
                painter.drawEllipse(pts[0], 3, 3)
                painter.drawEllipse(pts[1], 3, 3)
                mid = (pts[0] + pts[1]) / 2
                self._draw_label(painter, QPoint(int(mid.x()) + 6, int(mid.y()) - 22),
                                 lines, color, occupied)
            elif kind == "path":
                color = custom or QColor(255, 210, 74)
                painter.setPen(QPen(color, width))
                painter.drawPolyline(QPolygonF(pts))
                for p in pts:
                    painter.drawEllipse(p, 2.5, 2.5)
                self._draw_label(painter, QPoint(int(pts[-1].x()) + 8, int(pts[-1].y()) - 22),
                                 lines, color, occupied)
            elif kind == "rect":
                color = custom or QColor(255, 90, 210)
                rect = QRectF(pts[0], pts[1]).normalized()
                fill = QColor(color)
                fill.setAlpha(40)
                painter.setPen(QPen(color, width))
                painter.setBrush(fill)
                painter.drawRect(rect)
                painter.setBrush(Qt.NoBrush)
                self._draw_label(painter, QPoint(int(rect.right()) + 6, int(rect.top())),
                                 lines, color, occupied)
            elif kind == "angle":
                painter.setPen(QPen(custom or COLOR_ANGLE, width))
                painter.drawLine(pts[0], pts[1])
                painter.drawLine(pts[1], pts[2])
                painter.drawEllipse(pts[1], 3, 3)
                self._draw_label(painter, QPoint(int(pts[1].x()) + 10,
                                                 int(pts[1].y()) - 24),
                                 lines, COLOR_ANGLE, occupied)
            elif kind in ("roi", "area"):
                color = custom or (COLOR_ROI if kind == "roi" else COLOR_AREA)
                poly = self._screen_polygon(ann["pts"])
                fill = QColor(color)
                fill.setAlpha(40)
                painter.setPen(QPen(color, width))
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
            elif kind == "ellipse":
                color = custom or COLOR_ELLIPSE
                rect = QRectF(pts[0], pts[1]).normalized()
                fill = QColor(color)
                fill.setAlpha(40)
                painter.setPen(QPen(color, width))
                painter.setBrush(fill)
                painter.drawEllipse(rect)
                painter.setBrush(Qt.NoBrush)
                self._draw_label(painter, QPoint(int(rect.right()) + 6, int(rect.top())),
                                 lines, color, occupied)
            elif kind == "cobb":
                painter.setPen(QPen(COLOR_COBB, 2))
                painter.drawLine(pts[0], pts[1])
                painter.drawLine(pts[2], pts[3])
                for p in pts:
                    painter.drawEllipse(p, 3, 3)
                mid = (pts[0] + pts[1] + pts[2] + pts[3]) / 4
                self._draw_label(painter, QPoint(int(mid.x()) + 10, int(mid.y())),
                                 lines, COLOR_COBB, occupied)
            elif kind == "text":
                font = QFont()
                font.setPointSize(int(ann.get("size", 14)))
                font.setBold(True)
                painter.setFont(font)
                p = pts[0]
                painter.setPen(QColor(0, 0, 0, 220))
                for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    painter.drawText(QPointF(p.x() + ox, p.y() + oy), ann["text"])
                painter.setPen(QColor(ann.get("color", "#ffff00")))
                painter.drawText(p, ann["text"])
            elif kind == "cursor3d":
                p = pts[0]
                off_plane = bool(ann.get("off_plane"))
                color = COLOR_CURSOR3D if ann.get("mine", True) else COLOR_CURSOR3D_LINKED
                painter.setPen(QPen(color, 2, Qt.DashLine if off_plane else Qt.SolidLine))
                painter.drawLine(QPointF(p.x() - 10, p.y()), QPointF(p.x() - 3, p.y()))
                painter.drawLine(QPointF(p.x() + 3, p.y()), QPointF(p.x() + 10, p.y()))
                painter.drawLine(QPointF(p.x(), p.y() - 10), QPointF(p.x(), p.y() - 3))
                painter.drawLine(QPointF(p.x(), p.y() + 3), QPointF(p.x(), p.y() + 10))
                self._draw_label(painter, QPoint(int(p.x()) + 12, int(p.y()) + 6),
                                 lines, color, occupied)
            if kind != "cursor3d":
                self.draw_selection(painter, ann, custom or QColor(roi_tools.color_of(ann)))

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
        elif kind == "ellipse":
            painter.setPen(QPen(COLOR_ELLIPSE, 2, Qt.DashLine))
            painter.drawEllipse(QRectF(pts[0], pts[1]).normalized())
        elif kind == "rect":
            painter.setPen(QPen(QColor(255, 90, 210), 2, Qt.DashLine))
            painter.drawRect(QRectF(pts[0], pts[1]).normalized())
        elif kind == "path":
            painter.setPen(QPen(QColor(255, 210, 74), 2, Qt.DashLine))
            painter.drawPolyline(QPolygonF(pts))
            for p in pts[:-1]:
                painter.drawEllipse(p, 2.5, 2.5)
        elif kind == "cobb":
            painter.setPen(QPen(COLOR_COBB, 2, Qt.DashLine))
            for i in range(0, len(pts) - 1, 2):
                painter.drawLine(pts[i], pts[i + 1])
        elif kind == "profile":
            painter.setPen(QPen(QColor(255, 200, 0), 2, Qt.DashLine))
            painter.drawLine(pts[0], pts[1])

    def _draw_reference_lines(self, painter):
        """다른 뷰포트 시리즈의 위치 (Scout / Reference Line)

        PACS처럼: 그 시리즈의 전체 슬라이스(스캔 플래닝)를 파란 가는 점선으로,
        지금 보고 있는 슬라이스는 노란 실선(2 px)으로 — 두 색을 달리해 구분. 스크롤하면 노란 선이 따라 움직임.
        """
        if self._reference_sources is None or self._series is None:
            return
        geom = self._series.geometry
        if geom is None:
            return
        rect = self._image_screen_rect()
        painter.save()
        painter.setClipRect(rect)
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        line_h = painter.fontMetrics().height()
        label_spots = []  # 이미 쓴 라벨 위치 - 겹치는 선(같은 위치의 슬라이스)은 아래로 쌓음
        for source in self._reference_sources():
            source_geom, index, label = source[:3]
            color = QColor(source[3]) if len(source) > 3 else QColor(200, 200, 200)
            coverage = source[4] if len(source) > 4 else True
            # Crosslink: 그 시리즈 전체 슬라이스를 가는 점선으로 (Ref Lines만 켜면 현재 슬라이스 한 줄)
            dash = QPen(color, 1, Qt.CustomDashLine)
            dash.setDashPattern([3, 4])
            dash.setCosmetic(True)
            painter.setPen(dash)
            show_coverage = coverage and self._show_overlay and self._overlay_items.get("coverage", True)
            for seg in (self._coverage_segments(source_geom, geom) if show_coverage else []):
                if seg[0] == index:
                    continue
                painter.drawLine(*seg[1:])
            # 현재 슬라이스: 노란 실선
            seg = reference_line(source_geom, index, geom, self._current_slice)
            if seg is None:
                continue
            (c1, r1), (c2, r2) = seg
            a = self._image_to_screen_f((c1 + 0.5, r1 + 0.5))
            b = self._image_to_screen_f((c2 + 0.5, r2 + 0.5))
            painter.setPen(QPen(COLOR_REFLINE, 2))
            painter.drawLine(a, b)
            if label:
                end = a if a.x() > b.x() else b
                x = min(end.x(), rect.right() - 60) + 2
                y = end.y() - 3
                while any(abs(x - px) < 50 and abs(y - py) < line_h for px, py in label_spots):
                    y += line_h
                label_spots.append((x, y))
                painter.setPen(QColor(0, 0, 0, 200))
                painter.drawText(QPointF(x + 1, y + 1), label)
                painter.setPen(color)
                painter.drawText(QPointF(x, y), label)
        painter.restore()

    def _coverage_segments(self, source_geom, geom):
        """source 시리즈 모든 슬라이스의 교선 → [(슬라이스 번호, 화면 점 a, 화면 점 b)]
        (영상 좌표 교선은 슬라이스·시리즈가 바뀔 때만 다시 계산)"""
        key = (id(source_geom), id(geom), self._current_slice)
        cache = self.__dict__.setdefault("_coverage_cache", {})
        pix = cache.get(key)
        if pix is None:
            if len(cache) > 64:
                cache.clear()
            pix, seen = [], set()
            for i in range(source_geom.num_slices):
                where = tuple(np.round(source_geom.origins[i], 1))   # 시네 · 다중 위상: 같은 위치는 한 번만
                if where in seen:
                    continue
                seen.add(where)
                seg = reference_line(source_geom, i, geom, self._current_slice)
                if seg is not None:
                    pix.append((i, seg))
            cache[key] = pix
        out = []
        for i, ((c1, r1), (c2, r2)) in pix:
            out.append((i, self._image_to_screen_f((c1 + 0.5, r1 + 0.5)),
                        self._image_to_screen_f((c2 + 0.5, r2 + 0.5))))
        return out

    # ─── 방향 표시 (A/P · R/L · S/I) · 위상 인코딩 방향 ───
    def _screen_axis_patient(self, screen_vec):
        """화면 방향 (x, y) → 환자 좌표 방향 (LPS). 위치 정보가 없으면 None"""
        geom = self._series.geometry if self._series is not None else None
        if geom is None or not (0 <= self._current_slice < geom.num_slices):
            return None
        inv = np.linalg.inv(self._orient.astype(float))   # 화면 → 영상 (회전 · 반전)
        dc, dr = inv @ np.asarray(screen_vec, float)
        v = dc * geom.row_dirs[self._current_slice] + dr * geom.col_dirs[self._current_slice]
        n = np.linalg.norm(v)
        return v / n if n > 1e-9 else None

    @staticmethod
    def _direction_letters(v):
        """LPS 방향 → 'L', 'PA'처럼 (주 방향 + 비스듬하면 둘째 방향)"""
        names = (("L", "R"), ("P", "A"), ("S", "I"))
        order = np.argsort(-np.abs(v))
        out = ""
        for k, axis in enumerate(order[:2]):
            if k == 0 or abs(v[axis]) >= 0.35:
                out += names[axis][0 if v[axis] > 0 else 1]
        return out

    def _draw_orientation(self, painter):
        """영상 가장자리 가운데에 방향 문자 (GE 콘솔처럼)"""
        if self._series is None or self._series.geometry is None:
            return
        right, down = self._screen_axis_patient((1, 0)), self._screen_axis_patient((0, 1))
        if right is None or down is None:
            return
        rect = QRectF(self._image_screen_rect()).intersected(QRectF(self.rect()))
        font = QFont(self._overlay_font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setBold(True)
        painter.save()
        painter.setFont(font)
        fm = painter.fontMetrics()
        spots = {"right": (right, rect.right() - 6, rect.center().y(), "r"),
                 "left": (-right, rect.left() + 6, rect.center().y(), "l"),
                 "bottom": (down, rect.center().x(), rect.bottom() - 6, "b"),
                 "top": (-down, rect.center().x(),
                         max(rect.top() + 6, 10 + fm.height()
                             if self._overlay_items.get("phase", True) and self._phase_direction() else 0)
                         + fm.ascent(),
                         "t")}   # 위상 표시(맨 위 가운데) 아래로
        for _name, (vec, x, y, side) in spots.items():
            text = self._direction_letters(vec)
            w = fm.horizontalAdvance(text)
            if side == "r":
                x -= w
            elif side in ("t", "b"):
                x -= w / 2
            if side in ("l", "r"):
                y += fm.ascent() / 2
            painter.setPen(QColor(0, 0, 0, 220))
            painter.drawText(QPointF(x + 1, y + 1), text)
            painter.setPen(QColor(255, 215, 90))
            painter.drawText(QPointF(x, y), text)
        painter.restore()

    def _phase_direction(self):
        """(0018,1312) InPlanePhaseEncodingDirection → ('ROW'|'COL', 화면에서 가로?, 'R↔L' 같은 글)"""
        if self._series is None or not (0 <= self._current_slice < self._series.num_slices):
            return None
        ds = self._series.slices[self._current_slice]
        value = str(getattr(ds, "InPlanePhaseEncodingDirection", "") or "").strip().upper()
        if value not in ("ROW", "COL"):
            return None
        image_vec = np.array([1, 0] if value == "ROW" else [0, 1], float)   # ROW: 행을 따라 = 가로
        screen = self._orient.astype(float) @ image_vec
        horizontal = abs(screen[0]) > abs(screen[1])
        v = self._screen_axis_patient((1, 0) if horizontal else (0, 1))
        if v is None:
            letters = "좌우" if horizontal else "상하"
        else:
            a, b = self._direction_letters(-v), self._direction_letters(v)
            letters = f"{a}{'↔' if horizontal else '↕'}{b}"
        return value, horizontal, letters

    def _draw_phase(self, painter):
        """영상 위쪽 가운데: [Phase] R↔L + 방향 화살표 (위상 인코딩 방향 - 모션 · 랩어라운드 아티팩트 방향)"""
        info = self._phase_direction()
        if info is None:
            return
        value, horizontal, letters = info
        font = QFont(self._overlay_font())
        font.setBold(True)
        painter.save()
        painter.setFont(font)
        fm = painter.fontMetrics()
        text = f"Phase: {letters}  ({value})"
        arrow_len = 26
        w = fm.horizontalAdvance(text) + arrow_len + 10
        x = self.width() / 2 - w / 2
        y = 6 + fm.ascent()
        painter.setPen(QColor(0, 0, 0, 220))
        painter.drawText(QPointF(x + 1, y + 1), text)
        painter.setPen(QColor(120, 220, 255))
        painter.drawText(QPointF(x, y), text)
        # 양쪽 화살표 (위상 방향 - DICOM 표준 태그에는 +/- 극성이 없음)
        cx = x + fm.horizontalAdvance(text) + 8 + arrow_len / 2
        cy = y - fm.ascent() / 2 + 1
        half = arrow_len / 2 if horizontal else fm.ascent() * 0.6
        p0 = QPointF(cx - half, cy) if horizontal else QPointF(cx, cy - half)
        p1 = QPointF(cx + half, cy) if horizontal else QPointF(cx, cy + half)
        painter.setPen(QPen(QColor(120, 220, 255), 2))
        painter.drawLine(p0, p1)
        head = 5
        for tip, sign in ((p0, -1), (p1, 1)):
            if horizontal:
                painter.drawLine(tip, QPointF(tip.x() - sign * head, tip.y() - head))
                painter.drawLine(tip, QPointF(tip.x() - sign * head, tip.y() + head))
            else:
                painter.drawLine(tip, QPointF(tip.x() - head, tip.y() - sign * head))
                painter.drawLine(tip, QPointF(tip.x() + head, tip.y() - sign * head))
        painter.restore()

    def _draw_key_marker(self, painter):
        if not self.is_key_image():
            return
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        painter.setFont(font)
        text = "★ KEY"
        x = self.width() / 2 - painter.fontMetrics().horizontalAdvance(text) / 2
        painter.setPen(QColor(0, 0, 0, 220))
        painter.drawText(QPointF(x + 1, 27), text)
        painter.setPen(COLOR_KEY)
        painter.drawText(QPointF(x, 26), text)

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

        message = "DICOM · NIfTI · NRRD · MHA · NumPy · PNG 파일을 열어주세요"
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
        title_h, version_h, msg_h, hint_h, gap = 34, 18, 26, 22, 16
        block_h = logo_size + gap + title_h + version_h + gap + msg_h + hint_h
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
        painter.drawText(QRect(0, y, w, title_h), Qt.AlignCenter, "DabbaView")
        y += title_h
        font = QFont()
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QColor(120, 120, 120))
        painter.drawText(QRect(0, y, w, version_h), Qt.AlignCenter, f"v{__version__}")
        y += version_h + gap

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
        right = self._content_right() - margin

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

    # 우하단 스케일 바의 세로 위치 (W/L 문구를 그 위에 둠)
    SCALE_BAR_OFFSET = 26  # 아래 가장자리에서 막대까지
    SCALE_TICK = 6

    def _draw_window_bottom_right(self, painter, above_scale_bar):
        """우하단 W/L (좌하단 표시와 같은 형식). 스케일 바가 있으면 그 위에"""
        text = f"W:{self._window_width:.0f} L:{self._window_center:.0f}"
        painter.setFont(self._overlay_font())
        fm = painter.fontMetrics()
        x = self._content_right() - 10 - fm.horizontalAdvance(text)
        if above_scale_bar:
            y = self.height() - self.SCALE_BAR_OFFSET - self.SCALE_TICK - 4 - fm.descent()
        else:
            y = self.height() - 8 - fm.descent()
        self._draw_text_shadow(painter, x, y, text)

    def _draw_scale_bar(self, painter):
        """우하단 스케일 바 (cm 눈금). 그렸으면 True"""
        mm_per_px = self._mm_per_screen_px()
        if not mm_per_px:
            return False
        length_mm = dicom_info.nice_scale_length_mm(self.width() * 0.3 * mm_per_px)
        if not length_mm:
            return False
        length_px = length_mm / mm_per_px
        margin = 10
        x1 = self._content_right() - margin
        x0 = x1 - length_px
        y = self.height() - self.SCALE_BAR_OFFSET
        tick_mm = 10 if length_mm >= 20 else (1 if length_mm <= 10 else 5)

        for color, offset in ((QColor(0, 0, 0, 200), 1), (QColor(235, 235, 235), 0)):
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(x0 + offset, y + offset),
                             QPointF(x1 + offset, y + offset))
            n_ticks = int(round(length_mm / tick_mm))
            for i in range(n_ticks + 1):
                tx = x0 + i * tick_mm / mm_per_px + offset
                tick = self.SCALE_TICK if i in (0, n_ticks) else 3
                painter.drawLine(QPointF(tx, y - tick + offset), QPointF(tx, y + offset))

        label = f"{length_mm / 10:g}cm" if length_mm >= 10 else f"{length_mm:g}mm"
        painter.setFont(self._overlay_font())
        fm = painter.fontMetrics()
        self._draw_text_shadow(painter, x1 - fm.horizontalAdvance(label),
                               y + 4 + fm.ascent(), label)
        return True

    def _draw_highlight(self, painter):
        if not self._highlight:
            return
        color, width = self._highlight
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.NoBrush)
        half = width // 2
        painter.drawRect(self.rect().adjusted(half, half, -half - 1 + width % 2,
                                              -half - 1 + width % 2))

    # 슬라이스 막대 (모든 뷰포트 공통 — View ▸ 슬라이스 막대)
    slice_bar_enabled = True

    def refresh_slice_bar(self):
        """막대 위치 · 범위를 지금 상태에 맞춤 (칸 크기가 바뀌거나 시리즈 · 슬라이스가 바뀔 때)"""
        bar = getattr(self, "_slice_bar", None)
        if bar is None:
            return
        bar.set_enabled_by_user(DicomViewport.slice_bar_enabled)
        bar.place(self.width(), self.height(), top=4, bottom=4)
        bar.set_range(self._current_slice, self._series.num_slices if self._series else 0)
        if bar.isVisible():
            bar.raise_()

    def _content_right(self):
        """오버레이 글자 · 스케일 바가 쓸 수 있는 오른쪽 끝 (슬라이스 막대를 비켜서)"""
        bar = getattr(self, "_slice_bar", None)
        return self.width() - (SLICE_BAR_W + 2 if bar is not None and bar.isVisible() else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._series:
            self._cache_valid = False
        self.refresh_slice_bar()

    # ─── 공개 API ───

    def set_window(self, center, width, user=False):
        """user=True: 사용자가 바꾼 경우 (프리셋 등) → window_adjusted로 동기화 전파"""
        self._smooth.pending.pop("wc", None)      # 따라가던 W/L 드래그가 프리셋을 덮지 않게
        self._smooth.pending.pop("ww", None)
        self._window_center = center
        self._window_width = max(1, width)
        self._cache_valid = False
        self.window_changed.emit(self._window_center, self._window_width)
        if user:
            self.window_adjusted.emit(self._window_center, self._window_width)
        self.update()

    def reset_window(self):
        """DICOM 기본 W/L로 되돌림"""
        if self._series:
            wc, ww = self._series.get_default_window()
            self.set_window(wc, ww, user=True)

    @property
    def window_level(self):
        """(center, width) - QWidget.window()와 이름이 겹치지 않도록"""
        return self._window_center, self._window_width

    def reset_view(self):
        """회전/반전을 원래대로 하고 화면에 맞춤"""
        self.stop_motion()
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
