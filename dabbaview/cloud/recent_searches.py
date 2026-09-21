# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
클라우드 창 검색칸의 최근 검색어 - 검색칸을 누르면 목록이 뜨고, 눌러서 다시 검색 · ✕로 지우기

- 최근 것부터 MAX_ITEMS개까지 설정에 저장 (앱을 다시 켜도 남음, 같은 말은 맨 위로)
- 목록이 떠 있을 때 글자를 치면 목록을 닫고 검색칸에 그대로 입력
"""
from PyQt5.QtCore import QEvent, QObject, QPoint, QSettings, Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton,
                             QVBoxLayout, QWidget)

KEY = "cloud/recent_searches"
MAX_ITEMS = 10


class RecentSearches:
    """저장소: QSettings 목록"""

    def __init__(self, settings=None):
        self._qs = settings or QSettings("DabbaView", "DabbaView")

    def items(self):
        value = self._qs.value(KEY, [])
        if isinstance(value, str):
            value = [value] if value else []
        return [str(v) for v in (value or []) if str(v).strip()]

    def _save(self, items):
        self._qs.setValue(KEY, list(items)[:MAX_ITEMS])

    def add(self, text):
        text = text.strip()
        if not text:
            return
        rest = [t for t in self.items() if t.lower() != text.lower()]
        self._save([text] + rest)

    def remove(self, text):
        self._save([t for t in self.items() if t != text])

    def clear(self):
        self._save([])


ROW_STYLE = """
QPushButton#RecentText { text-align: left; border: none; padding: 5px 8px; color: #dbe2ea;
                         background: transparent; }
QPushButton#RecentText:hover { background: #094771; color: #fff; }
QToolButton#RecentX { border: none; color: #9aa7b5; padding: 0 6px; font-size: 12px; }
QToolButton#RecentX:hover { color: #fff; background: #c0392b; border-radius: 3px; }
QPushButton#RecentClear { border: none; color: #8fa3b8; padding: 4px 8px; text-align: right;
                          background: transparent; font-size: 11px; }
QPushButton#RecentClear:hover { color: #fff; }
"""


class RecentSearchPopup(QFrame):
    """검색칸 아래에 뜨는 최근 검색어 목록"""

    chosen = pyqtSignal(str)

    def __init__(self, line_edit, store):
        super().__init__(line_edit, Qt.Popup)
        self.edit = line_edit
        self.store = store
        self.setObjectName("RecentPopup")
        self.setStyleSheet("QFrame#RecentPopup { background: #2b2b2b; border: 1px solid #3d8bfd; "
                           "border-radius: 4px; }" + ROW_STYLE)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(0)

    def _rebuild(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        items = self.store.items()
        title = QLabel("최근 검색어")
        title.setStyleSheet("color:#8fa3b8; font-size:11px; padding:3px 8px;")
        self._layout.addWidget(title)
        for text in items:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(0)
            button = QPushButton(f"🕘  {text}")
            button.setObjectName("RecentText")
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(f"'{text}'로 다시 검색")
            button.clicked.connect(lambda _c=False, t=text: self._choose(t))
            remove = QToolButton()
            remove.setObjectName("RecentX")
            remove.setText("✕")
            remove.setFocusPolicy(Qt.NoFocus)
            remove.setToolTip("이 검색어 지우기")
            remove.clicked.connect(lambda _c=False, t=text: self._remove(t))
            h.addWidget(button, 1)
            h.addWidget(remove)
            self._layout.addWidget(row)
        clear = QPushButton("모두 지우기")
        clear.setObjectName("RecentClear")
        clear.setFocusPolicy(Qt.NoFocus)
        clear.clicked.connect(self._clear)
        self._layout.addWidget(clear)
        return bool(items)

    def open(self):
        if not self._rebuild():
            return False
        self.setFixedWidth(max(self.edit.width(), 240))
        self.adjustSize()
        self.move(self.edit.mapToGlobal(QPoint(0, self.edit.height() + 2)))
        self.show()
        return True

    def _choose(self, text):
        self.hide()
        self.chosen.emit(text)

    def _remove(self, text):
        self.store.remove(text)
        if self._rebuild():
            self.adjustSize()
        else:
            self.hide()

    def _clear(self):
        self.store.clear()
        self.hide()

    def keyPressEvent(self, event):
        """글자를 치면 목록을 닫고 검색칸으로 넘김 (Esc는 닫기만)"""
        self.hide()
        if event.key() != Qt.Key_Escape:
            self.edit.setFocus()
            QApplication.sendEvent(self.edit, event)


class _EditWatcher(QObject):
    """검색칸을 누르거나 ↓를 누르면 최근 검색어 목록을 띄움"""

    def __init__(self, popup):
        super().__init__(popup.edit)
        self.popup = popup

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if not self.popup.isVisible():
                self.popup.open()
        elif t == QEvent.KeyPress and event.key() == Qt.Key_Down:
            return self.popup.open()
        return False


def attach(line_edit, on_choose, settings=None):
    """line_edit에 최근 검색어 목록을 붙임 → (store, popup). on_choose(text)는 고른 검색어로 검색"""
    store = RecentSearches(settings)
    popup = RecentSearchPopup(line_edit, store)

    def choose(text):
        line_edit.setText(text)
        on_choose()
    popup.chosen.connect(choose)
    watcher = _EditWatcher(popup)
    line_edit.installEventFilter(watcher)
    popup._watcher = watcher
    return store, popup
