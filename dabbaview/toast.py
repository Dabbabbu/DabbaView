# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
주의 알림 (토스트) - 뷰어 오른쪽 위에 잠깐 떴다 사라지는 색 띠

상태바 글자는 눈에 잘 띄지 않아, "지금 그려도 화면에 안 보인다" 같은 놓치면 안 되는 말은
영상 바로 위에 색을 넣어 띄운다. 클릭을 가로채는 창이 아니라 뷰어 위에 얹힌 작은 위젯이다.
"""
from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

# 종류별 색 — (바탕, 글자, 테두리)
LEVELS = {
    "caution": ("#ffb020", "#1c1400", "#ffd27a"),   # 일부만 숨김 (주황)
    "warning": ("#e5484d", "#ffffff", "#ff9a9e"),   # 전부 숨김 (빨강)
}
MARGIN = 10


class Toast(QFrame):
    """host 위젯의 오른쪽 위에 뜨는 알림 한 줄 (+ 바로 고치는 버튼)"""

    def __init__(self, host, top_offset=None):
        super().__init__(host)
        self._host = host
        self._top_offset = top_offset or (lambda: MARGIN)
        self._on_action = None
        self.setObjectName("DabbaToast")
        self.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        row.setSpacing(10)
        self._text = QLabel()
        self._text.setTextFormat(Qt.RichText)
        self._text.setWordWrap(True)
        row.addWidget(self._text, 1)
        self._action = QPushButton()
        self._action.setCursor(Qt.PointingHandCursor)
        self._action.setFocusPolicy(Qt.NoFocus)
        self._action.clicked.connect(self._run_action)
        row.addWidget(self._action, 0, Qt.AlignVCenter)
        self._close = QPushButton("✕")
        self._close.setObjectName("ToastClose")
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setFocusPolicy(Qt.NoFocus)
        self._close.setFixedSize(24, 24)
        self._close.setToolTip("닫기")
        self._close.clicked.connect(self.hide)
        row.addWidget(self._close, 0, Qt.AlignTop)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.setFocusPolicy(Qt.NoFocus)
        host.installEventFilter(self)
        self.hide()

    def show_message(self, title, detail="", level="caution", action_text="", on_action=None,
                     msec=8000):
        """알림을 띄운다. 이미 떠 있으면 내용을 바꾸고 시간을 다시 센다"""
        bg, fg, border = LEVELS.get(level, LEVELS["caution"])
        self.setStyleSheet(
            f"QFrame#DabbaToast {{ background: {bg}; border: 2px solid {border}; border-radius: 8px; }}"
            f"QFrame#DabbaToast QLabel {{ color: {fg}; font-size: 13px; background: transparent; }}"
            f"QFrame#DabbaToast QPushButton {{ color: {fg}; background: rgba(0,0,0,0.18);"
            f" border: 1px solid {fg}; border-radius: 5px; padding: 4px 10px; font-weight: bold; }}"
            "QFrame#DabbaToast QPushButton:hover { background: rgba(0,0,0,0.32); }"
            "QFrame#DabbaToast QPushButton#ToastClose { background: transparent; border: none;"
            " padding: 0; font-size: 14px; }"
            "QFrame#DabbaToast QPushButton#ToastClose:hover { background: rgba(0,0,0,0.25);"
            " border-radius: 12px; }")
        body = f"<b>⚠ {title}</b>"
        if detail:
            body += f"<br>{detail}"
        self._text.setText(body)
        self._on_action = on_action
        self._action.setText(action_text)
        self._action.setVisible(bool(action_text and on_action))
        self.place()
        self.show()
        self.raise_()
        if msec and msec > 0:
            self._timer.start(int(msec))
        else:
            self._timer.stop()

    def place(self):
        """host 오른쪽 위 (폭은 host의 절반을 넘지 않게)"""
        host = self._host
        width = max(280, min(460, host.width() - 2 * MARGIN))
        self.setFixedWidth(width)
        self.adjustSize()
        try:
            top = int(self._top_offset())
        except Exception:  # noqa: BLE001 - 자리를 못 구하면 맨 위에
            top = MARGIN
        self.move(max(MARGIN, host.width() - width - MARGIN), max(0, top))

    def _run_action(self):
        callback, self._on_action = self._on_action, None
        self.hide()
        if callback is not None:
            callback()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize and self.isVisible():
            self.place()
        return False
