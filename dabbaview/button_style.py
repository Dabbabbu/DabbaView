# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
버튼 눌림 효과 - 앱 전체 대화상자 버튼에 '눌리는 느낌'을 준다

기본 Qt 버튼은 눌러도 색이 거의 변하지 않아, 눌렸는지 알기 어려웠다
(새 버전 알림 · 설정 · 내보내기 등 대화상자 버튼).

 - 마우스를 올리면  : 조금 밝아지고 테두리가 파랗게
 - 누르고 있는 동안 : 더 어둡게 + 아래로 1px 내려앉음 (실제로 눌린 느낌)
 - 기본 버튼(Enter) : 파란 테두리로 어느 것이 기본인지 보이게
 - 끌 수 없는 버튼  : 흐리게

도구 막대 · 영상 위 막대처럼 자기 스타일을 쓰는 곳은 그대로 둔다
(QDialog · QMessageBox 안의 버튼만 고른다).
"""

# QDialog(= QMessageBox 포함) 안의 버튼에만 적용 — 도구 막대는 건드리지 않는다
BUTTON_STYLE = """
QDialog QPushButton, QMessageBox QPushButton {
    background: #343a42;
    color: #e6ecf3;
    border: 1px solid #4a525c;
    border-radius: 5px;
    padding: 5px 14px;
    min-height: 18px;
}
QDialog QPushButton:hover, QMessageBox QPushButton:hover {
    background: #3f4752;
    border-color: #5aa9ff;
}
QDialog QPushButton:pressed, QMessageBox QPushButton:pressed {
    background: #1d2b3c;
    border-color: #5aa9ff;
    padding-top: 6px;        /* 글자가 1px 내려가 '눌린' 느낌 */
    padding-bottom: 4px;
}
QDialog QPushButton:default, QMessageBox QPushButton:default {
    border: 1px solid #5aa9ff;
}
QDialog QPushButton:focus, QMessageBox QPushButton:focus {
    outline: none;
    border-color: #5aa9ff;
}
QDialog QPushButton:disabled, QMessageBox QPushButton:disabled {
    background: #2b3038;
    color: #6b7580;
    border-color: #3a414a;
}
QDialog QPushButton:checked, QMessageBox QPushButton:checked {
    background: #1d3a5c;
    border-color: #5aa9ff;
    color: #cfe6ff;
}
"""


def install(app):
    """앱 전체에 버튼 눌림 효과 적용 (이미 있는 스타일시트 뒤에 덧붙임)"""
    app.setStyleSheet((app.styleSheet() or "") + BUTTON_STYLE)
