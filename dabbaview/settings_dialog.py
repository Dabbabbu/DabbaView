# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Settings 대화상자: 마우스 매핑 / W/L 프리셋 / Hanging Protocol / DICOM 노드
"""
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
                             QWidget, QFormLayout, QComboBox, QSpinBox,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QCheckBox, QLineEdit, QLabel,
                             QDialogButtonBox, QMessageBox, QAbstractItemView)
from PyQt5.QtCore import Qt

from .app_settings import (MOUSE_BINDING_LABELS, DEFAULT_MOUSE_BINDINGS,
                           DEFAULT_WINDOW_PRESETS, DEFAULT_HANGING_PROTOCOLS,
                           ROI_WINDOW_METHODS)
from .multi_viewport import LAYOUTS
from . import dicom_net as net


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    return t


def _cell(table, row, col):
    item = table.item(row, col)
    return item.text().strip() if item else ""


def _table_buttons(table, new_row, defaults=None):
    row = QHBoxLayout()
    add = QPushButton("추가")
    add.clicked.connect(lambda: _append_row(table, new_row()))
    delete = QPushButton("삭제")
    delete.clicked.connect(lambda: [table.removeRow(r) for r in
                                    sorted({i.row() for i in table.selectedIndexes()},
                                           reverse=True)])
    row.addWidget(add)
    row.addWidget(delete)
    if defaults:
        reset = QPushButton("기본값으로")
        reset.clicked.connect(defaults)
        row.addWidget(reset)
    row.addStretch()
    return row


def _append_row(table, values):
    r = table.rowCount()
    table.insertRow(r)
    for c, v in enumerate(values):
        table.setItem(r, c, QTableWidgetItem(str(v)))
    table.setCurrentCell(r, 0)
    return r


class SettingsDialog(QDialog):

    TABS = ("mouse", "presets", "hanging", "nodes", "reading", "ai", "cloud", "cache")

    def __init__(self, app_settings, parent=None, tab="mouse"):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(720, 520)
        self._settings = app_settings

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._mouse_tab(), "Mouse")
        self._tabs.addTab(self._presets_tab(), "W/L Presets")
        self._tabs.addTab(self._hanging_tab(), "Hanging Protocols")
        self._tabs.addTab(self._nodes_tab(), "DICOM Nodes")
        self._tabs.addTab(self._reading_tab(), "Reading")
        self._tabs.addTab(self._ai_tab(), "AI")
        self._tabs.addTab(self._cloud_tab(), "Cloud")
        self._tabs.addTab(self._cache_tab(), "Cache")
        if tab in self.TABS:
            self._tabs.setCurrentIndex(self.TABS.index(tab))
        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ─── 마우스 ───
    def _mouse_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("버튼별 동작은 선택한 도구와 관계없이 항상 적용됩니다.\n"
                                "좌클릭 드래그의 '선택한 도구'는 툴바에서 고른 도구를 뜻합니다."))
        form = QFormLayout()
        self._mouse_combos = {}
        for key, (label, choices) in MOUSE_BINDING_LABELS.items():
            combo = QComboBox()
            for value, text in choices.items():
                combo.addItem(text, value)
            combo.setCurrentIndex(max(0, combo.findData(self._settings.mouse.get(key))))
            self._mouse_combos[key] = combo
            form.addRow(label + ":", combo)
        self._fast_step = QSpinBox()
        self._fast_step.setRange(1, 50)
        self._fast_step.setSuffix(" 장")
        self._fast_step.setValue(self._settings.mouse.get("fast_scroll_step"))
        form.addRow("빠른 이동 간격:", self._fast_step)
        self._roi_method = QComboBox()
        for value, text in ROI_WINDOW_METHODS.items():
            self._roi_method.addItem(text, value)
        self._roi_method.setCurrentIndex(
            max(0, self._roi_method.findData(self._settings.mouse.get("roi_window_method"))))
        form.addRow("ROI 자동 W/L 계산:", self._roi_method)
        layout.addLayout(form)
        reset = QPushButton("PACS 표준 기본값으로")
        reset.clicked.connect(self._reset_mouse)
        layout.addWidget(reset, alignment=Qt.AlignLeft)
        layout.addStretch()
        return page

    def _reset_mouse(self):
        for key, combo in self._mouse_combos.items():
            combo.setCurrentIndex(combo.findData(DEFAULT_MOUSE_BINDINGS[key]))
        self._fast_step.setValue(DEFAULT_MOUSE_BINDINGS["fast_scroll_step"])
        self._roi_method.setCurrentIndex(
            self._roi_method.findData(DEFAULT_MOUSE_BINDINGS["roi_window_method"]))

    # ─── 프리셋 ───
    def _presets_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self._preset_table = _table(["Name", "Center (L)", "Width (W)"])
        self._fill_presets(self._settings.window_presets())
        layout.addWidget(self._preset_table)
        layout.addLayout(_table_buttons(self._preset_table, lambda: ["New", 40, 400],
                                        lambda: self._fill_presets(DEFAULT_WINDOW_PRESETS)))
        return page

    def _fill_presets(self, presets):
        self._preset_table.setRowCount(0)
        for p in presets:
            _append_row(self._preset_table, [p["name"], f"{p['center']:g}", f"{p['width']:g}"])

    def presets_from_table(self):
        presets = []
        t = self._preset_table
        for r in range(t.rowCount()):
            name = _cell(t, r, 0)
            try:
                center, width = float(_cell(t, r, 1)), float(_cell(t, r, 2))
            except ValueError:
                raise ValueError(f"프리셋 {r + 1}행: Center/Width는 숫자여야 합니다.")
            if not name:
                raise ValueError(f"프리셋 {r + 1}행: 이름이 비어 있습니다.")
            if width <= 0:
                raise ValueError(f"프리셋 '{name}': Width는 0보다 커야 합니다.")
            presets.append({"name": name, "center": center, "width": width})
        return presets

    # ─── Hanging Protocol ───
    def _hanging_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self._auto_hanging = QCheckBox("폴더를 열 때 자동 적용")
        self._auto_hanging.setChecked(self._settings.auto_hanging())
        layout.addWidget(self._auto_hanging)
        layout.addWidget(QLabel(
            "Body: StudyDescription/BodyPartExamined/시리즈 설명에 포함될 키워드 ('|'로 구분)\n"
            "Slots: 칸 순서대로 SeriesDescription 키워드, 쉼표로 구분 (예: T1, T2, FLAIR, DWI)\n"
            f"Layout: {', '.join(LAYOUTS)}"))
        self._hanging_table = _table(["Name", "Modality", "Body", "Layout", "Slots"])
        self._fill_hanging(self._settings.hanging_protocols())
        layout.addWidget(self._hanging_table)
        layout.addLayout(_table_buttons(
            self._hanging_table, lambda: ["New Protocol", "MR", "", "2x2", "T1, T2"],
            lambda: self._fill_hanging(DEFAULT_HANGING_PROTOCOLS)))
        return page

    def _fill_hanging(self, protocols):
        self._hanging_table.setRowCount(0)
        for p in protocols:
            _append_row(self._hanging_table, [p.get("name", ""), p.get("modality", ""),
                                              p.get("body", ""), p.get("layout", "2x2"),
                                              ", ".join(p.get("slots", []))])

    def hanging_from_table(self):
        protocols = []
        t = self._hanging_table
        for r in range(t.rowCount()):
            layout = _cell(t, r, 3).lower()
            if layout not in LAYOUTS:
                raise ValueError(f"프로토콜 {r + 1}행: Layout '{layout}'은(는) 지원하지 않습니다.")
            protocols.append({"name": _cell(t, r, 0) or f"Protocol {r + 1}",
                              "modality": _cell(t, r, 1).upper(),
                              "body": _cell(t, r, 2), "layout": layout,
                              "slots": [s.strip() for s in _cell(t, r, 4).split(",")]})
        return protocols

    # ─── DICOM 노드 ───
    def _nodes_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._local_ae = QLineEdit(self._settings.local_ae_title())
        self._local_ae.setMaxLength(16)
        form.addRow("이 컴퓨터 AE Title:", self._local_ae)
        layout.addLayout(form)
        self._node_table = _table(["Name", "AE Title", "Host", "Port", "Type"])
        for n in self._settings.dicom_nodes():
            _append_row(self._node_table, [n.get("name", ""), n.get("ae_title", ""),
                                           n.get("host", ""), n.get("port", 104),
                                           n.get("type", "storage")])
        layout.addWidget(self._node_table)
        buttons = _table_buttons(self._node_table,
                                 lambda: ["PACS", "PACS_AE", "127.0.0.1", 104, "storage"])
        echo = QPushButton("선택 노드 C-ECHO")
        echo.clicked.connect(self._echo_selected)
        buttons.insertWidget(2, echo)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("Type: storage (DICOM Send) 또는 print (DICOM Print)"))
        return page

    def nodes_from_table(self):
        nodes = []
        t = self._node_table
        for r in range(t.rowCount()):
            try:
                port = int(_cell(t, r, 3))
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                raise ValueError(f"노드 {r + 1}행: Port는 1~65535 숫자여야 합니다.")
            node_type = _cell(t, r, 4).lower() or "storage"
            if node_type not in ("storage", "print"):
                raise ValueError(f"노드 {r + 1}행: Type은 storage 또는 print 입니다.")
            ae = _cell(t, r, 1)
            if not ae or len(ae) > 16:
                raise ValueError(f"노드 {r + 1}행: AE Title은 1~16자여야 합니다.")
            nodes.append({"name": _cell(t, r, 0) or ae, "ae_title": ae,
                          "host": _cell(t, r, 2), "port": port, "type": node_type})
        return nodes

    def _echo_selected(self):
        rows = sorted({i.row() for i in self._node_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "C-ECHO", "노드를 선택하세요.")
            return
        try:
            node = self.nodes_from_table()[rows[0]]
        except ValueError as e:
            QMessageBox.warning(self, "C-ECHO", str(e))
            return
        ok, message = net.echo(node, self._local_ae.text().strip() or "DABBAVIEW")
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "C-ECHO", f"{node['ae_title']}@{node['host']}:{node['port']}\n{message}")

    # ─── Reading ───
    def _reading_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        folder_row = QHBoxLayout()
        self._report_folder = QLineEdit(self._settings.report_folder())
        self._report_folder.setPlaceholderText("판독문 파일이 저장되는 폴더 (선택)")
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse_report_folder)
        folder_row.addWidget(self._report_folder, 1)
        folder_row.addWidget(browse)
        form.addRow("판독문 폴더:", folder_row)
        self._report_creator = QLineEdit(self._settings.report_creator())
        form.addRow("기본 Creator:", self._report_creator)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "판독문 폴더를 지정하면 새 파일을 감시해서 불러온 검사와 자동으로 연결합니다.\n"
            "매칭: 파일명(또는 폴더명)에 PatientID와 검사일(YYYYMMDD)이 있으면 그 검사,\n"
            "날짜가 없으면 그 환자의 검사가 하나일 때만 연결합니다. DICOM SR은 StudyInstanceUID로 연결합니다.\n"
            "예: 1234567_20260917_report.txt\n"
            "지원: .txt .rtf .jpg .png .bmp .tiff .pdf .dcm(SR)"))
        layout.addStretch()
        return page

    def _ai_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._monai_url = QLineEdit(self._settings.monai_url())
        self._monai_url.setPlaceholderText("http://127.0.0.1:8000")
        form.addRow("MONAI Label 서버:", self._monai_url)
        self._monai_token = QLineEdit(self._settings.monai_token())
        self._monai_token.setEchoMode(QLineEdit.Password)
        self._monai_token.setPlaceholderText("인증을 쓰는 서버만 (선택)")
        form.addRow("Access Token:", self._monai_token)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "AI Research 패널(툴바 🧠 AI)의 모델 탭에서 이 서버로 자동 세그멘테이션을 요청하고,\n"
            "수정한 라벨을 피드백으로 제출할 수 있습니다 (active learning).\n"
            "서버로는 픽셀 볼륨(NIfTI)만 전송되며 환자 이름·ID 등 DICOM 정보는 보내지 않습니다.\n"
            "예: monailabel start_server --app apps/radiology --studies datasets/ --conf models segmentation"))
        layout.addStretch()
        return page

    def _cloud_tab(self):
        from .cloud import google_drive, onedrive
        from .cloud.secure_store import SecureStore, backend_name
        self._secure = SecureStore(self._settings._qs)
        page = QWidget()
        layout = QVBoxLayout(page)
        google = QFormLayout()
        google.addRow(QLabel("<b>Google Drive</b> (Google Cloud Console → OAuth 클라이언트 '데스크톱 앱')"))
        self._google_id = QLineEdit(self._settings.google_client_id())
        self._google_id.setPlaceholderText("xxxxxxxx.apps.googleusercontent.com")
        google.addRow("OAuth Client ID:", self._google_id)
        self._google_secret = QLineEdit(self._secure.get(google_drive.SECRET_KEY) or "")
        self._google_secret.setEchoMode(QLineEdit.Password)
        self._google_secret.setPlaceholderText("GOCSPX-... (데스크톱 앱 클라이언트와 함께 발급)")
        google.addRow("Client Secret:", self._google_secret)
        self._google_key = QLineEdit(self._settings.google_api_key())
        self._google_key.setPlaceholderText("선택 - 할당량 추적용")
        google.addRow("API Key:", self._google_key)
        layout.addLayout(google)
        ms = QFormLayout()
        ms.addRow(QLabel("<b>OneDrive</b> (Azure Portal → 앱 등록, 리디렉션 URI http://localhost)"))
        self._onedrive_id = QLineEdit(self._settings.onedrive_client_id())
        self._onedrive_id.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        ms.addRow("Application (client) ID:", self._onedrive_id)
        layout.addLayout(ms)
        row = QHBoxLayout()
        for text, fn in (("Google 로그아웃", self._sign_out_google),
                         ("OneDrive 로그아웃", self._sign_out_onedrive)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        guide = QPushButton("설정 방법…")
        guide.clicked.connect(lambda: QMessageBox.information(
            self, "Cloud 설정 방법", google_drive.SETUP_HELP + "\n\n" + onedrive.SETUP_HELP))
        row.addWidget(guide)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel(
            f"앱에 내장된 키는 없습니다. 각자 만든 클라이언트 키를 입력하세요.\n"
            f"로그인 토큰과 Client Secret은 {backend_name()}에 저장됩니다.\n"
            "File → Open from Google Drive / OneDrive로 폴더를 탐색해 DICOM을 내려받아 엽니다.\n"
            "권한은 읽기 전용입니다 (Drive: drive.readonly, OneDrive: Files.Read.All)."))
        layout.addStretch()
        return page

    def _cache_tab(self):
        from . import cache
        from PyQt5.QtWidgets import QSlider
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        path_row = QHBoxLayout()
        path = QLineEdit(cache.cache_root())
        path.setReadOnly(True)
        open_btn = QPushButton("폴더 열기")
        open_btn.clicked.connect(self._open_cache_folder)
        path_row.addWidget(path, 1)
        path_row.addWidget(open_btn)
        form.addRow("캐시 위치:", path_row)
        self._cache_usage = QLabel()
        form.addRow("사용량:", self._cache_usage)
        limit_row = QHBoxLayout()
        self._cache_limit = QSlider(Qt.Horizontal)
        self._cache_limit.setRange(cache.MIN_LIMIT_GB, cache.MAX_LIMIT_GB)
        self._cache_limit.setValue(cache.limit_gb())
        self._cache_limit.setTickPosition(QSlider.TicksBelow)
        self._cache_limit.setTickInterval(5)
        self._cache_limit_label = QLabel()
        self._cache_limit.valueChanged.connect(
            lambda v: self._cache_limit_label.setText(f"{v} GB"))
        self._cache_limit_label.setText(f"{cache.limit_gb()} GB")
        self._cache_limit_label.setMinimumWidth(50)
        limit_row.addWidget(self._cache_limit, 1)
        limit_row.addWidget(self._cache_limit_label)
        form.addRow("최대 용량:", limit_row)
        self._cache_enabled = QCheckBox("폴더 메타데이터·썸네일 캐시 사용 (다시 열 때 파싱 생략)")
        self._cache_enabled.setChecked(cache.enabled())
        form.addRow("", self._cache_enabled)
        layout.addLayout(form)
        row = QHBoxLayout()
        refresh = QPushButton("새로 고침")
        refresh.clicked.connect(self._refresh_cache_usage)
        clear = QPushButton("Clear Cache")
        clear.clicked.connect(self._clear_cache)
        row.addWidget(refresh)
        row.addWidget(clear)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel(
            "• 폴더를 처음 열면 메타데이터(시리즈 분류·슬라이스 정렬)와 썸네일을 저장해 두고,\n"
            "  파일 목록·수정일·크기가 같으면 다음에 파싱 없이 바로 엽니다. 바뀌면 자동으로 다시 읽습니다.\n"
            "• Google Drive / OneDrive에서 받은 파일은 파일 ID + 수정 시각으로 보관해\n"
            "  같은 파일을 다시 열면 내려받지 않습니다 (클라우드에서 바뀌면 다시 받음).\n"
            "• 최대 용량을 넘으면 오래 안 쓴 것부터 자동으로 지웁니다 (LRU).\n"
            "• 캐시에는 영상 메타데이터·파일이 들어 있습니다 (환자 정보 포함) - 공용 PC에서는 비우세요."))
        layout.addStretch()
        self._refresh_cache_usage()
        return page

    def _refresh_cache_usage(self):
        from . import cache
        u = cache.usage()
        self._cache_usage.setText(
            f"<b>{cache.human_size(u['total'])}</b> / {self._cache_limit.value()} GB  "
            f"(메타데이터 {cache.human_size(u['metadata'])} · 썸네일 {cache.human_size(u['thumbnails'])} · "
            f"클라우드 파일 {cache.human_size(u['cloud'])})")

    def _clear_cache(self):
        from . import cache
        if QMessageBox.question(self, "Clear Cache",
                                "캐시를 모두 지울까요?\n(지금 열려 있는 영상에는 영향이 없습니다)") \
                != QMessageBox.Yes:
            return
        cache.clear()
        self._refresh_cache_usage()

    def _open_cache_folder(self):
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices
        from . import cache
        QDesktopServices.openUrl(QUrl.fromLocalFile(cache.cache_root()))

    def _sign_out_google(self):
        from .cloud import google_drive
        self._secure.delete(google_drive.TOKEN_KEY)
        QMessageBox.information(self, "Google Drive", "저장된 Google 로그인 토큰을 지웠습니다.")

    def _sign_out_onedrive(self):
        from .cloud import onedrive
        self._secure.delete(onedrive.CACHE_KEY)
        QMessageBox.information(self, "OneDrive", "저장된 Microsoft 로그인 토큰을 지웠습니다.")

    def _browse_report_folder(self):
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, "판독문 폴더", self._report_folder.text())
        if folder:
            self._report_folder.setText(folder)

    # ─── 저장 ───
    def _save(self):
        try:
            presets = self.presets_from_table()
            protocols = self.hanging_from_table()
            nodes = self.nodes_from_table()
        except ValueError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return
        values = {key: combo.currentData() for key, combo in self._mouse_combos.items()}
        values["fast_scroll_step"] = self._fast_step.value()
        values["roi_window_method"] = self._roi_method.currentData()
        self._settings.save_mouse(values)
        self._settings.save_window_presets(presets)
        self._settings.save_hanging_protocols(protocols)
        self._settings.set_auto_hanging(self._auto_hanging.isChecked())
        self._settings.save_dicom_nodes(nodes)
        self._settings.set_local_ae_title(self._local_ae.text())
        self._settings.set_report_folder(self._report_folder.text().strip())
        self._settings.set_report_creator(self._report_creator.text())
        self._settings.set_monai_url(self._monai_url.text())
        self._settings.set_monai_token(self._monai_token.text())
        self._settings.set_cloud_ids(self._google_id.text(), self._google_key.text(),
                                     self._onedrive_id.text())
        from . import cache
        cache.set_limit_gb(self._cache_limit.value())
        self._settings._qs.setValue("cache_enabled", self._cache_enabled.isChecked())
        from .cloud import google_drive
        secret = self._google_secret.text().strip()
        if secret:
            self._secure.set(google_drive.SECRET_KEY, secret)
        else:
            self._secure.delete(google_drive.SECRET_KEY)
        self.accept()
