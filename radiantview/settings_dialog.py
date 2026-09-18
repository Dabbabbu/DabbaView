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

    TABS = ("mouse", "presets", "hanging", "nodes", "reading")

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
        ok, message = net.echo(node, self._local_ae.text().strip() or "RADIANTVIEW")
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
        self.accept()
