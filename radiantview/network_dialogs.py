"""
DICOM Send / DICOM Print 대화상자 (네트워크 작업은 백그라운드 스레드)
"""
import threading

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QComboBox, QSpinBox, QPushButton, QLabel,
                             QProgressBar, QMessageBox, QRadioButton,
                             QButtonGroup, QGroupBox)
from PyQt5.QtCore import QThread, pyqtSignal

from . import dicom_net as net
from .render import render_8bit


class NetworkWorker(QThread):
    """fn(progress, cancel_event) 을 백그라운드에서 실행"""

    progress = pyqtSignal(int, int)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.cancel_event = threading.Event()

    def run(self):
        try:
            message = self._fn(lambda a, b: self.progress.emit(a, b), self.cancel_event)
            self.succeeded.emit(message)
        except net.Cancelled:
            self.failed.emit("취소되었습니다.")
        except Exception as e:  # 네트워크/프로토콜 오류를 사용자에게 그대로 표시
            self.failed.emit(str(e) or e.__class__.__name__)


class _NetworkDialog(QDialog):
    """노드 선택 + 대상 범위 + 진행률 공통 부분"""

    node_type = "storage"
    title = ""
    action_text = ""

    def __init__(self, app_settings, sources, parent=None, open_settings=None):
        """sources: {'image': [(series, index, window)], 'series': [...], 'key': [...]}"""
        super().__init__(parent)
        self.setWindowTitle(self.title)
        self.setMinimumWidth(460)
        self._settings = app_settings
        self._sources = sources
        self._open_settings = open_settings
        self._worker = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        node_row = QHBoxLayout()
        self._node_combo = QComboBox()
        node_row.addWidget(self._node_combo, 1)
        manage = QPushButton("노드 관리…")
        manage.clicked.connect(self._manage_nodes)
        node_row.addWidget(manage)
        form.addRow("대상:", node_row)
        self._reload_nodes()
        layout.addLayout(form)

        scope_box = QGroupBox("범위")
        scope_layout = QVBoxLayout(scope_box)
        self._scope = QButtonGroup(self)
        labels = {"image": "현재 영상", "series": "현재 시리즈 전체", "key": "Key Image"}
        for i, key in enumerate(("image", "series", "key")):
            count = len(sources.get(key, []))
            radio = QRadioButton(f"{labels[key]} ({count}장)")
            radio.setEnabled(count > 0)
            radio.setProperty("scope", key)
            self._scope.addButton(radio, i)
            scope_layout.addWidget(radio)
        for radio in self._scope.buttons():
            if radio.isEnabled():
                radio.setChecked(True)
                break
        layout.addWidget(scope_box)

        self._extra_options(layout)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        buttons = QHBoxLayout()
        self._echo_btn = QPushButton("연결 확인 (C-ECHO)")
        self._echo_btn.clicked.connect(self._verify)
        buttons.addWidget(self._echo_btn)
        buttons.addStretch()
        self._run_btn = QPushButton(self.action_text)
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self._start)
        buttons.addWidget(self._run_btn)
        self._close_btn = QPushButton("닫기")
        self._close_btn.clicked.connect(self.reject)
        buttons.addWidget(self._close_btn)
        layout.addLayout(buttons)

    def _extra_options(self, layout):
        pass

    def _reload_nodes(self):
        self._node_combo.clear()
        for node in self._settings.dicom_nodes():
            if node.get("type", "storage") == self.node_type:
                self._node_combo.addItem(
                    f"{node['name']}  ({node['ae_title']}@{node['host']}:{node['port']})", node)
        if self._node_combo.count() == 0:
            self._node_combo.addItem("(등록된 노드 없음 — '노드 관리'에서 추가)", None)

    def _manage_nodes(self):
        if self._open_settings:
            self._open_settings("nodes")
            self._reload_nodes()

    def _node(self):
        node = self._node_combo.currentData()
        if not node:
            QMessageBox.information(self, self.title, "먼저 대상 노드를 추가하세요.")
        return node

    def _selected_items(self):
        button = self._scope.checkedButton()
        return self._sources.get(button.property("scope"), []) if button else []

    def _verify(self):
        node = self._node()
        if node:
            self._run(lambda progress, cancel: self._echo_message(node), busy="연결 확인 중…")

    def _echo_message(self, node):
        ok, message = net.echo(node, self._settings.local_ae_title())
        if not ok:
            raise net.DicomNetError(message)
        return message

    def _run(self, fn, busy):
        self._set_busy(True, busy)
        self._worker = NetworkWorker(fn, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(lambda m: self._finish(True, m))
        self._worker.failed.connect(lambda m: self._finish(False, m))
        self._worker.start()

    def _on_progress(self, done, total):
        self._progress.setVisible(True)
        self._progress.setMaximum(total)
        self._progress.setValue(done)

    def _set_busy(self, busy, text=""):
        self._run_btn.setEnabled(not busy)
        self._echo_btn.setEnabled(not busy)
        self._close_btn.setText("취소" if busy else "닫기")
        self._status.setText(text)

    def _finish(self, ok, message):
        self._worker = None
        self._set_busy(False)
        self._status.setText(("✅ " if ok else "❌ ") + message)
        self.last_result = (ok, message)

    def reject(self):
        if self._worker is not None:
            self._worker.cancel_event.set()
            self._worker.wait(20000)
            return
        super().reject()

    def _start(self):
        raise NotImplementedError


class DicomSendDialog(_NetworkDialog):
    node_type = "storage"
    title = "DICOM Send"
    action_text = "전송"

    def _start(self):
        node = self._node()
        items = self._selected_items()
        if not node or not items:
            return
        local_ae = self._settings.local_ae_title()

        def job(progress, cancel):
            datasets = [series.get_full_dataset(index) for series, index, _ in items]
            sent, failed = net.send_datasets(node, local_ae, datasets, progress, cancel)
            if failed:
                raise net.DicomNetError(f"{sent}장 전송, {failed}장 실패")
            return f"{sent}장 전송 완료 → {node['ae_title']}"

        self._run(job, busy=f"{len(items)}장 전송 중…")


class DicomPrintDialog(_NetworkDialog):
    node_type = "print"
    title = "DICOM Print"
    action_text = "인쇄"

    def _extra_options(self, layout):
        box = QGroupBox("필름")
        form = QFormLayout(box)

        def combo(values, default):
            c = QComboBox()
            c.addItems(values)
            c.setCurrentText(default)
            return c
        self._film_size = combo(net.FILM_SIZES, "14INX17IN")
        self._film_format = combo(net.FILM_FORMATS, "2,2")
        self._orientation = combo(net.ORIENTATIONS, "PORTRAIT")
        self._medium = combo(net.MEDIUM_TYPES, "BLUE FILM")
        self._magnification = combo(net.MAGNIFICATION_TYPES, "REPLICATE")
        self._copies = QSpinBox()
        self._copies.setRange(1, 99)
        form.addRow("Film Size:", self._film_size)
        form.addRow("Format (열,행):", self._film_format)
        form.addRow("Orientation:", self._orientation)
        form.addRow("Medium:", self._medium)
        form.addRow("Magnification:", self._magnification)
        form.addRow("Copies:", self._copies)
        layout.addWidget(box)

    def options(self):
        return {"film_size": self._film_size.currentText(),
                "film_format": self._film_format.currentText(),
                "orientation": self._orientation.currentText(),
                "medium": self._medium.currentText(),
                "magnification": self._magnification.currentText(),
                "copies": self._copies.value()}

    def _start(self):
        node = self._node()
        items = self._selected_items()
        if not node or not items:
            return
        local_ae = self._settings.local_ae_title()
        options = self.options()

        def job(progress, cancel):
            images = [render_8bit(series, index, window) for series, index, window in items]
            images = [img for img in images if img is not None]
            films = net.print_images(node, local_ae, images, options, progress, cancel)
            return f"{len(images)}장 → 필름 {films}매 인쇄 요청 완료 ({node['ae_title']})"

        self._run(job, busy=f"{len(items)}장 인쇄 준비 중…")
