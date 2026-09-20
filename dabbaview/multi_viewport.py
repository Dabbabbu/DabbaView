# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
다중 뷰포트 레이아웃 매니저

- 1x1 ~ 4x4 레이아웃 (최대 16칸)
- 동기화 스크롤: 같은 Frame of Reference + 평행한 시리즈는 물리적 위치로,
  비교(Compare) 연결된 칸은 슬라이스 간격(offset)을 유지하며 함께 이동
- 동기화 윈도잉: 같은 모달리티 칸에 W/L 전파
- Reference Line: 다른 칸의 현재 슬라이스 위치를 선으로 표시
- 드래그 앤 드롭: 시리즈 트리/패널, Finder 파일·폴더
"""
from PyQt5.QtWidgets import (QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
                             QPushButton, QButtonGroup)
from PyQt5.QtCore import Qt, QEvent, pyqtSignal

from .viewport import DicomViewport
from .series_tree import SERIES_MIME_TYPE

ACTIVE_BORDER = ("#ffd400", 2)       # GE 스타일 노란 테두리 (활성 칸)
SELECTED_BORDER = ("#3d8bfd", 2)     # 함께 움직이는 칸 (Ctrl·Shift 클릭으로 고름)
DROP_TARGET_BORDER = ("#ff8c00", 4)
MAX_VIEWPORTS = 16

LAYOUTS = {
    "1x1": (1, 1), "1x2": (1, 2), "2x1": (2, 1), "2x2": (2, 2),
    "2x3": (2, 3), "3x3": (3, 3), "3x4": (3, 4), "4x4": (4, 4),
}
LAYOUT_BUTTONS = [("1x1", "▣"), ("1x2", "◫"), ("2x1", "◩"), ("2x2", "⊞"),
                  ("3x3", "3×3"), ("4x4", "4×4")]


def grid_for_count(n):
    """n칸이 들어가는 가장 작은 레이아웃 이름"""
    for name in ("1x1", "1x2", "2x2", "2x3", "3x3", "3x4", "4x4"):
        rows, cols = LAYOUTS[name]
        if rows * cols >= n:
            return name
    return "4x4"



# Reference Line 전체 커버리지 색 (Multi View 칸 순서: 파랑, 초록, 주황, 보라, 분홍, 청록 …)
COVERAGE_COLORS = ["#4aa3ff", "#3ddc84", "#ff9f40", "#c78bff", "#ff6b9a", "#35d0d0", "#e6e6e6", "#b8b83a"]

def _proportional(index, src_count, dst_count):
    """장수가 다른 시리즈: 같은 비율 위치로 (예: 195장의 100번째 → 20장의 10번째)"""
    if src_count <= 1 or dst_count <= 1:
        return 0
    return int(round(index * (dst_count - 1) / (src_count - 1)))


class MultiViewport(QWidget):
    """다중 뷰포트 관리 위젯"""

    active_viewport_changed = pyqtSignal(int)  # viewport index
    series_dropped = pyqtSignal(int, str)      # viewport index, SeriesInstanceUID
    paths_dropped = pyqtSignal(int, list)      # viewport index, 파일/폴더 경로
    layout_changed = pyqtSignal(str)
    selection_changed = pyqtSignal(list)       # 함께 움직이는 칸 번호들

    LAYOUT_1x1 = "1x1"
    LAYOUT_1x2 = "1x2"
    LAYOUT_2x1 = "2x1"
    LAYOUT_2x2 = "2x2"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._viewports = []
        self._active_index = 0
        self._current_layout = self.LAYOUT_1x1
        self._sync_scroll = False
        self._sync_window = False
        self._reference_lines = False
        self._selected = set()                 # 활성 칸 외에 함께 움직이는 칸
        self._crosslink = False                # Crosslink: 다른 시리즈 전체 스캔 범위 표시
        self._compare_offsets = {}  # (i, j) → j 슬라이스 - i 슬라이스
        self._syncing = False
        self._maximized = False  # Space: 활성 칸만 크게
        self._init_ui()

    def _init_ui(self):
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # 레이아웃 선택 버튼
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(2)
        self._btn_group = QButtonGroup(self)
        self._layout_buttons = {}
        for layout_id, icon in LAYOUT_BUTTONS:
            btn = QPushButton(icon)
            btn.setFixedSize(40 if len(icon) > 1 else 32, 28)
            btn.setCheckable(True)
            btn.setToolTip(layout_id)
            btn.setStyleSheet("""
                QPushButton { background: #333; color: #ccc; border: 1px solid #555;
                              font-size: 13px; }
                QPushButton:checked { background: #007acc; color: white; }
                QPushButton:hover { background: #444; }
            """)
            btn.clicked.connect(lambda checked, lid=layout_id: self.set_layout(lid))
            self._btn_group.addButton(btn)
            self._layout_buttons[layout_id] = btn
            btn_layout.addWidget(btn)
        btn_layout.addStretch()
        self._main_layout.addLayout(btn_layout)

        # 뷰포트 그리드 컨테이너
        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(2)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.addWidget(self._grid_container)

        for i in range(MAX_VIEWPORTS):
            vp = DicomViewport()
            vp.setProperty("vp_index", i)
            vp.setMinimumSize(120, 120)  # 4x4 레이아웃도 화면에 들어가도록
            vp.setAcceptDrops(True)
            vp.installEventFilter(self)
            vp.scrolled.connect(lambda index, src=i: self._on_scrolled(src, index))
            vp.stepped.connect(lambda kind, direction, src=i: self._on_stepped(src, kind, direction))
            vp.window_adjusted.connect(
                lambda c, w, src=i: self._on_window_adjusted(src, c, w))
            vp.slice_changed.connect(lambda *_: self._refresh_reference_lines())
            vp.set_reference_source(lambda v=vp: self._reference_sources_for(v))
            self._viewports.append(vp)

        self._apply_layout()

    # ─── 이벤트 (활성화 / 드래그 앤 드롭) ───

    def eventFilter(self, obj, event):
        """뷰포트 클릭 시 활성 뷰포트 변경 + 드래그 앤 드롭 처리"""
        if obj not in self._viewports:
            return super().eventFilter(obj, event)
        etype = event.type()
        idx = self._viewports.index(obj)

        if etype == QEvent.MouseButtonPress:
            mods = event.modifiers()
            if mods & (Qt.ControlModifier | Qt.MetaModifier):      # Ctrl(⌘)+클릭: 하나씩 고르기
                self.toggle_selected(idx)
            elif mods & Qt.ShiftModifier:                          # Shift+클릭: 활성 칸부터 여기까지
                self.select_range(self._active_index, idx)
            else:
                self._selected.clear()
                self.set_active(idx)

        elif etype in (QEvent.DragEnter, QEvent.DragMove):
            if self._accepts(event.mimeData()):
                event.acceptProposedAction()
                obj.set_highlight(*DROP_TARGET_BORDER)
            else:
                event.ignore()
            return True

        elif etype == QEvent.DragLeave:
            self._highlight_active()
            return True

        elif etype == QEvent.Drop:
            self._highlight_active()
            mime = event.mimeData()
            if mime.hasFormat(SERIES_MIME_TYPE):
                uid = bytes(mime.data(SERIES_MIME_TYPE)).decode('utf-8')
                event.acceptProposedAction()
                self.series_dropped.emit(idx, uid)
            elif mime.hasUrls():
                paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
                if paths:
                    event.acceptProposedAction()
                    self.paths_dropped.emit(idx, paths)
            return True

        return super().eventFilter(obj, event)

    @staticmethod
    def _accepts(mime):
        if mime.hasFormat(SERIES_MIME_TYPE):
            return True
        return mime.hasUrls() and any(u.isLocalFile() for u in mime.urls())

    # ─── 레이아웃 ───

    def set_layout(self, layout_id):
        """레이아웃 변경 ('1x1' ~ '4x4')"""
        if layout_id not in LAYOUTS:
            return
        self._maximized = False  # 레이아웃을 바꾸면 최대화 해제
        self._current_layout = layout_id
        btn = self._layout_buttons.get(layout_id)
        if btn is not None:
            btn.setChecked(True)
        else:
            # 버튼이 없는 레이아웃(2x3 등)이면 모든 버튼 해제
            self._btn_group.setExclusive(False)
            for b in self._btn_group.buttons():
                b.setChecked(False)
            self._btn_group.setExclusive(True)
        if self._active_index >= self.num_visible:
            self._active_index = 0
        self._apply_layout()
        self.layout_changed.emit(layout_id)

    @property
    def current_layout(self):
        return self._current_layout

    def _apply_layout(self):
        """현재 레이아웃 적용"""
        for vp in self._viewports:
            self._grid_layout.removeWidget(vp)
            vp.hide()
        if self._maximized:
            # 활성 칸만 전체 크기로 (다른 칸은 숨김, 시리즈·동기화 상태는 유지)
            vp = self.active_viewport
            self._grid_layout.addWidget(vp, 0, 0)
            vp.show()
            vp.set_highlight(None)
            return
        rows, cols = LAYOUTS[self._current_layout]
        for i in range(rows * cols):
            self._grid_layout.addWidget(self._viewports[i], i // cols, i % cols)
            self._viewports[i].show()
        self._highlight_active()
        self._refresh_reference_lines()

    def toggle_maximize(self):
        """활성 칸만 크게 ↔ 원래 레이아웃. 새 상태(True=최대화) 반환"""
        if not self._maximized and self.num_visible <= 1:
            return False
        self._maximized = not self._maximized
        self._apply_layout()
        return self._maximized

    @property
    def is_maximized(self):
        return self._maximized

    def toggle_selected(self, index):
        """Ctrl(⌘)+클릭: 함께 움직일 칸에 넣거나 뺌 (활성 칸은 항상 포함)"""
        if not 0 <= index < self.num_visible:
            return
        if index == self._active_index:
            return
        if index in self._selected:
            self._selected.discard(index)
        else:
            self._selected.add(index)
        self._highlight_active()
        self.selection_changed.emit(sorted(self.selected_indices))

    def select_range(self, start, end):
        """Shift+클릭: 두 칸 사이를 모두 선택"""
        lo, hi = sorted((start, end))
        self._selected = {i for i in range(lo, hi + 1) if i < self.num_visible and i != self._active_index}
        self._highlight_active()
        self.selection_changed.emit(sorted(self.selected_indices))

    @property
    def selected_indices(self):
        """함께 움직이는 칸 (활성 칸 포함). 하나뿐이면 다중 선택 아님"""
        return {self._active_index} | {i for i in self._selected if i < self.num_visible}

    def clear_selection(self):
        if self._selected:
            self._selected.clear()
            self._highlight_active()
            self.selection_changed.emit(sorted(self.selected_indices))

    def _sync_targets(self, src):
        """src가 움직였을 때 따라갈 칸들"""
        selected = self.selected_indices
        if len(selected) > 1:
            return [i for i in selected if i != src] if src in selected else []
        if self._sync_scroll:
            return [i for i in range(self.num_visible) if i != src]
        return []

    def set_active(self, index):
        """활성 뷰포트 설정"""
        if 0 <= index < len(self._viewports):
            self._selected.discard(index)
            self._active_index = index
            if self._maximized:
                self._apply_layout()  # 최대화 중이면 새 활성 칸을 크게
            else:
                self._highlight_active()
            self.active_viewport_changed.emit(index)

    def _highlight_active(self):
        """활성 칸은 노란 테두리, 함께 고른 칸은 파란 테두리 (1x1에서는 생략)"""
        multi = self.num_visible > 1
        selected = self.selected_indices
        for i, vp in enumerate(self._viewports):
            if multi and i == self._active_index:
                vp.set_highlight(*ACTIVE_BORDER)
            elif multi and i in selected:
                vp.set_highlight(*SELECTED_BORDER)
            else:
                vp.set_highlight(None)

    @property
    def viewports(self):
        return list(self._viewports)

    @property
    def visible_viewports(self):
        return self._viewports[:self.num_visible]

    @property
    def active_index(self):
        return self._active_index

    @property
    def active_viewport(self):
        """현재 활성 뷰포트 반환"""
        return self._viewports[self._active_index]

    def get_viewport(self, index):
        if 0 <= index < len(self._viewports):
            return self._viewports[index]
        return None

    def set_series_to_active(self, series):
        """활성 뷰포트에 시리즈 설정"""
        self.active_viewport.set_series(series)
        self._compare_offsets = {k: v for k, v in self._compare_offsets.items()
                                 if self._active_index not in k}
        self._refresh_reference_lines()

    def set_series_to_viewport(self, index, series):
        """특정 뷰포트에 시리즈 설정"""
        vp = self.get_viewport(index)
        if vp:
            vp.set_series(series)
            self._refresh_reference_lines()

    def show_series(self, series_list, layout=None):
        """시리즈 목록을 칸 순서대로 배치 (None은 빈 칸). 레이아웃 자동 선택 가능"""
        series_list = list(series_list)[:MAX_VIEWPORTS]
        self.set_layout(layout or grid_for_count(max(1, len(series_list))))
        self._compare_offsets = {}
        for i in range(self.num_visible):
            s = series_list[i] if i < len(series_list) else None
            self._viewports[i].set_series(s)
        self.set_active(0)
        self._refresh_reference_lines()

    @property
    def num_visible(self):
        rows, cols = LAYOUTS[self._current_layout]
        return rows * cols

    # ─── 동기화 / Reference Line ───

    def set_sync_scroll(self, enabled):
        self._sync_scroll = enabled

    def set_sync_window(self, enabled):
        self._sync_window = enabled

    @property
    def sync_scroll(self):
        return self._sync_scroll

    def set_reference_lines(self, enabled):
        self._reference_lines = enabled
        self._refresh_reference_lines()

    def link_compare(self, i, j):
        """두 칸을 비교 연결: 현재 슬라이스 간격을 유지하며 함께 스크롤"""
        a, b = self._viewports[i], self._viewports[j]
        self._compare_offsets[(i, j)] = b.current_slice - a.current_slice
        self._compare_offsets[(j, i)] = a.current_slice - b.current_slice

    def _on_scrolled(self, src, index):
        if self._syncing or src >= self.num_visible:
            return
        targets = self._sync_targets(src)
        if not targets:
            return
        source = self._viewports[src]
        src_geom = source.sync_geometry()
        self._syncing = True
        try:
            for j in targets:
                target = self._viewports[j]
                if target.series is None:
                    continue
                if (src, j) in self._compare_offsets:
                    target.go_to_slice(index + self._compare_offsets[(src, j)], user=False)
                    continue
                dst_geom = target.sync_geometry()
                if (src_geom is not None and dst_geom is not None
                        and src_geom.is_linkable_with(dst_geom)
                        and src_geom.is_parallel_to(index, dst_geom, target.current_slice)):
                    # 같은 좌표계 + 평행: 소스 슬라이스 중심에서 가장 가까운 슬라이스
                    nearest, _ = dst_geom.nearest_slice(src_geom.center_point(index))
                    target.go_to_slice(nearest, user=False)
                else:
                    # 좌표계가 다르거나 방향이 다른 시리즈: 장수 비율로 맞춰서 이동
                    target.go_to_slice(_proportional(index, source.series.num_slices,
                                                     target.series.num_slices), user=False)
        finally:
            self._syncing = False

    def _on_stepped(self, src, kind, direction):
        """방향키(위치 · 위상)를 함께 고른 칸에도 그대로 적용"""
        if self._syncing or src >= self.num_visible:
            return
        self._syncing = True
        try:
            for j in self._sync_targets(src):
                target = self._viewports[j]
                if target.series is not None:
                    target.step_slice(kind, direction, user=False)
        finally:
            self._syncing = False

    def _on_window_adjusted(self, src, center, width):
        if not self._sync_window or self._syncing or src >= self.num_visible:
            return
        modality = self._viewports[src].series.modality if self._viewports[src].series else ""
        self._syncing = True
        try:
            for j, target in enumerate(self.visible_viewports):
                if j != src and target.series is not None and target.series.modality == modality:
                    target.set_window(center, width, user=False)
        finally:
            self._syncing = False

    def set_crosslink(self, enabled):
        """Crosslink: 다른 칸 시리즈의 전체 스캔 범위를 이 영상 위에 점선으로"""
        self._crosslink = enabled
        self._refresh_reference_lines()

    def _reference_sources_for(self, viewport):
        """viewport에 그릴 다른 칸들의 선 (같은 환자 + 같은 좌표계)

        Crosslink ON → 그 시리즈 전체 슬라이스(점선) + 현재 슬라이스(노란 실선)
        Ref Lines ON → 현재 슬라이스 한 줄만
        """
        if not (self._reference_lines or self._crosslink) or viewport.series is None:
            return []
        own = viewport.sync_geometry()
        if own is None:
            return []
        sources = []
        for i, vp in enumerate(self.visible_viewports):
            if vp is viewport or vp.series is None:
                continue
            geom = vp.sync_geometry()
            if (geom is None or not own.is_linkable_with(geom)
                    or vp.series.patient_id != viewport.series.patient_id):
                continue
            label = f"S{vp.series.series_number or ''}:{vp.current_slice + 1}"
            # 칸마다 다른 색 (커버리지 점선) - 현재 슬라이스는 노란 실선
            sources.append((geom, vp.current_slice, label, COVERAGE_COLORS[i % len(COVERAGE_COLORS)],
                            self._crosslink))
        return sources

    def _refresh_reference_lines(self):
        if self._reference_lines or self._crosslink:
            for vp in self.visible_viewports:
                vp.update()
