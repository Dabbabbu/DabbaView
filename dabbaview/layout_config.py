# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Multi View 레이아웃 목록 편집 (INFINITT의 Image set layout Config)

왼쪽 = 고를 수 있는 레이아웃(기본 + 직접 만든 것), 오른쪽 = 드롭다운에 나올 목록.
➕ · ➖ 로 넣고 빼고, ▲ · ▼ 로 순서를 바꾼다. 행 · 열을 직접 넣어 새 레이아웃도 만든다.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QSpinBox, QVBoxLayout, QWidget)

from . import layouts


def _preview(rows, cols, size=54):
    """레이아웃 모양을 작은 격자 그림으로 (한눈에 보이게)"""
    box = QWidget()
    grid = QGridLayout(box)
    grid.setSpacing(1)
    grid.setContentsMargins(0, 0, 0, 0)
    cell_w = max(3, size // max(cols, 1))
    cell_h = max(3, size // max(rows, 1))
    for r in range(rows):
        for c in range(cols):
            cell = QLabel()
            cell.setFixedSize(cell_w, cell_h)
            cell.setStyleSheet("background: #4a6a8a; border: 1px solid #22303d;")
            grid.addWidget(cell, r, c)
    return box


class LayoutConfigDialog(QDialog):
    def __init__(self, active, auto, parent=None):
        super().__init__(parent)
        self.setWindowTitle("레이아웃 목록 (Image set layout)")
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("드롭다운에 나올 레이아웃을 고르세요. 이름은 <b>행 x 열</b>입니다 "
                                "(2x3 = 2줄 3칸)."))

        columns = QHBoxLayout()
        left_box = QGroupBox("고를 수 있는 레이아웃 (Default)")
        left_layout = QVBoxLayout(left_box)
        self.available = QListWidget()
        self.available.itemDoubleClicked.connect(lambda _i: self._add())
        left_layout.addWidget(self.available)
        make = QHBoxLayout()
        make.addWidget(QLabel("직접 만들기:"))
        self.rows = QSpinBox()
        self.rows.setRange(1, layouts.MAX_ROWS)
        self.rows.setPrefix("행 ")
        self.cols = QSpinBox()
        self.cols.setRange(1, layouts.MAX_COLS)
        self.cols.setPrefix("열 ")
        self.cols.setValue(2)
        add_custom = QPushButton("➕ 만들어 넣기")
        add_custom.clicked.connect(self._add_custom)
        make.addWidget(self.rows)
        make.addWidget(self.cols)
        make.addWidget(add_custom)
        make.addStretch(1)
        left_layout.addLayout(make)
        columns.addWidget(left_box, 1)

        middle = QVBoxLayout()
        middle.addStretch(1)
        for text, tip, slot in (("➕", "오른쪽 목록에 넣기", self._add),
                                ("➖", "오른쪽 목록에서 빼기", self._remove),
                                ("▲", "위로", lambda: self._move(-1)),
                                ("▼", "아래로", lambda: self._move(1))):
            button = QPushButton(text)
            button.setFixedWidth(46)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            middle.addWidget(button)
        middle.addStretch(1)
        columns.addLayout(middle)

        right_box = QGroupBox("드롭다운에 나올 목록 (User define)")
        right_layout = QVBoxLayout(right_box)
        self.chosen = QListWidget()
        self.chosen.itemDoubleClicked.connect(lambda _i: self._remove())
        right_layout.addWidget(self.chosen)
        reset = QPushButton("기본값으로")
        reset.clicked.connect(self._reset)
        right_layout.addWidget(reset, alignment=Qt.AlignLeft)
        columns.addWidget(right_box, 1)
        layout.addLayout(columns)

        self.auto = QCheckBox("Auto — 영상을 펼칠 때 시리즈 수에 맞는 레이아웃을 스스로 고름")
        self.auto.setChecked(bool(auto))
        layout.addWidget(self.auto)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_available()
        self._fill_chosen(active)

    # ─── 목록 ───
    def _fill_available(self, extra=()):
        self.available.clear()
        for layout_id in list(layouts.DEFAULT_LAYOUTS) + [e for e in extra
                                                          if e not in layouts.DEFAULT_LAYOUTS]:
            self._add_row(self.available, layout_id)

    def _fill_chosen(self, values):
        self.chosen.clear()
        for layout_id in layouts.clean_list(values):
            self._add_row(self.chosen, layout_id)

    @staticmethod
    def _add_row(list_widget, layout_id):
        rows, cols = layouts.parse(layout_id)
        item = QListWidgetItem(f"{layouts.label(layout_id)}    ({rows}줄 × {cols}칸 = {rows * cols}칸)")
        item.setData(Qt.UserRole, layout_id)
        list_widget.addItem(item)
        return item

    def _ids(self, list_widget):
        return [list_widget.item(i).data(Qt.UserRole) for i in range(list_widget.count())]

    def _add(self):
        item = self.available.currentItem()
        if item is None:
            return
        layout_id = item.data(Qt.UserRole)
        if layout_id not in self._ids(self.chosen):
            self.chosen.setCurrentItem(self._add_row(self.chosen, layout_id))

    def _add_custom(self):
        layout_id = layouts.clean(layouts.name(self.rows.value(), self.cols.value()))
        if not layout_id:
            return
        if layout_id not in self._ids(self.available):
            self._fill_available(self._ids(self.available) + [layout_id])
        if layout_id not in self._ids(self.chosen):
            self.chosen.setCurrentItem(self._add_row(self.chosen, layout_id))

    def _remove(self):
        row = self.chosen.currentRow()
        if row >= 0 and self.chosen.count() > 1:      # 하나는 남겨 둠 (드롭다운이 비지 않게)
            self.chosen.takeItem(row)

    def _move(self, step):
        row = self.chosen.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.chosen.count():
            return
        item = self.chosen.takeItem(row)
        self.chosen.insertItem(target, item)
        self.chosen.setCurrentRow(target)

    def _reset(self):
        self._fill_chosen(layouts.DEFAULT_ACTIVE)

    def result_values(self):
        """(드롭다운 목록, Auto 켬?)"""
        return layouts.clean_list(self._ids(self.chosen)), self.auto.isChecked()
