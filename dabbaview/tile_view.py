# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Tile 모드: 여러 슬라이스를 격자로 한눈에 (2x2 ~ 6x6)

- 휠: 한 줄씩 이동, Shift+휠: 한 페이지씩
- 우클릭 드래그: W/L (모든 칸 공통)
- 더블클릭: 해당 슬라이스를 Stack 모드로 열기
- Key Image 모아보기: 여러 시리즈의 (series, index) 목록도 표시 가능
"""
import time

import numpy as np
from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui import QImage, QPainter, QColor, QFont, QPen

from . import mouse_feel
from .annotations import image_key


class TileView(QWidget):

    tile_activated = pyqtSignal(object, int)  # series, slice index
    window_changed = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setMouseTracking(True)
        self._items = []          # [(series, index)]
        self._grid = 4
        self._first = 0           # 첫 칸에 보이는 항목 번호
        self._window = (400.0, 2000.0)
        self._store = None        # AnnotationStore (Key Image 별표)
        self._title = ""
        self._drag_pos = None
        self._selected = None     # 선택한 항목 번호
        self._cache = {}          # (series_uid, index, wc, ww) → QImage

    # 설정
    def set_series(self, series, window=None, start_index=0):
        self._title = series.description if series else ""
        self._items = [(series, i) for i in range(series.num_slices)] if series else []
        if window:
            self._window = window
        elif series:
            self._window = series.get_default_window()
        self._first = (start_index // self._grid) * self._grid
        self._selected = start_index if self._items else None
        self._clamp_first()
        self.update()

    def set_items(self, items, title="", window=None):
        """임의의 (series, index) 목록 표시 (예: Key Image 모아보기)"""
        self._items = list(items)
        self._title = title
        if window:
            self._window = window
        self._first = 0
        self._selected = None
        self.update()

    def set_grid(self, n):
        self._grid = max(1, min(8, int(n)))
        # 선택한 칸이 있는 줄이 맨 위에 오도록 다시 맞춤
        anchor = self._selected if self._selected is not None else self._first
        self._first = (anchor // self._grid) * self._grid
        self._clamp_first()
        self.update()

    def set_window(self, center, width):
        self._window = (center, max(1.0, width))
        self.update()

    def set_annotation_store(self, store):
        self._store = store
        store.changed.connect(self.update)

    @property
    def grid(self):
        return self._grid

    @property
    def items(self):
        return list(self._items)

    @property
    def selected_index(self):
        return self._selected

    @property
    def first_visible(self):
        return self._first

    def _clamp_first(self):
        per_page = self._grid * self._grid
        last_row_start = max(0, ((len(self._items) - 1) // self._grid) * self._grid
                             - (per_page - self._grid))
        self._first = max(0, min(self._first, last_row_start))

    # 렌더링
    def _tile_rect(self, slot):
        header = 22
        cw = self.width() / self._grid
        ch = (self.height() - header) / self._grid
        r, c = divmod(slot, self._grid)
        return QRectF(c * cw, header + r * ch, cw, ch)

    def _image_for(self, series, index, is_rgb_ok=True):
        wc, ww = self._window
        key = (series.series_uid, index, round(wc, 2), round(ww, 2))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        arr = series.get_pixel_array(index)
        if arr is None:
            return None
        if arr.ndim == 3 and arr.shape[2] == 3:
            img = np.ascontiguousarray(np.clip(arr, 0, 255).astype(np.uint8))
            h, w, _ = img.shape
            qimg = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        else:
            if arr.ndim == 3:
                arr = arr[..., 0]
            img = np.clip((arr - (wc - ww / 2)) / ww * 255, 0, 255).astype(np.uint8)
            img = np.ascontiguousarray(img)
            h, w = img.shape
            qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8).copy()
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[key] = qimg
        return qimg

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        font = QFont()
        font.setPointSize(9)
        p.setFont(font)

        total = len(self._items)
        per_page = self._grid * self._grid
        p.setPen(QColor(200, 200, 200))
        if total:
            last = min(total, self._first + per_page)
            p.drawText(QPointF(8, 15), f"{self._title}   {self._first + 1}–{last} / {total}"
                                       f"   (Tile {self._grid}x{self._grid})")
        else:
            p.drawText(self.rect(), Qt.AlignCenter, "표시할 영상이 없습니다")

        for slot in range(per_page):
            n = self._first + slot
            if n >= total:
                break
            series, index = self._items[n]
            rect = self._tile_rect(slot).adjusted(1, 1, -1, -1)
            img = self._image_for(series, index)
            if img is not None:
                scale = min(rect.width() / img.width(), rect.height() / img.height())
                w, h = img.width() * scale, img.height() * scale
                target = QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h)
                p.drawImage(target, img)
            p.setPen(QColor(230, 230, 230))
            p.drawText(QPointF(rect.left() + 4, rect.top() + 13), f"Im {index + 1}")
            if len({s.series_uid for s, _ in self._items}) > 1:
                p.drawText(QPointF(rect.left() + 4, rect.bottom() - 4),
                           (series.description or "")[:24])
            if self._store is not None and self._store.is_key_image(image_key(series, index)):
                p.setPen(QColor(255, 215, 0))
                p.drawText(QPointF(rect.right() - 16, rect.top() + 14), "★")
            border = QColor(255, 212, 0) if n == self._selected else QColor(60, 60, 60)
            p.setPen(QPen(border, 2 if n == self._selected else 1))
            p.drawRect(rect)
        p.end()

    # 입력
    def _item_at(self, pos):
        for slot in range(self._grid * self._grid):
            if self._tile_rect(slot).contains(QPointF(pos)):
                n = self._first + slot
                return n if n < len(self._items) else None
        return None

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        step = self._grid * (self._grid if event.modifiers() & Qt.ShiftModifier else 1)
        self._first += -step if delta > 0 else step
        self._clamp_first()
        self.update()

    def mousePressEvent(self, event):
        self._drag_pos = event.pos()
        self._drag_ms = time.monotonic() * 1000.0
        self._drag_unit = mouse_feel.wl_unit(self._window[1])   # 드래그 동안 고정 (2D 뷰와 같은 감도)
        if event.button() == Qt.LeftButton:
            self._selected = self._item_at(event.pos())
            self.update()

    def mouseMoveEvent(self, event):
        if self._drag_pos is None or not (event.buttons() & Qt.RightButton):
            return
        dx = event.pos().x() - self._drag_pos.x()
        dy = event.pos().y() - self._drag_pos.y()
        self._drag_pos = event.pos()
        now = time.monotonic() * 1000.0
        speed = (dx * dx + dy * dy) ** 0.5 / max(4.0, now - getattr(self, "_drag_ms", now - 16))
        self._drag_ms = now
        # 천천히 = 정밀, 빠르게 = 가속 (Settings ▸ Mouse ▸ 조작감의 W/L 감도 · 가속은 기본값)
        step = getattr(self, "_drag_unit", 4.0) * mouse_feel.speed_gain(speed)
        wc, ww = self._window
        self.set_window(wc + dy * step, ww + dx * step)
        self.window_changed.emit(*self._window)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        n = self._item_at(event.pos())
        if n is not None:
            series, index = self._items[n]
            self.tile_activated.emit(series, index)
