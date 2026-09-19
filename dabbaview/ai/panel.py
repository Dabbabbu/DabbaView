# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
AI Research 사이드 패널 (오른쪽 도크)

탭: ✏️ 세그멘트 (도구·라벨·통계) / 📋 데이터셋 (워크리스트) /
    📦 내보내기 (형식·분할·전처리) / 🤖 모델 (MONAI Label·ONNX)
"""
import os
import traceback

import numpy as np
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPixmap, QImage, QKeySequence
from PyQt5.QtWidgets import (
    QAction, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QToolButton, QVBoxLayout, QWidget)

from . import segmentation as seg
from .export import FORMATS, ExportOptions, Cancelled, export_cases
from .monai_label import MonaiLabelClient, MonaiLabelError, image_id_for
from .onnx_infer import ONNX_AVAILABLE, OnnxModelError, OnnxSegmenter
from .preprocess import PreprocessOptions, apply as apply_preprocess
from .volume import load_volume
from .worklist import STATUSES, STATUS_DONE

# CT 값 범위 프리셋 (HU)
THRESHOLD_PRESETS = [
    ("직접 입력", None),
    ("연부조직 (-100 ~ 200)", (-100, 200)),
    ("뼈 (300 ~ 3000)", (300, 3000)),
    ("폐 (-1000 ~ -400)", (-1000, -400)),
    ("지방 (-190 ~ -30)", (-190, -30)),
    ("조영 혈관 (150 ~ 500)", (150, 500)),
    ("공기 (-1100 ~ -900)", (-1100, -900)),
]

TOOL_BUTTONS = [
    (seg.TOOL_BRUSH, "🖌", "Brush", "D", "브러시: 드래그해서 현재 라벨로 칠하기"),
    (seg.TOOL_ERASER, "⌫", "Eraser", "X", "지우개: 드래그해서 현재 라벨 지우기"),
    (seg.TOOL_WAND, "🪄", "Wand", "W", "Magic Wand: 클릭한 픽셀 값 ±허용범위의 연결 영역 선택"),
    (seg.TOOL_THRESHOLD, "▤", "Threshold", "G",
     "Threshold: 클릭한 슬라이스에서 값 범위 안의 픽셀을 현재 라벨로 (범위는 미리보기로 표시)"),
    (seg.TOOL_MEDSAM, "🎯", "MedSAM", "M",
     "MedSAM: 클릭한 곳의 구조를 자동으로 찾아 현재 라벨로 (Settings → AI에서 ONNX 지정)"),
]


def _color_icon(rgb, size=14):
    pix = QPixmap(size, size)
    pix.fill(QColor(*rgb))
    return QIcon(pix)


class TaskWorker(QThread):
    """fn(progress, cancelled) 을 백그라운드에서 실행"""

    progress = pyqtSignal(str, float)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            result = self._fn(lambda text, frac=0.0: self.progress.emit(text, float(frac)),
                              lambda: self._cancel)
            self.succeeded.emit(result)
        except Cancelled:
            self.failed.emit("취소했습니다.")
        except (MonaiLabelError, OnnxModelError, ValueError, OSError) as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - 예상 못 한 오류도 UI에 알림
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}")


class AIResearchPanel(QDockWidget):
    def __init__(self, main_window, controller, worklist):
        super().__init__("AI Research", main_window)
        self.setObjectName("AIResearchPanel")
        self.main = main_window
        self.ctl = controller
        self.labels = controller.labels
        self.worklist = worklist
        self._worker = None
        self._monai_models = []
        self._onnx = None
        self.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.setMinimumWidth(330)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._build_segment_tab()), "✏️ 세그멘트")
        self.tabs.addTab(self._build_dataset_tab(), "📋 데이터셋")
        self.tabs.addTab(self._scroll(self._build_export_tab()), "📦 내보내기")
        self.model_tab_page = self._scroll(self._build_model_tab())
        self.tabs.addTab(self.model_tab_page, "🤖 모델")
        from .models_tab import ModelsTab
        self.models_tab = ModelsTab(self)
        self.models_page = self._scroll(self.models_tab)
        self.tabs.addTab(self.models_page, "🧩 Models")
        self.ctl.medsam_handler = self._medsam_click
        self._medsam = None

        body = QWidget()
        vbox = QVBoxLayout(body)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.addWidget(self.tabs, 1)
        progress_row = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximum(1000)
        self._cancel_btn = QPushButton("취소")
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._cancel_task)
        progress_row.addWidget(self._progress, 1)
        progress_row.addWidget(self._cancel_btn)
        vbox.addLayout(progress_row)
        self._task_label = QLabel()
        self._task_label.setWordWrap(True)
        self._task_label.setStyleSheet("color: #9ab;")
        vbox.addWidget(self._task_label)
        self.setWidget(body)

        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(400)
        self._stats_timer.timeout.connect(self.refresh_stats)

        controller.changed.connect(self._on_mask_changed)
        controller.tool_changed.connect(self._sync_tool_buttons)
        self.labels.changed.connect(self._rebuild_label_list)
        worklist.changed.connect(self._rebuild_worklist)
        self._rebuild_label_list()
        self._rebuild_worklist()
        self._create_shortcuts()

    @staticmethod
    def _scroll(widget):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    # ─── 현재 대상 ───

    def target(self):
        """(series, 현재 슬라이스, (center, width)) - 활성 뷰포트 기준"""
        vp = self.main._target_viewport()
        series = vp.series if vp is not None else None
        if series is None:
            return None, 0, (40.0, 400.0)
        return series, vp.current_slice, vp.window_level

    def _require_series(self):
        series, k, window = self.target()
        if series is None:
            QMessageBox.information(self, "AI Research", "시리즈를 먼저 여세요.")
            return None, 0, window
        case = self.ctl.case(series)
        if not case.editable:
            QMessageBox.warning(self, "AI Research",
                                "슬라이스 크기가 서로 다른 시리즈는 라벨링할 수 없습니다.")
            return None, 0, window
        return series, k, window

    def on_series_changed(self):
        self.refresh_stats()
        series, _, _ = self.target()
        in_list = series is not None and self.worklist.get(series.series_uid) is not None
        self._add_current_btn.setEnabled(series is not None and not in_list)

    # ═══ 세그멘트 탭 ═══

    def _build_segment_tab(self):
        page = QWidget()
        vbox = QVBoxLayout(page)

        tools = QGroupBox("도구")
        tl = QVBoxLayout(tools)
        row = QHBoxLayout()
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(False)
        self._tool_buttons = {}
        for tool, icon, name, key, tip in TOOL_BUTTONS:
            btn = QToolButton()
            btn.setText(f"{icon}\n{name}")
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn.setCheckable(True)
            btn.setMinimumSize(64, 44)
            btn.setToolTip(f"{tip} ({key})\nEsc: 도구 해제")
            btn.clicked.connect(lambda checked, t=tool: self.ctl.set_tool(t if checked else None))
            self._tool_buttons[tool] = btn
            row.addWidget(btn)
        tl.addLayout(row)

        form = QFormLayout()
        size_row = QHBoxLayout()
        self._brush_slider = QSlider(Qt.Horizontal)
        self._brush_slider.setRange(1, 60)
        self._brush_slider.setValue(int(self.ctl.brush_radius))
        self._brush_label = QLabel(f"{int(self.ctl.brush_radius)} px")
        self._brush_slider.valueChanged.connect(self._on_brush_size)
        size_row.addWidget(self._brush_slider, 1)
        size_row.addWidget(self._brush_label)
        form.addRow("브러시 반지름:", size_row)

        wand_row = QHBoxLayout()
        self._wand_tol = QDoubleSpinBox()
        self._wand_tol.setRange(0, 100000)
        self._wand_tol.setDecimals(1)
        self._wand_tol.setValue(self.ctl.wand_tolerance)
        self._wand_tol.setToolTip("클릭한 픽셀 값 ± 이 값 안의 연결된 픽셀을 선택 (CT는 HU)")
        self._wand_tol.valueChanged.connect(lambda v: setattr(self.ctl, "wand_tolerance", v))
        self._wand_3d = QCheckBox("3D")
        self._wand_3d.setToolTip("체크하면 모든 슬라이스에서 연결된 영역까지 선택")
        self._wand_3d.toggled.connect(lambda v: setattr(self.ctl, "wand_3d", v))
        wand_row.addWidget(self._wand_tol, 1)
        wand_row.addWidget(self._wand_3d)
        form.addRow("Wand 허용범위 ±:", wand_row)
        tl.addLayout(form)
        vbox.addWidget(tools)

        # Threshold
        th = QGroupBox("Threshold 세그멘테이션")
        thl = QVBoxLayout(th)
        self._th_preset = QComboBox()
        for name, _ in THRESHOLD_PRESETS:
            self._th_preset.addItem(name)
        self._th_preset.currentIndexChanged.connect(self._on_threshold_preset)
        thl.addWidget(self._th_preset)
        rng = QHBoxLayout()
        self._th_min = QDoubleSpinBox()
        self._th_max = QDoubleSpinBox()
        for spin, value in ((self._th_min, self.ctl.threshold_range[0]),
                            (self._th_max, self.ctl.threshold_range[1])):
            spin.setRange(-100000, 100000)
            spin.setDecimals(1)
            spin.setValue(value)
            spin.valueChanged.connect(self._on_threshold_range)
        rng.addWidget(QLabel("최소"))
        rng.addWidget(self._th_min, 1)
        rng.addWidget(QLabel("최대"))
        rng.addWidget(self._th_max, 1)
        thl.addLayout(rng)
        th_btns = QHBoxLayout()
        b1 = QPushButton("현재 슬라이스에 적용")
        b1.clicked.connect(lambda: self._apply_threshold(all_slices=False))
        b2 = QPushButton("전체 슬라이스에 적용")
        b2.clicked.connect(lambda: self._apply_threshold(all_slices=True))
        th_btns.addWidget(b1)
        th_btns.addWidget(b2)
        thl.addLayout(th_btns)
        hint = QLabel("Threshold 도구(G)를 켜면 범위에 드는 픽셀이 미리보기로 보입니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        thl.addWidget(hint)
        vbox.addWidget(th)

        # 편집
        edit = QGroupBox("편집")
        el = QGridLayout(edit)
        interp = QPushButton("⇕ 슬라이스 보간")
        interp.setToolTip("현재 라벨이 칠해진 슬라이스 사이의 빈 슬라이스를 모양 기반으로 채웁니다.\n"
                          "예: 5, 10, 15번 슬라이스만 칠하고 누르면 사이 슬라이스 자동 생성")
        interp.clicked.connect(self._interpolate)
        undo = QPushButton("↶ 되돌리기")
        undo.setToolTip("마지막 편집 되돌리기 (Ctrl+Z)")
        undo.clicked.connect(self.undo)
        clear_slice = QPushButton("현재 슬라이스 지우기")
        clear_slice.clicked.connect(lambda: self._clear(all_slices=False))
        clear_all = QPushButton("라벨 전체 지우기")
        clear_all.clicked.connect(lambda: self._clear(all_slices=True))
        el.addWidget(interp, 0, 0)
        el.addWidget(undo, 0, 1)
        el.addWidget(clear_slice, 1, 0)
        el.addWidget(clear_all, 1, 1)
        vbox.addWidget(edit)

        # 라벨 매니저
        lab = QGroupBox("라벨 (선택 = 칠할 라벨, 체크 = 표시)")
        ll = QVBoxLayout(lab)
        self._label_list = QListWidget()
        self._label_list.setMinimumHeight(120)
        self._label_list.currentItemChanged.connect(self._on_label_selected)
        self._label_list.itemChanged.connect(self._on_label_item_changed)
        self._label_list.itemDoubleClicked.connect(lambda _item: self._rename_label())
        ll.addWidget(self._label_list)
        lb = QHBoxLayout()
        for text, tip, slot in (("＋", "라벨 추가", self._add_label),
                                ("이름", "이름 변경 (더블클릭)", self._rename_label),
                                ("색상", "색상 변경", self._recolor_label),
                                ("－", "라벨 삭제 (모든 마스크에서 제거)", self._remove_label)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            lb.addWidget(b)
        ll.addLayout(lb)
        vbox.addWidget(lab)

        # 오버레이
        ov = QGroupBox("오버레이")
        ol = QHBoxLayout(ov)
        self._show_overlay = QCheckBox("표시")
        self._show_overlay.setChecked(True)
        self._show_overlay.toggled.connect(self.ctl.set_show_overlay)
        ol.addWidget(self._show_overlay)
        ol.addWidget(QLabel("투명도"))
        self._opacity = QSlider(Qt.Horizontal)
        self._opacity.setRange(5, 100)
        self._opacity.setValue(int(self.ctl.opacity * 100))
        self._opacity.valueChanged.connect(lambda v: self.ctl.set_opacity(v / 100))
        ol.addWidget(self._opacity, 1)
        vbox.addWidget(ov)

        # 통계
        st = QGroupBox("어노테이션 통계 (현재 시리즈)")
        sl = QVBoxLayout(st)
        self._stats_table = QTableWidget(0, 4)
        self._stats_table.setHorizontalHeaderLabels(["라벨", "볼륨 (mL)", "복셀", "슬라이스"])
        self._stats_table.verticalHeader().setVisible(False)
        self._stats_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._stats_table.setMinimumHeight(110)
        sl.addWidget(self._stats_table)
        vbox.addWidget(st)
        vbox.addStretch()
        return page

    def _create_shortcuts(self):
        """D/X/W/G 도구, Ctrl+Z 되돌리기 - 패널이 닫혀 있어도 동작 (누르면 패널 열림)"""
        self.tool_actions = []
        for tool, _icon, name, key, _tip in TOOL_BUTTONS:
            action = QAction(f"Segment: {name}", self.main)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(lambda _=False, t=tool: self.select_tool(t))
            self.main.addAction(action)
            self.tool_actions.append(action)
        self.undo_action = QAction("Segment: Undo", self.main)
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self.undo)
        self.main.addAction(self.undo_action)

    def select_tool(self, tool):
        if not self.isVisible():
            self.show()
            self.raise_()
        self.tabs.setCurrentIndex(0)
        self.ctl.set_tool(None if self.ctl.tool == tool else tool)

    def _sync_tool_buttons(self, tool):
        for t, btn in self._tool_buttons.items():
            btn.setChecked(t == tool)

    def _on_brush_size(self, value):
        self.ctl.brush_radius = float(value)
        self._brush_label.setText(f"{value} px")
        self.ctl.changed.emit("")  # 브러시 커서 크기 갱신

    def _on_threshold_preset(self, index):
        rng = THRESHOLD_PRESETS[index][1]
        if rng is None:
            return
        for spin, v in ((self._th_min, rng[0]), (self._th_max, rng[1])):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
        self._on_threshold_range()

    def _on_threshold_range(self, *_):
        self.ctl.threshold_range = (self._th_min.value(), self._th_max.value())
        if self.ctl.tool == seg.TOOL_THRESHOLD:
            self.ctl.changed.emit("")  # 미리보기 갱신

    def _apply_threshold(self, all_slices):
        series, k, _ = self._require_series()
        if series is None:
            return
        if all_slices:
            self._busy("Threshold 적용 중...")
        self.ctl.apply_threshold(series, None if all_slices else [k])
        self._busy(None)

    def _interpolate(self):
        series, _, _ = self._require_series()
        if series is not None:
            self._busy("슬라이스 보간 중...")
            self.ctl.interpolate(series)
            self._busy(None)

    def undo(self):
        series, _, _ = self.target()
        if series is not None and not self.ctl.undo(series):
            self.main.statusBar().showMessage("되돌릴 편집이 없습니다.", 3000)

    def _clear(self, all_slices):
        series, k, _ = self._require_series()
        if series is None:
            return
        name = self.labels.name(self.ctl.active_label)
        if all_slices and QMessageBox.question(
                self, "라벨 지우기", f"이 시리즈의 모든 슬라이스에서 '{name}' 라벨을 지울까요?\n"
                "(되돌리기로 복구할 수 있습니다)") != QMessageBox.Yes:
            return
        self.ctl.clear_label(series, self.ctl.active_label, None if all_slices else k)

    def _busy(self, text):
        self._task_label.setText(text or "")
        if text:
            self.main.statusBar().showMessage(text)
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()

    # ─── 라벨 매니저 ───

    def _rebuild_label_list(self):
        current = self.ctl.active_label
        self._label_list.blockSignals(True)
        self._label_list.clear()
        for label in self.labels:
            item = QListWidgetItem(_color_icon(label["color"]),
                                   f"{label['id']}. {label['name']}")
            item.setData(Qt.UserRole, label["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if self.labels.is_visible(label["id"])
                               else Qt.Unchecked)
            self._label_list.addItem(item)
            if label["id"] == current:
                self._label_list.setCurrentItem(item)
        if self._label_list.currentItem() is None and self._label_list.count():
            self._label_list.setCurrentRow(0)
            self.ctl.active_label = self._label_list.item(0).data(Qt.UserRole)
        self._label_list.blockSignals(False)
        self.refresh_stats()

    def _selected_label(self):
        item = self._label_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _on_label_selected(self, current, _previous):
        if current is not None:
            self.ctl.active_label = current.data(Qt.UserRole)
            self.ctl.changed.emit("")

    def _on_label_item_changed(self, item):
        self.labels.set_visible(item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    def _add_label(self):
        name, ok = QInputDialog.getText(self, "라벨 추가", "라벨 이름 (예: Spleen, Tumor):")
        if ok and name.strip():
            label = self.labels.add(name.strip())
            if label is None:
                QMessageBox.warning(self, "라벨 추가", "라벨은 최대 255개까지 만들 수 있습니다.")
                return
            self.ctl.active_label = label["id"]
            self._rebuild_label_list()

    def _rename_label(self):
        label_id = self._selected_label()
        if label_id is None:
            return
        name, ok = QInputDialog.getText(self, "라벨 이름", "새 이름:",
                                        text=self.labels.name(label_id))
        if ok:
            self.labels.rename(label_id, name)

    def _recolor_label(self):
        label_id = self._selected_label()
        if label_id is None:
            return
        color = QColorDialog.getColor(QColor(*self.labels.color(label_id)), self, "라벨 색상")
        if color.isValid():
            self.labels.set_color(label_id, (color.red(), color.green(), color.blue()))

    def _remove_label(self):
        label_id = self._selected_label()
        if label_id is None:
            return
        if len(self.labels) <= 1:
            QMessageBox.information(self, "라벨 삭제", "라벨이 하나 이상 있어야 합니다.")
            return
        if QMessageBox.question(
                self, "라벨 삭제",
                f"'{self.labels.name(label_id)}' 라벨을 삭제할까요?\n"
                "열려 있는 시리즈의 마스크에서도 이 라벨이 지워집니다.") != QMessageBox.Yes:
            return
        self.ctl.remove_label_everywhere(label_id)
        self.labels.remove(label_id)

    # ─── 통계 ───

    def _on_mask_changed(self, series_uid):
        if series_uid:
            self.worklist.mark_edited(series_uid)
            self._stats_timer.start()

    def refresh_stats(self):
        series, _, _ = self.target()
        stats = self.ctl.statistics(series) if series is not None else []
        self._stats_table.setRowCount(len(stats))
        for row, s in enumerate(stats):
            name = QTableWidgetItem(_color_icon(self.labels.color(s["id"])), s["name"])
            slices = (f"{s['slices']} ({s['first']}–{s['last']})" if s["slices"] else "0")
            for col, item in enumerate((name, QTableWidgetItem(f"{s['volume_ml']:.2f}"),
                                        QTableWidgetItem(f"{s['voxels']:,}"),
                                        QTableWidgetItem(slices))):
                self._stats_table.setItem(row, col, item)
        if series is not None and self.worklist.get(series.series_uid) is not None:
            self.worklist.set_stats(series.series_uid, {
                s["name"]: {"ml": round(s["volume_ml"], 3), "slices": s["slices"]}
                for s in stats if s["voxels"]})
            self._rebuild_worklist()

    # ═══ 데이터셋 탭 ═══

    def _build_dataset_tab(self):
        page = QWidget()
        vbox = QVBoxLayout(page)
        btns = QGridLayout()
        self._add_current_btn = QPushButton("＋ 현재 시리즈")
        self._add_current_btn.clicked.connect(self._add_current)
        add_all = QPushButton("＋ 불러온 시리즈 전부")
        add_all.clicked.connect(self._add_all_loaded)
        open_btn = QPushButton("열기")
        open_btn.clicked.connect(self._open_selected)
        remove_btn = QPushButton("목록에서 제거")
        remove_btn.clicked.connect(self._remove_selected)
        btns.addWidget(self._add_current_btn, 0, 0)
        btns.addWidget(add_all, 0, 1)
        btns.addWidget(open_btn, 1, 0)
        btns.addWidget(remove_btn, 1, 1)
        vbox.addLayout(btns)

        self._wl_table = QTableWidget(0, 6)
        self._wl_table.setHorizontalHeaderLabels(["환자 ID", "검사일", "모달리티", "시리즈",
                                                  "상태", "라벨"])
        self._wl_table.verticalHeader().setVisible(False)
        self._wl_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._wl_table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self._wl_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self._wl_table.cellDoubleClicked.connect(lambda *_: self._open_selected())
        vbox.addWidget(self._wl_table, 1)
        self._wl_summary = QLabel()
        vbox.addWidget(self._wl_summary)
        hint = QLabel("상태: 칠하기 시작하면 '진행중', 다 끝나면 '완료'로 바꾸세요.\n"
                      "내보내기에서 '완료'된 케이스만 골라 데이터셋을 만들 수 있습니다.")
        hint.setStyleSheet("color: #888;")
        hint.setWordWrap(True)
        vbox.addWidget(hint)
        return page

    def _rebuild_worklist(self):
        entries = list(self.worklist)
        table = self._wl_table
        table.setRowCount(len(entries))
        loaded = {s.series_uid for s in self.main._loader.get_series_list()}
        for row, e in enumerate(entries):
            desc = e.get("description") or "(설명 없음)"
            values = [e.get("patient_id", ""), e.get("study_date", ""), e.get("modality", ""),
                      f"{desc} ({e.get('slices', 0)})"]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, e["series_uid"])
                if e["series_uid"] not in loaded:
                    item.setForeground(QColor(150, 150, 150))
                    item.setToolTip("불러오지 않은 케이스 - 더블클릭하면 폴더를 엽니다\n"
                                    + e.get("folder", ""))
                table.setItem(row, col, item)
            combo = QComboBox()
            combo.addItems(STATUSES)
            combo.setCurrentText(e.get("status", STATUSES[0]))
            combo.currentTextChanged.connect(
                lambda text, uid=e["series_uid"]: self.worklist.set_status(uid, text))
            table.setCellWidget(row, 4, combo)
            stats = e.get("stats") or {}
            summary = ", ".join(f"{n} {v['ml']:.1f}mL" for n, v in stats.items()) or "-"
            table.setItem(row, 5, QTableWidgetItem(summary))
        counts = self.worklist.counts()
        self._wl_summary.setText(f"총 {len(entries)}건 · " +
                                 " · ".join(f"{k} {v}" for k, v in counts.items()))
        if hasattr(self, "_add_current_btn"):
            series, _, _ = self.target()
            self._add_current_btn.setEnabled(
                series is not None and self.worklist.get(series.series_uid) is None)

    def _selected_uid(self):
        row = self._wl_table.currentRow()
        item = self._wl_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item is not None else None

    def _add_current(self):
        series, _, _ = self.target()
        if series is not None:
            self.worklist.add(series)
            if not self.ctl.case(series).is_empty():
                self.worklist.mark_edited(series.series_uid)
            self.refresh_stats()

    def _add_all_loaded(self):
        added = 0
        for series in self.main._loader.get_series_list():
            if series.num_slices >= 1 and self.worklist.add(series):
                added += 1
        self.main.statusBar().showMessage(f"워크리스트에 {added}건 추가", 4000)

    def _remove_selected(self):
        uid = self._selected_uid()
        if uid:
            self.worklist.remove(uid)

    def _open_selected(self):
        uid = self._selected_uid()
        if not uid:
            return
        series = self.main._loader.get_series_by_uid(uid)
        if series is not None:
            self.main._select_series(series)
            return
        entry = self.worklist.get(uid)
        folder = entry.get("folder") if entry else ""
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "워크리스트", f"폴더를 찾을 수 없습니다:\n{folder}")
            return
        self.main.open_series_from_folder(folder, uid)

    # ═══ 내보내기 탭 ═══

    def _build_export_tab(self):
        page = QWidget()
        vbox = QVBoxLayout(page)

        src = QGroupBox("대상")
        sl = QVBoxLayout(src)
        self._src_current = QRadioButton("현재 시리즈")
        self._src_done = QRadioButton("워크리스트: '완료' 케이스")
        self._src_all = QRadioButton("워크리스트: 전체 케이스")
        self._src_current.setChecked(True)
        for r in (self._src_current, self._src_done, self._src_all):
            sl.addWidget(r)
        vbox.addWidget(src)

        fmt = QGroupBox("형식")
        fl = QGridLayout(fmt)
        self._fmt_checks = {}
        tips = {"nifti": "영상 + 라벨을 .nii.gz로 (nnU-Net, MONAI 등)",
                "numpy": "영상(float32) + 마스크(uint8) .npy",
                "png": "슬라이스별 PNG + 마스크 PNG (라벨 번호 = 픽셀 값)",
                "coco": "슬라이스 PNG + COCO JSON (폴리곤·바운딩 박스)",
                "voc": "Pascal VOC (XML 바운딩 박스 + 팔레트 PNG 마스크)",
                "dicom_seg": "DICOM Segmentation 객체 (PACS·3D Slicer·OHIF에서 열람)"}
        for i, (key, name) in enumerate(FORMATS.items()):
            cb = QCheckBox(name)
            cb.setToolTip(tips[key])
            cb.setChecked(key == "nifti")
            self._fmt_checks[key] = cb
            fl.addWidget(cb, i // 2, i % 2)
        self._only_labeled = QCheckBox("2D 형식: 라벨이 있는 슬라이스만")
        fl.addWidget(self._only_labeled, 3, 0, 1, 2)
        vbox.addWidget(fmt)

        split = QGroupBox("Train / Val / Test 분할")
        split.setCheckable(True)
        split.setChecked(False)
        self._split_group = split
        spl = QHBoxLayout(split)
        self._split_spins = []
        for name, value in (("Train", 70), ("Val", 15), ("Test", 15)):
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setSuffix("%")
            spin.setValue(value)
            spl.addWidget(QLabel(name))
            spl.addWidget(spin)
            self._split_spins.append(spin)
        self._seed = QSpinBox()
        self._seed.setRange(0, 999999)
        self._seed.setValue(42)
        self._seed.setToolTip("분할 셔플 시드 (같은 값이면 같은 분할)")
        spl.addWidget(QLabel("seed"))
        spl.addWidget(self._seed)
        vbox.addWidget(split)
        split_hint = QLabel("케이스가 여러 개면 케이스 단위, 하나면 슬라이스 단위(2D 형식)로 나눕니다.")
        split_hint.setWordWrap(True)
        split_hint.setStyleSheet("color: #888;")
        vbox.addWidget(split_hint)

        vbox.addWidget(self._build_preprocess_group())

        btn_row = QHBoxLayout()
        preview = QPushButton("전처리 미리보기")
        preview.clicked.connect(self._preview_preprocess)
        export = QPushButton("📦 내보내기...")
        export.setDefault(True)
        export.clicked.connect(self._export)
        btn_row.addWidget(preview)
        btn_row.addWidget(export)
        vbox.addLayout(btn_row)
        note = QLabel("NIfTI/NumPy/PNG/COCO/VOC에는 환자 정보가 들어가지 않습니다.\n"
                      "DICOM SEG는 원본 검사를 참조하므로 환자 정보가 포함됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        vbox.addWidget(note)
        vbox.addStretch()
        return page

    def _build_preprocess_group(self):
        group = QGroupBox("전처리 (내보낼 때 적용)")
        form = QFormLayout(group)

        crop_row = QHBoxLayout()
        self._pp_crop = QCheckBox("라벨 영역으로 크롭, 여유")
        self._pp_margin = QDoubleSpinBox()
        self._pp_margin.setRange(0, 500)
        self._pp_margin.setValue(10)
        self._pp_margin.setSuffix(" mm")
        crop_row.addWidget(self._pp_crop)
        crop_row.addWidget(self._pp_margin)
        form.addRow("크롭:", crop_row)

        rs_row = QHBoxLayout()
        self._pp_resample = QCheckBox()
        rs_row.addWidget(self._pp_resample)
        self._pp_spacing = []
        for name, value in (("x", 1.0), ("y", 1.0), ("z", 1.0)):
            spin = QDoubleSpinBox()
            spin.setRange(0.05, 20)
            spin.setDecimals(2)
            spin.setValue(value)
            spin.setToolTip({"x": "열 방향 (mm)", "y": "행 방향 (mm)", "z": "슬라이스 방향 (mm)"}[name])
            rs_row.addWidget(QLabel(name))
            rs_row.addWidget(spin)
            self._pp_spacing.append(spin)
        form.addRow("리샘플링 (mm):", rs_row)

        dn_row = QHBoxLayout()
        self._pp_denoise = QComboBox()
        self._pp_denoise.addItems(["없음", "가우시안", "미디안"])
        self._pp_denoise_param = QDoubleSpinBox()
        self._pp_denoise_param.setRange(0.1, 15)
        self._pp_denoise_param.setValue(1.0)
        self._pp_denoise_param.setToolTip("가우시안: σ (복셀) / 미디안: 커널 크기")
        self._pp_denoise.currentIndexChanged.connect(
            lambda i: self._pp_denoise_param.setValue(1.0 if i != 2 else 3))
        dn_row.addWidget(self._pp_denoise, 1)
        dn_row.addWidget(self._pp_denoise_param)
        form.addRow("노이즈 제거:", dn_row)

        self._pp_hist = QComboBox()
        self._pp_hist.setToolTip("선택한 기준 시리즈의 밝기 분포에 맞춥니다 (MR 시리즈 간 밝기 통일 등)")
        self._pp_hist.addItem("사용 안 함", None)
        self._pp_hist.view().setMinimumWidth(320)
        self._pp_hist.showPopup = self._wrap_hist_popup(self._pp_hist.showPopup)
        form.addRow("히스토그램 매칭:", self._pp_hist)

        nm_row = QHBoxLayout()
        self._pp_normalize = QCheckBox()
        self._pp_wc = QDoubleSpinBox()
        self._pp_ww = QDoubleSpinBox()
        for spin, v in ((self._pp_wc, 40), (self._pp_ww, 400)):
            spin.setRange(-100000, 100000)
            spin.setValue(v)
        self._pp_ww.setMinimum(1)
        use_wl = QPushButton("현재 W/L")
        use_wl.clicked.connect(self._use_current_window)
        nm_row.addWidget(self._pp_normalize)
        nm_row.addWidget(QLabel("L"))
        nm_row.addWidget(self._pp_wc)
        nm_row.addWidget(QLabel("W"))
        nm_row.addWidget(self._pp_ww)
        nm_row.addWidget(use_wl)
        form.addRow("정규화 0-1:", nm_row)
        return group

    def _wrap_hist_popup(self, original):
        """펼칠 때 불러온 시리즈 목록으로 갱신"""
        def show():
            current = self._pp_hist.currentData()
            self._pp_hist.clear()
            self._pp_hist.addItem("사용 안 함", None)
            for s in self.main._loader.get_series_list():
                self._pp_hist.addItem(f"{s.modality} {s.description} ({s.num_slices})",
                                      s.series_uid)
            index = self._pp_hist.findData(current)
            self._pp_hist.setCurrentIndex(max(0, index))
            original()
        return show

    def _use_current_window(self):
        _, _, (center, width) = self.target()
        self._pp_wc.setValue(center)
        self._pp_ww.setValue(width)

    def _preprocess_options(self, load_reference=True):
        o = PreprocessOptions()
        o.crop = self._pp_crop.isChecked()
        o.crop_margin_mm = self._pp_margin.value()
        o.resample = self._pp_resample.isChecked()
        x, y, z = (s.value() for s in self._pp_spacing)
        o.target_spacing = (z, y, x)
        o.denoise = {1: "gaussian", 2: "median"}.get(self._pp_denoise.currentIndex())
        o.gaussian_sigma = self._pp_denoise_param.value()
        o.median_size = max(1, int(round(self._pp_denoise_param.value())))
        o.normalize = self._pp_normalize.isChecked()
        o.window = (self._pp_wc.value(), self._pp_ww.value())
        ref_uid = self._pp_hist.currentData()
        if ref_uid and load_reference:
            ref = self.main._loader.get_series_by_uid(ref_uid)
            if ref is not None:
                o.histogram_reference = load_volume(ref).array
        return o

    def _export_options(self):
        o = ExportOptions()
        o.formats = {k for k, cb in self._fmt_checks.items() if cb.isChecked()}
        if self._split_group.isChecked():
            ratios = tuple(s.value() for s in self._split_spins)
            o.split = ratios if sum(ratios) > 0 else None
            o.seed = self._seed.value()
        o.only_labeled_slices = self._only_labeled.isChecked()
        return o

    def _export_sources(self):
        """[(이름, series 또는 (folder, uid), window)]"""
        if self._src_current.isChecked():
            series, _, window = self._require_series()
            return [("case_001", series, window)] if series is not None else []
        only_done = self._src_done.isChecked()
        sources = []
        entries = [e for e in self.worklist
                   if not only_done or e.get("status") == STATUS_DONE]
        for i, e in enumerate(entries, start=1):
            series = self.main._loader.get_series_by_uid(e["series_uid"])
            target = series if series is not None else (e.get("folder", ""), e["series_uid"])
            window = self.main._window_for(series) if series is not None else None
            sources.append((f"case_{i:03d}", target, window))
        return sources

    def _export(self):
        options = self._export_options()
        if not options.formats:
            QMessageBox.information(self, "내보내기", "형식을 하나 이상 고르세요.")
            return
        sources = self._export_sources()
        if not sources:
            if not self._src_current.isChecked():
                QMessageBox.information(self, "내보내기", "내보낼 워크리스트 케이스가 없습니다.")
            return
        out = QFileDialog.getExistingDirectory(self, "내보낼 폴더 선택")
        if not out:
            return
        if os.listdir(out) and QMessageBox.question(
                self, "내보내기", "폴더가 비어 있지 않습니다. 같은 이름의 파일은 덮어씁니다.\n"
                "계속할까요?") != QMessageBox.Yes:
            return
        try:
            options.preprocess = self._preprocess_options()
        except ValueError as e:
            QMessageBox.warning(self, "전처리", f"히스토그램 기준 시리즈를 읽지 못했습니다:\n{e}")
            return
        self.ctl.save_all()  # 편집 중인 마스크를 파일에 반영
        labels = self.labels.to_list()
        # 불러온 시리즈의 마스크는 여기(UI 스레드)에서 복사해 넘김
        prepared = []
        for name, target, window in sources:
            if isinstance(target, tuple):
                prepared.append((name, target, None, window))
            else:
                case = self.ctl.case(target)
                prepared.append((name, target,
                                 None if case.is_empty() else case.mask.copy(), window))

        def task(progress, cancelled):
            from ..dicom_loader import DicomLoader
            cases = []
            for name, target, mask, window in prepared:
                if isinstance(target, tuple):
                    folder, uid = target
                    progress(f"{name}: 폴더 읽는 중 {folder}", 0.0)
                    loader = DicomLoader()
                    loader.load_directory(folder)
                    series = loader.get_series_by_uid(uid)
                    if series is None:
                        raise ValueError(f"{folder}에서 시리즈를 찾지 못했습니다.")
                    case = seg.SegCase(series)
                    mask = None if case.is_empty() else case.mask
                    window = window or series.get_default_window()
                    target = series
                cases.append((name, target, mask, window))
            return export_cases(cases, out, options, labels, progress, cancelled)

        def done(summary):
            n = len(summary["cases"])
            QMessageBox.information(
                self, "내보내기 완료",
                f"{n}개 케이스를 내보냈습니다.\n형식: {', '.join(FORMATS[f] for f in summary['formats'])}\n"
                + (f"전처리: {', '.join(summary['preprocessing'])}\n" if summary["preprocessing"] else "")
                + out)
        self._run_task("내보내는 중...", task, done)

    def _preview_preprocess(self):
        series, k, window = self._require_series()
        if series is None:
            return
        try:
            options = self._preprocess_options()
        except ValueError as e:
            QMessageBox.warning(self, "전처리", str(e))
            return
        case = self.ctl.case(series)
        mask = None if case.is_empty() else case.mask.copy()

        def task(progress, cancelled):
            progress("볼륨 읽는 중...", 0.1)
            volume = load_volume(series)
            after, after_mask = apply_preprocess(volume, mask, options,
                                                 progress=lambda s: progress(s, 0.5))
            # 현재 슬라이스 중심과 같은 환자 좌표에 있는 전처리 후 슬라이스
            h, w = volume.shape[1:]
            world = volume.affine_lps @ np.array([(w - 1) / 2, (h - 1) / 2, k, 1.0])
            index = np.linalg.solve(after.affine_lps, world)
            k2 = int(np.clip(round(index[2]), 0, after.shape[0] - 1))
            return volume.array[k], after.array[k2], after, options

        def done(result):
            before, after_slice, after, opts = result
            PreprocessPreview(before, after_slice, window, opts, after, self).exec_()
        self._run_task("전처리 미리보기 계산 중...", task, done)

    # ═══ 모델 탭 ═══

    def _build_model_tab(self):
        page = QWidget()
        vbox = QVBoxLayout(page)

        mon = QGroupBox("MONAI Label 서버")
        ml = QVBoxLayout(mon)
        url_row = QHBoxLayout()
        self._monai_url = QLineEdit(self.main._app_settings.monai_url())
        self._monai_url.setPlaceholderText("http://127.0.0.1:8000 (Settings → AI)")
        connect = QPushButton("연결")
        connect.clicked.connect(self._monai_connect)
        url_row.addWidget(self._monai_url, 1)
        url_row.addWidget(connect)
        ml.addLayout(url_row)
        self._monai_info = QLabel("연결 안 됨")
        self._monai_info.setWordWrap(True)
        self._monai_info.setStyleSheet("color: #888;")
        ml.addWidget(self._monai_info)
        form = QFormLayout()
        self._monai_model = QComboBox()
        form.addRow("모델:", self._monai_model)
        ml.addLayout(form)
        run = QPushButton("🤖 자동 세그멘테이션 (현재 시리즈)")
        run.clicked.connect(self._monai_infer)
        ml.addWidget(run)
        fb_row = QHBoxLayout()
        submit = QPushButton("수정한 라벨 제출")
        submit.setToolTip("현재 마스크를 서버 데이터스토어에 최종(final) 라벨로 제출합니다.\n"
                          "서버가 이 라벨로 모델을 다시 학습합니다 (active learning).")
        submit.clicked.connect(self._monai_submit)
        train = QPushButton("학습 시작")
        train.clicked.connect(self._monai_train)
        fb_row.addWidget(submit)
        fb_row.addWidget(train)
        ml.addLayout(fb_row)
        vbox.addWidget(mon)

        onnx = QGroupBox("ONNX 모델 (로컬 추론)")
        ol = QVBoxLayout(onnx)
        path_row = QHBoxLayout()
        self._onnx_path = QLineEdit()
        self._onnx_path.setPlaceholderText(".onnx 파일")
        self._onnx_path.setReadOnly(True)
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._onnx_browse)
        path_row.addWidget(self._onnx_path, 1)
        path_row.addWidget(browse)
        ol.addLayout(path_row)
        self._onnx_info = QLabel("onnxruntime " + ("사용 가능" if ONNX_AVAILABLE else "없음 (설치 필요)"))
        self._onnx_info.setWordWrap(True)
        self._onnx_info.setStyleSheet("color: #888;")
        ol.addWidget(self._onnx_info)
        oform = QFormLayout()
        norm_row = QHBoxLayout()
        self._onnx_wc = QDoubleSpinBox()
        self._onnx_ww = QDoubleSpinBox()
        for spin, v in ((self._onnx_wc, 40), (self._onnx_ww, 400)):
            spin.setRange(-100000, 100000)
            spin.setValue(v)
        self._onnx_ww.setMinimum(1)
        self._onnx_use_view = QCheckBox("현재 W/L")
        self._onnx_use_view.setChecked(True)
        norm_row.addWidget(self._onnx_use_view)
        norm_row.addWidget(QLabel("L"))
        norm_row.addWidget(self._onnx_wc)
        norm_row.addWidget(QLabel("W"))
        norm_row.addWidget(self._onnx_ww)
        oform.addRow("입력 정규화:", norm_row)
        ol.addLayout(oform)
        hint = QLabel("입력 (N,C,H,W) = 슬라이스별 2D, (N,C,D,H,W) = 3D. 윈도잉 후 0~1로 넣습니다.\n"
                      "출력 채널이 여러 개면 argmax (채널 번호 = 라벨 번호),\n"
                      "하나면 0.5 이상을 현재 선택한 라벨로 칠합니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        ol.addWidget(hint)
        onnx_run = QPushButton("▶ ONNX 추론 (현재 시리즈)")
        onnx_run.clicked.connect(self._onnx_run)
        ol.addWidget(onnx_run)
        vbox.addWidget(onnx)

        apply_box = QGroupBox("결과 적용")
        al = QVBoxLayout(apply_box)
        self._apply_mode = QComboBox()
        self._apply_mode.addItems(["빈 곳만 채우기 (기존 라벨 유지)", "교체 (기존 마스크 덮어쓰기)"])
        al.addWidget(self._apply_mode)
        al.addWidget(QLabel("결과는 반투명 오버레이로 표시되며 되돌리기(Ctrl+Z)로 취소할 수 있습니다."))
        vbox.addWidget(apply_box)
        vbox.addStretch()
        return page

    def _monai_client(self):
        url = self._monai_url.text().strip()
        if url and url != self.main._app_settings.monai_url():
            self.main._app_settings.set_monai_url(url)
        return MonaiLabelClient(url, self.main._app_settings.monai_token())

    def _monai_connect(self):
        client = self._monai_client()

        def task(progress, cancelled):
            progress("서버 정보 요청 중...", 0.3)
            return client.info()

        def done(info):
            self._monai_models = MonaiLabelClient.models(info)
            self._monai_model.clear()
            for name, kind, labels in self._monai_models:
                self._monai_model.addItem(f"{name} ({kind}) - {', '.join(labels)}", name)
            self._monai_info.setText(f"연결됨: {info.get('name', '')} {info.get('version', '')} · "
                                     f"모델 {len(self._monai_models)}개")
        self._run_task("MONAI Label 연결 중...", task, done)

    def _monai_labels_for(self, model):
        for name, _kind, labels in self._monai_models:
            if name == model:
                return labels
        return {}

    def _monai_infer(self):
        series, _, _ = self._require_series()
        model = self._monai_model.currentData()
        if series is None:
            return
        if not model:
            QMessageBox.information(self, "MONAI Label", "먼저 서버에 연결하고 모델을 고르세요.")
            return
        client = self._monai_client()
        server_labels = self._monai_labels_for(model)

        def task(progress, cancelled):
            progress("볼륨 읽는 중...", 0.1)
            volume = load_volume(series)
            progress(f"서버에서 '{model}' 추론 중... (시간이 걸릴 수 있습니다)", 0.4)
            return client.infer(model, volume)

        def done(result):
            # 서버 라벨 번호 → 로컬 라벨 (이름으로 맞추고 없으면 새로 만듦)
            mapped = np.zeros_like(result)
            names = {v: k for k, v in server_labels.items()}
            for value in np.unique(result):
                if value == 0:
                    continue
                local = self.labels.ensure(names.get(int(value), f"{model} {value}"), int(value))
                mapped[result == value] = local["id"]
            self._apply_result(series, mapped, f"MONAI Label '{model}'")
        self._run_task("MONAI Label 자동 세그멘테이션...", task, done)

    def _monai_submit(self):
        series, _, _ = self._require_series()
        if series is None:
            return
        case = self.ctl.case(series)
        if case.is_empty():
            QMessageBox.information(self, "MONAI Label", "제출할 라벨이 없습니다.")
            return
        client = self._monai_client()
        mask = case.mask.copy()
        label_info = [{"name": l["name"], "idx": l["id"]} for l in self.labels
                      if (mask == l["id"]).any()]
        image_id = image_id_for(series.series_uid)

        def task(progress, cancelled):
            progress("볼륨 읽는 중...", 0.1)
            volume = load_volume(series)
            progress("영상 업로드 중...", 0.4)
            client.upload_image(image_id, volume)
            progress("라벨 제출 중...", 0.8)
            client.submit_label(image_id, mask, volume, label_info)
            return image_id

        def done(iid):
            QMessageBox.information(self, "MONAI Label",
                                    f"라벨을 제출했습니다 (영상 ID: {iid}).\n"
                                    "'학습 시작'으로 모델을 다시 학습할 수 있습니다.")
        self._run_task("라벨 제출 중...", task, done)

    def _monai_train(self):
        model = self._monai_model.currentData()
        if not model:
            QMessageBox.information(self, "MONAI Label", "먼저 서버에 연결하고 모델을 고르세요.")
            return
        client = self._monai_client()
        self._run_task("학습 요청 중...", lambda p, c: client.train(model),
                       lambda r: QMessageBox.information(self, "MONAI Label",
                                                         f"학습을 시작했습니다.\n{r[:300]}"))

    def _onnx_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "ONNX 모델", "", "ONNX (*.onnx)")
        if not path:
            return
        try:
            self._onnx = OnnxSegmenter(path)
        except OnnxModelError as e:
            QMessageBox.warning(self, "ONNX", str(e))
            return
        self._onnx_path.setText(path)
        self._onnx_info.setText(self._onnx.describe())

    def _onnx_run(self):
        series, _, window = self._require_series()
        if series is None:
            return
        if self._onnx is None:
            QMessageBox.information(self, "ONNX", "먼저 .onnx 모델을 고르세요.")
            return
        if not self._onnx_use_view.isChecked():
            window = (self._onnx_wc.value(), self._onnx_ww.value())
        model = self._onnx
        label = self.ctl.active_label

        def task(progress, cancelled):
            progress("볼륨 읽는 중...", 0.05)
            volume = load_volume(series)
            result = model.predict(volume.array, window, single_label=label,
                                   progress=lambda i, n: progress(f"추론 {i}/{n}", i / max(n, 1)),
                                   cancelled=cancelled)
            if result is None:
                raise Cancelled()
            return result

        def done(result):
            for value in np.unique(result):
                if value and self.labels.get(int(value)) is None:
                    self.labels.ensure(f"Class {value}", int(value))
            self._apply_result(series, result, "ONNX")
        self._run_task("ONNX 추론 중...", task, done)

    # ═══ 오픈소스 모델 (Models 탭) ═══

    def show_models_tab(self):
        if not self.isVisible():
            self.show()
        self.raise_()
        self.tabs.setCurrentWidget(self.models_page)

    def apply_model_result(self, series, mask, names, source):
        """모델 결과 → 라벨 목록에 구조 이름·색 등록 (번호가 겹치면 새 번호로) → 오버레이. 구조 수"""
        from .model_hub import distinct_colors
        values = [int(v) for v in np.unique(mask) if v]
        colors = distinct_colors(len(values))
        lut = np.zeros(256, dtype=np.uint8)
        for i, value in enumerate(values):
            name = names.get(value) or f"{source} {value}"
            existed = self.labels.by_name(name) is not None
            label = self.labels.ensure(name, value)
            if label is None:   # 라벨 255개가 가득 참
                continue
            if not existed and names.get(value):
                self.labels.set_color(label["id"], colors[i])
            lut[value] = label["id"]
        self._apply_result(series, lut[mask], source)
        return len(values)

    def run_onnx_file(self, path):
        """Settings에 지정한 ONNX 모델을 불러와 기존 ONNX 추론 실행"""
        try:
            self._onnx = OnnxSegmenter(path)
        except OnnxModelError as e:
            QMessageBox.warning(self, "ONNX", str(e))
            return
        self._onnx_path.setText(path)
        self._onnx_info.setText(self._onnx.describe())
        self._onnx_run()

    def prepare_export(self, formats):
        """내보내기 탭을 현재 시리즈 + 지정 형식으로 맞추고 내보내기 시작"""
        self._src_current.setChecked(True)
        for key, cb in self._fmt_checks.items():
            cb.setChecked(key in formats)
        self.tabs.setCurrentIndex(2)
        self._export()

    def refresh_medsam_state(self):
        get = self.main._app_settings.model_value
        ready = bool(get("medsam_encoder") and get("medsam_decoder"))
        button = self._tool_buttons.get(seg.TOOL_MEDSAM)
        if button is not None:
            button.setToolTip(button.toolTip().split("\n")[0] + "\n"
                              + ("모델 준비됨" if ready else "Settings → AI에서 MedSAM ONNX 파일을 지정하세요")
                              + "\nEsc: 도구 해제")

    def enable_medsam(self):
        self.select_tool(seg.TOOL_MEDSAM)

    def _medsam_model(self):
        from .medsam import SamError, SamSegmenter
        get = self.main._app_settings.model_value
        enc, dec, mode = get("medsam_encoder"), get("medsam_decoder"), get("medsam_mode")
        if not (enc and dec and os.path.exists(enc) and os.path.exists(dec)):
            QMessageBox.information(self, "MedSAM", "Settings → AI에서 MedSAM 인코더/디코더 .onnx 파일을 지정하세요.")
            return None
        if self._medsam is None or self._medsam.paths != (enc, dec) or self._medsam.mode != mode:
            try:
                self._medsam = SamSegmenter(enc, dec, mode)
            except SamError as e:
                QMessageBox.warning(self, "MedSAM", str(e))
                return None
        return self._medsam

    def _medsam_click(self, series, k, pos):
        """MedSAM 도구 클릭: 그 슬라이스에서 클릭한 구조를 찾아 현재 라벨로 (백그라운드)"""
        from .. import dicom_info
        from .medsam import click_box
        model = self._medsam_model()
        if model is None or self._worker is not None:
            return
        image = series.get_pixel_array(k)
        if image is None or image.ndim != 2:
            return
        _s, _k, window = self.target()
        get = self.main._app_settings.model_value
        index = (pos[0] - 0.5, pos[1] - 0.5)   # 이미지 좌표 → 픽셀 인덱스
        box = point = None
        if get("medsam_prompt") == "point":
            point = index
        else:
            spacing = dicom_info.pixel_spacing(series.slices[k]) or (1.0, 1.0)
            try:
                size = float(get("medsam_box_mm") or 40)
            except ValueError:
                size = 40.0
            box = click_box(index, size, spacing, image.shape)
        label = self.ctl.active_label

        def task(progress, cancelled):
            progress("MedSAM 추론 중…", 0.3)
            return model.segment((series.series_uid, k), image, window, point=point, box=box)

        def done(region):
            n = self.ctl.apply_slice_mask(series, k, region, label)
            self.main.statusBar().showMessage(
                f"MedSAM: {n:,} 픽셀을 '{self.labels.name(label)}' 라벨로 (Ctrl+Z로 되돌리기)", 6000)
        self._run_task("MedSAM 추론 중…", task, done)

    def _apply_result(self, series, result, source):
        case = self.ctl.case(series)
        if self._apply_mode.currentIndex() == 0 and not case.is_empty():
            merged = case.mask.copy()
            empty = merged == 0
            merged[empty] = result[empty]
            result = merged
        self.ctl.set_mask(series, result)
        n = int((result > 0).sum())
        self.main.statusBar().showMessage(f"{source} 결과 적용: {n:,} 복셀 (Ctrl+Z로 되돌리기)", 8000)
        self.refresh_stats()

    # ═══ 백그라운드 작업 ═══

    def _run_task(self, text, fn, on_success):
        if self._worker is not None:
            QMessageBox.information(self, "AI Research", "다른 작업이 진행 중입니다.")
            return
        worker = TaskWorker(fn, self)
        worker.progress.connect(self._on_task_progress)
        worker.succeeded.connect(lambda result: self._task_done(on_success, result))
        worker.failed.connect(self._task_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._cancel_btn.setVisible(True)
        self._task_label.setText(text)
        worker.start()

    def _on_task_progress(self, text, fraction):
        self._task_label.setText(text)
        self._progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))

    def _finish_task(self):
        self._worker = None
        self._progress.setVisible(False)
        self._cancel_btn.setVisible(False)

    def _task_done(self, on_success, result):
        self._finish_task()
        self._task_label.setText("")
        on_success(result)

    def _task_failed(self, message):
        self._finish_task()
        self._task_label.setText(f"실패: {message}")
        QMessageBox.warning(self, "AI Research", message)

    def _cancel_task(self):
        if self._worker is not None:
            self._worker.cancel()
            self._task_label.setText("취소하는 중...")

    def shutdown(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(3000)
        self.ctl.save_all()


class PreprocessPreview(QDialog):
    """전처리 전/후 슬라이스 비교"""

    def __init__(self, before, after, window, options, after_volume, parent=None):
        super().__init__(parent)
        self.setWindowTitle("전처리 미리보기")
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        normalized = options.normalize
        for title, arr, norm in (("원본", before, False), ("전처리 후", after, normalized)):
            box = QVBoxLayout()
            box.addWidget(QLabel(title))
            img = QLabel()
            img.setPixmap(self._pixmap(arr, window, norm))
            box.addWidget(img)
            row.addLayout(box)
        layout.addLayout(row)
        steps = ", ".join(options.describe()) or "(전처리 없음)"
        sp = ", ".join(f"{v:.2f}" for v in after_volume.spacing)
        layout.addWidget(QLabel(f"{steps}\n결과 크기 {after_volume.shape} · 간격 (z, y, x) = {sp} mm"))
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignRight)

    @staticmethod
    def _pixmap(arr, window, normalized, size=360):
        if normalized:
            img = np.clip(arr, 0, 1) * 255
        else:
            center, width = window
            img = np.clip((arr - (center - width / 2)) / max(width, 1) * 255, 0, 255)
        img = np.ascontiguousarray(img.astype(np.uint8))
        h, w = img.shape
        pix = QPixmap.fromImage(QImage(img.data, w, h, w, QImage.Format_Grayscale8).copy())
        return pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
