# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
📍 랜드마크 목록 - 찍어 둔 점을 모아 보고, 누르면 그 영상(시리즈 · 슬라이스)으로 바로 이동

- 이름 · 설명은 표에서 바로 고침
- 지금 연 검사만 / 전체
- 삭제 · CSV · 3D Slicer JSON 내보내기
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)


class LandmarkListDialog(QDialog):
    COLUMNS = ["이름", "설명", "시리즈", "영상", "좌표 (L, P, S mm)"]

    def __init__(self, main, parent=None):
        super().__init__(parent or main)
        self.main = main
        self.store = main._landmarks
        self.setWindowTitle("📍 랜드마크 목록")
        self.setWindowFlags(Qt.Tool | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.resize(640, 360)
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setStyleSheet("color: #9ab;")
        top.addWidget(self.summary, 1)
        self.only_open = QCheckBox("지금 연 검사만")
        self.only_open.setChecked(True)
        self.only_open.toggled.connect(self.refresh)
        top.addWidget(self.only_open)
        layout.addLayout(top)

        table = QTableWidget(0, len(self.COLUMNS))
        self.table = table
        table.setHorizontalHeaderLabels(self.COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        table.setColumnWidth(2, 150)
        table.cellClicked.connect(lambda row, _col: self._go(row))
        table.itemChanged.connect(self._edited)
        layout.addWidget(table)

        buttons = QHBoxLayout()
        tool = QPushButton("📍 점 찍기 (F)")
        tool.setToolTip("랜드마크 도구로 바꿈 — 영상을 클릭하면 점 추가, Esc로 Select로 돌아감")
        tool.clicked.connect(lambda: main.select_tool_by_id("landmark"))
        delete = QPushButton("삭제")
        delete.clicked.connect(self._delete)
        csv_button = QPushButton("CSV…")
        csv_button.clicked.connect(self._export_csv)
        json_button = QPushButton("JSON (Slicer)…")
        json_button.clicked.connect(self._export_json)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        for b in (tool, delete, csv_button, json_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        hint = QLabel("행을 누르면 그 영상으로 이동합니다 · 이름 · 설명은 더블클릭해서 고칩니다 · "
                      "찍은 점은 검사별로 이 컴퓨터에 저장되어 다시 열면 그대로 나옵니다")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(hint)

        self._rows = []   # 표의 행 → 저장소의 번호
        self.store.changed.connect(self.refresh)
        self.refresh()

    def _open_studies(self):
        loader = getattr(self.main, "_loader", None)
        if loader is None:
            return set()
        return {s.study_uid for s in loader.get_series_list() if s.study_uid}

    def refresh(self):
        points = list(self.store)
        studies = self._open_studies()
        only = self.only_open.isChecked()
        self._rows = [i for i, p in enumerate(points)
                      if not only or not p.get("study_uid") or p.get("study_uid") in studies]
        loader = getattr(self.main, "_loader", None)
        table = self.table
        table.blockSignals(True)
        table.setRowCount(len(self._rows))
        for r, i in enumerate(self._rows):
            p = points[i]
            series = loader.get_series_by_uid(p.get("series_uid")) if loader is not None else None
            desc = (series.description if series is not None else p.get("series_desc")) or "-"
            where = "" if series is not None else "  (열려 있지 않음)"
            index = p.get("slice")
            cells = [p["name"], p.get("label", ""), desc + where,
                     "" if index is None else str(int(index) + 1),
                     ", ".join(f"{v:.1f}" for v in p["position"])]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c not in (0, 1):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if series is None and c == 2:
                    item.setForeground(Qt.gray)
                table.setItem(r, c, item)
        table.blockSignals(False)
        hidden = len(points) - len(self._rows)
        self.summary.setText(f"랜드마크 {len(self._rows)}개" + (f" (다른 검사 {hidden}개 숨김)" if hidden else ""))

    def _index(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _go(self, row):
        index = self._index(row)
        if index is not None:
            self.main.go_to_landmark(index)

    def _edited(self, item):
        field = {0: "name", 1: "label"}.get(item.column())
        index = self._index(item.row())
        if field and index is not None:
            self.store.update(index, **{field: item.text()})

    def _delete(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        indexes = sorted((self._index(r) for r in rows if self._index(r) is not None), reverse=True)
        if not indexes:
            return
        if len(indexes) > 1 and QMessageBox.question(
                self, "랜드마크", f"랜드마크 {len(indexes)}개를 지울까요?") != QMessageBox.Yes:
            return
        for index in indexes:
            self.store.remove(index)

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "랜드마크 CSV", "landmarks.csv", "CSV (*.csv)")
        if path:
            self.store.save_csv(path)

    def _export_json(self):
        path, _ = QFileDialog.getSaveFileName(self, "랜드마크 JSON (3D Slicer)",
                                              "landmarks.mrk.json", "Markups JSON (*.json)")
        if path:
            self.store.save_json(path)
