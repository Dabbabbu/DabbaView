# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
AI Research 패널의 📊 Analysis 탭

정합 · 융합 · 랜드마크 · 표면 모델 · 입자 분석 · 컬러맵 · 매크로
"""
import os

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QHeaderView, QLabel, QListWidget, QMessageBox,
                             QPushButton, QScrollArea, QSlider, QSpinBox, QTableWidget,
                             QTableWidgetItem, QToolBox, QVBoxLayout, QWidget)

from . import colormaps, derived_series, registration as reg
from .fusion import BLEND_MODES, FusionLayer
from .measure import PARTICLE_COLUMNS, analyze_particles, save_particles_csv


def _series_combo():
    combo = QComboBox()
    combo.setMinimumContentsLength(18)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    return combo


class AnalysisTab(QWidget):
    def __init__(self, main_window, panel):
        super().__init__()
        self.main = main_window
        self.panel = panel
        self._transform = None
        self._surfaces = []   # (이름, polydata)
        self._particles = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toolbox = QToolBox()
        self.sections = {}
        for key, builder, title in (
                ("registration", self._build_registration, "🎯 Image Registration (정합)"),
                ("fusion", self._build_fusion, "🌈 Image Fusion (융합)"),
                ("landmarks", self._build_landmarks, "📍 Landmarks / Fiducials"),
                ("surface", self._build_surface, "🧊 Surface Model (표면 모델)"),
                ("particles", self._build_particles, "🔬 Particle Analysis (입자 분석)"),
                ("colormap", self._build_colormap, "🎨 Color Map (LUT)"),
                ("macros", self._build_macros, "⚡ Macros")):
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setWidget(builder())
            self.sections[key] = self.toolbox.count()
            self.toolbox.addItem(area, title)
        layout.addWidget(self.toolbox, 1)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #9ab;")
        layout.addWidget(self._status)
        self.main._landmarks.changed.connect(self._rebuild_landmarks)
        self.main._macros.changed.connect(self._rebuild_macros)
        self._rebuild_macros()

    # ─── 공통 ───

    def _series(self):
        return self.main._loader.get_series_list()

    def _fill_series(self, combo, prefer=None):
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for s in self._series():
            combo.addItem(f"{s.modality} · {s.description or '(설명 없음)'} ({s.num_slices})",
                          s.series_uid)
        index = combo.findData(prefer or current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _by_uid(self, uid):
        return self.main._loader.get_series_by_uid(uid) if uid else None

    def refresh_series(self):
        vp_series = self.main._target_viewport().series
        current = vp_series.series_uid if vp_series else None
        self._fill_series(self._reg_fixed, current)
        self._fill_series(self._reg_moving)
        self._fill_series(self._fusion_series)
        self._fill_labels()

    def show_section(self, key):
        self.refresh_series()
        self.toolbox.setCurrentIndex(self.sections[key])

    def _run(self, text, fn, done):
        self.panel._run_task(text, fn, done)

    # ═══ 정합 ═══

    def _build_registration(self):
        page = QWidget()
        form = QFormLayout(page)
        self._reg_fixed = _series_combo()
        self._reg_moving = _series_combo()
        form.addRow("기준 (fixed):", self._reg_fixed)
        form.addRow("이동 (moving):", self._reg_moving)
        self._reg_method = QComboBox()
        for key, name in reg.METHODS.items():
            self._reg_method.addItem(name, key)
        form.addRow("방법:", self._reg_method)
        self._reg_metric = QComboBox()
        for key, name in reg.METRICS.items():
            self._reg_metric.addItem(name, key)
        form.addRow("Metric:", self._reg_metric)
        self._reg_iter = QSpinBox()
        self._reg_iter.setRange(20, 2000)
        self._reg_iter.setValue(300)
        form.addRow("최대 반복:", self._reg_iter)
        refresh = QPushButton("시리즈 목록 새로 고침")
        refresh.clicked.connect(self.refresh_series)
        form.addRow(refresh)
        run = QPushButton("🎯 정합 실행")
        run.clicked.connect(self._register)
        form.addRow(run)
        row = QHBoxLayout()
        save = QPushButton("변환 저장…")
        save.clicked.connect(self._save_transform)
        load = QPushButton("변환 불러와 적용…")
        load.clicked.connect(self._load_transform)
        row.addWidget(save)
        row.addWidget(load)
        form.addRow(row)
        self._reg_result = QLabel("결과: moving을 fixed 격자로 옮긴 새 시리즈 + 융합 표시")
        self._reg_result.setWordWrap(True)
        self._reg_result.setStyleSheet("color: #999;")
        form.addRow(self._reg_result)
        return page

    def _reg_pair(self):
        fixed = self._by_uid(self._reg_fixed.currentData())
        moving = self._by_uid(self._reg_moving.currentData())
        if fixed is None or moving is None:
            QMessageBox.information(self, "정합", "기준·이동 시리즈를 고르세요 (새로 고침).")
            return None, None
        if fixed is moving:
            QMessageBox.information(self, "정합", "서로 다른 두 시리즈를 고르세요.")
            return None, None
        return fixed, moving

    def _register(self):
        fixed, moving = self._reg_pair()
        if fixed is None:
            return
        method = self._reg_method.currentData()
        metric = self._reg_metric.currentData()
        iterations = self._reg_iter.value()

        def task(progress, cancelled):
            from ..ai.volume import load_volume
            progress("볼륨 읽는 중...", 0.02)
            fv, mv = load_volume(fixed), load_volume(moving)
            transform, value = reg.register(fv, mv, method, iterations, 0.2, progress,
                                            cancelled, metric)
            progress("다시 샘플링 중...", 0.97)
            return transform, value, reg.resample_to(fv, mv, transform)

        def done(result):
            transform, value, array = result
            self._transform = transform
            self._apply_registered(fixed, moving, array,
                                   f"{reg.describe(transform)} · metric {value:.4f}")
        self._run("정합 중...", task, done)

    def _apply_registered(self, fixed, moving, array, text):
        name = f"{moving.description} → {fixed.description} (정합)"
        series = derived_series(array, fixed, name, "registered", modality=moving.modality)
        self.main.add_derived_series(series, select=False)
        self._reg_result.setText(f"완료: {text}\n새 시리즈: {name}")
        # 기준 시리즈 위에 정합 결과를 컬러로 겹쳐 표시
        self.main._select_series(fixed)
        self.refresh_series()
        self._fusion_series.setCurrentIndex(max(0, self._fusion_series.findData(series.series_uid)))
        self._fusion_lut.setCurrentText("Hot")
        self._enable_fusion(True)
        self.toolbox.setCurrentIndex(self.sections["fusion"])

    def _save_transform(self):
        if self._transform is None:
            QMessageBox.information(self, "정합", "먼저 정합을 실행하세요.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "변환 저장", "registration.tfm",
                                              "ITK Transform (*.tfm *.txt *.h5)")
        if path:
            reg.save_transform(path, self._transform)
            self._status.setText(f"변환 저장: {path}")

    def _load_transform(self):
        fixed, moving = self._reg_pair()
        if fixed is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "변환 불러오기", "",
                                              "ITK Transform (*.tfm *.txt *.h5)")
        if not path:
            return
        try:
            transform = reg.load_transform(path)
        except RuntimeError as e:
            QMessageBox.warning(self, "정합", f"변환 파일을 읽지 못했습니다:\n{e}")
            return

        def task(progress, cancelled):
            from ..ai.volume import load_volume
            progress("적용 중...", 0.3)
            return reg.resample_to(load_volume(fixed), load_volume(moving), transform)

        def done(array):
            self._transform = transform
            self._apply_registered(fixed, moving, array,
                                   f"{os.path.basename(path)}: {reg.describe(transform)}")
        self._run("변환 적용 중...", task, done)

    # ═══ 융합 ═══

    def _build_fusion(self):
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(QLabel("현재 보고 있는 시리즈(기준) 위에 겹칩니다."))
        self._fusion_series = _series_combo()
        form.addRow("겹칠 시리즈:", self._fusion_series)
        self._fusion_lut = QComboBox()
        self._fusion_lut.addItems(colormaps.names())
        self._fusion_lut.setCurrentText("Hot")
        form.addRow("컬러맵:", self._fusion_lut)
        self._fusion_mode = QComboBox()
        self._fusion_mode.addItems(BLEND_MODES)
        form.addRow("Blend:", self._fusion_mode)
        op_row = QHBoxLayout()
        self._fusion_opacity = QSlider(Qt.Horizontal)
        self._fusion_opacity.setRange(0, 100)
        self._fusion_opacity.setValue(50)
        self._fusion_opacity_label = QLabel("50%")
        op_row.addWidget(self._fusion_opacity, 1)
        op_row.addWidget(self._fusion_opacity_label)
        form.addRow("투명도:", op_row)
        self._fusion_checker = QSpinBox()
        self._fusion_checker.setRange(4, 256)
        self._fusion_checker.setValue(32)
        self._fusion_checker.setSuffix(" px")
        form.addRow("체커보드 칸:", self._fusion_checker)
        wl_row = QHBoxLayout()
        self._fusion_wc = QDoubleSpinBox()
        self._fusion_ww = QDoubleSpinBox()
        for spin in (self._fusion_wc, self._fusion_ww):
            spin.setRange(-100000, 100000)
            spin.setDecimals(1)
        self._fusion_ww.setMinimum(1)
        auto = QPushButton("자동")
        auto.setToolTip("겹칠 시리즈의 기본 W/L")
        auto.clicked.connect(self._fusion_auto_window)
        wl_row.addWidget(QLabel("L"))
        wl_row.addWidget(self._fusion_wc)
        wl_row.addWidget(QLabel("W"))
        wl_row.addWidget(self._fusion_ww)
        wl_row.addWidget(auto)
        form.addRow("W/L:", wl_row)
        btns = QHBoxLayout()
        on = QPushButton("🌈 융합 켜기")
        on.clicked.connect(lambda: self._enable_fusion(True))
        off = QPushButton("끄기")
        off.clicked.connect(lambda: self._enable_fusion(False))
        btns.addWidget(on)
        btns.addWidget(off)
        form.addRow(btns)
        for w in (self._fusion_lut, self._fusion_mode):
            w.currentIndexChanged.connect(self._update_fusion)
        self._fusion_opacity.valueChanged.connect(self._update_fusion)
        self._fusion_checker.valueChanged.connect(self._update_fusion)
        self._fusion_wc.valueChanged.connect(self._update_fusion)
        self._fusion_ww.valueChanged.connect(self._update_fusion)
        self._fusion_series.currentIndexChanged.connect(self._fusion_series_changed)
        self._fusion_layer = None
        return page

    def _fusion_series_changed(self, *_):
        """융합 중에 겹칠 시리즈를 바꾸면 W/L을 새 시리즈에 맞춰 바로 다시 표시"""
        self._fusion_auto_window()
        if self.main._target_viewport().fusion is not None:
            self._enable_fusion(True)

    def _fusion_auto_window(self):
        series = self._by_uid(self._fusion_series.currentData())
        if series is None:
            return
        c, w = series.get_default_window()
        for spin, v in ((self._fusion_wc, c), (self._fusion_ww, w)):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)

    def _enable_fusion(self, on):
        vp = self.main._target_viewport()
        if not on:
            vp.set_fusion(None)
            self._fusion_layer = None
            return
        series = self._by_uid(self._fusion_series.currentData())
        if vp.series is None or series is None:
            QMessageBox.information(self, "융합", "기준 시리즈를 열고 겹칠 시리즈를 고르세요.")
            return
        if series is vp.series:
            QMessageBox.information(self, "융합", "현재 시리즈와 다른 시리즈를 고르세요.")
            return
        if self._fusion_ww.value() <= 1:
            self._fusion_auto_window()
        from ..ai.volume import load_volume
        try:
            volume = load_volume(series)
        except ValueError as e:
            QMessageBox.warning(self, "융합", str(e))
            return
        self._fusion_layer = FusionLayer(volume, (0, 1), None)
        self._update_fusion()
        vp.set_fusion(self._fusion_layer)
        self._status.setText(f"융합: {series.description} ({self._fusion_mode.currentText()})")

    def _update_fusion(self, *_):
        self._fusion_opacity_label.setText(f"{self._fusion_opacity.value()}%")
        layer = self._fusion_layer
        if layer is None:
            return
        layer.lut = colormaps.get_lut(self._fusion_lut.currentText())
        layer.mode = self._fusion_mode.currentText()
        layer.opacity = self._fusion_opacity.value() / 100.0
        layer.checker = self._fusion_checker.value()
        layer.window = (self._fusion_wc.value(), self._fusion_ww.value())
        self.main._target_viewport().refresh_fusion()

    # ═══ 랜드마크 ═══

    def _build_landmarks(self):
        page = QWidget()
        v = QVBoxLayout(page)
        tool = QPushButton("📍 랜드마크 도구 (F) - 영상 클릭으로 점 추가")
        tool.clicked.connect(lambda: self.main.select_tool_by_id("landmark"))
        v.addWidget(tool)
        self._lm_table = QTableWidget(0, 5)
        self._lm_table.setHorizontalHeaderLabels(["이름", "라벨", "X (L)", "Y (P)", "Z (S)"])
        self._lm_table.verticalHeader().setVisible(False)
        self._lm_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._lm_table.setMinimumHeight(140)
        self._lm_table.itemChanged.connect(self._landmark_edited)
        self._lm_table.cellDoubleClicked.connect(self._go_to_landmark)
        v.addWidget(self._lm_table)
        row = QHBoxLayout()
        for text, slot in (("삭제", self._delete_landmark), ("전체 삭제", self._clear_landmarks),
                           ("CSV…", self._export_landmarks_csv),
                           ("JSON…", self._export_landmarks_json),
                           ("불러오기…", self._import_landmarks)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)
        self._lm_3d = QCheckBox("3D Volume 탭에도 표시")
        self._lm_3d.setChecked(True)
        self._lm_3d.toggled.connect(self._landmarks_to_3d)
        v.addWidget(self._lm_3d)
        hint = QLabel("좌표는 환자 좌표 LPS (mm). JSON은 3D Slicer Markups(.mrk.json) 형식.\n"
                      "더블클릭하면 그 위치로 이동합니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        v.addWidget(hint)
        return page

    def _rebuild_landmarks(self):
        table = self._lm_table
        table.blockSignals(True)
        points = list(self.main._landmarks)
        table.setRowCount(len(points))
        for r, p in enumerate(points):
            items = [QTableWidgetItem(p["name"]), QTableWidgetItem(p["label"])]
            for v in p["position"]:
                item = QTableWidgetItem(f"{v:.2f}")
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                items.append(item)
            for c, item in enumerate(items):
                table.setItem(r, c, item)
        table.blockSignals(False)
        self._landmarks_to_3d()

    def _landmark_edited(self, item):
        field = {0: "name", 1: "label"}.get(item.column())
        if field:
            self.main._landmarks.update(item.row(), **{field: item.text()})

    def _go_to_landmark(self, row, _col):
        points = list(self.main._landmarks)
        if 0 <= row < len(points):
            vp = self.main._target_viewport()
            vp.set_reference_point(np.array(points[row]["position"]))

    def _delete_landmark(self):
        self.main._landmarks.remove(self._lm_table.currentRow())

    def _clear_landmarks(self):
        if len(self.main._landmarks) and QMessageBox.question(
                self, "랜드마크", "모든 랜드마크를 지울까요?") == QMessageBox.Yes:
            self.main._landmarks.clear()

    def _export_landmarks_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "랜드마크 CSV", "landmarks.csv", "CSV (*.csv)")
        if path:
            self.main._landmarks.save_csv(path)
            self._status.setText(f"저장: {path}")

    def _export_landmarks_json(self):
        path, _ = QFileDialog.getSaveFileName(self, "랜드마크 JSON (3D Slicer)",
                                              "landmarks.mrk.json", "Markups JSON (*.json)")
        if path:
            self.main._landmarks.save_json(path)
            self._status.setText(f"저장: {path}")

    def _import_landmarks(self):
        path, _ = QFileDialog.getOpenFileName(self, "랜드마크 불러오기", "",
                                              "Markups JSON (*.json)")
        if path:
            try:
                n = self.main._landmarks.load_json(path)
                self._status.setText(f"랜드마크 {n}개 불러옴")
            except (OSError, ValueError, KeyError) as e:
                QMessageBox.warning(self, "랜드마크", f"읽지 못했습니다: {e}")

    def _landmarks_to_3d(self, *_):
        widget = self.main._volume_widget
        if hasattr(widget, "set_landmarks"):
            widget.set_landmarks(list(self.main._landmarks) if self._lm_3d.isChecked() else [])

    # ═══ 표면 모델 ═══

    def _build_surface(self):
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(QLabel("AI 세그멘테이션 라벨 → Marching Cubes 3D 메시"))
        self._surf_label = QComboBox()
        form.addRow("라벨:", self._surf_label)
        self._surf_smooth = QSpinBox()
        self._surf_smooth.setRange(0, 200)
        self._surf_smooth.setValue(20)
        self._surf_smooth.setToolTip("Windowed Sinc 스무딩 반복 (0 = 끔)")
        form.addRow("스무딩:", self._surf_smooth)
        self._surf_decimate = QSpinBox()
        self._surf_decimate.setRange(0, 95)
        self._surf_decimate.setValue(50)
        self._surf_decimate.setSuffix(" %")
        self._surf_decimate.setToolTip("줄일 삼각형 비율 (데시메이션)")
        form.addRow("데시메이션:", self._surf_decimate)
        make = QPushButton("🧊 표면 생성 → 3D 뷰")
        make.clicked.connect(self._make_surface)
        form.addRow(make)
        row = QHBoxLayout()
        export = QPushButton("STL/OBJ 내보내기…")
        export.clicked.connect(self._export_surface)
        clear = QPushButton("3D 메시 지우기")
        clear.clicked.connect(lambda: hasattr(self.main._volume_widget, "clear_meshes")
                              and self.main._volume_widget.clear_meshes())
        row.addWidget(export)
        row.addWidget(clear)
        form.addRow(row)
        self._surf_info = QLabel()
        self._surf_info.setWordWrap(True)
        form.addRow(self._surf_info)
        return page

    def _fill_labels(self):
        combo = self._surf_label
        current = combo.currentData()
        combo.clear()
        for label in self.main._labels:
            combo.addItem(f"{label['id']}. {label['name']}", label["id"])
        combo.setCurrentIndex(max(0, combo.findData(current)))
        pcombo = self._part_label
        current = pcombo.currentData()
        pcombo.clear()
        pcombo.addItem("임계값 범위 사용", None)
        for label in self.main._labels:
            pcombo.addItem(f"AI 라벨: {label['name']}", label["id"])
        pcombo.setCurrentIndex(max(0, pcombo.findData(current)))

    def _make_surface(self):
        from . import surface
        vp = self.main._target_viewport()
        series = vp.series
        label_id = self._surf_label.currentData()
        if series is None or label_id is None:
            self._fill_labels()
            QMessageBox.information(self, "표면 모델", "시리즈를 열고 라벨을 고르세요.")
            return
        case = self.main._seg.case(series)
        if not case.editable or case.is_empty():
            QMessageBox.information(self, "표면 모델",
                                    "이 시리즈에 AI 세그멘테이션이 없습니다 (세그멘트 탭에서 칠하세요).")
            return
        mask = case.mask.copy()
        affine = case.affine_lps
        smooth, dec = self._surf_smooth.value(), self._surf_decimate.value() / 100.0
        name = self.main._labels.name(label_id)
        color = self.main._labels.color(label_id)

        def task(progress, cancelled):
            progress("Marching Cubes...", 0.3)
            poly = surface.label_surface(mask, label_id, affine, smooth, dec)
            return poly, surface.mesh_stats(poly)

        def done(result):
            poly, (tris, area, vol_ml) = result
            self._surfaces.append((name, poly))
            widget = self.main._volume_widget
            if hasattr(widget, "add_polydata"):
                widget.add_polydata(poly, name, color)
                self.main._tab_widget.setCurrentWidget(widget)
            self._surf_info.setText(f"{name}: 삼각형 {tris:,} · 표면적 {area:,.0f} mm² · "
                                    f"부피 {vol_ml:.2f} mL")
        self._run("표면 생성 중...", task, done)

    def _export_surface(self):
        from . import surface
        if not self._surfaces:
            QMessageBox.information(self, "표면 모델", "먼저 표면을 생성하세요.")
            return
        name, poly = self._surfaces[-1]
        path, _ = QFileDialog.getSaveFileName(self, "메시 내보내기", f"{name}.stl",
                                              "STL (*.stl);;OBJ (*.obj);;PLY (*.ply)")
        if path:
            try:
                surface.save_mesh(path, poly)
                self._status.setText(f"저장: {path}")
            except OSError as e:
                QMessageBox.warning(self, "표면 모델", str(e))

    # ═══ 입자 분석 ═══

    def _build_particles(self):
        page = QWidget()
        v = QVBoxLayout(page)
        form = QFormLayout()
        self._part_label = QComboBox()
        form.addRow("대상:", self._part_label)
        th_row = QHBoxLayout()
        self._part_min = QDoubleSpinBox()
        self._part_max = QDoubleSpinBox()
        for spin, value in ((self._part_min, 100), (self._part_max, 3000)):
            spin.setRange(-100000, 100000)
            spin.setValue(value)
        th_row.addWidget(self._part_min)
        th_row.addWidget(QLabel("~"))
        th_row.addWidget(self._part_max)
        form.addRow("임계값:", th_row)
        self._part_min_area = QSpinBox()
        self._part_min_area.setRange(1, 1000000)
        self._part_min_area.setValue(10)
        self._part_min_area.setSuffix(" px")
        form.addRow("최소 면적:", self._part_min_area)
        self._part_edges = QCheckBox("가장자리에 닿은 객체 제외")
        form.addRow(self._part_edges)
        v.addLayout(form)
        row = QHBoxLayout()
        run = QPushButton("🔬 현재 슬라이스 분석")
        run.clicked.connect(self._analyze_particles)
        export = QPushButton("CSV…")
        export.clicked.connect(self._export_particles)
        row.addWidget(run)
        row.addWidget(export)
        v.addLayout(row)
        self._part_table = QTableWidget(0, len(PARTICLE_COLUMNS))
        self._part_table.setHorizontalHeaderLabels([c[1] for c in PARTICLE_COLUMNS])
        self._part_table.verticalHeader().setVisible(False)
        self._part_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._part_table.setMinimumHeight(150)
        self._part_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        v.addWidget(self._part_table)
        self._part_summary = QLabel()
        v.addWidget(self._part_summary)
        return page

    def _analyze_particles(self):
        vp = self.main._target_viewport()
        series = vp.series
        if series is None:
            return
        k = vp.current_slice
        label_id = self._part_label.currentData()
        if label_id is None:
            image = series.get_pixel_array(k)
            binary = (image >= self._part_min.value()) & (image <= self._part_max.value())
        else:
            case = self.main._seg.case(series)
            if not case.editable or case.is_empty():
                QMessageBox.information(self, "입자 분석", "AI 세그멘테이션이 없습니다.")
                return
            binary = case.mask[k] == label_id
        spacing = vp._spacing() or (1.0, 1.0)
        geom = series.geometry
        to_world = (lambda c, r: geom.pixel_to_patient(k, c, r)) if geom is not None else None
        self._particles = analyze_particles(binary, spacing, self._part_min_area.value(),
                                            to_world, self._part_edges.isChecked())
        self._particles_slice = k
        table = self._part_table
        table.setRowCount(len(self._particles))
        for r, p in enumerate(self._particles):
            for c, (key, _label) in enumerate(PARTICLE_COLUMNS):
                v = p[key]
                table.setItem(r, c, QTableWidgetItem(f"{v:.3f}" if isinstance(v, float) else str(v)))
        if self._particles:
            areas = np.array([p["area_mm2"] for p in self._particles])
            self._part_summary.setText(
                f"slice {k + 1}: 객체 {len(areas)}개 · 총 면적 {areas.sum():.1f} mm² · "
                f"평균 {areas.mean():.1f} mm² · 평균 원형도 "
                f"{np.mean([p['circularity'] for p in self._particles]):.3f}")
        else:
            self._part_summary.setText(f"slice {k + 1}: 객체 없음")

    def _export_particles(self):
        if not self._particles:
            QMessageBox.information(self, "입자 분석", "먼저 분석하세요.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "입자 분석 CSV", "particles.csv", "CSV (*.csv)")
        if path:
            save_particles_csv(path, self._particles, self._particles_slice)
            self._status.setText(f"저장: {path}")

    # ═══ 컬러맵 ═══

    def _build_colormap(self):
        page = QWidget()
        form = QFormLayout(page)
        self._lut_combo = QComboBox()
        self._lut_combo.addItems(colormaps.names())
        self._lut_combo.currentTextChanged.connect(self.main.apply_colormap)
        form.addRow("컬러맵:", self._lut_combo)
        load = QPushButton("LUT 파일 불러오기… (ImageJ .lut / 텍스트)")
        load.clicked.connect(self._load_lut)
        form.addRow(load)
        form.addRow(QLabel("현재 뷰포트에 적용되며 오른쪽에 컬러바가 표시됩니다.\n"
                           "Process → Color Map 메뉴에서도 바꿀 수 있습니다."))
        return page

    def _load_lut(self):
        path, _ = QFileDialog.getOpenFileName(self, "LUT 불러오기", "",
                                              "LUT (*.lut *.txt *.csv);;All Files (*)")
        if not path:
            return
        try:
            name, _lut = colormaps.load_lut(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "LUT", str(e))
            return
        for combo in (self._lut_combo, self._fusion_lut):
            if combo.findText(name) < 0:
                combo.addItem(name)
        self._lut_combo.setCurrentText(name)
        self.main.rebuild_colormap_menu()

    def sync_colormap(self, name):
        self._lut_combo.blockSignals(True)
        self._lut_combo.setCurrentText(name)
        self._lut_combo.blockSignals(False)

    # ═══ 매크로 ═══

    def _build_macros(self):
        page = QWidget()
        v = QVBoxLayout(page)
        self._macro_list = QListWidget()
        self._macro_list.setMinimumHeight(140)
        self._macro_list.itemDoubleClicked.connect(lambda _i: self._run_macro())
        v.addWidget(self._macro_list)
        row = QHBoxLayout()
        for text, tip, slot in (("▶ 실행", "선택한 매크로 실행 (더블클릭)", self._run_macro),
                                ("편집", "Python 콘솔에서 열기", self._edit_macro),
                                ("삭제", "", self._delete_macro),
                                ("폴더", "매크로 폴더 열기", self._open_macro_folder)):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)
        hint = QLabel("Python 콘솔(F3)에서 '매크로로 저장'하면 여기에 추가됩니다.\n"
                      "매크로는 콘솔과 같은 app / np / ndi / plt / skimage를 씁니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        v.addWidget(hint)
        return page

    def _rebuild_macros(self):
        self._macro_list.clear()
        self._macro_list.addItems(self.main._macros.names())

    def _selected_macro(self):
        item = self._macro_list.currentItem()
        return item.text() if item is not None else None

    def _run_macro(self):
        name = self._selected_macro()
        if name:
            console = self.main._console
            console.show()
            ok = console.execute(self.main._macros.load(name), label=f"[매크로] {name}")
            self._status.setText(f"매크로 '{name}' " + ("실행 완료" if ok else "오류 - 콘솔 확인"))

    def _edit_macro(self):
        name = self._selected_macro()
        if name:
            self.main._console.load_code(self.main._macros.load(name),
                                         self.main._macros.path(name))

    def _delete_macro(self):
        name = self._selected_macro()
        if name and QMessageBox.question(self, "매크로", f"'{name}' 매크로를 지울까요?") == QMessageBox.Yes:
            self.main._macros.delete(name)

    def _open_macro_folder(self):
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.main._macros.folder))

