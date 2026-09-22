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
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QDialog, QFrame, QHBoxLayout,
                             QHeaderView, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
                             QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout)

from .dicom_loader import is_retryable

STAGE_METADATA = "메타데이터 읽기"
STAGE_DECODE = "픽셀 디코딩"
STAGE_TIMEOUT = "타임아웃"
STAGE_CLOUD = "클라우드 미다운로드"
STAGE_IO = "읽기 오류"


def _stage(source, reason):
    """불러오기(메타데이터) / 표시(픽셀) 단계 + 이유 → 표시할 단계 이름"""
    if reason.startswith("클라우드"):
        return STAGE_CLOUD
    if reason.startswith("시간 초과"):
        return STAGE_TIMEOUT
    if reason.startswith("읽기 오류"):
        return STAGE_IO
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

    def discard(self, paths):
        """다시 읽기로 한 파일은 목록에서 뺌 (다시 실패하면 새 이유로 다시 들어옴)"""
        paths = set(paths)
        before = len(self._items)
        self._items = {key: value for key, value in self._items.items()
                       if not (key[1] is None and key[0] in paths)}
        if len(self._items) != before:
            self.changed.emit()

    def items(self):
        return [(path, frame, stage, reason)
                for (path, frame), (stage, reason) in self._items.items()]

    def load_failures(self):
        """폴더를 읽을 때 건너뛴 파일만 [(경로, 이유)] (표시 중 디코딩 실패는 제외)"""
        return [(path, reason) for (path, frame), (stage, reason) in self._items.items()
                if frame is None and stage != STAGE_DECODE]

    def retryable_paths(self, folder=None):
        """다시 시도할 수 있는 파일 (시간 초과 · 클라우드 · 읽기 오류) — folder를 주면 그 폴더만"""
        return [path for path, reason in self.load_failures()
                if is_retryable(reason) and (folder is None or os.path.dirname(path) == folder)]

    def by_folder(self):
        """폴더 → (실패 수, 다시 시도할 수 있는 수, 가장 흔한 이유) — 실패가 많은 폴더부터"""
        groups = {}
        for path, reason in self.load_failures():
            folder = os.path.dirname(path)
            entry = groups.setdefault(folder, [0, 0, {}])
            entry[0] += 1
            entry[1] += is_retryable(reason)
            kind = reason.split(":", 1)[0]
            entry[2][kind] = entry[2].get(kind, 0) + 1
        return sorted(((folder, n, r, max(kinds, key=kinds.get))
                       for folder, (n, r, kinds) in groups.items()),
                      key=lambda g: (-g[1], g[0]))

    def __len__(self):
        return len(self._items)


class LoadErrorButton(QToolButton):
    """상태바의 '⚠ 로딩 실패 N' 버튼 (없으면 숨김)"""

    def __init__(self, log, parent=None, retry=None):
        super().__init__(parent)
        self._log = log
        self._retry = retry    # retry(경로 목록) — 실패한 파일 다시 읽기
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
        FailedFilesDialog(self._log, self.window(), retry=self._retry).exec_()


class FailedFilesDialog(QDialog):
    """실패 목록: 폴더 | 파일명 | 단계 | 이유 + Show in Finder"""

    COLUMNS = ["폴더", "파일명", "단계", "이유"]

    def __init__(self, log, parent=None, retry=None):
        super().__init__(parent)
        self._log = log
        self._retry = retry
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
        hint = QLabel("시간 초과 · 클라우드 · 읽기 오류는 '⟳ 재시도'로 다시 읽을 수 있습니다 (여러 행을 고르면 그것만). "
                      "압축 형식 오류는 디코더(pylibjpeg, GDCM)가 지원하지 않거나 파일이 손상된 경우입니다. "
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
        self.retry_button = QPushButton()
        self.retry_button.setToolTip("시간 초과 · 클라우드 · 읽기 오류로 건너뛴 파일을 한도를 늘려 다시 읽습니다\n"
                                     "(DICOM이 아니거나 손상된 파일은 다시 해도 같아 제외)")
        self.retry_button.clicked.connect(self._on_retry)
        self.retry_button.setVisible(retry is not None)
        copy = QPushButton("목록 복사")
        copy.clicked.connect(self.copy)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.finder_button)
        buttons.addWidget(self.retry_button)
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

    def _retry_targets(self):
        """선택한 행 중 다시 시도할 수 있는 파일 (2개 이상 선택했으면 그것만, 아니면 전부)"""
        chosen = self.selected_paths()
        retryable = set(self._log.retryable_paths())
        if len(chosen) > 1:
            return [p for p in chosen if p in retryable]
        return sorted(retryable)

    def _on_retry(self):
        paths = self._retry_targets()
        if paths and self._retry is not None:
            self.accept()
            self._retry(paths)

    def _update_buttons(self):
        paths = self.selected_paths()
        self.finder_button.setEnabled(bool(paths))
        targets = self._retry_targets()
        self.retry_button.setText(f"⟳ 재시도 ({len(targets):,})" if targets else "⟳ 재시도")
        self.retry_button.setEnabled(bool(targets))
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


