# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""ROI Manager (ImageJ 스타일, 오른쪽 도크)

- 현재 영상 / 현재 시리즈의 ROI·측정 목록: ☑ 표시, 이름(더블클릭 편집), 종류, 슬라이스, 면적, 평균, 색, 🔒
- 정량 ROI 만들기 (원: 반지름 mm, 타원: 장축/단축 mm, 사각형: 가로/세로 mm, 중심 좌표)
- Properties: 이름·색·중심·크기를 숫자로 수정, 잠금
- Measure (선택 ROI 통계 표) · 측정값 표 · 부피 (같은 이름 ROI, 여러 슬라이스) → CSV / 클립보드
- 복사/붙여넣기 (Ctrl+C / Ctrl+V), 여러 슬라이스로 복제, 좌우 대칭(Mirror)
- ROI 세트 저장/불러오기 (.roi.json, 환자 좌표 mm 기준 → 다른 시리즈·환자에도), 템플릿
- Compare: 다른 검사(이전 검사)의 ROI를 현재 영상에 점선으로 겹쳐 보기
- Batch: Worklist의 모든 검사에 같은 ROI 적용 → 결과 CSV
"""
import csv
import io
import json
import os

from PyQt5.QtCore import QItemSelectionModel, QPointF, Qt, QTimer
from PyQt5.QtGui import QColor, QPen, QPolygonF
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog,
                             QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                             QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                             QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTabWidget,
                             QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from . import dicom_info, roi_tools
from .annotation_edit import MeasureSettings, fmt_length
from .annotations import MEASURE_TYPES, ROI_TYPES, image_key

MANAGED_TYPES = ROI_TYPES + MEASURE_TYPES
STAT_HEADER = ["ROI Name", "Slice", "Type", "Area(mm²)", "Perimeter(mm)", "Mean", "StdDev",
               "Min", "Max", "Median", "Pixels"]


def templates_dir():
    from .ai import data_dir
    folder = data_dir("roi_templates")
    os.makedirs(folder, exist_ok=True)
    return folder


def _num(value, digits=2):
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def stats_row(name, slice_no, ann, stats):
    return [name, slice_no, roi_tools.type_name(ann), stats.get("area_mm2"),
            stats.get("perimeter_mm"), stats.get("mean"), stats.get("std"), stats.get("min"),
            stats.get("max"), stats.get("median"), stats.get("pixels")]


def measure_on(ann, series, index):
    """series의 index 슬라이스 픽셀로 ROI 통계 (Measure·Batch 공통)"""
    ds = series.slices[index]
    label, factor = dicom_info.value_label(ds)
    sp = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
    return roi_tools.roi_statistics(ann, series.get_pixel_array(index), sp, factor)


# ─── 결과 표 (CSV / 클립보드) ───

class ResultTable(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("")
        self.title.setStyleSheet("color: #9ab;")
        layout.addWidget(self.title)
        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        for text, slot in (("📋 클립보드 복사", self.copy), ("💾 CSV 저장", self.save_csv)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.header, self.rows = [], []

    def show_rows(self, title, header, rows):
        self.title.setText(title)
        self.header, self.rows = list(header), [list(r) for r in rows]
        self.table.setColumnCount(len(header))
        self.table.setHorizontalHeaderLabels(header)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(_num(value))
                if isinstance(value, (int, float)):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

    def _text(self, delimiter):
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        writer.writerow(self.header)
        for row in self.rows:
            writer.writerow([_num(v, 4) for v in row])
        return buf.getvalue()

    def copy(self):
        QApplication.clipboard().setText(self._text("\t"))

    def save_csv(self):
        if not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", "roi_results.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(self._text(","))


# ─── 정량 ROI 만들기 / 속성 ───

class QuantRoiDialog(QDialog):
    """중심 좌표 + 크기(mm)로 원/타원/사각형 ROI"""

    def __init__(self, kind, center, spacing, parent=None, ann=None):
        super().__init__(parent)
        self.kind = kind
        self.spacing = spacing
        editing = ann is not None
        self.setWindowTitle("ROI 속성" if editing else "정량 ROI 만들기")
        form = QFormLayout(self)
        self.type_box = QComboBox()
        self.type_box.addItems(["원 (Circle)", "타원 (Ellipse)", "사각형 (Rectangle)"])
        self.type_box.setCurrentIndex({"circle": 0, "ellipse": 1, "rect": 2}.get(kind, 0))
        self.type_box.setEnabled(not editing)
        self.type_box.currentIndexChanged.connect(self._sync)
        form.addRow("종류:", self.type_box)
        self.name = QLineEdit(ann.get("name", "") if ann else "")
        form.addRow("이름:", self.name)
        self.cx, self.cy = (QDoubleSpinBox(), QDoubleSpinBox())
        for box, value in ((self.cx, center[0]), (self.cy, center[1])):
            box.setRange(-10000, 10000)
            box.setDecimals(2)
            box.setValue(value)
            box.setSuffix(" px")
        form.addRow("중심 X (열):", self.cx)
        form.addRow("중심 Y (행):", self.cy)
        self.w, self.h = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.w, self.h):
            box.setRange(0.1, 5000)
            box.setDecimals(2)
            box.setSuffix(" mm")
        self.w.setValue(10.0)
        self.h.setValue(10.0)
        self.w_label, self.h_label = QLabel(), QLabel()
        form.addRow(self.w_label, self.w)
        form.addRow(self.h_label, self.h)
        self.color = roi_tools.DEFAULT_COLORS.get("ellipse")
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self._pick_color)
        form.addRow("색:", self.color_button)
        self.lock = QCheckBox("잠금 (실수로 움직이지 않게)")
        form.addRow(self.lock)
        self.info = QLabel()
        self.info.setStyleSheet("color: #999;")
        form.addRow(self.info)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if ann is not None:
            cx, cy, w, h = roi_tools.shape_params(ann, spacing)
            self.cx.setValue(cx)
            self.cy.setValue(cy)
            self.w.setValue(max(0.1, w))
            self.h.setValue(max(0.1, h))
            self.color = roi_tools.color_of(ann)
            self.lock.setChecked(bool(ann.get("locked")))
            if ann["type"] == "ellipse" and ann.get("circle"):
                self.w.setValue(max(0.05, w / 2))
        self.w.valueChanged.connect(self._sync_circle)
        self._sync()
        self._update_color()

    def _circle(self):
        return self.type_box.currentIndex() == 0

    def _sync(self):
        circle = self._circle()
        self.w_label.setText("반지름:" if circle else ("장축(가로):" if self.type_box.currentIndex() == 1
                                                     else "가로:"))
        self.h_label.setText("" if circle else ("단축(세로):" if self.type_box.currentIndex() == 1
                                                else "세로:"))
        self.h.setVisible(not circle)
        self.h_label.setVisible(not circle)
        self.info.setText(f"픽셀 간격 {self.spacing[1]:.3f} × {self.spacing[0]:.3f} mm "
                          "(열 × 행). 좌표는 이미지 픽셀 (왼쪽 위 = 0, 0).")

    def _sync_circle(self):
        pass

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self.color), self, "ROI 색")
        if color.isValid():
            self.color = color.name()
            self._update_color()

    def _update_color(self):
        self.color_button.setText(self.color)
        self.color_button.setStyleSheet(f"background: {self.color}; color: #000;")

    def result(self):
        """(주석 종류, 점, 추가 필드)"""
        index = self.type_box.currentIndex()
        kind = "rect" if index == 2 else "ellipse"
        if index == 0:
            width = height = self.w.value() * 2
        else:
            width, height = self.w.value(), self.h.value()
        pts = roi_tools.make_shape(kind, (self.cx.value(), self.cy.value()), width, height,
                                   self.spacing)
        fields = {"name": self.name.text().strip(), "color": self.color,
                  "locked": self.lock.isChecked()}
        if index == 0:
            fields["circle"] = True
        return kind, pts, fields


class GeneralPropertiesDialog(QDialog):
    """측정(거리·경로·각도)과 자유곡선 ROI: 이름·색·잠금·표시 + 이동(dx, dy)"""

    def __init__(self, ann, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{roi_tools.type_name(ann)} 속성")
        form = QFormLayout(self)
        self.name = QLineEdit(ann.get("name", ""))
        form.addRow("이름:", self.name)
        self.color = roi_tools.color_of(ann)
        self.color_button = QPushButton(self.color)
        self.color_button.setStyleSheet(f"background: {self.color}; color: #000;")
        self.color_button.clicked.connect(self._pick)
        form.addRow("색:", self.color_button)
        self.dx, self.dy = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.dx, self.dy):
            box.setRange(-5000, 5000)
            box.setDecimals(2)
            box.setSuffix(" px")
        form.addRow("이동 X:", self.dx)
        form.addRow("이동 Y:", self.dy)
        self.lock = QCheckBox("잠금")
        self.lock.setChecked(bool(ann.get("locked")))
        form.addRow(self.lock)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick(self):
        color = QColorDialog.getColor(QColor(self.color), self)
        if color.isValid():
            self.color = color.name()
            self.color_button.setText(self.color)
            self.color_button.setStyleSheet(f"background: {self.color}; color: #000;")


class MeasureSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("측정 표시 설정")
        form = QFormLayout(self)
        self.font = QSpinBox()
        self.font.setRange(7, 28)
        self.font.setValue(int(MeasureSettings.font_pt))
        self.font.setSuffix(" pt")
        form.addRow("측정값 글자 크기:", self.font)
        self.unit = QComboBox()
        self.unit.addItems(["mm", "cm", "px"])
        self.unit.setCurrentText(MeasureSettings.unit)
        form.addRow("단위:", self.unit)
        note = QLabel("px: 거리는 픽셀, 면적은 픽셀 수로 표시합니다.")
        note.setStyleSheet("color: #999;")
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


# ─── ROI Manager ───

class RoiManagerDock(QDockWidget):
    COLUMNS = ["", "Name", "Type", "Slice", "Area(mm²)", "Mean", "Value", "🔒"]

    def __init__(self, main):
        super().__init__("ROI Manager", main)
        self.setObjectName("RoiManagerDock")
        self.main = main
        self._clipboard = None
        self._compare = None        # (series, [(slice, 주석)])
        self._state = None
        self._refreshing = False
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)

        top = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItems(["현재 영상", "현재 시리즈 (모든 슬라이스)"])
        self.scope.currentIndexChanged.connect(self.refresh)
        top.addWidget(QLabel("범위:"))
        top.addWidget(self.scope, 1)
        layout.addLayout(top)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.installEventFilter(self)   # macOS Return · Windows F2 로 이름 바꾸기
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_row_selection)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setMinimumHeight(110)
        layout.addWidget(self.table, 2)

        grid = QGridLayout()
        grid.setSpacing(3)
        buttons = [
            ("＋ 원", lambda: self.create_roi("circle"), "중심 + 반지름(mm)으로 원 ROI"),
            ("＋ 타원", lambda: self.create_roi("ellipse"), "중심 + 장축/단축(mm)"),
            ("＋ 사각형", lambda: self.create_roi("rect"), "중심 + 가로/세로(mm)"),
            ("Properties", self.edit_properties, "선택한 ROI 이름·색·위치·크기 숫자로 수정"),
            ("Select All", self.select_all, "목록 전체 선택"),
            ("Deselect All", self.deselect_all, "선택 해제"),
            ("🗑 Delete", self.delete_selected, "선택 삭제 (잠긴 것은 제외)"),
            ("🔒 Lock", self.toggle_lock, "선택한 ROI/측정 잠금·해제"),
            ("📊 Measure", self.measure, "선택(없으면 전체) ROI 통계 표"),
            ("📏 측정값 표", self.measurements_table, "거리·경로·각도·면적 측정값 모두"),
            ("🧊 Volume", self.volume, "같은 이름 ROI의 여러 슬라이스 부피"),
            ("⧉ 슬라이스 복제", self.copy_to_slices, "선택 ROI를 다른 슬라이스들에 복제"),
            ("⇋ Mirror", self.mirror, "좌우 대칭 위치에 복사"),
            ("💾 저장", self.save_set, ".roi.json (환자 좌표 mm)"),
            ("📂 불러오기", self.load_set, "다른 시리즈/환자에 적용"),
        ]
        for i, (text, slot, tip) in enumerate(buttons):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            grid.addWidget(b, i // 4, i % 4)
        self.template_button = QToolButton()
        self.template_button.setText("템플릿 ▾")
        self.template_button.setPopupMode(QToolButton.InstantPopup)
        self.template_menu = QMenu(self)
        self.template_menu.aboutToShow.connect(self._fill_template_menu)
        self.template_button.setMenu(self.template_menu)
        grid.addWidget(self.template_button, len(buttons) // 4, len(buttons) % 4)
        row = len(buttons) // 4 + 1
        self.compare_button = QToolButton()
        self.compare_button.setText("Compare ▾")
        self.compare_button.setToolTip("다른 검사(이전 검사)의 ROI를 현재 영상에 점선으로")
        self.compare_button.setPopupMode(QToolButton.InstantPopup)
        self.compare_menu = QMenu(self)
        self.compare_menu.aboutToShow.connect(self._fill_compare_menu)
        self.compare_button.setMenu(self.compare_menu)
        grid.addWidget(self.compare_button, row, 0, 1, 2)
        batch = QPushButton("⚙ Batch (Worklist)")
        batch.setToolTip("Worklist의 모든 검사에 선택 ROI 적용 → 결과 CSV")
        batch.clicked.connect(self.batch)
        grid.addWidget(batch, row, 2, 1, 2)
        layout.addLayout(grid)

        self.tabs = QTabWidget()
        self.results = ResultTable()
        self.tabs.addTab(self.results, "결과")
        self.tabs.setMinimumHeight(130)
        layout.addWidget(self.tabs, 2)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #9ab;")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # 창 높이가 모자라면 버튼 줄이 서로 겹치지 않도록 줄이지 않고 스크롤한다
        for b in body.findChildren((QPushButton, QToolButton)):
            b.setMinimumHeight(b.sizeHint().height())
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = body
        self.setWidget(scroll)
        self.setMinimumWidth(420)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(300)
        main._annotation_store.changed.connect(self._schedule_refresh)
        from .viewport import DicomViewport
        DicomViewport.add_overlay_painter(self._draw_compare)

    # ─── 상태 ───
    def vp(self):
        return self.main._target_viewport()

    def series(self):
        return self.vp().series

    def _poll(self):
        """활성 뷰포트·시리즈·슬라이스가 바뀌면 목록 갱신 (보이는 동안만)"""
        if not self.isVisible():
            return
        vp = self.vp()
        state = (id(vp), vp.series.series_uid if vp.series else None, vp.current_slice)
        if state != self._state:
            self._state = state
            try:
                vp.selection_changed.disconnect(self._on_vp_selection)
            except TypeError:
                pass
            vp.selection_changed.connect(self._on_vp_selection)
            self.refresh()

    def _schedule_refresh(self):
        QTimer.singleShot(0, self.refresh)

    def entries(self, scope=None):
        """[(slice index, 주석)] 범위 안의 관리 대상 (ROI + 측정)"""
        vp, series = self.vp(), self.series()
        if series is None:
            return []
        scope = self.scope.currentIndex() if scope is None else scope
        store = self.main._annotation_store
        indices = [vp.current_slice] if scope == 0 else range(series.num_slices)
        out = []
        for k in indices:
            for ann in store.items(image_key(series, k)):
                if ann["type"] in MANAGED_TYPES:
                    out.append((k, ann))
        return out

    def refresh(self):
        if self._refreshing or self.table is None:
            return
        self._refreshing = True
        try:
            entries = self.entries()
            selected = set(self.vp().selected_ids())
            self.table.blockSignals(True)
            self.table.setRowCount(len(entries))
            for r, (k, ann) in enumerate(entries):
                stats = ann.get("stats") or {}
                value = ""
                if ann["type"] in ("distance", "path"):
                    value = fmt_length(ann.get("mm", 0.0))
                elif ann["type"] in ("angle", "cobb"):
                    value = f"{ann.get('deg', 0.0):.1f}°"
                elif ann["type"] == "area":
                    value = f"{ann.get('area', 0.0):.1f} mm²"
                area = stats.get("area_mm2")
                cells = ["", ann.get("name", ""), roi_tools.type_name(ann), str(k + 1),
                         _num(area, 1) if area is not None else "",
                         _num(stats.get("mean"), 1) if "mean" in stats else "", value,
                         "🔒" if ann.get("locked") else ""]
                for c, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.UserRole, ann["id"])
                    item.setData(Qt.UserRole + 1, k)
                    flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                    if c == 0:
                        flags |= Qt.ItemIsUserCheckable
                        item.setCheckState(Qt.Checked if ann.get("visible", True) else Qt.Unchecked)
                        item.setBackground(QColor(roi_tools.color_of(ann)))
                    if c == 1:
                        flags |= Qt.ItemIsEditable
                    item.setFlags(flags)
                    self.table.setItem(r, c, item)
            self.table.blockSignals(False)
            self._select_rows(lambda r: self.table.item(r, 1).data(Qt.UserRole) in selected)
            n_roi = sum(1 for _k, a in entries if a["type"] in ROI_TYPES)
            self.status.setText(f"ROI {n_roi}개 · 측정 {len(entries) - n_roi}개"
                                + ("  ·  Compare 켜짐" if self._compare else ""))
        finally:
            self._refreshing = False

    def selected_entries(self, default_all=False, rois_only=False):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        ids = {self.table.item(r, 1).data(Qt.UserRole) for r in rows if self.table.item(r, 1)}
        entries = self.entries()
        chosen = [(k, a) for k, a in entries if a["id"] in ids]
        if not chosen and default_all:
            chosen = entries
        if rois_only:
            chosen = [(k, a) for k, a in chosen if a["type"] in ROI_TYPES]
        return chosen

    # ─── 표 편집 ───
    def eventFilter(self, obj, event):
        """표에서 이름 바꾸기 키를 누르면 이름 칸(1열) 편집 시작"""
        from PyQt5.QtCore import QEvent
        from .platform_keys import is_rename_key
        if obj is self.table and event.type() == QEvent.KeyPress and is_rename_key(event):
            row = self.table.currentRow()
            if row >= 0:
                item = self.table.item(row, 1)
                if item is not None and item.flags() & Qt.ItemIsEditable:
                    self.table.editItem(item)
                    return True
        return super().eventFilter(obj, event)

    def _on_item_changed(self, item):
        ann_id = item.data(Qt.UserRole)
        store = self.main._annotation_store
        if item.column() == 0:
            store.update(ann_id, visible=item.checkState() == Qt.Checked)
        elif item.column() == 1:
            store.update(ann_id, name=item.text().strip())

    def _on_cell_clicked(self, row, column):
        if column == 7:
            self.toggle_lock()

    def _on_cell_double_clicked(self, row, column):
        if column == 0:   # 색 칸 더블클릭 → 색 고르기
            item = self.table.item(row, 0)
            _key, ann = self.main._annotation_store.find(item.data(Qt.UserRole))
            if ann is not None:
                color = QColorDialog.getColor(QColor(roi_tools.color_of(ann)), self, "색")
                if color.isValid():
                    self.main._annotation_store.update(ann["id"], color=color.name())

    def _on_row_selection(self):
        if self._refreshing:
            return
        chosen = self.selected_entries()
        vp = self.vp()
        here = [a["id"] for k, a in chosen if k == vp.current_slice]
        vp.select_ids(here, emit=False)
        if len(chosen) == 1 and chosen[0][0] != vp.current_slice:
            vp.go_to_slice(chosen[0][0])   # 다른 슬라이스의 ROI → 그 슬라이스로 이동

    def _on_vp_selection(self, ids):
        if self._refreshing:
            return
        self._select_rows(lambda r: self.table.item(r, 1).data(Qt.UserRole) in ids)

    def _select_rows(self, predicate):
        """조건에 맞는 행을 모두 선택 (selectRow는 기존 선택을 지우므로 selectionModel 사용)"""
        model = self.table.selectionModel()
        self.table.blockSignals(True)
        model.blockSignals(True)
        model.clearSelection()
        for r in range(self.table.rowCount()):
            if predicate(r):
                model.select(self.table.model().index(r, 0),
                             QItemSelectionModel.Select | QItemSelectionModel.Rows)
        model.blockSignals(False)
        self.table.blockSignals(False)
        self.table.viewport().update()

    def select_names(self, names):
        self._select_rows(lambda r: self.table.item(r, 1).text() in names)

    def select_all(self):
        self.table.selectAll()

    def deselect_all(self):
        self.table.clearSelection()
        self.vp().select_ids(set())

    # ─── 만들기 / 속성 ───
    def create_roi(self, kind):
        vp, series = self.vp(), self.series()
        if series is None:
            QMessageBox.information(self, "ROI", "먼저 영상을 여세요.")
            return
        arr = vp.current_array()
        center = (arr.shape[1] / 2, arr.shape[0] / 2) if arr is not None else (0, 0)
        spacing = vp.pixel_spacing() or (1.0, 1.0)
        dialog = QuantRoiDialog(kind, center, spacing, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        shape, pts, fields = dialog.result()
        ann = vp.add_roi(shape, pts, **fields)
        vp.select_ids({ann["id"]})
        self.status.setText(" | ".join(vp._annotation_text(ann)))

    def edit_properties(self):
        chosen = self.selected_entries() or [
            (self.vp().current_slice, a) for a in self.vp().selected_annotations()]
        if len(chosen) != 1:
            QMessageBox.information(self, "Properties", "ROI/측정을 하나 선택하세요.")
            return
        k, ann = chosen[0]
        vp = self.vp()
        if k != vp.current_slice:
            vp.go_to_slice(k)
        store = self.main._annotation_store
        spacing = vp.pixel_spacing() or (1.0, 1.0)
        if ann["type"] in ("ellipse", "rect"):
            kind = "circle" if ann.get("circle") else ann["type"]
            dialog = QuantRoiDialog(kind, (0, 0), spacing, self, ann=ann)
            if dialog.exec_() != QDialog.Accepted:
                return
            _shape, pts, fields = dialog.result()
            tmp = vp.recompute_annotation(dict(ann, pts=pts))
            store.update(ann["id"], **vp._measured_fields(tmp), **fields)
        else:
            dialog = GeneralPropertiesDialog(ann, self)
            if dialog.exec_() != QDialog.Accepted:
                return
            changes = {"name": dialog.name.text().strip(), "color": dialog.color,
                       "locked": dialog.lock.isChecked()}
            dx, dy = dialog.dx.value(), dialog.dy.value()
            if dx or dy:
                tmp = vp.recompute_annotation(dict(ann, pts=roi_tools.moved(ann, dx, dy)))
                changes.update(vp._measured_fields(tmp))
            store.update(ann["id"], **changes)

    def delete_selected(self):
        chosen = self.selected_entries()
        if not chosen:
            return
        series, store = self.series(), self.main._annotation_store
        with store.group():
            for k, ann in chosen:
                if not ann.get("locked"):
                    store.remove(image_key(series, k), ann["id"])

    def toggle_lock(self):
        chosen = self.selected_entries() or [
            (self.vp().current_slice, a) for a in self.vp().selected_annotations()]
        if not chosen:
            return
        lock = not all(a.get("locked") for _k, a in chosen)
        store = self.main._annotation_store
        with store.group():
            for _k, ann in chosen:
                store.update(ann["id"], locked=lock)
        self.status.setText(f"{len(chosen)}개 {'잠금' if lock else '잠금 해제'}")

    # ─── 분석 ───
    def measure(self):
        """선택(없으면 전체) ROI 통계 → 결과 표"""
        series = self.series()
        chosen = self.selected_entries(default_all=True, rois_only=True)
        if series is None or not chosen:
            QMessageBox.information(self, "Measure", "ROI가 없습니다.")
            return
        rows = []
        for k, ann in chosen:
            stats = measure_on(ann, series, k)
            rows.append(stats_row(ann.get("name") or roi_tools.type_name(ann), k + 1, ann, stats))
        self.results.show_rows(f"{series.description} — ROI {len(rows)}개", STAT_HEADER, rows)
        self.tabs.setCurrentWidget(self.results)

    def measurements_table(self):
        series = self.series()
        entries = [(k, a) for k, a in self.entries(scope=1) if a["type"] in MEASURE_TYPES]
        rows = []
        for k, ann in entries:
            if ann["type"] in ("distance", "path"):
                value, unit = ann.get("mm", 0.0), "mm"
            elif ann["type"] == "area":
                value, unit = ann.get("area", 0.0), "mm²"
            else:
                value, unit = ann.get("deg", 0.0), "°"
            extra = ann.get("perimeter") if ann["type"] == "area" else (
                len(ann["pts"]) if ann["type"] == "path" else "")
            rows.append([ann.get("name", ""), k + 1, roi_tools.type_name(ann), value, unit, extra])
        self.results.show_rows(f"{series.description if series else ''} — 측정 {len(rows)}개",
                               ["Name", "Slice", "Type", "Value", "Unit", "Perimeter / Points"],
                               rows)
        self.tabs.setCurrentWidget(self.results)

    def volume(self):
        series = self.series()
        entries = self.entries(scope=1)
        chosen = self.selected_entries()
        if chosen:   # 선택한 ROI와 같은 이름만
            names = {a.get("name") or roi_tools.type_name(a) for _k, a in chosen}
            entries = [(k, a) for k, a in entries if (a.get("name") or roi_tools.type_name(a)) in names]
        result = roi_tools.roi_volume(entries, series) if series else {}
        if not result:
            QMessageBox.information(self, "Volume", "ROI가 없습니다. 같은 이름의 ROI를 여러 슬라이스에 그리세요.")
            return
        rows = [[name, len(r["slices"]), f"{r['slices'][0] + 1}–{r['slices'][-1] + 1}",
                 r["area_sum_mm2"], r["spacing"], r["volume_ml"]] for name, r in result.items()]
        self.results.show_rows("부피 = Σ 면적 × 슬라이스 간격 (Cavalieri)",
                               ["ROI Name", "Slices", "Range", "ΣArea(mm²)", "Spacing(mm)",
                                "Volume(mL)"], rows)
        self.tabs.setCurrentWidget(self.results)

    # ─── 복사 / 붙여넣기 / 복제 / 대칭 ───
    def _current_selection_or_last(self):
        vp = self.vp()
        chosen = [(vp.current_slice, a) for a in vp.selected_annotations()]
        if not chosen:
            chosen = self.selected_entries()
        return chosen

    def copy(self):
        series = self.series()
        chosen = [(k, a) for k, a in self._current_selection_or_last() if a["type"] in MANAGED_TYPES]
        if series is None or not chosen:
            self.main.statusBar().showMessage("복사할 ROI/측정을 선택하세요 (Select 도구로 클릭).", 4000)
            return False
        self._clipboard = roi_tools.export_rois(chosen, series)
        QApplication.clipboard().setText(json.dumps(self._clipboard, ensure_ascii=False))
        self.main.statusBar().showMessage(f"ROI {len(chosen)}개 복사 — 다른 슬라이스/시리즈에서 Ctrl+V", 5000)
        return True

    def paste(self):
        data = self._clipboard
        if data is None:
            try:
                data = json.loads(QApplication.clipboard().text())
                if data.get("format") != roi_tools.ROI_FILE_FORMAT:
                    data = None
            except (ValueError, AttributeError):
                data = None
        vp, series = self.vp(), self.series()
        if data is None or series is None:
            return False
        placed = roi_tools.place_on_slice(data, series, vp.current_slice)
        self._add_placed([(vp.current_slice, a) for a in placed], series)
        vp.select_ids({a["id"] for a in placed})
        self.main.statusBar().showMessage(f"ROI {len(placed)}개 붙여넣기 (슬라이스 {vp.current_slice + 1})", 5000)
        return True

    def _add_placed(self, placed, series):
        """[(slice, 주석)] 추가 (통계를 그 슬라이스 픽셀로 계산, 되돌리기 한 번)"""
        store = self.main._annotation_store
        with store.group():
            for k, ann in placed:
                ds = series.slices[k]
                label, factor = dicom_info.value_label(ds)
                sp = dicom_info.pixel_spacing(ds) or (1.0, 1.0)
                roi_tools.recompute(ann, series.get_pixel_array(k), sp, factor, label)
                ann["calibrated"] = dicom_info.pixel_spacing(ds) is not None
                ann["slice"] = k
                store.add(image_key(series, k), ann)

    def copy_to_slices(self):
        series = self.series()
        chosen = self._current_selection_or_last()
        if series is None or not chosen:
            QMessageBox.information(self, "슬라이스 복제", "복제할 ROI를 선택하세요.")
            return
        n = series.num_slices
        text, ok = QInputDialog.getText(
            self, "슬라이스 복제", f"복제할 슬라이스 (1–{n}), 예: 1-{n} 또는 5,7,9-12:",
            text=f"1-{n}")
        if not ok:
            return
        targets = set()
        try:
            for part in text.replace(" ", "").split(","):
                if "-" in part:
                    a, b = part.split("-")
                    targets.update(range(int(a) - 1, int(b)))
                elif part:
                    targets.add(int(part) - 1)
        except ValueError:
            QMessageBox.warning(self, "슬라이스 복제", "형식이 올바르지 않습니다.")
            return
        data = roi_tools.export_rois(chosen, series)
        placed = []
        for k in sorted(t for t in targets if 0 <= t < n and t not in {c[0] for c in chosen}):
            placed += [(k, a) for a in roi_tools.place_on_slice(data, series, k)]
        self._add_placed(placed, series)
        self.status.setText(f"{len(placed)}개 ROI를 {len(targets)}개 슬라이스에 복제 (Ctrl+Z로 한 번에 취소)")

    def mirror(self):
        vp, series = self.vp(), self.series()
        chosen = [(k, a) for k, a in self._current_selection_or_last() if k == vp.current_slice]
        arr = vp.current_array()
        if series is None or not chosen or arr is None:
            QMessageBox.information(self, "Mirror", "현재 영상에서 ROI를 선택하세요.")
            return
        from .annotations import new_id
        placed = []
        for k, ann in chosen:
            copy = {key: value for key, value in ann.items() if key not in ("id", "stats")}
            copy.update(id=new_id(), pts=roi_tools.mirrored(ann, arr.shape[1]),
                        name=(ann.get("name") or roi_tools.type_name(ann)) + " (mirror)",
                        locked=False)
            placed.append((k, copy))
        self._add_placed(placed, series)
        vp.select_ids({a["id"] for _k, a in placed})
        self.status.setText("영상 가운데 세로선 기준 좌우 대칭으로 복사했습니다.")

    # ─── 저장 / 불러오기 / 템플릿 ───
    def save_set(self):
        series = self.series()
        chosen = self.selected_entries(default_all=True)
        if series is None or not chosen:
            QMessageBox.information(self, "저장", "저장할 ROI가 없습니다.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "ROI 세트 저장", "rois.roi.json",
                                              "ROI set (*.roi.json);;JSON (*.json)")
        if path:
            roi_tools.save_rois(path, roi_tools.export_rois(chosen, series))
            self.status.setText(f"{len(chosen)}개 저장: {path}")

    def _ask_mode(self, data):
        series = self.series()
        same = roi_tools.same_frame(data, series)
        box = QMessageBox(self)
        box.setWindowTitle("ROI 적용 방식")
        box.setText("ROI를 어떻게 놓을까요?")
        box.setInformativeText(
            ("같은 좌표계(Frame of Reference)입니다 — 환자 좌표(mm)로 같은 위치에 놓을 수 있습니다."
             if same else "좌표계가 다릅니다 (다른 검사/환자). 영상 상대 위치를 권장합니다."))
        patient = box.addButton("환자 좌표 (mm)", QMessageBox.AcceptRole)
        relative = box.addButton("영상 상대 위치 (현재 슬라이스)", QMessageBox.ActionRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(patient if same else relative)
        box.exec_()
        clicked = box.clickedButton()
        return "patient" if clicked is patient else "relative" if clicked is relative else None

    def apply_set(self, data, label):
        series = self.series()
        if series is None:
            return
        mode = self._ask_mode(data)
        if mode is None:
            return
        placed, notes = roi_tools.place_rois(data, series, mode, self.vp().current_slice)
        self._add_placed(placed, series)
        if placed:
            self.vp().go_to_slice(placed[0][0])
        self.status.setText(f"{label}: ROI {len(placed)}개 적용 ({'환자 좌표' if mode == 'patient' else '상대 위치'})"
                            + ("\n" + "\n".join(notes) if notes else ""))

    def load_set(self):
        path, _ = QFileDialog.getOpenFileName(self, "ROI 세트 불러오기", "",
                                              "ROI set (*.roi.json *.json)")
        if not path:
            return
        try:
            data = roi_tools.load_rois(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "불러오기", str(e))
            return
        self.apply_set(data, os.path.basename(path))

    def _fill_template_menu(self):
        menu = self.template_menu
        menu.clear()
        save = menu.addAction("현재 ROI를 템플릿으로 저장…")
        save.triggered.connect(self.save_template)
        menu.addSeparator()
        names = sorted(f[:-len(".roi.json")] for f in os.listdir(templates_dir())
                       if f.endswith(".roi.json"))
        if not names:
            empty = menu.addAction("(저장한 템플릿 없음 — 예: Cardiac 17-segment, Liver 9-segment)")
            empty.setEnabled(False)
        for name in names:
            action = menu.addAction(f"적용: {name}")
            action.triggered.connect(lambda _=False, n=name: self.apply_template(n))
        if names:
            delete = menu.addMenu("삭제")
            for name in names:
                action = delete.addAction(name)
                action.triggered.connect(lambda _=False, n=name: self.delete_template(n))

    def save_template(self):
        series = self.series()
        chosen = self.selected_entries(default_all=True, rois_only=True)
        if series is None or not chosen:
            QMessageBox.information(self, "템플릿", "템플릿으로 저장할 ROI를 선택하세요.")
            return
        name, ok = QInputDialog.getText(self, "템플릿 저장", "템플릿 이름 (예: Liver 9-segment ROI):")
        name = name.strip().replace(os.sep, "_")
        if ok and name:
            roi_tools.save_rois(os.path.join(templates_dir(), name + ".roi.json"),
                                roi_tools.export_rois(chosen, series))
            self.status.setText(f"템플릿 '{name}' 저장 ({len(chosen)}개 ROI)")

    def apply_template(self, name):
        try:
            data = roi_tools.load_rois(os.path.join(templates_dir(), name + ".roi.json"))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "템플릿", str(e))
            return
        self.apply_set(data, f"템플릿 '{name}'")

    def delete_template(self, name):
        if QMessageBox.question(self, "템플릿 삭제", f"'{name}' 템플릿을 삭제할까요?") == QMessageBox.Yes:
            try:
                os.remove(os.path.join(templates_dir(), name + ".roi.json"))
            except OSError:
                pass

    # ─── Compare (다른 검사 ROI 겹쳐 보기) ───
    def _series_with_rois(self):
        store = self.main._annotation_store
        current = self.series()
        out = []
        for s in self.main._loader.get_series_list():
            if current is not None and s.series_uid == current.series_uid:
                continue
            entries = [(k, a) for k in range(s.num_slices)
                       for a in store.items(image_key(s, k)) if a["type"] in ROI_TYPES]
            if entries:
                out.append((s, entries))
        # 같은 환자·다른 검사를 위로 (이전 검사 비교용)
        pid = current.patient_id if current is not None else None
        out.sort(key=lambda se: (se[0].patient_id != pid, se[0].study_date or ""), reverse=False)
        return out

    def _fill_compare_menu(self):
        menu = self.compare_menu
        menu.clear()
        if self._compare is not None:
            off = menu.addAction(f"끄기 ({self._compare[0].description})")
            off.triggered.connect(lambda: self.set_compare(None))
            menu.addSeparator()
        candidates = self._series_with_rois()
        if not candidates:
            empty = menu.addAction("(ROI가 있는 다른 시리즈 없음)")
            empty.setEnabled(False)
        for s, entries in candidates:
            action = menu.addAction(f"{s.study_date or ''}  {s.description}  — ROI {len(entries)}개")
            action.triggered.connect(lambda _=False, se=(s, entries): self.set_compare(se))

    def set_compare(self, source):
        self._compare = source
        for vp in self.main._all_viewports():
            vp.update()
        self.refresh()

    def _draw_compare(self, vp, painter):
        """Compare 켜져 있으면 다른 검사 ROI를 현재 영상에 점선으로 (환자 좌표로 투영)"""
        if self._compare is None or vp.series is None:
            return
        src_series, entries = self._compare
        ds = vp.current_dataset()
        if ds is None:
            return
        thickness = max(1.0, roi_tools.slice_spacing(vp.series) / 2 + 0.5)
        used = []   # 라벨이 겹치지 않게 아래로 밀기
        painter.save()
        font = vp.label_font()
        font.setPointSize(max(8, font.pointSize() - 1))
        painter.setFont(font)
        for k, ann in entries:
            src_ds = src_series.slices[k]
            lps = roi_tools.image_to_patient(src_ds, roi_tools.polygon_of(ann, 64))
            proj = roi_tools.patient_to_image(ds, lps) if lps else None
            if proj is None:   # 공간 정보가 없으면 같은 슬라이스 번호에만
                if k != vp.current_slice:
                    continue
                pts = roi_tools.polygon_of(ann, 64)
            else:
                if min(abs(d) for _x, _y, d in proj) > thickness:
                    continue
                pts = [(x, y) for x, y, _d in proj]
            poly = QPolygonF([vp._image_to_screen_f(p) for p in pts])
            color = QColor(roi_tools.color_of(ann))
            painter.setPen(QPen(color, 1.5, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(poly)
            stats = ann.get("stats") or {}
            text = f"prior: {ann.get('name') or roi_tools.type_name(ann)}"
            if "mean" in stats:
                text += f"  mean {stats['mean']:.1f}"
            box = poly.boundingRect()
            painter.setPen(color)
            spot = box.bottomLeft() + QPointF(0, 14)
            line = painter.fontMetrics().height()
            while any(abs(spot.y() - y) < line and abs(spot.x() - x) < 160 for x, y in used):
                spot += QPointF(0, line)
            used.append((spot.x(), spot.y()))
            painter.drawText(spot, text)
        painter.restore()

    # ─── Batch ───
    def batch(self):
        series = self.series()
        chosen = self.selected_entries(default_all=True, rois_only=True)
        worklist = list(getattr(self.main, "_worklist", []) or [])
        if series is None or not chosen:
            QMessageBox.information(self, "Batch", "적용할 ROI를 선택하세요.")
            return
        if not worklist:
            QMessageBox.information(self, "Batch", "Worklist가 비어 있습니다 (AI 패널 → Worklist에 검사 추가).")
            return
        data = roi_tools.export_rois(chosen, series)
        mode = self._ask_mode(data)
        if mode is None:
            return
        loaded = {s.series_uid: s for s in self.main._loader.get_series_list()}

        def task(progress, cancelled):
            from .dicom_loader import DicomLoader
            rows, skipped = [], []
            for i, entry in enumerate(worklist):
                if cancelled():
                    break
                uid = entry.get("series_uid")
                progress(f"{i + 1}/{len(worklist)} {entry.get('description', '')}", i / len(worklist))
                target = loaded.get(uid)
                if target is None and entry.get("folder") and os.path.isdir(entry["folder"]):
                    loader = DicomLoader()
                    loader.load_paths([entry["folder"]], recursive=False)
                    target = loader.get_series_by_uid(uid)
                if target is None or not target.num_slices:
                    skipped.append(entry.get("description") or uid)
                    continue
                placed, _notes = roi_tools.place_rois(data, target, mode, target.num_slices // 2)
                for k, ann in placed:
                    stats = measure_on(ann, target, k)
                    rows.append([entry.get("patient_id", ""), entry.get("patient_name", ""),
                                 entry.get("study_date", ""), target.description]
                                + stats_row(ann.get("name") or roi_tools.type_name(ann), k + 1, ann, stats))
            return rows, skipped

        def done(result):
            rows, skipped = result
            self.results.show_rows(
                f"Batch: 검사 {len(worklist) - len(skipped)}개 × ROI {len(chosen)}개"
                + (f" (건너뜀 {len(skipped)}: {', '.join(skipped[:3])})" if skipped else ""),
                ["Patient ID", "Patient", "Study Date", "Series"] + STAT_HEADER, rows)
            self.tabs.setCurrentWidget(self.results)
            if rows:
                self.results.save_csv()

        from .ai.panel import TaskWorker
        worker = TaskWorker(task, self)
        worker.progress.connect(lambda text, _f: self.status.setText("Batch " + text))
        worker.succeeded.connect(done)
        worker.failed.connect(lambda m: QMessageBox.warning(self, "Batch", m))
        worker.finished.connect(worker.deleteLater)
        self._batch_worker = worker
        worker.start()
