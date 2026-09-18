# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 태그 정보 뷰어 다이얼로그
"""
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QTableWidget,
                              QTableWidgetItem, QHeaderView, QLineEdit,
                              QLabel, QHBoxLayout)
from PyQt5.QtCore import Qt


class TagViewer(QDialog):
    """DICOM 태그를 테이블 형태로 보여주는 다이얼로그"""

    def __init__(self, tags_dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DICOM Tags")
        self.resize(700, 600)
        self._tags = tags_dict
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # 검색
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("검색:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("태그 이름 또는 값 검색...")
        self._search.textChanged.connect(self._filter_tags)
        search_layout.addWidget(self._search)
        layout.addLayout(search_layout)

        # 테이블
        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Tag", "Value"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self._table)

        self._populate_table(self._tags)

    def _populate_table(self, tags):
        self._table.setRowCount(len(tags))
        for i, (key, value) in enumerate(sorted(tags.items())):
            self._table.setItem(i, 0, QTableWidgetItem(key))
            self._table.setItem(i, 1, QTableWidgetItem(str(value)))

    def _filter_tags(self, text):
        text = text.lower()
        filtered = {k: v for k, v in self._tags.items()
                    if text in k.lower() or text in str(v).lower()}
        self._populate_table(filtered)
