# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Library 내보내기 대화상자: 범위 · 형식 · 포함 항목 · 출력 경로 · 미리보기"""
import os

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QTextBrowser,
                             QVBoxLayout)

from . import library_export as LE


class LibraryExportDialog(QDialog):
    def __init__(self, store, selected=(), collection=None, measure_provider=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.selected = list(selected)
        self.measure_provider = measure_provider
        self.setWindowTitle("Library 내보내기")
        self.resize(760, 640)
        layout = QVBoxLayout(self)

        scope = QGroupBox("범위")
        sl = QGridLayout(scope)
        self.all = QRadioButton(f"전체 라이브러리 ({len(store.studies)}개)")
        self.coll = QRadioButton("컬렉션:")
        self.sel = QRadioButton(f"선택한 스터디 ({len(self.selected)}개)")
        self.sel.setEnabled(bool(self.selected))
        group = QButtonGroup(self)
        for b in (self.all, self.coll, self.sel):
            group.addButton(b)
        self.collection = QComboBox()
        paths = LE.collection_paths(store)
        for cid, name in sorted(paths.items(), key=lambda kv: kv[1].lower()):
            self.collection.addItem(f"{name} ({store.count_in(cid)})", cid)
        self.coll.setEnabled(bool(paths))
        sl.addWidget(self.all, 0, 0, 1, 2)
        sl.addWidget(self.coll, 1, 0)
        sl.addWidget(self.collection, 1, 1)
        sl.addWidget(self.sel, 2, 0, 1, 2)
        (self.sel if self.selected else self.coll if collection in paths else self.all).setChecked(True)
        if collection in paths:
            self.collection.setCurrentIndex(self.collection.findData(collection))
        layout.addWidget(scope)

        form = QFormLayout()
        self.format = QComboBox()
        for key, text in LE.FORMATS:
            self.format.addItem(text, key)
        self.format.currentIndexChanged.connect(self._format_changed)
        form.addRow("형식:", self.format)
        path_row = QHBoxLayout()
        self.path = QLineEdit()
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        form.addRow("출력 경로:", path_row)
        layout.addLayout(form)

        include = QGroupBox("포함할 항목")
        il = QGridLayout(include)
        self.include = {}
        for i, (key, text) in enumerate(LE.INCLUDE_KEYS):
            cb = QCheckBox(text)
            cb.setChecked(key != "measurements" or measure_provider is not None)
            if key == "measurements" and measure_provider is None:
                cb.setEnabled(False)
            if key == "measurements":
                cb.setToolTip("불러온 스터디의 ROI·측정만 (불러오지 않은 스터디는 비어 있음)")
            cb.toggled.connect(self.preview)
            self.include[key] = cb
            il.addWidget(cb, i // 2, i % 2)
        layout.addWidget(include)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #ffb347;")
        layout.addWidget(self.note)
        layout.addWidget(QLabel("미리보기"))
        self.view = QTextBrowser()
        layout.addWidget(self.view, 1)
        buttons = QHBoxLayout()
        refresh = QPushButton("미리보기 새로 고침")
        refresh.clicked.connect(self.preview)
        self.go = QPushButton("내보내기")
        self.go.setDefault(True)
        self.go.clicked.connect(self.run)
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        buttons.addWidget(refresh)
        buttons.addStretch()
        buttons.addWidget(self.go)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        for b in (self.all, self.coll, self.sel):
            b.toggled.connect(self.preview)
        self.collection.currentIndexChanged.connect(self.preview)
        self._format_changed()
        self.preview()

    # ─── 상태 ───
    def fmt(self):
        return self.format.currentData()

    def uids(self):
        if self.sel.isChecked():
            return [u for u in self.selected if u in self.store.studies]
        if self.coll.isChecked() and self.collection.currentData():
            return self.store.search("", self.collection.currentData())
        return self.store.search("")

    def included(self):
        return {k: cb.isChecked() for k, cb in self.include.items()}

    def _default_path(self):
        folder = os.path.expanduser("~/Documents")
        stamp = "dabbaview_library"
        if self.fmt() in ("png_each", "jpg_each"):
            return os.path.join(folder, stamp + "_images")
        return os.path.join(folder, f"{stamp}.{LE.EXTENSIONS.get(self.fmt(), 'txt')}")

    def _format_changed(self):
        fmt = self.fmt()
        current = self.path.text().strip()
        if not current or os.path.basename(current).startswith("dabbaview_library"):
            self.path.setText(self._default_path())
        elif fmt not in ("png_each", "jpg_each"):
            root, _ext = os.path.splitext(current)
            self.path.setText(root + "." + LE.EXTENSIONS.get(fmt, "txt"))
        self.note.setText(LE.HWP_MESSAGE if fmt == "hwp" else
                          ("폴더에 스터디마다 이미지 파일을 만듭니다." if fmt in ("png_each", "jpg_each") else
                           "JSON은 태그·컬렉션·메모를 포함한 백업이며 Library → 가져오기로 복원합니다."
                           if fmt == "json" else ""))
        self.go.setEnabled(fmt != "hwp")

    def _browse(self):
        if self.fmt() in ("png_each", "jpg_each"):
            path = QFileDialog.getExistingDirectory(self, "이미지를 저장할 폴더", os.path.dirname(self.path.text()))
        else:
            ext = LE.EXTENSIONS.get(self.fmt(), "txt")
            path, _ = QFileDialog.getSaveFileName(self, "저장", self.path.text(), f"*.{ext}")
        if path:
            self.path.setText(path)

    def preview(self):
        records = LE.build_records(self.store, self.uids(), self.included(), None, want_thumbnails=False)
        self.view.setHtml(LE.preview_html(records, self.included()))

    def run(self):
        uids = self.uids()
        if not uids:
            QMessageBox.information(self, "내보내기", "내보낼 스터디가 없습니다.")
            return
        path = self.path.text().strip()
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            records = LE.build_records(self.store, uids, self.included(), self.measure_provider)
            out = LE.export(self.fmt(), records, self.included(), path, self.store)
        except LE.UnsupportedFormat as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "내보내기", str(e))
            return
        except Exception as e:  # noqa: BLE001 - 쓰기 오류는 알림
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "내보내기", f"내보내지 못했습니다: {e}")
            return
        QApplication.restoreOverrideCursor()
        self.last_output = out
        box = QMessageBox(self)
        box.setWindowTitle("내보내기")
        box.setText(f"스터디 {len(records)}개를 내보냈습니다.\n{out}")
        open_button = box.addButton("열기", QMessageBox.AcceptRole)
        box.addButton("닫기", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))
