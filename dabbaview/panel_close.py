# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
탭 · 패널 닫기 버튼 (X) - 마우스를 올린 탭(또는 도크 제목)에만 X가 보이고, 누르면 부드럽게 닫힘

- HoverCloseTabs: QTabBar에 붙임. 올린 탭의 X만 보임
- HoverTitleBar: QDockWidget 제목 줄. 올리면 X(와 떼어내기) 보임
- fade_out: 닫기 전에 잠깐 흐려짐
"""
from PyQt5.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer
from PyQt5.QtWidgets import (QDockWidget, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QStyle,
                             QTabBar, QToolButton, QWidget)

FADE_MS = 160
_LIVE = set()   # 파이썬 쪽 객체가 지워지면 이벤트 필터 · 슬롯이 사라짐 → 탭 줄이 있는 동안 붙잡아 둠


def fade_out(widget, done):
    """widget을 잠깐 흐리게 한 뒤 done() - 이미 숨겨졌거나 효과를 못 쓰면 바로"""
    if widget is None or not widget.isVisible():
        done()
        return
    try:
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(FADE_MS)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InQuad)

        def finish():
            widget.setGraphicsEffect(None)   # 다시 열 때는 온전히 보이게
            done()
        anim.finished.connect(finish)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
    except RuntimeError:
        done()


class HoverCloseTabs(QObject):
    """QTabBar: 마우스를 올린 탭에만 X. on_close(index), closable(index) → 닫을 수 있는 탭만"""

    def __init__(self, bar, on_close, closable=None):
        super().__init__(bar)
        self.bar = bar
        self.on_close = on_close
        self.closable = closable or (lambda i: True)
        self._hover = -1
        self._pending = False
        bar.setTabsClosable(True)
        bar.setMouseTracking(True)
        bar.tabCloseRequested.connect(self._close)
        bar.installEventFilter(self)
        _LIVE.add(self)
        bar.destroyed.connect(lambda *_: _LIVE.discard(self))
        self.refresh()

    def _buttons(self, i):
        return [b for b in (self.bar.tabButton(i, QTabBar.RightSide), self.bar.tabButton(i, QTabBar.LeftSide))
                if b is not None]

    def _own_button(self, i):
        """기본 닫기 아이콘은 어두운 테마에서 거의 안 보임 → 밝은 ✕ 버튼으로 바꿈 (새 탭도)"""
        for side in (QTabBar.RightSide, QTabBar.LeftSide):
            b = self.bar.tabButton(i, side)
            if b is None or b.property("dv_x"):
                continue
            x = QToolButton(self.bar)
            x.setText("✕")
            x.setProperty("dv_x", True)
            x.setToolTip("닫기")
            x.setAutoRaise(True)
            x.setFixedSize(16, 16)
            x.setStyleSheet("QToolButton { color: #cfd6df; border: none; font-size: 11px; padding: 0; } "
                            "QToolButton:hover { color: #fff; background: #c0392b; border-radius: 3px; }")
            x.clicked.connect(lambda _c=False, btn=x: self._clicked(btn))
            self.bar.setTabButton(i, side, x)

    def _clicked(self, btn):
        for i in range(self.bar.count()):
            if btn in self._buttons(i):
                self._close(i)
                return

    def refresh(self):
        for i in range(self.bar.count()):
            self._own_button(i)
            show = i == self._hover and self.closable(i)
            for b in self._buttons(i):
                if b.isVisible() != show:
                    b.setVisible(show)

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.MouseMove, QEvent.HoverMove, QEvent.Enter):
            pos = event.pos() if hasattr(event, "pos") else obj.mapFromGlobal(obj.cursor().pos())
            hover = self.bar.tabAt(pos)
            if hover != self._hover:
                self._hover = hover
                self.refresh()
        elif t == QEvent.Leave:
            self._hover = -1
            self.refresh()
        elif t in (QEvent.Show, QEvent.LayoutRequest, QEvent.Resize, QEvent.Paint, QEvent.ChildAdded,
                   QEvent.StyleChange, QEvent.Polish):
            # QTabBar가 배치하면서 X를 다시 보이게 함 (필터보다 나중) → 이벤트가 끝난 뒤 다시 숨김
            if not self._pending:
                self._pending = True
                QTimer.singleShot(0, self._deferred)
        return False

    def _deferred(self):
        self._pending = False
        try:
            self.refresh()
        except RuntimeError:   # 탭 줄이 이미 지워짐
            pass

    def _close(self, index):
        if self.closable(index):
            self._hover = -1
            self.on_close(index)
            self.refresh()


class HoverTitleBar(QWidget):
    """도크 제목 줄: 제목 + (마우스를 올리면) 떼어내기 · X"""

    def __init__(self, dock, on_close):
        super().__init__(dock)
        self.dock = dock
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 4, 3)
        layout.setSpacing(2)
        self.title = QLabel(dock.windowTitle())
        self.title.setStyleSheet("color: #cfd6df; font-weight: bold;")
        layout.addWidget(self.title, 1)
        self.float_btn = self._button(QStyle.SP_TitleBarNormalButton, "떼어내기 / 붙이기",
                                      lambda: dock.setFloating(not dock.isFloating()))
        self.close_btn = self._button(QStyle.SP_TitleBarCloseButton, "닫기", on_close)
        self.close_btn.setText("✕")
        self.close_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        layout.addWidget(self.float_btn)
        layout.addWidget(self.close_btn)
        dock.windowTitleChanged.connect(self.title.setText)
        self._set_hover(False)

    def _button(self, icon, tip, slot):
        b = QToolButton(self)
        b.setIcon(self.style().standardIcon(icon))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setFixedSize(20, 20)
        b.setStyleSheet("QToolButton { color: #bbb; border: none; } "
                        "QToolButton:hover { color: #fff; background: #c0392b; border-radius: 3px; }")
        b.clicked.connect(lambda _checked=False: slot())   # clicked(bool)의 값을 넘기지 않음
        return b

    def _set_hover(self, on):
        movable = bool(self.dock.features() & QDockWidget.DockWidgetFloatable)
        self.float_btn.setVisible(on and movable)
        self.close_btn.setVisible(on)

    def enterEvent(self, event):
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hover(False)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):   # 제목 두 번 클릭: 떼어내기 / 붙이기 (기본 제목 줄과 같게)
        if self.dock.features() & QDockWidget.DockWidgetFloatable:
            self.dock.setFloating(not self.dock.isFloating())
        super().mouseDoubleClickEvent(event)


def reopen_hint(name, action):
    """'다시 열기: View ▸ 패널 ▸ 이름 (단축키)'"""
    key = action.shortcut().toString() if action is not None and not action.shortcut().isEmpty() else ""
    return f"{name} 닫힘 — 다시 열기: View ▸ 패널 ▸ {name}" + (f" ({key})" if key else "")
