# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
영상에 붙는 막대 (INFINITT PACS 방식)

- 오른쪽 세로 막대 = 슬라이스 (SliceScrollBar)
- 아래쪽 가로 막대 = 위상 (PhaseScrollBar) — 위상이 여럿인 cine 영상에서만
- 지금 위치를 보여 주고, 끌어서 빠르게 넘김 (칸마다 하나씩, 칸 크기에 맞게 붙음)
- 빈 곳을 누르면 그 위치로 바로 이동, 휠은 뷰포트와 같게 한 장씩
- 하나뿐이면 숨김. View ▸ 슬라이스 막대에서 끄고 켬
"""
from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget

WIDTH = 13               # 막대 너비 (px)
MIN_HEIGHT = 60          # 이보다 낮은 칸에서는 숨김 (아주 작은 칸)
THUMB_MIN = 14           # 손잡이 최소 길이
TRACK_COLOR = QColor(255, 255, 255, 28)
TRACK_BORDER = QColor(0, 0, 0, 120)
THUMB_COLOR = QColor(235, 235, 235, 190)
THUMB_ACTIVE = QColor(255, 212, 0, 230)     # 끌거나 마우스를 올렸을 때 (활성 칸 테두리와 같은 노랑)
TEXT_COLOR = QColor(255, 255, 255, 220)


class SliceScrollBar(QWidget):
    """뷰포트 오른쪽에 붙는 세로 슬라이스 막대"""

    slice_requested = pyqtSignal(int)      # 사용자가 고른 슬라이스 번호 (0부터)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.ArrowCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        self._total = 0
        self._index = 0
        self._dragging = False
        self._hover = False
        self.setVisible(False)

    # ─── 상태 ───
    def set_range(self, index, total):
        if (index, total) == (self._index, self._total):
            return
        self._index, self._total = index, max(0, total)
        self._apply_visible()
        self.update()

    def _apply_visible(self):
        self.setVisible(self._total > 1 and self.height() >= MIN_HEIGHT
                        and not self.property("disabled"))

    def set_enabled_by_user(self, on):
        """View 메뉴에서 끄고 켜기"""
        self.setProperty("disabled", not on)
        self._apply_visible()

    # ─── 위치 ───
    def place(self, parent_width, parent_height, top=0, bottom=0):
        """부모(뷰포트) 오른쪽 끝에 붙임. top·bottom은 위아래로 비울 여백"""
        height = max(0, parent_height - top - bottom)
        self.setGeometry(parent_width - WIDTH, top, WIDTH, height)
        self._apply_visible()

    def _thumb(self):
        """(위, 길이) — 슬라이스 한 장이 차지하는 길이에 맞춤 (최소 THUMB_MIN)"""
        track = self.height()
        total = max(1, self._total)
        length = max(THUMB_MIN, track / total)
        if total <= 1:
            return 0.0, track
        free = track - length
        return free * (self._index / (total - 1)), length

    def _index_at(self, y):
        """막대에서 누른 높이 → 슬라이스 번호 (손잡이 가운데가 손끝에 오도록 보정)"""
        _top, length = self._thumb()
        free = max(1.0, self.height() - length)
        ratio = (y - length / 2) / free
        return int(round(max(0.0, min(1.0, ratio)) * max(0, self._total - 1)))

    # ─── 그리기 ───
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(TRACK_BORDER, 1))
        painter.setBrush(TRACK_COLOR)
        painter.drawRoundedRect(rect, 3, 3)

        top, length = self._thumb()
        thumb = QRectF(2.5, top + 1, self.width() - 5, max(6.0, length - 2))
        painter.setPen(Qt.NoPen)
        painter.setBrush(THUMB_ACTIVE if (self._dragging or self._hover) else THUMB_COLOR)
        painter.drawRoundedRect(thumb, 2, 2)

        if self._dragging or self._hover:      # 지금 몇 번째인지 (막대 왼쪽에)
            text = f"{self._index + 1}/{self._total}"
            painter.setFont(self.font())
            fm = painter.fontMetrics()
            width = fm.horizontalAdvance(text) + 8
            y = max(0.0, min(self.height() - fm.height() - 2, top + length / 2 - fm.height() / 2))
            box = QRectF(-width - 3, y, width, fm.height() + 2)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(TEXT_COLOR)
            painter.drawText(box, Qt.AlignCenter, text)

    # ─── 마우스 ───
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._dragging = True
        self.slice_requested.emit(self._index_at(event.pos().y()))
        self.update()

    def mouseMoveEvent(self, event):
        self._hover = True
        if self._dragging:
            self.slice_requested.emit(self._index_at(event.pos().y()))
        self.update()

    def mouseReleaseEvent(self, _event):
        self._dragging = False
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        """막대 위에서 굴려도 영상이 넘어가게 (뷰포트로 넘김)"""
        parent = self.parent()
        if parent is not None:
            parent.wheelEvent(event)
        else:
            event.ignore()


class PhaseScrollBar(SliceScrollBar):
    """뷰포트 아래쪽에 붙는 가로 위상 막대

    세로 막대와 같은 모양 · 조작이고, 방향만 가로다 (왼쪽 = 첫 위상).
    cine처럼 한 자리에서 시간에 따라 여러 장을 찍은 영상에서만 나온다.
    """

    phase_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.slice_requested.connect(self.phase_requested)   # 이름만 바꿔 다시 내보냄

    def _apply_visible(self):
        self.setVisible(self._total > 1 and self.width() >= MIN_HEIGHT
                        and not self.property("disabled"))

    def place(self, parent_width, parent_height, left=0, right=0):
        """부모(뷰포트) 아래쪽 끝에 붙임. left·right는 좌우로 비울 여백"""
        width = max(0, parent_width - left - right)
        self.setGeometry(left, parent_height - WIDTH, width, WIDTH)
        self._apply_visible()

    def _thumb(self):
        """(왼쪽, 길이) — 위상 하나가 차지하는 길이에 맞춤"""
        track = self.width()
        total = max(1, self._total)
        length = max(THUMB_MIN, track / total)
        if total <= 1:
            return 0.0, track
        return (track - length) * (self._index / (total - 1)), length

    def _index_at(self, x):
        _left, length = self._thumb()
        free = max(1.0, self.width() - length)
        ratio = (x - length / 2) / free
        return int(round(max(0.0, min(1.0, ratio)) * max(0, self._total - 1)))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(TRACK_BORDER, 1))
        painter.setBrush(TRACK_COLOR)
        painter.drawRoundedRect(rect, 3, 3)

        left, length = self._thumb()
        thumb = QRectF(left + 1, 2.5, max(6.0, length - 2), self.height() - 5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(THUMB_ACTIVE if (self._dragging or self._hover) else THUMB_COLOR)
        painter.drawRoundedRect(thumb, 2, 2)

        if self._dragging or self._hover:      # 몇 번째 위상인지 (막대 위쪽에)
            text = f"Phase {self._index + 1}/{self._total}"
            fm = painter.fontMetrics()
            width = fm.horizontalAdvance(text) + 8
            x = max(0.0, min(self.width() - width, left + length / 2 - width / 2))
            box = QRectF(x, -fm.height() - 4, width, fm.height() + 2)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(TEXT_COLOR)
            painter.drawText(box, Qt.AlignCenter, text)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._dragging = True
        self.slice_requested.emit(self._index_at(event.pos().x()))
        self.update()

    def mouseMoveEvent(self, event):
        self._hover = True
        if self._dragging:
            self.slice_requested.emit(self._index_at(event.pos().x()))
        self.update()
