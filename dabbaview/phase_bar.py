# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Phase 버튼 띠 (INFINITT PACS 방식) - 영상 위에 [1] [2] [3] … 위상 번호

- 번호를 누르면 그 위상으로 (위치는 그대로)
- [ALL] (기본) = 전체 위상: 휠 · 시네가 영상 순서대로
- 번호를 누르면 그 위상만: 휠은 슬라이스 위치 이동 (위상 고정), 시네 재생은 그 위치의 위상
- 위상이 없는 시리즈(한 위치에 한 장)에서는 띠가 숨겨진다
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QWidget)

from .phases import phase_map

BUTTON_STYLE = """
QPushButton { background: #2b2b2b; color: #cfd6df; border: 1px solid #3d3d3d; border-radius: 3px;
              padding: 1px 6px; min-width: 20px; min-height: 16px; font-size: 11px; }
QPushButton:hover { background: #3a3a3a; color: #fff; }
QPushButton:checked { background: #6b5d00; color: #ffd200; border: 1px solid #ffd200; font-weight: bold; }
QPushButton[current="true"] { border: 1px dashed #ffd200; color: #ffd200; }
"""


class PhaseBar(QWidget):
    """위상 번호 버튼 띠. 뷰포트 위에 놓고 set_viewport로 연결"""

    phase_selected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._viewport = None
        self._map = None
        self._buttons = []
        self._all_mode = True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)
        self.title = QLabel("Phase")
        self.title.setStyleSheet("color: #9aa7b5; font-weight: bold;")
        layout.addWidget(self.title)
        self._row = QWidget()
        self._row_layout = QHBoxLayout(self._row)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setSpacing(2)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._row)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(32)
        layout.addWidget(self._scroll, 1)
        self.position_label = QLabel("")
        self.position_label.setStyleSheet("color: #9aa7b5;")
        layout.addWidget(self.position_label)
        self.hint = QLabel("번호 = 그 위상만 · ALL = 전체")
        self.hint.setStyleSheet("color: #6f7a87;")
        layout.addWidget(self.hint)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(self._clicked)
        self.setStyleSheet("PhaseBar { background: #232323; border-bottom: 1px solid #3d3d3d; }")
        self.hide()

    # ─── 연결 ───
    def set_viewport(self, viewport):
        self._viewport = viewport
        viewport.slice_changed.connect(lambda *_: self.refresh())

    def set_series(self, series):
        self._map = phase_map(series) if series is not None else None
        self._all_mode = True   # 시리즈를 바꾸면 전체 모드로
        self._rebuild()
        self._apply_navigator()
        self.refresh()

    # ─── 내부 ───
    def _rebuild(self):
        for b in self._buttons:
            self._group.removeButton(b)
            b.setParent(None)
        self._buttons = []
        if self._map is None:
            self.hide()
            return
        for i, text in enumerate(self._map.labels):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setStyleSheet(BUTTON_STYLE)
            b.setToolTip(f"위상 {text} / {self._map.n_phases} ({self._map.unit}) — 이 위상만 보기 "
                         "(휠은 슬라이스 위치, 재생은 이 위치의 위상)")
            b.setProperty("phase", i)
            self._row_layout.addWidget(b)
            self._group.addButton(b)
            self._buttons.append(b)
        self.all_button = QPushButton("ALL")
        self.all_button.setCheckable(True)
        self.all_button.setChecked(True)   # 기본: 전체 (예전과 같은 스크롤 · 재생)
        self.all_button.setStyleSheet(BUTTON_STYLE)
        self.all_button.setToolTip("전체 위상: 휠 · 시네가 영상 순서대로 (기본)")
        self.all_button.setProperty("phase", -1)
        self._row_layout.addWidget(self.all_button)
        self._group.addButton(self.all_button)
        self._row_layout.addStretch(1)
        self._all_mode = True
        self.show()

    def _clicked(self, button):
        if self._map is None or self._viewport is None:
            return
        phase = int(button.property("phase"))
        self._all_mode = phase < 0
        self._apply_navigator()
        if phase >= 0:
            where = self._map.where(self._viewport.current_slice)
            position = where[0] if where else 0
            index = self._map.slice_at(position, phase)
            if index is not None:
                self._viewport.go_to_slice(index, user=True)
        self.refresh()
        self.phase_selected.emit(phase)

    def _apply_navigator(self, *_):
        if self._viewport is None:
            return
        self._viewport.slice_navigator = None if (self._map is None or self._all_mode) else self._navigate

    def _navigate(self, index, direction, kind):
        """뷰포트가 부름: 휠이면 위치, 시네면 위상. 처리 못 하면 None"""
        if self._map is None:
            return None
        if kind == "cine":
            return self._map.step_phase(index, 1 if direction >= 0 else -1)
        return self._map.step_position(index, direction)

    def refresh(self):
        if self._map is None or self._viewport is None:
            return
        where = self._map.where(self._viewport.current_slice)
        if where is None:
            return
        position, phase = where
        for i, b in enumerate(self._buttons):
            want = (not self._all_mode) and i == phase
            if b.isChecked() != want:
                b.blockSignals(True)
                b.setChecked(want)
                b.blockSignals(False)
            current = "true" if (self._all_mode and i == phase) else "false"
            if b.property("current") != current:   # 전체 모드: 지금 위상에 점선 테두리
                b.setProperty("current", current)
                b.style().unpolish(b)
                b.style().polish(b)
        self.all_button.blockSignals(True)
        self.all_button.setChecked(self._all_mode)
        self.all_button.blockSignals(False)
        self.position_label.setText(f"위치 {position + 1}/{self._map.n_positions}  ·  "
                                    f"위상 {phase + 1}/{self._map.n_phases}")
        button = self._buttons[phase] if phase < len(self._buttons) else None
        if button is not None:   # 버튼이 많으면 현재 위상이 보이게 스크롤
            self._scroll.ensureWidgetVisible(button, 40, 0)
