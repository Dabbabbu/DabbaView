# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Zoom · W/L 조절 칸 - 맨 아래 상태바에 있어 영상을 가리지 않고, 마우스 끌기로 확대 · W/L 조절

- 🔍 Zoom: 누른 채 위로 끌면 확대, 아래로 축소 (두 번 클릭: 화면 맞춤)
- ◐ W/L: 좌우 = Width, 위아래 = Level — 영상 위 우클릭 드래그와 같음 (두 번 클릭: DICOM 기본값)
- 대상은 지금 보는 영상 (Multi View에서는 선택한 칸)
"""
from PyQt5.QtCore import QPoint, Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

PAD_STYLE = """
QLabel#DragPad { background: #262a30; color: #cfd6df; border: 1px solid #3a414a; border-radius: 4px;
                 font-size: 11px; padding: 1px 8px; }
QLabel#DragPad:hover { background: #2f3945; border-color: #3d8bfd; color: #fff; }
QLabel#DragPad[dragging="true"] { background: #094771; border-color: #3d8bfd; color: #fff; }
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


class DragPadStrip(QWidget):
    """상태바에 넣는 [🔍 Zoom ↕] [◐ W/L ✥]. target() → 조절할 DicomViewport (없으면 None)"""

    def __init__(self, target, parent=None):
        super().__init__(parent)
        self.target = target
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
        vp._adjust_window(dx, dy)
        vp.update()
        center, width = vp.window_level
        self.wl.setText(f"◐ W {width:.0f} L {center:.0f}")

    def _fit(self):
        vp = self._viewport()
        if vp is not None:
            vp._fit_to_window()
            vp.update()

    def _reset_window(self):
        vp = self._viewport()
        if vp is not None:
            vp.reset_window()
