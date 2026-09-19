# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""AI 패널 → Models 탭: 오픈소스 모델 목록·상태·원클릭 설치·실행

TotalSegmentator / nnU-Net: 전용 Python 환경에 pip 설치 후 명령줄로 실행
MONAI Label: 기존 '🤖 모델' 탭 (서버)
MedSAM: ONNX 인코더/디코더 → 세그멘트 탭의 🎯 MedSAM 도구 (클릭 한 번)
사용자 ONNX: Settings에 지정한 .onnx → 로컬 추론
REST API: Settings에 지정한 주소로 볼륨 전송 → 라벨 수신
결과는 세그멘테이션 오버레이 + 라벨 목록(구조 이름·색)에 자동 등록
"""
import os

import numpy as np
from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QGroupBox,
                             QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import model_hub as hub
from .volume import load_volume

ROWS = [
    ("totalseg", "TotalSegmentator", "pip · 로컬", "CT/MR 전신 해부학 구조 자동 세그멘테이션"),
    ("nnunet", "nnU-Net v2", "pip · 로컬", "범용 세그멘테이션 (사전학습 모델 폴더 지정)"),
    ("monai", "MONAI Label", "서버", "Active learning 서버 (🤖 모델 탭)"),
    ("medsam", "MedSAM", "ONNX · 로컬", "클릭 한 번으로 관심 영역 (Segment Anything)"),
    ("onnx", "사용자 ONNX", "ONNX · 로컬", "Settings에 지정한 .onnx 세그멘테이션 모델"),
    ("rest", "REST API", "원격", "Settings에 지정한 서버로 추론 요청"),
]


class ModelsTab(QWidget):
    log_line = pyqtSignal(str)   # 작업 스레드 → 로그 (스레드 안전)

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.main = panel.main
        self._status = {}
        layout = QVBoxLayout(self)

        env_row = QHBoxLayout()
        self.env_label = QLabel()
        self.env_label.setWordWrap(True)
        self.env_label.setStyleSheet("color: #9ab;")
        env_row.addWidget(self.env_label, 1)
        for text, slot in (("새로 고침", self.refresh), ("설정…", self.open_settings),
                           ("환경 폴더", self.open_env_folder)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            env_row.addWidget(b)
        layout.addLayout(env_row)

        self.table = QTableWidget(len(ROWS), 4)
        self.table.setHorizontalHeaderLabels(["모델", "종류", "상태", "동작"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self._buttons = {}
        for r, (key, name, kind, desc) in enumerate(ROWS):
            item = QTableWidgetItem(name)
            item.setToolTip(desc)
            self.table.setItem(r, 0, item)
            self.table.setItem(r, 1, QTableWidgetItem(kind))
            self.table.setItem(r, 2, QTableWidgetItem("…"))
            cell = QWidget()
            row = QHBoxLayout(cell)
            row.setContentsMargins(2, 0, 2, 0)
            buttons = {}
            for action, text in self._actions(key):
                b = QPushButton(text)
                b.clicked.connect(lambda _=False, k=key, a=action: self.do(k, a))
                row.addWidget(b)
                buttons[action] = b
            self._buttons[key] = buttons
            self.table.setCellWidget(r, 3, cell)
        self.table.setMinimumHeight(self.table.verticalHeader().defaultSectionSize() * 7 + 8)
        layout.addWidget(self.table)

        opts = QGroupBox("TotalSegmentator 옵션")
        form = QFormLayout(opts)
        self.task = QComboBox()
        for key, text in hub.TOTALSEG_TASKS:
            self.task.addItem(text, key)
        form.addRow("과제:", self.task)
        self.fast = QCheckBox("빠르게 (--fast, 3 mm 해상도)")
        form.addRow(self.fast)
        layout.addWidget(opts)

        out_row = QHBoxLayout()
        export = QPushButton("📦 결과 내보내기 (NIfTI / DICOM SEG)…")
        export.clicked.connect(self.export_result)
        help_rest = QPushButton("REST 형식")
        help_rest.clicked.connect(lambda: QMessageBox.information(self, "REST API 형식",
                                                                  hub.REST_PROTOCOL))
        out_row.addWidget(export, 1)
        out_row.addWidget(help_rest)
        layout.addLayout(out_row)

        layout.addWidget(QLabel("실행 로그"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setStyleSheet("font-family: Menlo, monospace; font-size: 11px;")
        layout.addWidget(self.log, 1)
        self.log_line.connect(self.log.appendPlainText)
        self._refreshed = False   # 상태 확인(외부 Python 실행)은 탭을 처음 열 때

    # ─── 상태 ───
    def settings(self):
        return self.main._app_settings

    def runtime(self):
        return hub.ModelRuntime(self.settings().model_value("python"))

    @staticmethod
    def _actions(key):
        return {"totalseg": [("install", "설치"), ("run", "실행")],
                "nnunet": [("install", "설치"), ("run", "실행")],
                "monai": [("open", "열기")],
                "medsam": [("tool", "🎯 도구"), ("settings", "설정")],
                "onnx": [("run", "실행"), ("settings", "설정")],
                "rest": [("run", "실행"), ("settings", "설정")]}[key]

    def showEvent(self, event):
        super().showEvent(event)
        if not self._refreshed:
            self._refreshed = True
            self.refresh()

    def refresh(self):
        runtime = self.runtime()
        self.env_label.setText(runtime.describe())
        get = self.settings().model_value
        status = {}
        for key in ("totalseg", "nnunet"):
            version = runtime.package_version(hub.PIP_MODELS[key]["package"])
            status[key] = (f"✅ 설치됨 {version}" if version else "⬜ 미설치", bool(version))
        if status["nnunet"][1]:
            folder = get("nnunet_folder")
            status["nnunet"] = (status["nnunet"][0] + (" · 모델 폴더 지정됨" if folder and os.path.isdir(folder)
                                                         else " · 모델 폴더 필요 (설정)"), True)
        url = self.settings().monai_url()
        status["monai"] = (("✅ 서버 " + url) if url else "⬜ 서버 주소 없음 (Settings → AI)", bool(url))
        enc, dec = get("medsam_encoder"), get("medsam_decoder")
        ok = bool(enc and dec and os.path.exists(enc) and os.path.exists(dec))
        status["medsam"] = ("✅ ONNX 파일 지정됨" if ok else "⬜ 인코더/디코더 .onnx 필요", ok)
        paths = [p for p in self.settings().onnx_model_paths() if os.path.exists(p)]
        status["onnx"] = ((f"✅ {len(paths)}개: " + ", ".join(os.path.basename(p) for p in paths))
                          if paths else "⬜ .onnx 경로 없음", bool(paths))
        rest = get("rest_url")
        status["rest"] = (("✅ " + rest) if rest else "⬜ 주소 없음", bool(rest))
        self._status = status
        for r, (key, *_rest) in enumerate(ROWS):
            text, ready = status[key]
            item = QTableWidgetItem(text)
            item.setForeground(Qt.green if ready else Qt.gray)
            self.table.setItem(r, 2, item)
            buttons = self._buttons[key]
            if "install" in buttons:
                buttons["install"].setText("업데이트" if ready else "설치")
            if "run" in buttons:
                buttons["run"].setEnabled(ready)
        self.panel.refresh_medsam_state()

    def open_settings(self):
        self.main._open_settings(tab="ai")
        self.refresh()

    def open_env_folder(self):
        runtime = self.runtime()
        folder = os.path.dirname(os.path.dirname(runtime.python())) if runtime.python() else runtime.env_dir
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ─── 동작 ───
    def do(self, key, action):
        if action == "install":
            self.install(key)
        elif action == "settings":
            self.open_settings()
        elif key == "monai":
            self.panel.tabs.setCurrentIndex(self.panel.tabs.indexOf(self.panel.model_tab_page))
        elif key == "medsam":
            self.panel.enable_medsam()
        elif key == "totalseg":
            self.run_totalseg()
        elif key == "nnunet":
            self.run_nnunet()
        elif key == "onnx":
            self.run_onnx()
        elif key == "rest":
            self.run_rest()

    def _log(self, text):
        self.log_line.emit(text)

    def install(self, key):
        info = hub.PIP_MODELS[key]
        runtime = self.runtime()
        where = runtime.python() or runtime.env_dir
        if QMessageBox.question(
                self, f"{info['name']} 설치",
                f"{info['name']}을(를) 설치할까요?\n\n다운로드: {info['size']}\n설치 위치: {where}\n\n"
                "인터넷 연결이 필요하며 몇 분 걸릴 수 있습니다. 진행 중에 취소할 수 있습니다.") \
                != QMessageBox.Yes:
            return
        self.log.clear()

        def task(progress, cancelled):
            progress(f"{info['name']} 설치 중… (로그 참고)", 0.05)
            runtime.install(info["package"], self._log, cancelled)
            return runtime.package_version(info["package"])

        def done(version):
            self._log(f"✅ 설치 완료: {info['name']} {version or ''}")
            self.refresh()
        self.panel._run_task(f"{info['name']} 설치 중…", task, done)

    def _series(self):
        series, _k, _window = self.panel._require_series()
        return series

    def _run_volume_model(self, title, fn):
        """현재 시리즈 볼륨 → fn(volume, log, progress, cancelled) → (마스크, 이름) → 오버레이"""
        series = self._series()
        if series is None:
            return
        self.log.clear()
        self._log(f"▶ {title}: {series.description} ({series.num_slices} slices)")

        def task(progress, cancelled):
            progress("볼륨 읽는 중…", 0.01)
            volume = load_volume(series)
            return fn(volume, self._log, lambda f, text=title: progress(text, f), cancelled)

        def done(result):
            mask, names = result
            n = self.panel.apply_model_result(series, mask, names, title)
            self._log(f"✅ {title}: 구조 {n}개를 라벨로 등록")
        self.panel._run_task(f"{title} 실행 중…", task, done)

    def run_totalseg(self):
        runtime, task = self.runtime(), self.task.currentData()
        fast, device = self.fast.isChecked(), self.settings().model_value("device")
        self._run_volume_model(
            "TotalSegmentator",
            lambda vol, log, progress, cancelled: hub.run_totalsegmentator(
                runtime, vol, task, fast, device, log,
                lambda f, _t=None: progress(f), cancelled))

    def run_nnunet(self):
        runtime, get = self.runtime(), self.settings().model_value
        folder, folds, device = get("nnunet_folder"), get("nnunet_folds"), get("device")
        self._run_volume_model(
            "nnU-Net",
            lambda vol, log, progress, cancelled: hub.run_nnunet(
                runtime, vol, folder, folds, device, log,
                lambda f, _t=None: progress(f), cancelled))

    def run_rest(self):
        url = self.settings().model_value("rest_url")
        from ..cloud.secure_store import SecureStore
        token = SecureStore(self.settings()._qs).get("models_rest_token") or ""
        series = self._series()
        modality = series.modality if series is not None else ""
        self._run_volume_model(
            "REST API",
            lambda vol, log, progress, cancelled: hub.run_rest(url, token, vol, modality, log))

    def run_onnx(self):
        paths = [p for p in self.settings().onnx_model_paths() if os.path.exists(p)]
        if not paths:
            QMessageBox.information(self, "ONNX", "Settings → AI → '사용자 ONNX 모델'에 .onnx 경로를 지정하세요.")
            return
        path = paths[0]
        if len(paths) > 1:
            from PyQt5.QtWidgets import QInputDialog
            names = [os.path.basename(p) for p in paths]
            name, ok = QInputDialog.getItem(self, "ONNX 모델", "실행할 모델:", names, 0, False)
            if not ok:
                return
            path = paths[names.index(name)]
        self.panel.run_onnx_file(path)

    def export_result(self):
        series = self._series()
        if series is None:
            return
        case = self.panel.ctl.case(series)
        if case is None or case.is_empty():
            QMessageBox.information(self, "내보내기", "현재 시리즈에 세그멘테이션 결과가 없습니다.")
            return
        self.panel.prepare_export(("nifti", "dicom_seg"))


def label_values(mask):
    return [int(v) for v in np.unique(mask) if v]
