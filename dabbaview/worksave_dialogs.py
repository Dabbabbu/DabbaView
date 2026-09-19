# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""종료할 때 작업 저장 물어보는 창 · 저장 방법 고르는 창"""
import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout)

from . import worksave

SAVE_SIDECAR, SAVE_COPY, SAVE_OVERWRITE = "sidecar", "copy", "overwrite"


class ExitSaveDialog(QDialog):
    """저장 후 종료 / 저장 없이 종료 / 취소"""

    SAVE, DISCARD, CANCEL = "save", "discard", "cancel"

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DabbaView 종료")
        self.setMinimumWidth(460)
        self.choice = self.CANCEL
        layout = QVBoxLayout(self)
        title = QLabel("저장되지 않은 작업이 있습니다.")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)
        items = worksave.counts(store)
        detail = QLabel("현재 작업: " + ", ".join(f"{name} {n}개" for name, n in items))
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addSpacing(6)
        buttons = QDialogButtonBox()
        save = buttons.addButton("저장 후 종료", QDialogButtonBox.AcceptRole)
        discard = buttons.addButton("저장 없이 종료", QDialogButtonBox.DestructiveRole)
        cancel = buttons.addButton("취소", QDialogButtonBox.RejectRole)
        save.setDefault(True)
        save.clicked.connect(lambda: self._done(self.SAVE))
        discard.clicked.connect(lambda: self._done(self.DISCARD))
        cancel.clicked.connect(lambda: self._done(self.CANCEL))
        layout.addWidget(buttons)

    def _done(self, choice):
        self.choice = choice
        self.accept() if choice != self.CANCEL else self.reject()


class SaveChoiceDialog(QDialog):
    """어떻게 저장할지: 원본 덮어쓰기 / 사본 / 어노테이션만"""

    def __init__(self, default_dir, parent=None, default=SAVE_SIDECAR):
        super().__init__(parent)
        self.setWindowTitle("어떻게 저장할까요?")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self._group = QButtonGroup(self)
        self.sidecar = QRadioButton("어노테이션만 별도 저장 (권장)")
        sidecar_note = QLabel(f"DICOM은 건드리지 않고 {worksave.base_dir()} 에 저장합니다.\n"
                              "다음에 같은 검사를 열면 복원할지 물어봅니다.")
        self.copy = QRadioButton("사본 만들어 저장")
        copy_note = QLabel("원본은 그대로 두고, 고른 폴더에 DICOM 사본과 annotations.json을 만듭니다.")
        row = QHBoxLayout()
        self.path = QLineEdit(os.path.join(default_dir or os.path.expanduser("~/Documents"), "DabbaView_저장"))
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        self.overwrite = QRadioButton("원본에 덮어쓰기")
        over_note = QLabel("⚠️ 원본 DICOM 파일이 수정됩니다 (개인 태그에 주석을 넣음, .bak 백업 생성).")
        over_note.setStyleSheet("color: #ff8a8a;")
        for button in (self.sidecar, self.copy, self.overwrite):
            self._group.addButton(button)
        for widget, note in ((self.sidecar, sidecar_note), (self.copy, copy_note), (self.overwrite, over_note)):
            layout.addWidget(widget)
            note.setWordWrap(True)
            note.setStyleSheet(note.styleSheet() + " margin-left: 22px; color: #9aa7b5;"
                               if widget is not self.overwrite else note.styleSheet() + " margin-left: 22px;")
            layout.addWidget(note)
            if widget is self.copy:
                layout.addLayout(row)
            layout.addSpacing(4)
        {SAVE_COPY: self.copy, SAVE_OVERWRITE: self.overwrite}.get(default, self.sidecar).setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "사본을 저장할 폴더", self.path.text())
        if folder:
            self.path.setText(folder)

    def method(self):
        if self.copy.isChecked():
            return SAVE_COPY
        if self.overwrite.isChecked():
            return SAVE_OVERWRITE
        return SAVE_SIDECAR

    def folder(self):
        return self.path.text().strip()


class RestoreDialog(QDialog):
    """다시 열었을 때 이전 작업 복원 여부"""

    def __init__(self, found, parent=None):
        super().__init__(parent)
        self.setWindowTitle("이전 작업 복원")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        total = sum(n for _s, _p, n in found)
        title = QLabel("이전 작업을 복원하시겠습니까?")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"이 검사에 저장해 둔 주석 {total}개가 있습니다 "
                                f"(검사 {len(found)}건). 자동 저장 폴더에서 읽어옵니다."))
        buttons = QDialogButtonBox()
        yes = buttons.addButton("복원", QDialogButtonBox.AcceptRole)
        buttons.addButton("복원하지 않음", QDialogButtonBox.RejectRole)
        yes.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowModality(Qt.ApplicationModal)
