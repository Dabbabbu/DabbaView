# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
운영체제 기본 규칙에 맞춘 키 - 이름 바꾸기

- macOS: Return(Enter)   (Finder와 같음)
- Windows · Linux: F2     (탐색기와 같음)
"""
import sys

from PyQt5.QtCore import Qt

IS_MAC = sys.platform == "darwin"

RENAME_KEYS = (Qt.Key_Return, Qt.Key_Enter) if IS_MAC else (Qt.Key_F2,)
RENAME_LABEL = "Return" if IS_MAC else "F2"
RENAME_LABEL_SHIFT = ("⇧Return" if IS_MAC else "Shift+F2")


def is_rename_key(event):
    """이름 바꾸기 키인지 (macOS Return · 그 밖의 OS F2)

    macOS에서 ⌘Return은 '열기'이므로 이름 바꾸기로 보지 않는다.
    """
    if event.key() not in RENAME_KEYS:
        return False
    if IS_MAC and event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
        return False
    return True


def is_open_key(event):
    """목록에서 '열기' 키. macOS는 Return을 이름 바꾸기에 쓰므로 ⌘Return"""
    if event.key() not in (Qt.Key_Return, Qt.Key_Enter):
        return False
    if not IS_MAC:
        return True
    return bool(event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))
