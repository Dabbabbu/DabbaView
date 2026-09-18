# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
한글 입력 상태에서도 한 글자 단축키(T, R, P, V ...)가 동작하도록 하는 보정

한글(두벌식 등) 입력 소스가 켜져 있으면 T 키가 'ㅅ'으로 들어와
QKeySequence("T")와 일치하지 않음 → 단축키 무시.
처리되지 않은 키 입력의 물리 키 코드(nativeVirtualKey)를 영문 키로 바꿔
같은 창의 QAction 단축키를 찾아 실행한다.
텍스트 입력 위젯에서는 한글 입력을 그대로 둔다.
"""
import sys

from PyQt5.QtCore import QObject, QEvent, Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QAction, QApplication, QAbstractSpinBox, QComboBox,
                              QLineEdit, QPlainTextEdit, QTextEdit)

# macOS 물리 키 코드 (kVK_ANSI_*) → Qt 키
_MAC_KEYCODES = {
    0: Qt.Key_A, 1: Qt.Key_S, 2: Qt.Key_D, 3: Qt.Key_F, 4: Qt.Key_H, 5: Qt.Key_G,
    6: Qt.Key_Z, 7: Qt.Key_X, 8: Qt.Key_C, 9: Qt.Key_V, 11: Qt.Key_B, 12: Qt.Key_Q,
    13: Qt.Key_W, 14: Qt.Key_E, 15: Qt.Key_R, 16: Qt.Key_Y, 17: Qt.Key_T,
    31: Qt.Key_O, 32: Qt.Key_U, 34: Qt.Key_I, 35: Qt.Key_P, 37: Qt.Key_L,
    38: Qt.Key_J, 40: Qt.Key_K, 45: Qt.Key_N, 46: Qt.Key_M,
}

_MODIFIER_MASK = (Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier
                  | Qt.MetaModifier)

_TEXT_INPUT_WIDGETS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)


def latin_key(event):
    """한글 등 비영문 문자로 들어온 키 입력의 영문 Qt 키 (해당 없으면 None)"""
    key = event.key()
    if 0 < key < 0x80 or key >= Qt.Key_Escape:
        return None  # 이미 영문/숫자이거나 기능 키 → Qt가 직접 처리
    native = event.nativeVirtualKey()
    if sys.platform == "darwin":
        return _MAC_KEYCODES.get(native)
    if Qt.Key_A <= native <= Qt.Key_Z:  # Windows: 가상 키 코드 = 대문자 ASCII
        return native
    return None


def _is_text_input(widget):
    if widget is None:
        return False
    if isinstance(widget, _TEXT_INPUT_WIDGETS):
        return not getattr(widget, "isReadOnly", lambda: False)()
    if isinstance(widget, QComboBox):
        return widget.isEditable()
    return False


def _shortcut_active(action):
    if not action.isEnabled():
        return False
    widgets = action.associatedWidgets()
    # 메뉴바 메뉴 안의 액션은 메뉴가 닫혀 있어도 단축키가 유효
    return any(w.isVisible() or w.parentWidget() is not None for w in widgets)


class LatinShortcutFilter(QObject):
    """QApplication 이벤트 필터 - 처리되지 않은 한글 키 입력을 단축키로 변환"""

    def eventFilter(self, obj, event):
        if event.type() != QEvent.KeyPress or event.isAutoRepeat():
            return False
        key = latin_key(event)
        if key is None:
            return False
        focus = QApplication.focusWidget()
        if _is_text_input(focus):
            return False
        window = (focus or obj).window() if hasattr(focus or obj, "window") else None
        if window is None:
            return False
        modifiers = int(event.modifiers() & _MODIFIER_MASK)
        sequence = QKeySequence(key | modifiers)
        for action in window.findChildren(QAction):
            if sequence in action.shortcuts() and _shortcut_active(action):
                action.trigger()
                return True
        return False


def install(app):
    app._latin_shortcut_filter = LatinShortcutFilter(app)
    app.installEventFilter(app._latin_shortcut_filter)
