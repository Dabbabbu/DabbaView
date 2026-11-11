# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Zoom · W/L · 원본 W/L · 그리기 칸 - 맨 아래 상태바에 있어 영상을 가리지 않는다

- 🔍 Zoom: 누른 채 위로 끌면 확대, 아래로 축소 (두 번 클릭: 화면 맞춤)
- ◐ W/L: 좌우 = Width, 위아래 = Level — 영상 위 우클릭 드래그와 같음 (두 번 클릭: 원본 W/L)
- ◑↺ 원본 W/L: 한 번 눌러 DICOM 태그의 Window Center/Width로 되돌리기
- ✏️ 그리기: 눌러서 측정 도구 고르기 (그리는 중이면 지금 도구 이름 · 눌러서 끄기)
- 대상은 지금 보는 영상 (Multi View에서는 선택한 칸)
"""
from PyQt5.QtCore import QPoint, Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QMenu, QWidget

from . import icons

PAD_STYLE = """
QLabel#DragPad { background: #262a30; color: #cfd6df; border: 1px solid #3a414a; border-radius: 4px;
                 font-size: 11px; padding: 1px 8px; }
QLabel#DragPad:hover { background: #2f3945; border-color: #3d8bfd; color: #fff; }
QLabel#DragPad[dragging="true"] { background: #094771; border-color: #3d8bfd; color: #fff; }
QLabel#DragPad[on="true"] { background: #0e3a5f; border-color: #3d8bfd; color: #9ccaff; }
"""

ZOOM_PER_PX = 0.008      # 1px 끌 때 확대 비율 (위로 100px ≈ 2.2배)


class DragPad(QLabel):
    """누른 채 끌면 dragged(dx, dy)를 보내는 칸"""

    dragged = pyqtSignal(int, int)
    double_clicked = pyqtSignal()
    released = pyqtSignal()

    def __init__(self, text, tip, cursor, parent=None):
        super().__init__(text, parent)
        self.setObjectName("DragPad")
        self.base_text = text
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(tip)
        self.setCursor(cursor)
        self.setFixedWidth(132)      # 끄는 동안 숫자가 바뀌어도 칸 크기는 그대로
        self._last = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last = QPoint(event.globalPos())
            self._set_dragging(True)

    def mouseMoveEvent(self, event):
        if self._last is None:
            return
        pos = QPoint(event.globalPos())
        dx, dy = pos.x() - self._last.x(), pos.y() - self._last.y()
        self._last = pos
        if dx or dy:
            self.dragged.emit(dx, dy)

    def mouseReleaseEvent(self, event):
        if self._last is not None:
            self._last = None
            self._set_dragging(False)
            self.released.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()

    def _set_dragging(self, on):
        self.setProperty("dragging", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class TapPad(QLabel):
    """끌지 않고 한 번 누르는 칸 (원본 W/L · 그리기)"""

    clicked = pyqtSignal()
    right_clicked = pyqtSignal()

    def __init__(self, text, tip, width=116, parent=None):
        super().__init__(text, parent)
        self.setObjectName("DragPad")
        self.base_text = text
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(width)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        elif event.button() == Qt.RightButton:
            self.right_clicked.emit()

    def set_on(self, on):
        self.setProperty("on", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class DragPadStrip(QWidget):
    """상태바에 넣는 [🔍 Zoom ↕] [◐ W/L ✥] [◑ 원본 W/L] [✏️ 그리기]

    target()    → 조절할 DicomViewport (없으면 None)
    draw_tools  → [(글자, 아이콘 이름, 도구 번호), …] 그리기 칸 메뉴에 넣을 도구들
    set_tool    → 도구를 바꾸는 함수, current_tool → 지금 도구 번호를 돌려주는 함수
    """

    def __init__(self, target, parent=None, draw_tools=(), set_tool=None, current_tool=None,
                 extras_menu=None):
        super().__init__(parent)
        self.target = target
        self._draw_tools = list(draw_tools)
        self._set_tool = set_tool
        self._current_tool = current_tool
        # 우클릭하면 띄울 메뉴를 만들어 주는 함수 (부모를 받아 QMenu를 돌려준다)
        self._extras_menu = extras_menu
        self.setObjectName("DragPadStrip")
        self.setStyleSheet(PAD_STYLE)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 6, 1)
        layout.setSpacing(4)
        self.zoom = DragPad("🔍 Zoom  ↕", "누른 채 위로 끌면 확대, 아래로 축소\n두 번 클릭: 화면 맞춤",
                            Qt.SizeVerCursor)
        self.wl = DragPad("◐ W/L  ✥", "누른 채 끌기: 좌우 = Width(대비), 위아래 = Level(밝기)\n"
                          "두 번 클릭: DICOM 기본값", Qt.SizeAllCursor)
        self.zoom.dragged.connect(self._zoom)
        self.wl.dragged.connect(self._window)
        self.zoom.double_clicked.connect(self._fit)
        self.wl.double_clicked.connect(self._reset_window)
        for pad in (self.zoom, self.wl):
            pad.released.connect(lambda p=pad: p.setText(p.base_text))
            layout.addWidget(pad)
        self.wl.released.connect(self._end_window)

        # Pan — 수정키 없이 끌어서 영상 옮기기 (⌥/가운데 버튼이 불편한 분들을 위해)
        self.pan = DragPad("✛ Pan  ✥", "누른 채 끌면 영상이 따라 움직입니다\n"
                           "두 번 클릭: 가운데로 (화면 맞춤)", Qt.SizeAllCursor)
        self.pan.dragged.connect(self._pan)
        self.pan.double_clicked.connect(self._fit)
        self.pan.released.connect(lambda: self.pan.setText(self.pan.base_text))
        layout.addWidget(self.pan)

        # 원본 W/L — 한 번 누르면 DICOM 태그 값으로
        self.reset_wl = TapPad("◑↺ 원본 W/L",
                               "DICOM 원본의 Window Center/Width로 되돌리기\n"
                               "W/L 칸을 두 번 클릭해도 됩니다")
        self.reset_wl.clicked.connect(self._reset_window)
        layout.addWidget(self.reset_wl)

        # 그리기 — 눌러서 측정 도구 고르기
        self.draw = TapPad("✏️ 그리기", "측정 도구 고르기 — 거리 · 각도 · 사각 · 타원 · 자유 등")
        self.draw.clicked.connect(self._open_draw_menu)
        self.draw.right_clicked.connect(self._open_draw_extras)
        self.draw.setToolTip(self.draw.toolTip() + "\n우클릭: 측정 숫자 · 랜드마크 표시 · 모두 지우기")
        layout.addWidget(self.draw)
        if not self._draw_tools:
            self.draw.hide()

    # ── 그리기 칸 ──
    def _open_draw_menu(self):
        """칸 바로 위에 도구 목록. 그리는 중이면 '그리기 끄기'도 같이"""
        if not self._draw_tools or self._set_tool is None:
            return
        now = self._current_tool() if self._current_tool else None
        menu = QMenu(self)
        for label, icon_name, tool_id in self._draw_tools:
            action = menu.addAction(icons.icon(icon_name), label)
            action.setCheckable(True)
            action.setChecked(tool_id == now)
            action.triggered.connect(lambda _=False, t=tool_id: self._choose_tool(t))
        if any(tool_id == now for _l, _i, tool_id in self._draw_tools):
            menu.addSeparator()
            off = menu.addAction(icons.icon("select"), "그리기 끄기 (Select로)")
            off.triggered.connect(lambda: self._choose_tool(None))
        pos = self.draw.mapToGlobal(self.draw.rect().topLeft())
        menu.exec_(pos - QPoint(0, menu.sizeHint().height() + 2))

    def _open_draw_extras(self):
        """✏️ 그리기 칸 우클릭 — 도구 막대 ✎ 버튼과 같은 '아노테이션 표시' 메뉴"""
        if self._extras_menu is None:
            return
        menu = self._extras_menu(self)
        if menu is None:
            return
        pos = self.draw.mapToGlobal(self.draw.rect().topLeft())
        menu.exec_(pos - QPoint(0, menu.sizeHint().height() + 2))

    def _choose_tool(self, tool_id):
        if self._set_tool is not None:
            self._set_tool(tool_id)
        self.refresh_tool()

    def refresh_tool(self):
        """지금 도구에 맞춰 그리기 칸의 글자 · 켜짐 상태를 고친다"""
        if not self._draw_tools:
            return
        now = self._current_tool() if self._current_tool else None
        for label, _icon_name, tool_id in self._draw_tools:
            if tool_id == now:
                self.draw.setText(f"✏️ {label}")
                self.draw.set_on(True)
                return
        self.draw.setText(self.draw.base_text)
        self.draw.set_on(False)

    def _viewport(self):
        vp = self.target()
        return vp if vp is not None and getattr(vp, "series", None) is not None else None

    def _zoom(self, _dx, dy):
        vp = self._viewport()
        if vp is None:
            return
        center = QPoint(vp.width() // 2, vp.height() // 2)   # 영상 가운데를 기준으로
        vp._zoom_by(max(0.2, 1.0 - dy * ZOOM_PER_PX), center)
        vp.update()
        self.zoom.setText(f"🔍 {vp._zoom * 100:.0f}%")

    def _window(self, dx, dy):
        vp = self._viewport()
        if vp is None:
            return
        vp._feel_drag("window", dx, dy, vp._drag_speed(dx, dy))   # 뷰포트와 같은 감도 커브 · 무게감
        center, width = vp.window_level
        self.wl.setText(f"◐ W {width:.0f} L {center:.0f}")

    def _end_window(self):
        vp = self._viewport()
        if vp is not None:
            vp._wl_drag_unit = None    # 다음 드래그는 그때의 Width로 감도를 다시 정함

    def _pan(self, dx, dy):
        vp = self._viewport()
        if vp is None:
            return
        vp._feel_drag("pan", dx, dy, vp._drag_speed(dx, dy))
        self.pan.setText("✛ 이동 중")

    def _fit(self):
        vp = self._viewport()
        if vp is not None:
            vp._fit_to_window()
            vp.update()

    def _reset_window(self):
        vp = self._viewport()
        if vp is not None:
            vp.reset_window()
