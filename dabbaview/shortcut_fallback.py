# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
단축키가 먹지 않는 두 가지 경우를 메꾼다

1. 한글 입력 상태에서 한 글자 단축키(T, R, P, V ...)

한글(두벌식 등) 입력 소스가 켜져 있으면 T 키가 'ㅅ'으로 들어와
QKeySequence("T")와 일치하지 않음 → 단축키 무시.
   처리되지 않은 키 입력의 물리 키 코드(nativeVirtualKey)를 영문 키로 바꿔
   같은 창의 QAction 단축키를 찾아 실행한다.

2. macOS에서 메뉴에 'Ctrl+R'로 적힌 단축키를 물리 ⌃(Control) 키로 누른 경우
   Qt는 macOS에서 Ctrl = ⌘(Command)로 보므로 ⌃R은 아무 데도 닿지 않는다.
   DabbaView는 마우스 조작에서도 ⌘와 ⌃를 같게 보므로(CTRL_MODIFIERS), 단축키도 같게 맞춘다.
   → ⌃+키를 ⌘+키로 바꿔 한 번 더 찾아 본다.

텍스트 입력 위젯에서는 둘 다 건드리지 않는다
(한글 입력과 ⌃A · ⌃E · ⌃K 같은 macOS 글자 편집 키를 그대로 두기 위해).
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

# 수정키 자체는 단축키로 보지 않는다
_MODIFIER_KEYS = (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
                  Qt.Key_CapsLock, Qt.Key_AltGr)


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
        focus = QApplication.focusWidget()
        if _is_text_input(focus):
            return False
        modifiers = int(event.modifiers() & _MODIFIER_MASK)
        # macOS에서 물리 ⌃만 누른 경우 → 메뉴에 적힌 'Ctrl'(=⌘)로 바꿔 찾는다
        mac_ctrl = bool(sys.platform == "darwin"
                        and modifiers & int(Qt.MetaModifier)
                        and not modifiers & int(Qt.ControlModifier))
        key = latin_key(event)
        if key is None:
            if not mac_ctrl:
                return False
            key = event.key()
            if key <= 0 or key in _MODIFIER_KEYS:
                return False
        if mac_ctrl:
            modifiers = (modifiers & ~int(Qt.MetaModifier)) | int(Qt.ControlModifier)
        window = (focus or obj).window() if hasattr(focus or obj, "window") else None
        if window is None:
            return False
        sequence = QKeySequence(key | modifiers)
        for action in window.findChildren(QAction):
            if sequence in action.shortcuts() and _shortcut_active(action):
                action.trigger()
                return True
        return False


def install(app):
    app._latin_shortcut_filter = LatinShortcutFilter(app)
    app.installEventFilter(app._latin_shortcut_filter)
