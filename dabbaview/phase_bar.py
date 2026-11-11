# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Phase 버튼 띠 (INFINITT PACS 방식) - 영상 위에 [1] [2] [3] … 위상 번호

두 줄: 위에 Slice(위치) 번호, 아래에 Phase(위상) 번호 — 지금 보는 칸이 노랗게 켜진다

- Slice 번호를 누르면 그 위치로 (위상은 그대로), Phase 번호를 누르면 그 위상으로 (위치는 그대로)
- [ALL] (기본) = 전체 위상: 휠이 영상 순서대로
- 🔒 이 슬라이스에서 위상 보기: 한 위치에 머문 채 휠 · 재생이 위상만 넘어감 (수축기 → 이완기)
- ⏮ ▶ ■ + FPS: 재생. 🔒가 꺼져 있으면 한 슬라이스의 위상을 다 돌고 다음 슬라이스로 넘어간다
- 방향키: ↑↓ 슬라이스 위치 (위상 고정), ←→ 위상
  View ▸ ↕ 방향키 메뉴에서 ↑↓를 잠시 '전체 순서' · '위상'으로 바꿀 수 있다
- 좌+우 버튼 함께 누르고 드래그: 위아래 = 슬라이스 위치, 좌우 = 위상
- 위상이 없는 시리즈(한 위치에 한 장)에서는 띠가 숨겨진다
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

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
        self._slice_buttons = []
        self._series = None
        self._all_mode = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 2)
        outer.setSpacing(2)

        # ── 윗줄: Slice(위치) 번호 ──
        top = QHBoxLayout()
        top.setSpacing(6)
        self.slice_title = QLabel("Slice")
        self.slice_title.setStyleSheet("color: #9aa7b5; font-weight: bold;")
        self.slice_title.setFixedWidth(40)
        top.addWidget(self.slice_title)
        self._slice_row, self._slice_row_layout, self._slice_scroll = self._make_strip()
        top.addWidget(self._slice_scroll, 1)
        self.slice_lock = QPushButton("🔒 이 슬라이스에서 위상 보기")
        top.addWidget(self.slice_lock)
        outer.addLayout(top)

        # ── 아랫줄: Phase(위상) 번호 + 재생 ──
        layout = QHBoxLayout()
        layout.setSpacing(6)
        self.title = QLabel("Phase")
        self.title.setStyleSheet("color: #9aa7b5; font-weight: bold;")
        self.title.setFixedWidth(40)
        layout.addWidget(self.title)
        self._row, self._row_layout, self._scroll = self._make_strip()
        layout.addWidget(self._scroll, 1)
        self.rewind_button = QPushButton("⏮")
        self.rewind_button.setToolTip("처음 위상으로 (재생 중이면 멈추고 처음으로)")
        self.play_button = QPushButton("▶")
        self.play_button.setToolTip("재생 / 멈춤 (Space)\n🔒가 켜져 있으면 이 슬라이스의 위상만 돌고,\n"
                                    "꺼져 있으면 한 슬라이스를 다 돌고 다음 슬라이스로 넘어갑니다")
        self.stop_button = QPushButton("■")
        self.stop_button.setToolTip("멈춤")
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(15)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setFixedWidth(72)
        self.fps_spin.setToolTip("초당 몇 장을 넘길지")
        self.fps_spin.valueChanged.connect(self._fps_changed)
        for b in (self.rewind_button, self.play_button, self.stop_button):
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(BUTTON_STYLE)
            b.setFixedWidth(30)
            layout.addWidget(b)
        layout.addWidget(self.fps_spin)
        self.rewind_button.clicked.connect(self._rewind)
        self.play_button.clicked.connect(self._toggle_play)
        self.stop_button.clicked.connect(self._stop)
        outer.addLayout(layout)
        layout = outer   # 아래 코드가 layout에 더 붙이지 않도록 (호환)
        self.position_label = QLabel("")   # 더 쓰지 않음 (안내 문구 제거)
        self.position_label.hide()
        self.slice_lock.setCheckable(True)
        self.slice_lock.setFocusPolicy(Qt.NoFocus)
        self.slice_lock.setStyleSheet(BUTTON_STYLE)
        self.slice_lock.setToolTip("켜면 이 슬라이스 위치에 머문 채 휠로 위상이 넘어갑니다 "
                                   "(수축기 → 이완기). ▶ Play도 이 위치의 위상을 돌립니다.")
        self.slice_lock.toggled.connect(self._slice_lock_toggled)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(self._clicked)
        self._slice_group = QButtonGroup(self)
        self._slice_group.setExclusive(True)
        self._slice_group.buttonClicked.connect(self._slice_clicked)
        self.setStyleSheet("PhaseBar { background: #232323; border-bottom: 1px solid #3d3d3d; }")
        self.hide()

    def _set_phase_rows_visible(self, on):
        """위상 번호 · 슬라이스 번호 띠와 🔒를 보이거나 숨긴다 (재생기는 늘 보인다)"""
        for widget in (self.slice_title, self._slice_scroll, self.slice_lock,
                       self.title, self._scroll):
            widget.setVisible(on)

    @staticmethod
    def _make_strip():
        """번호 버튼이 줄줄이 들어갈 가로 스크롤 띠 → (속 widget, layout, scroll)"""
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)
        scroll = QScrollArea()
        scroll.setWidget(row)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedHeight(26)
        return row, row_layout, scroll

    # ─── 연결 ───
    def set_viewport(self, viewport):
        self._viewport = viewport
        viewport.slice_changed.connect(lambda *_: self.refresh())
        viewport.cine_state_changed.connect(self._cine_state)

    # ─── 재생 ───
    def _cine_state(self, playing):
        self.play_button.setText("❚❚" if playing else "▶")

    def _fps_changed(self, value):
        if self._viewport is not None:
            self._viewport.set_cine_fps(value)

    def _toggle_play(self):
        if self._viewport is None:
            return
        self._viewport.set_cine_fps(self.fps_spin.value())
        self._viewport.toggle_cine()
        self._viewport.setFocus()

    def _stop(self):
        if self._viewport is not None:
            self._viewport.stop_cine()
            self._viewport.setFocus()

    def _rewind(self):
        """처음 위상으로 — 🔒면 이 위치의 1번 위상, 아니면 첫 슬라이스"""
        if self._viewport is None or self._map is None:
            return
        self._viewport.stop_cine()
        where = self._map.where(self._viewport.current_slice)
        position = where[0] if where else 0
        index = (self._map.slice_at(position, 0) if self.slice_lock.isChecked()
                 else self._map.slice_at(0, 0))
        if index is not None:
            self._viewport.go_to_slice(index, user=True)
        self._viewport.setFocus()

    def _slice_clicked(self, button):
        """Slice 번호 — 그 위치로 (지금 위상은 그대로)"""
        if self._map is None or self._viewport is None:
            return
        position = int(button.property("position"))
        where = self._map.where(self._viewport.current_slice)
        phase = where[1] if where else 0
        index = self._map.slice_at(position, phase)
        if index is None:
            index = self._map.slice_at(position, 0)
        if index is not None:
            self._viewport.go_to_slice(index, user=True)
        self.refresh()
        self._viewport.setFocus()

    def set_series(self, series):
        self._series = series
        self._map = phase_map(series) if series is not None else None
        self._all_mode = True   # 시리즈를 바꾸면 전체 모드로
        self.slice_lock.blockSignals(True)
        self.slice_lock.setChecked(False)
        self.slice_lock.blockSignals(False)
        self._rebuild()
        self._apply_navigator()
        self.refresh()

    # ─── 내부 ───
    @staticmethod
    def _clear_strip(layout, group):
        """띠를 완전히 비운다 — 버튼뿐 아니라 끝의 빈칸(stretch)까지

        전에는 번호 버튼만 지우고 ALL 버튼과 빈칸은 두어서, 시리즈를 바꿀 때마다
        ALL이 하나씩 쌓였다 (열한 번 바꾸면 ALL이 열한 개).
        """
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if group is not None:
                group.removeButton(widget)
            widget.setParent(None)
            widget.deleteLater()

    def _rebuild(self):
        self._clear_strip(self._row_layout, self._group)
        self._clear_strip(self._slice_row_layout, self._slice_group)
        self._buttons = []
        self._slice_buttons = []
        self.all_button = None
        if self._map is None:
            # 위상이 없는 보통 시리즈: 번호 띠는 숨기고 재생기만 남긴다
            # (도구 막대에 있던 ▶ · FPS를 여기로 모았으므로 여기서도 재생할 수 있어야 한다)
            has_images = self._series is not None and self._series.num_slices > 1
            self._set_phase_rows_visible(False)
            self.setVisible(has_images)
            return
        self._set_phase_rows_visible(True)
        for i, text in enumerate(self._map.labels):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)   # 방향키가 버튼 사이를 옮겨 다니지 않게
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
        self.all_button.setFocusPolicy(Qt.NoFocus)
        self.all_button.setStyleSheet(BUTTON_STYLE)
        self.all_button.setToolTip("전체 위상: 휠 · 시네가 영상 순서대로 (기본)")
        self.all_button.setProperty("phase", -1)
        self._row_layout.addWidget(self.all_button)
        self._group.addButton(self.all_button)
        self._row_layout.addStretch(1)

        # 윗줄: 슬라이스(위치) 번호
        for i in range(self._map.n_positions):
            b = QPushButton(str(i + 1))
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(BUTTON_STYLE)
            b.setToolTip(f"슬라이스 위치 {i + 1} / {self._map.n_positions} — 누르면 이 위치로 "
                         "(지금 위상은 그대로)")
            b.setProperty("position", i)
            self._slice_row_layout.addWidget(b)
            self._slice_group.addButton(b)
            self._slice_buttons.append(b)
        self._slice_row_layout.addStretch(1)
        self.slice_title.setText(f"Slice {self._map.n_positions}")
        self.title.setText(f"Phase {self._map.n_phases}")
        self._all_mode = True
        self.show()

    def _clicked(self, button):
        if self._map is None or self._viewport is None:
            return
        phase = int(button.property("phase"))
        self._all_mode = phase < 0
        if self._all_mode and self.slice_lock.isChecked():
            self.slice_lock.setChecked(False)   # ALL = 예전 동작으로
        self._apply_navigator()
        if phase >= 0:
            where = self._map.where(self._viewport.current_slice)
            position = where[0] if where else 0
            index = self._map.slice_at(position, phase)
            if index is not None:
                self._viewport.go_to_slice(index, user=True)
        self.refresh()
        self._viewport.setFocus()   # 이어서 방향키를 쓸 수 있게
        self.phase_selected.emit(phase)

    def _slice_lock_toggled(self, on):
        if self._viewport is not None:
            self._viewport.setFocus()
        if on:
            self._all_mode = False   # 슬라이스 고정은 전체 모드와 같이 쓸 수 없음
        self._apply_navigator()
        self.refresh()

    def _apply_navigator(self, *_):
        if self._viewport is None:
            return
        # 방향키(위치 · 위상)는 어느 모드에서나 되도록 항상 걸어 두고, 휠 · 시네만 모드에 따라 처리
        self._viewport.slice_navigator = self._navigate if self._map is not None else None

    def _navigate(self, index, direction, kind):
        """뷰포트가 부름 → 갈 슬라이스 (처리 못 하면 None)

        시네: 이 위치의 위상. 휠: 슬라이스 고정이면 위상, 아니면 위치(위상 고정)
        """
        if self._map is None:
            return None
        if kind == "sequence":         # ↑↓ 기본: 전체 순서
            return self._map.step_sequence(index, direction)
        if kind == "phase":            # ↑↓ '위상만'
            return self._map.step_phase(index, 1 if direction >= 0 else -1)
        if kind == "position":         # ↑↓ '슬라이스 위치만'
            return self._map.step_position(index, direction)
        if kind == "cine":
            # 🔒 켜짐: 이 슬라이스의 위상만 돌린다 (수축기 → 이완기 반복)
            # 🔒 꺼짐: 한 슬라이스의 위상을 다 돌면 다음 슬라이스로 (전체 순서)
            if self.slice_lock.isChecked():
                return self._map.step_phase(index, 1 if direction >= 0 else -1)
            # 끝까지 갔으면 처음으로 (계속 반복 재생)
            return self._map.step_sequence(index, direction, wrap=True)
        if self._all_mode and kind == "wheel":
            return None                # ALL: 예전처럼 영상 순서대로
        if self.slice_lock.isChecked():
            return self._map.step_phase(index, 1 if direction >= 0 else -1)
        return self._map.step_position(index, direction)

    def refresh(self):
        if self._viewport is None:
            return
        if self._viewport.series is not self._series:   # 다른 길로 시리즈가 바뀌어도 따라감
            self.set_series(self._viewport.series)
            return
        if self._map is None:
            return                      # 재생기만 보이는 상태 — 갱신할 번호가 없다
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
        if self.all_button is not None:
            self.all_button.blockSignals(True)
            self.all_button.setChecked(self._all_mode)
            self.all_button.blockSignals(False)
        # 윗줄: 지금 슬라이스 위치를 켜 둔다 (Phase와 같은 모양)
        for i, b in enumerate(self._slice_buttons):
            want = i == position
            if b.isChecked() != want:
                b.blockSignals(True)
                b.setChecked(want)
                b.blockSignals(False)
        button = self._buttons[phase] if phase < len(self._buttons) else None
        if button is not None:   # 버튼이 많으면 현재 위상이 보이게 스크롤
            self._scroll.ensureWidgetVisible(button, 40, 0)
        if position < len(self._slice_buttons):
            self._slice_scroll.ensureWidgetVisible(self._slice_buttons[position], 40, 0)
