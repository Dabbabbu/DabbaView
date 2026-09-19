# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""불러오기 실패 목록: 상태바 버튼 + 목록 창

- 폴더를 읽을 때 건너뛴 파일 (손상, 시간 초과)
- 표시하려다 디코딩에 실패한 영상 (압축 코덱 없음, 손상) — 아무 스레드에서나 보고됨
"""
import os

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel,
                             QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
                             QVBoxLayout)


class LoadErrorLog(QObject):
    """실패 목록 (경로·프레임 → 단계, 이유). add_decode_error는 스레드 안전 (시그널로 넘김)"""

    changed = pyqtSignal()
    _decode_error = pyqtSignal(str, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = {}   # (경로, 프레임) → (단계, 이유)
        self._decode_error.connect(self._add_decode, Qt.QueuedConnection)

    def reset(self, load_errors):
        """새로 불러온 폴더의 건너뛴 파일로 교체"""
        self._items = {(path, None): ("불러오기", reason) for path, reason in load_errors}
        self.changed.emit()

    def extend(self, load_errors):
        for path, reason in load_errors:
            self._items[(path, None)] = ("불러오기", reason)
        self.changed.emit()

    def add_decode_error(self, path, frame, reason):
        """dicom_loader.decode_error_listeners 에 등록 (워커 스레드에서도 호출됨)"""
        self._decode_error.emit(path, frame, reason)

    def _add_decode(self, path, frame, reason):
        key = (path, frame)
        if key not in self._items:
            self._items[key] = ("표시", reason)
            self.changed.emit()

    def items(self):
        return [(path, frame, stage, reason)
                for (path, frame), (stage, reason) in self._items.items()]

    def __len__(self):
        return len(self._items)


class LoadErrorButton(QToolButton):
    """상태바의 '⚠ 로딩 실패 N' 버튼 (없으면 숨김)"""

    def __init__(self, log, parent=None):
        super().__init__(parent)
        self._log = log
        self.setAutoRaise(True)
        self.setStyleSheet("QToolButton { color: #ffb347; }")
        self.setToolTip("불러오거나 표시하지 못한 파일 목록 보기")
        self.clicked.connect(self.show_dialog)
        log.changed.connect(self._update)
        self._update()

    def _update(self):
        n = len(self._log)
        self.setText(f"⚠ 로딩 실패 {n}")
        self.setVisible(n > 0)

    def show_dialog(self):
        FailedFilesDialog(self._log, self.window()).exec_()


class FailedFilesDialog(QDialog):
    def __init__(self, log, parent=None):
        super().__init__(parent)
        self.setWindowTitle("로딩 실패 파일")
        self.resize(820, 420)
        items = log.items()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{len(items)}개 파일(프레임)을 건너뛰었습니다. 나머지 영상은 정상적으로 불러왔습니다."))
        table = QTableWidget(len(items), 4)
        table.setHorizontalHeaderLabels(["파일", "단계", "이유", "경로"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        for r, (path, frame, stage, reason) in enumerate(items):
            name = os.path.basename(path) + (f" [프레임 {frame + 1}]" if frame is not None else "")
            for c, text in enumerate((name, stage, reason, os.path.dirname(path))):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                table.setItem(r, c, cell)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(table)
        hint = QLabel("압축 형식 오류는 디코더(pylibjpeg, GDCM)가 지원하지 않거나 파일이 손상된 경우입니다. "
                      "시간 초과 파일은 네트워크 드라이브·클라우드 동기화 폴더에서 자주 생깁니다 — "
                      "로컬로 복사한 뒤 다시 열어 보세요.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        copy = QPushButton("목록 복사")
        copy.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(
            f"{path}\t{'' if frame is None else frame + 1}\t{stage}\t{reason}"
            for path, frame, stage, reason in items)))
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