class FailedFoldersBox(QFrame):
    """시리즈 목록 아래: 불러오지 못한 파일을 폴더(≈시리즈)별로 따로 보여 주고 다시 읽기

    성공한 시리즈는 위 목록에 그대로, 읽지 못한 폴더는 여기에 — 더블클릭하면 그 폴더만 다시 읽음.
    """

    def __init__(self, log, retry, show_list, parent=None):
        super().__init__(parent)
        self._log = log
        self._retry = retry
        self.setObjectName("failedFolders")
        self.setStyleSheet("#failedFolders { border-top: 1px solid #4a3a1a; }"
                           "#failedFolders QToolButton { font-size: 11px; padding: 0 3px; }")
        from PyQt5.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)   # 시리즈 목록이 공간을 차지하게
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 2)
        layout.setSpacing(2)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("color: #ffb347; font-size: 11px; font-weight: bold;")
        head.addWidget(self.title, 1)
        self.retry_all = QToolButton()
        self.retry_all.setAutoRaise(True)
        self.retry_all.setStyleSheet("QToolButton { color: #ffcf7a; }")
        self.retry_all.clicked.connect(lambda: self._retry(self._log.retryable_paths()))
        head.addWidget(self.retry_all)
        more = QToolButton()
        more.setText("목록")
        more.setAutoRaise(True)
        more.setToolTip("파일별 이유 보기")
        more.clicked.connect(show_list)
        head.addWidget(more)
        layout.addLayout(head)
        self.list = QListWidget()
        self.list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.list.setStyleSheet("QListWidget { background: #1d1a14; color: #e8d2a8; font-size: 11px; }")
        self.list.setToolTip("더블클릭: 이 폴더의 실패한 파일 다시 읽기  ·  우클릭: 더 보기")
        self.list.itemDoubleClicked.connect(self._retry_item)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.list)
        log.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        groups = self._log.by_folder()
        self.list.clear()
        total = sum(n for _f, n, _r, _k in groups)
        retryable = sum(r for _f, _n, r, _k in groups)
        self.setVisible(bool(groups))
        if not groups:
            return
        self.title.setText(f"⚠ 불러오지 못한 파일 {total:,}개 · 폴더 {len(groups)}개")
        self.retry_all.setText(f"⟳ 재시도 {retryable:,}" if retryable else "")
        self.retry_all.setVisible(bool(retryable))
        for folder, n, r, kind in groups:
            name = os.path.basename(folder) or folder
            item = QListWidgetItem(f"📁 {name} — {n:,}개 ({kind})" + ("" if r else "  · 재시도 불가"))
            item.setData(Qt.UserRole, folder)
            item.setToolTip(f"{folder}\n{n:,}개 실패, 그중 {r:,}개는 다시 시도할 수 있음\n더블클릭: 다시 읽기")
            if not r:
                item.setForeground(Qt.gray)
            self.list.addItem(item)
        row = self.list.sizeHintForRow(0) if self.list.count() else 18
        self.list.setFixedHeight(min(5, self.list.count()) * row + 6)   # 최대 5줄, 나머지는 스크롤

    def _retry_item(self, item):
        paths = self._log.retryable_paths(item.data(Qt.UserRole))
        if paths:
            self._retry(paths)

    def _menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        folder = item.data(Qt.UserRole)
        menu = QMenu(self)
        paths = self._log.retryable_paths(folder)
        again = menu.addAction(f"⟳ 이 폴더 다시 읽기 ({len(paths):,})")
        again.setEnabled(bool(paths))
        reveal = menu.addAction("Finder에서 보기" if sys.platform == "darwin" else "폴더 열기")
        chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
        if chosen is again:
            self._retry(paths)
        elif chosen is reveal:
            reveal_in_file_manager(folder)
