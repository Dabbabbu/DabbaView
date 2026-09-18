"""
RadiantView 메인 윈도우
"""
import os
import threading
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QSplitter, QToolBar,
    QAction, QActionGroup, QFileDialog, QStatusBar,
    QSlider, QLabel, QProgressDialog, QMessageBox,
    QSpinBox, QApplication, QMenuBar, QTabWidget, QMenu
)
from PyQt5.QtCore import Qt, QSize, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QIcon, QKeySequence, QFont

from .dicom_loader import DicomLoader
from .viewport import DicomViewport
from .tag_viewer import TagViewer
from .multi_viewport import MultiViewport
from .mpr_viewer import MPRWidget
from .volume_renderer import VolumeRenderWidget, VTK_AVAILABLE
from .anonymizer import AnonymizeDialog
from .video_exporter import VideoExportDialog
from .series_tree import SeriesTreeWidget
from .cursor_sync import CursorSyncController


MAX_RECENT_PATHS = 10


class DirectoryLoadWorker(QThread):
    """폴더 로딩을 백그라운드 스레드에서 수행하고 진행률을 시그널로 전달"""

    progress = pyqtSignal(int, int)          # current, total
    finished_loading = pyqtSignal(object, int)  # DicomLoader, loaded count

    def __init__(self, paths, target_viewport=None, remember=True,
                 parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self.paths = list(self._paths)
        self.remember = remember  # 성공 시 Recent Files에 기록할지
        # None: 기존 목록 교체 / 정수: 기존 목록에 추가 후 Multi View 해당 뷰포트에 표시
        self.target_viewport = target_viewport
        self._cancel_event = threading.Event()
        self._last_percent = -1

    def cancel(self):
        self._cancel_event.set()

    def was_cancelled(self):
        return self._cancel_event.is_set()

    def _on_progress(self, current, total):
        # 파일마다 시그널을 보내면 UI 이벤트 큐가 넘치므로 1% 단위로만 전달
        percent = current * 100 // total
        if percent != self._last_percent or current == total:
            self._last_percent = percent
            self.progress.emit(current, total)

    def run(self):
        # 새 로더에 채운 뒤 완료 시 교체 → 로딩 중에도 기존 시리즈를 안전하게 볼 수 있음
        loader = DicomLoader()
        loaded = loader.load_paths(
            self._paths, progress_callback=self._on_progress,
            cancel_event=self._cancel_event)
        self.finished_loading.emit(loader, loaded)


class MainWindow(QMainWindow):
    """RadiantView 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RadiantView - DICOM Viewer")
        self.resize(1400, 900)

        self._loader = DicomLoader()
        self._current_series = None
        self._load_worker = None
        self._load_progress = None
        # MPR/3D는 전체 픽셀을 읽어야 하므로 해당 탭을 열 때만 구성
        self._mpr_series = None
        self._volume_series = None
        # 마지막으로 연 폴더: 열기 대화상자의 시작 위치로만 사용 (자동 로드 안 함)
        self._settings = QSettings("RadiantView", "RadiantView")

        self._init_ui()
        self._cursor_sync = CursorSyncController(self)
        self._init_menubar()
        self._init_toolbar()
        self._init_statusbar()
        self._connect_signals()

        # 다크 테마
        self._apply_dark_theme()

    def _init_ui(self):
        """UI 초기화"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 스플리터: 왼쪽(시리즈 목록) | 오른쪽(뷰포트)
        splitter = QSplitter(Qt.Horizontal)

        # ─── 왼쪽 패널: 시리즈 목록 ───
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)

        left_layout.addWidget(QLabel("📁 Series"))
        self._series_tree = SeriesTreeWidget()
        self._series_tree.series_selected.connect(self._on_series_selected)
        left_layout.addWidget(self._series_tree)

        splitter.addWidget(left_panel)

        # ─── 오른쪽: 탭 위젯 (2D / Multi / MPR / 3D) ───
        self._tab_widget = QTabWidget()
        self._tab_widget.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                background: #2d2d2d; color: #aaa;
                padding: 8px 16px; border: none;
                border-bottom: 2px solid transparent;
            }
            QTabBar::tab:selected {
                color: white; border-bottom: 2px solid #007acc;
            }
            QTabBar::tab:hover { color: #ddd; }
        """)

        # 탭 1: 단일 뷰포트
        self._viewport = DicomViewport()
        self._tab_widget.addTab(self._viewport, "2D View")

        # 탭 2: 다중 뷰포트
        self._multi_viewport = MultiViewport()
        self._tab_widget.addTab(self._multi_viewport, "Multi View")

        # 탭 3: MPR
        self._mpr_widget = MPRWidget()
        self._tab_widget.addTab(self._mpr_widget, "MPR")

        # 탭 4: 3D Volume
        self._volume_widget = VolumeRenderWidget()
        self._tab_widget.addTab(self._volume_widget,
                                "3D Volume" if VTK_AVAILABLE else "3D (VTK 필요)")

        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        splitter.addWidget(self._tab_widget)

        # 트리 3단계(환자/검사/시리즈) 라벨이 잘리지 않도록 넓게
        left_panel.setMinimumWidth(220)
        splitter.setSizes([340, 1060])
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter)

    def _init_menubar(self):
        """메뉴바"""
        menubar = self.menuBar()

        # File 메뉴
        file_menu = menubar.addMenu("&File")

        open_file = QAction("Open DICOM File...", self)
        open_file.setShortcut(QKeySequence("Ctrl+O"))
        open_file.triggered.connect(self._open_file)
        file_menu.addAction(open_file)

        open_dir = QAction("Open DICOM Folder...", self)
        open_dir.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_dir.triggered.connect(self._open_directory)
        file_menu.addAction(open_dir)

        # 최근 연 파일/폴더 (열 때마다 목록을 새로 구성)
        self._recent_menu = QMenu("Recent Files", self)
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        file_menu.addMenu(self._recent_menu)

        file_menu.addSeparator()

        export_action = QAction("Export as Image...", self)
        export_action.setShortcut(QKeySequence("Ctrl+S"))
        export_action.triggered.connect(self._export_image)
        file_menu.addAction(export_action)

        export_video_action = QAction("Export as Video...", self)
        export_video_action.setShortcut(QKeySequence("Ctrl+Shift+E"))
        export_video_action.triggered.connect(self._export_video)
        file_menu.addAction(export_video_action)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # View 메뉴
        view_menu = menubar.addMenu("&View")

        reset_view = QAction("Reset View", self)
        reset_view.setShortcut(QKeySequence("R"))
        reset_view.triggered.connect(self._viewport.reset_view)
        view_menu.addAction(reset_view)

        invert_action = QAction("Invert", self)
        invert_action.setShortcut(QKeySequence("I"))
        invert_action.triggered.connect(
            lambda: self._viewport.keyPressEvent(
                type('', (), {'key': lambda: Qt.Key_I})()))
        view_menu.addAction(invert_action)

        view_menu.addSeparator()

        overlay_action = QAction("Toggle Overlay", self)
        overlay_action.setShortcut(QKeySequence("O"))
        overlay_action.triggered.connect(self._toggle_overlay)
        view_menu.addAction(overlay_action)

        # Tools 메뉴
        tools_menu = menubar.addMenu("&Tools")

        tags_action = QAction("DICOM Tags...", self)
        tags_action.setShortcut(QKeySequence("Ctrl+T"))
        tags_action.triggered.connect(self._show_tags)
        tools_menu.addAction(tags_action)

        tools_menu.addSeparator()

        anon_action = QAction("Anonymize Series...", self)
        anon_action.triggered.connect(self._show_anonymize)
        tools_menu.addAction(anon_action)

        tools_menu.addSeparator()

        clear_meas = QAction("Clear All Measurements", self)
        clear_meas.triggered.connect(self._viewport.clear_measurements)
        tools_menu.addAction(clear_meas)

        # Window presets 메뉴
        preset_menu = menubar.addMenu("&Presets")
        presets = [
            ("Brain", 40, 80),
            ("Subdural", 75, 215),
            ("Stroke", 40, 40),
            ("Bone", 400, 2000),
            ("Lung", -600, 1600),
            ("Abdomen", 60, 400),
            ("Liver", 80, 150),
            ("Soft Tissue", 50, 350),
            ("Spine", 50, 250),
            ("Mediastinum", 50, 350),
        ]
        for name, wc, ww in presets:
            action = QAction(f"{name} (C:{wc} W:{ww})", self)
            action.triggered.connect(
                lambda checked, c=wc, w=ww: self._viewport.set_window(c, w))
            preset_menu.addAction(action)

    def _init_toolbar(self):
        """도구 모음"""
        toolbar = QToolBar("Tools")
        toolbar.setIconSize(QSize(24, 24))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        tool_group = QActionGroup(self)
        tool_group.setExclusive(True)

        tools = [
            ("🖱 Window", DicomViewport.TOOL_WINDOW, "1",
             "윈도잉 (좌클릭 드래그)"),
            ("✋ Pan", DicomViewport.TOOL_PAN, "2",
             "팬 (좌클릭 드래그)"),
            ("🔍 Zoom", DicomViewport.TOOL_ZOOM, "3",
             "줌 (좌클릭 드래그 또는 Ctrl+휠)"),
            ("📏 Distance", DicomViewport.TOOL_MEASURE, "4",
             "거리 측정 (클릭→클릭)"),
            ("📐 Angle", DicomViewport.TOOL_ANGLE, "5",
             "각도 측정 (3점 클릭)"),
        ]

        for label, tool_id, shortcut, tooltip in tools:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.setToolTip(tooltip)
            action.triggered.connect(
                lambda checked, t=tool_id: self._viewport.set_tool(t))
            tool_group.addAction(action)
            toolbar.addAction(action)
            if tool_id == DicomViewport.TOOL_WINDOW:
                action.setChecked(True)

        toolbar.addSeparator()

        # 시네 재생
        cine_action = QAction("▶ Play", self)
        cine_action.setShortcut(QKeySequence("Space"))
        cine_action.triggered.connect(self._viewport.toggle_cine)
        toolbar.addAction(cine_action)

        toolbar.addSeparator()

        # FPS 조절
        toolbar.addWidget(QLabel(" FPS: "))
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(15)
        self._fps_spin.valueChanged.connect(self._viewport.set_cine_fps)
        toolbar.addWidget(self._fps_spin)

        toolbar.addSeparator()

        # 슬라이스 슬라이더
        toolbar.addWidget(QLabel(" Slice: "))
        self._slice_slider = QSlider(Qt.Horizontal)
        self._slice_slider.setMinimum(0)
        self._slice_slider.setMaximum(0)
        self._slice_slider.setFixedWidth(200)
        self._slice_slider.valueChanged.connect(self._viewport.go_to_slice)
        toolbar.addWidget(self._slice_slider)

        self._slice_label = QLabel(" 0/0 ")
        toolbar.addWidget(self._slice_label)

        toolbar.addSeparator()

        # 크로스 레퍼런스
        self._sync_action = QAction("⌖ Sync Cursor", self)
        self._sync_action.setCheckable(True)
        self._sync_action.setShortcut(QKeySequence("C"))
        self._sync_action.setToolTip(
            "크로스 레퍼런스 (C)\n"
            "켜면 좌클릭/드래그 위치가 같은 좌표계(Frame of Reference)의\n"
            "다른 뷰포트·MPR에 십자선으로 표시되고 가장 가까운 슬라이스로 이동합니다.\n"
            "켜져 있는 동안 윈도잉은 가운데 버튼 드래그로 조절하세요.")
        self._sync_action.toggled.connect(self._cursor_sync.set_enabled)
        toolbar.addAction(self._sync_action)

    def _init_statusbar(self):
        """상태바"""
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._status_wl = QLabel("W: 0  L: 0")
        self._status_zoom = QLabel("Zoom: 100%")
        self._status_pos = QLabel("")
        self._statusbar.addWidget(self._status_wl)
        self._statusbar.addWidget(self._status_zoom)
        self._statusbar.addPermanentWidget(self._status_pos)

    def _connect_signals(self):
        """시그널 연결"""
        # 크로스 레퍼런스 참여 뷰: 2D, Multi View 4개, MPR
        self._cursor_sync.add_view(self._viewport)
        for vp in self._multi_viewport.viewports:
            self._cursor_sync.add_view(vp)
        self._cursor_sync.add_view(self._mpr_widget)
        self._cursor_sync.synced.connect(self._on_cursor_synced)

        # Multi View 드래그 앤 드롭
        self._multi_viewport.series_dropped.connect(self._on_series_dropped)
        self._multi_viewport.paths_dropped.connect(self._on_paths_dropped)

        self._viewport.slice_changed.connect(self._on_slice_changed)
        self._viewport.window_changed.connect(self._on_window_changed)
        self._viewport.zoom_changed.connect(self._on_zoom_changed)
        self._viewport.measurement_completed.connect(
            self._on_measurement_completed)

    # ─── 파일 열기 ───

    def _open_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Open DICOM File", self._last_dir(),
            "DICOM Files (*.dcm *.DCM *.dicom);;All Files (*)")
        if filepath:
            self.load_path(filepath)

    def _open_directory(self):
        dirpath = QFileDialog.getExistingDirectory(
            self, "Open DICOM Folder", self._last_dir())
        if dirpath:
            self.load_path(dirpath)

    def _last_dir(self):
        path = self._settings.value("last_dir", "", type=str)
        return path if path and os.path.isdir(path) else os.path.expanduser("~")

    def load_path(self, path, remember=True):
        """파일 또는 폴더 경로 로드

        remember=True면 열기 대화상자의 다음 시작 위치로 기억 (자동 로드는 하지 않음)
        """
        self.load_paths([path], remember=remember)

    def load_paths(self, paths, remember=True, target_viewport=None):
        """파일/폴더 경로들을 백그라운드에서 로드

        target_viewport가 None이면 기존 시리즈 목록을 교체하고,
        정수면 기존 목록에 추가한 뒤 Multi View의 해당 뷰포트에 첫 시리즈 표시.
        """
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        if remember:
            first = paths[0]
            folder = first if os.path.isdir(first) else os.path.dirname(first)
            self._settings.setValue("last_dir", folder)
        self.load_directory_async(paths, target_viewport, remember)

    # ─── 최근 파일 ───

    def recent_paths(self):
        paths = self._settings.value("recent_paths", [], type=list)
        return [p for p in paths if isinstance(p, str) and p]

    def _add_recent_paths(self, paths):
        recent = self.recent_paths()
        for path in reversed(paths):
            path = os.path.abspath(path)
            if path in recent:
                recent.remove(path)
            recent.insert(0, path)
        self._settings.setValue("recent_paths", recent[:MAX_RECENT_PATHS])

    def _clear_recent_paths(self):
        self._settings.remove("recent_paths")

    def _rebuild_recent_menu(self):
        menu = self._recent_menu
        menu.clear()
        paths = self.recent_paths()
        if not paths:
            empty = menu.addAction("(최근 항목 없음)")
            empty.setEnabled(False)
        for path in paths:
            name = os.path.basename(path.rstrip(os.sep)) or path
            icon = "📁" if os.path.isdir(path) else "📄"
            action = menu.addAction(f"{icon}  {name}    —  {os.path.dirname(path)}")
            action.setToolTip(path)
            if not os.path.exists(path):
                # 이동/삭제된 경로는 표시만 하고 열 수 없게
                action.setText(f"{name}  (찾을 수 없음)")
                action.setEnabled(False)
            action.triggered.connect(
                lambda checked=False, p=path: self.load_path(p))
        menu.addSeparator()
        clear = menu.addAction("Clear Recent")
        clear.setEnabled(bool(paths))
        clear.triggered.connect(self._clear_recent_paths)

    def load_directory_async(self, paths, target_viewport=None,
                             remember=True):
        """백그라운드 스레드에서 로딩. UI는 시그널로만 갱신"""
        if self._load_worker is not None:
            self._statusbar.showMessage("이미 불러오는 중입니다.", 3000)
            return
        if isinstance(paths, str):
            paths = [paths]

        progress = QProgressDialog("Loading DICOM files...",
                                   "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(500)
        progress.setValue(0)

        worker = DirectoryLoadWorker(paths, target_viewport, remember, self)
        worker.progress.connect(self._on_load_progress)
        worker.finished_loading.connect(self._on_load_finished)
        worker.finished.connect(worker.deleteLater)
        progress.canceled.connect(worker.cancel)

        self._load_worker = worker
        self._load_progress = progress
        worker.start()

    def _on_load_progress(self, current, total):
        if self._load_progress is not None:
            self._load_progress.setLabelText(
                f"Loading DICOM files... ({current}/{total})")
            self._load_progress.setValue(current * 100 // total)

    def _on_load_finished(self, loader, loaded):
        cancelled = self._load_worker.was_cancelled()
        target_viewport = self._load_worker.target_viewport
        loaded_paths = self._load_worker.paths
        remember = self._load_worker.remember
        self._load_worker = None
        if self._load_progress is not None:
            self._load_progress.close()
            self._load_progress = None

        if cancelled:
            self._statusbar.showMessage("Loading cancelled", 5000)
            return

        if loaded == 0:
            QMessageBox.warning(
                self, "Warning",
                "No DICOM files found in the selected folder.")
            return

        # 실제로 불러오기에 성공한 경로만 최근 목록에 기록
        if remember:
            self._add_recent_paths(loaded_paths)

        if target_viewport is None:
            self._loader = loader
            self._update_series_list()
        else:
            # 기존 목록에 추가하고, 드롭한 뷰포트를 활성화한 뒤 첫 새 시리즈 선택
            new_uids = self._loader.merge(loader)
            self._multi_viewport.set_active(target_viewport)
            self._series_tree.populate(self._loader.get_series_list(),
                                       select_uid=new_uids[0])
        errors = loader.load_errors
        if errors:
            self._statusbar.showMessage(
                f"Loaded {loaded} files ({len(errors)} errors)", 5000)
        else:
            self._statusbar.showMessage(
                f"Loaded {loaded} files", 5000)

    def _on_series_dropped(self, viewport_index, uid):
        """트리에서 Multi View 뷰포트로 시리즈를 드롭"""
        series = self._loader.get_series_by_uid(uid)
        if series is None:
            return
        self._multi_viewport.set_active(viewport_index)
        self._select_series(series)
        self._series_tree.select_uid(uid)

    def _on_paths_dropped(self, viewport_index, paths):
        """외부 파일/폴더를 Multi View 뷰포트로 드롭"""
        self.load_paths(paths, target_viewport=viewport_index)

    def _on_cursor_synced(self, linked, skipped):
        if linked == 0 and skipped > 0:
            self._statusbar.showMessage(
                "Sync Cursor: 같은 좌표계(Frame of Reference)의 시리즈가 없습니다", 3000)

    def _update_series_list(self):
        """시리즈 트리 갱신 (Patient → Study → Series, 전부 펼치고 첫 시리즈 선택)"""
        self._series_tree.populate(self._loader.get_series_list())

    def _on_series_selected(self, uid):
        series = self._loader.get_series_by_uid(uid)
        if series:
            self._select_series(series)

    def _select_series(self, series):
        self._current_series = series
        self._viewport.set_series(series)
        self._multi_viewport.set_series_to_active(series)
        self._slice_slider.setMaximum(max(0, series.num_slices - 1))
        self._slice_slider.setValue(0)
        self._sync_volume_tabs()

    def _on_tab_changed(self, index):
        self._sync_volume_tabs()

    def _sync_volume_tabs(self):
        """현재 탭이 MPR/3D일 때만 볼륨 구성 (전체 슬라이스 픽셀 로딩 필요)"""
        series = self._current_series
        if series is None:
            return
        current = self._tab_widget.currentWidget()
        if current is self._mpr_widget and self._mpr_series is not series:
            self._mpr_series = series
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._mpr_widget.set_series(series)
            finally:
                QApplication.restoreOverrideCursor()
        elif current is self._volume_widget and self._volume_series is not series:
            self._volume_series = series
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._volume_widget.set_series(series)
            finally:
                QApplication.restoreOverrideCursor()

    # ─── 시그널 핸들러 ───

    def _on_slice_changed(self, current, total):
        self._slice_slider.blockSignals(True)
        self._slice_slider.setValue(current)
        self._slice_slider.blockSignals(False)
        self._slice_label.setText(f" {current + 1}/{total} ")

    def _on_window_changed(self, center, width):
        self._status_wl.setText(f"W: {width:.0f}  L: {center:.0f}")

    def _on_zoom_changed(self, zoom):
        self._status_zoom.setText(f"Zoom: {zoom:.0%}")

    def _on_measurement_completed(self, distance):
        self._statusbar.showMessage(f"Distance: {distance:.2f} mm", 5000)

    # ─── 기타 기능 ───

    def _show_tags(self):
        if not self._current_series:
            QMessageBox.information(self, "Info", "시리즈를 먼저 선택하세요.")
            return
        tags = self._current_series.get_dicom_tags(
            self._viewport.current_slice)
        dialog = TagViewer(tags, self)
        dialog.exec_()

    def _show_anonymize(self):
        if not self._current_series:
            QMessageBox.information(self, "Info", "시리즈를 먼저 선택하세요.")
            return
        dialog = AnonymizeDialog(self._current_series, self)
        dialog.exec_()

    def _toggle_overlay(self):
        self._viewport._show_overlay = not self._viewport._show_overlay
        self._viewport.update()

    def _export_image(self):
        if not self._viewport._cached_pixmap:
            return
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Image", "dicom_export.png",
            "PNG (*.png);;JPEG (*.jpg);;BMP (*.bmp)")
        if filepath:
            self._viewport._cached_pixmap.save(filepath)
            self._statusbar.showMessage(f"Exported: {filepath}", 5000)

    def _export_video(self):
        if not self._current_series or self._current_series.num_slices == 0:
            QMessageBox.information(self, "Info", "시리즈를 먼저 선택하세요.")
            return
        dialog = VideoExportDialog(
            self._current_series,
            self._viewport._window_center,
            self._viewport._window_width,
            self._viewport._inverted, self)
        if dialog.exec_():
            self._statusbar.showMessage(dialog.result_message, 8000)

    def _apply_dark_theme(self):
        """다크 테마 적용"""
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #d4d4d4;
            }
            QMenuBar {
                background-color: #2d2d2d;
                color: #d4d4d4;
            }
            QMenuBar::item:selected {
                background-color: #3d3d3d;
            }
            QMenu {
                background-color: #2d2d2d;
                color: #d4d4d4;
                border: 1px solid #555;
            }
            QMenu::item:selected {
                background-color: #094771;
            }
            QToolBar {
                background-color: #2d2d2d;
                border: none;
                spacing: 4px;
                padding: 2px;
            }
            QToolBar QAction {
                color: #d4d4d4;
            }
            QToolBar QToolButton {
                padding: 3px 6px;
                border: 1px solid transparent;
                border-radius: 3px;
            }
            QToolBar QToolButton:hover {
                background-color: #3d3d3d;
            }
            QToolBar QToolButton:checked {
                background-color: #094771;
                border: 1px solid #007acc;
                color: white;
            }
            QTreeWidget {
                background-color: #252526;
                color: #d4d4d4;
                border: 1px solid #3d3d3d;
                alternate-background-color: #2a2a2a;
            }
            QTreeWidget::item:selected {
                background-color: #094771;
            }
            QTreeWidget::item:hover {
                background-color: #2a2d2e;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: #d4d4d4;
                border: 1px solid #3d3d3d;
                padding: 4px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #3d3d3d;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #007acc;
                width: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QStatusBar {
                background-color: #007acc;
                color: white;
            }
            QLabel {
                color: #d4d4d4;
            }
            QSpinBox {
                background-color: #3c3c3c;
                color: #d4d4d4;
                border: 1px solid #555;
                padding: 2px;
            }
            QProgressDialog {
                background-color: #2d2d2d;
                color: #d4d4d4;
            }
            QSplitter::handle {
                background-color: #3d3d3d;
                width: 2px;
            }
        """)

    # ─── 드래그 앤 드롭 ───

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.load_paths([u.toLocalFile() for u in urls if u.isLocalFile()])
