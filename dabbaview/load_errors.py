# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""불러오기 실패 목록: 상태바 버튼 + 목록 창

- 폴더를 읽을 때 건너뛴 파일 (손상, 시간 초과)
- 표시하려다 디코딩에 실패한 영상 (압축 코덱 없음, 손상) — 아무 스레드에서나 보고됨
"""
import os
import subprocess
import sys

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QDialog, QHBoxLayout,
                             QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                             QToolButton, QVBoxLayout)

STAGE_METADATA = "메타데이터 읽기"
STAGE_DECODE = "픽셀 디코딩"
STAGE_TIMEOUT = "타임아웃"
STAGE_CLOUD = "클라우드 미다운로드"


def _stage(source, reason):
    """불러오기(메타데이터) / 표시(픽셀) 단계 + 이유 → 표시할 단계 이름"""
    if reason.startswith("클라우드"):
        return STAGE_CLOUD
    if reason.startswith("시간 초과"):
        return STAGE_TIMEOUT
    return STAGE_METADATA if source == "load" else STAGE_DECODE


def reveal_in_file_manager(path):
    """Finder(탐색기)에서 파일을 선택해 보여 줌. 파일이 없으면 폴더를, 폴더도 없으면 False"""
    folder = os.path.dirname(path)
    exists = os.path.exists(path)
    if not exists and not os.path.isdir(folder):
        return False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if exists else ["open", folder])
            return True
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"] if exists
                             else ["explorer", os.path.normpath(folder)])
            return True
    except OSError:
        pass
    return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


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
        self._items = {(path, None): (_stage("load", reason), reason)
                       for path, reason in load_errors}
        self.changed.emit()

    def extend(self, load_errors):
        for path, reason in load_errors:
            self._items[(path, None)] = (_stage("load", reason), reason)
        self.changed.emit()

    def add_decode_error(self, path, frame, reason):
        """dicom_loader.decode_error_listeners 에 등록 (워커 스레드에서도 호출됨)"""
        self._decode_error.emit(path, frame, reason)

    def _add_decode(self, path, frame, reason):
        key = (path, frame)
        if key not in self._items:
            self._items[key] = (_stage("decode", reason), reason)
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
    """실패 목록: 폴더 | 파일명 | 단계 | 이유 + Show in Finder"""

    COLUMNS = ["폴더", "파일명", "단계", "이유"]

    def __init__(self, log, parent=None):
        super().__init__(parent)
        self.setWindowTitle("로딩 실패 파일")
        self.resize(1000, 460)
        self._items = sorted(log.items(), key=lambda it: (os.path.dirname(it[0]), it[0],
                                                          it[1] if it[1] is not None else -1))
        layout = QVBoxLayout(self)
        counts = {}
        for _path, _frame, stage, _reason in self._items:
            counts[stage] = counts.get(stage, 0) + 1
        summary = ", ".join(f"{stage} {n}" for stage, n in counts.items())
        layout.addWidget(QLabel(
            f"{len(self._items)}개 파일(프레임)을 건너뛰었습니다 ({summary}). "
            "나머지 영상은 정상적으로 불러왔습니다."))

        table = QTableWidget(len(self._items), len(self.COLUMNS))
        self.table = table
        table.setHorizontalHeaderLabels(self.COLUMNS)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setTextElideMode(Qt.ElideMiddle)   # 긴 폴더 경로는 가운데를 줄임
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        for r, (path, frame, stage, reason) in enumerate(self._items):
            name = os.path.basename(path) + (f"  [프레임 {frame + 1}]" if frame is not None else "")
            cells = (os.path.dirname(path), name, stage, reason)
            tips = (path, path, stage, reason)
            for c, (text, tip) in enumerate(zip(cells, tips)):
                cell = QTableWidgetItem(text)
                cell.setToolTip(tip)
                table.setItem(r, c, cell)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        table.setColumnWidth(0, 330)
        table.itemSelectionChanged.connect(self._update_buttons)
        table.itemDoubleClicked.connect(lambda _item: self.show_in_finder())
        layout.addWidget(table)

        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setStyleSheet("color: #bbb;")
        layout.addWidget(self.path_label)
        hint = QLabel("압축 형식 오류는 디코더(pylibjpeg, GDCM)가 지원하지 않거나 파일이 손상된 경우입니다. "
                      "타임아웃·클라우드 미다운로드는 OneDrive 같은 동기화 폴더에서 생깁니다 — "
                      "Finder에서 폴더를 우클릭해 '다운로드'(항상 이 기기에 유지)한 뒤 다시 열어 보세요. "
                      "행을 더블클릭하면 Finder에서 보여 줍니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self.finder_button = QPushButton("Show in Finder" if sys.platform == "darwin"
                                         else "폴더 열기")
        self.finder_button.clicked.connect(self.show_in_finder)
        copy = QPushButton("목록 복사")
        copy.clicked.connect(self.copy)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.finder_button)
        buttons.addWidget(copy)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        if self._items:
            table.selectRow(0)
        self._update_buttons()

    def selected_paths(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._items[r][0] for r in rows]

    def _update_buttons(self):
        paths = self.selected_paths()
        self.finder_button.setEnabled(bool(paths))
        self.path_label.setText(paths[0] if len(paths) == 1 else
                                (f"{len(paths)}개 선택" if paths else ""))

    def show_in_finder(self):
        """선택한 파일을 Finder에서 보여 줌 (여러 개면 폴더마다 한 번)"""
        shown = set()
        for path in self.selected_paths():
            folder = os.path.dirname(path)
            if folder in shown:
                continue
            shown.add(folder)
            if not reveal_in_file_manager(path):
                self.path_label.setText(f"찾을 수 없음: {path}")

    def copy(self):
        """탭으로 구분한 표 (폴더, 파일명, 프레임, 단계, 이유) — 엑셀에 바로 붙여넣기"""
        lines = ["폴더\t파일명\t프레임\t단계\t이유"]
        for path, frame, stage, reason in self._items:
            lines.append(f"{os.path.dirname(path)}\t{os.path.basename(path)}\t"
                         f"{'' if frame is None else frame + 1}\t{stage}\t{reason}")
        QApplication.clipboard().setText("\n".join(lines))
