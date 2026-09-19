# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DabbaView 메인 윈도우
"""
import os
import threading
import time
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QSplitter, QToolBar,
    QAction, QActionGroup, QFileDialog, QStatusBar,
    QSlider, QLabel, QProgressDialog, QMessageBox,
    QSpinBox, QApplication, QMenuBar, QTabWidget, QMenu, QStackedWidget,
    QComboBox, QPushButton, QInputDialog, QToolButton, QSizePolicy
)
from PyQt5.QtCore import (Qt, QSize, QThread, pyqtSignal, QSettings,
                          QVariantAnimation, QEasingCurve, QTimer, QUrl)
from PyQt5.QtGui import QIcon, QKeySequence, QFont, QDesktopServices

from .dicom_loader import DicomLoader
from .viewport import DicomViewport
from .tag_viewer import TagViewer
from .multi_viewport import MultiViewport
from .mpr_viewer import MPRWidget
from .volume_renderer import VolumeRenderWidget, vtk_usable
from .anonymizer import AnonymizeDialog
from .ai.labels import LabelSet
from .ai.panel import AIResearchPanel
from .ai.segmentation import SegmentationController
from .ai.worklist import Worklist
from .formats import OPEN_FILTERS
from . import APP_NAME, GITHUB_URL, __version__
from .about_dialog import AboutDialog
from .analysis import colormaps
from .analysis.console import PythonConsoleDock
from .analysis.landmarks import LandmarkStore
from .analysis.macros import MacroStore
from .analysis.plots import AnalysisPlotDock
from .analysis.processing import FILTERS
from .analysis.tab import AnalysisTab
from .formats.convert_dialog import ConvertDialog
from .video_exporter import VideoExportDialog
from .series_tree import SeriesTreeWidget
from .cursor_sync import CursorSyncController
from .image_info_panel import ImageInfoPanel
from .series_panel import SeriesPanel
from .tile_view import TileView
from .app_settings import AppSettings
from .annotations import AnnotationStore
from .hanging import find_protocol, assign_slots, protocol_from_layout
from .settings_dialog import SettingsDialog
from .network_dialogs import DicomSendDialog, DicomPrintDialog
from .render import render_8bit
from .multi_viewport import LAYOUTS
from .series_tree import group_series
from .reading import ReadingDialog, ReportStore, ReportLibrary
from .dicom_info import orientation_name


MAX_RECENT_PATHS = 10

# 이름 변경 전(RadiantView)의 설정 저장소
LEGACY_SETTINGS = ("RadiantView", "RadiantView")


def migrate_legacy_settings(settings):
    """RadiantView 시절 설정(최근 파일, 마우스 매핑 등)을 처음 한 번만 가져옴

    새 설정이 비어 있을 때만 복사하고, 이전 설정은 지우지 않는다.
    """
    # macOS는 시스템 공통 설정(NSGlobalDomain)까지 allKeys()에 포함하므로
    # 앱 자체 설정만 보도록 fallback을 끔
    settings.setFallbacksEnabled(False)
    if settings.value("migrated_from_legacy", False, type=bool):
        return 0
    old = QSettings(*LEGACY_SETTINGS)
    old.setFallbacksEnabled(False)
    copied = 0
    if not settings.allKeys():
        for key in old.allKeys():
            settings.setValue(key, old.value(key))
            copied += 1
    settings.setValue("migrated_from_legacy", True)
    settings.sync()
    return copied


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
        self.loader = DicomLoader()   # 진행 중 멈춘 파일 확인용 (메인 스레드에서 slow_files만 읽음)
        self.last_progress = time.monotonic()

    def cancel(self):
        self._cancel_event.set()

    def was_cancelled(self):
        return self._cancel_event.is_set()

    def _on_progress(self, current, total):
        self.last_progress = time.monotonic()
        if total <= 0:   # 폴더 탐색 단계: 개수 모름
            self.progress.emit(0, 0)
            return
        # 파일마다 시그널을 보내면 UI 이벤트 큐가 넘치므로 1% 단위로만 전달
        percent = current * 100 // total
        if percent != self._last_percent or current == total:
            self._last_percent = percent
            self.progress.emit(current, total)

    def run(self):
        # 새 로더에 채운 뒤 완료 시 교체 → 로딩 중에도 기존 시리즈를 안전하게 볼 수 있음
        loader = self.loader
        loaded = loader.load_paths(
            self._paths, progress_callback=self._on_progress,
            cancel_event=self._cancel_event)
        self.finished_loading.emit(loader, loaded)


class MainWindow(QMainWindow):
    """DabbaView 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{__version__} - DICOM Viewer")
        self.resize(1400, 900)

        self._loader = DicomLoader()
        self._current_series = None
        self._load_worker = None
        self._load_progress = None
        # MPR/3D는 전체 픽셀을 읽어야 하므로 해당 탭을 열 때만 구성
        self._mpr_series = None
        self._volume_series = None
        # 마지막으로 연 폴더: 열기 대화상자의 시작 위치로만 사용 (자동 로드 안 함)
        self._settings = QSettings("DabbaView", "DabbaView")
        migrate_legacy_settings(self._settings)
        self._app_settings = AppSettings(self._settings)
        self._annotation_store = AnnotationStore(self)
        self._report_store = ReportStore()
        self._report_library = ReportLibrary(self)
        self._reading_dialogs = {}  # study_uid → 열린 Reading 창

        self._init_ui()
        self._cursor_sync = CursorSyncController(self)
        self._info_panel = ImageInfoPanel(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self._info_panel)
        self._info_panel.hide()
        # AI Research: 라벨 목록·마스크 편집·워크리스트를 모든 뷰포트가 공유
        self._pending_select_uid = None
        self._pending_segmentations = []  # 참조 시리즈를 기다리는 DICOM SEG 경로
        self._labels = LabelSet(parent=self)
        self._seg = SegmentationController(self._labels, self)
        self._worklist = Worklist(parent=self)
        self._ai_panel = AIResearchPanel(self, self._seg, self._worklist)
        self.addDockWidget(Qt.RightDockWidgetArea, self._ai_panel)
        self._ai_panel.hide()
        # 분석 (3D Slicer / ImageJ 스타일): 랜드마크·매크로 공유, 하단 도크 두 개(탭)
        self._landmarks = LandmarkStore(self)
        self._macros = MacroStore(parent=self)
        self._plot_dock = AnalysisPlotDock(self)
        self._console = PythonConsoleDock(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._plot_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._console)
        self.tabifyDockWidget(self._plot_dock, self._console)
        self._plot_dock.hide()
        self._console.hide()
        self._analysis_tab = AnalysisTab(self, self._ai_panel)
        self._ai_panel.tabs.addTab(self._analysis_tab, "🧰 Image Tools")
        self._labels.changed.connect(self._analysis_tab._fill_labels)
        self._create_image_actions()
        self._init_menubar()
        self._init_toolbar()
        self._init_statusbar()
        self._configure_viewports()
        self._connect_signals()
        self._restore_series_panel()
        self._report_library.set_folder(self._app_settings.report_folder())

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
        self._splitter = splitter

        # ─── 왼쪽 패널: 시리즈 목록 ───
        left_panel = QWidget()
        self._left_panel = left_panel
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)

        # 상단: 레이아웃 선택 (INFINITT 방식) + 보기 전환
        header = QHBoxLayout()
        header.addWidget(QLabel("Series"))
        header.addStretch()
        self._layout_combo = QComboBox()
        self._layout_combo.addItems(["2D", "1X1", "1X2", "2X2", "3X3", "Default", "ALL"])
        self._layout_combo.setToolTip(
            "2D: 단일 뷰 / 1X1~3X3: Multi View (현재 시리즈부터 순서대로)\n"
            "Default: Hanging Protocol 적용 / ALL: 현재 검사의 모든 시리즈")
        self._layout_combo.activated[str].connect(self._apply_layout_choice)
        header.addWidget(self._layout_combo)
        self._view_toggle = QPushButton("☰")
        self._view_toggle.setCheckable(True)
        self._view_toggle.setFixedWidth(32)
        self._view_toggle.setToolTip("썸네일 목록 ↔ 환자/검사 트리")
        self._view_toggle.toggled.connect(self._toggle_series_view)
        header.addWidget(self._view_toggle)
        left_layout.addLayout(header)

        self._series_stack = QStackedWidget()
        self._series_panel = SeriesPanel()
        # 클릭(뗄 때)/Enter = 활성 칸에 로드, 끌기 = 드래그 앤 드롭 (누르는 순간엔 로드 안 함)
        self._series_panel.series_selected.connect(self._on_series_highlighted)
        self._series_panel.series_activated.connect(self._on_series_selected)
        self._series_tree = SeriesTreeWidget()
        self._series_tree.use_external_thumbnails(True)
        self._series_tree.series_selected.connect(self._on_series_highlighted)
        self._series_tree.series_activated.connect(self._on_series_selected)
        self._series_panel.thumbnail_ready.connect(self._series_tree.set_thumbnail)
        self._series_stack.addWidget(self._series_panel)
        self._series_stack.addWidget(self._series_tree)
        left_layout.addWidget(self._series_stack)

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

        # 탭 1: 단일 뷰포트 (Stack) / Tile
        self._viewport = DicomViewport()
        self._tile_view = TileView()
        self._tile_view.tile_activated.connect(self._on_tile_activated)
        self._tile_view.window_changed.connect(
            lambda c, w: self._viewport.set_window(c, w))
        self._stack2d = QStackedWidget()
        self._stack2d.addWidget(self._viewport)
        self._stack2d.addWidget(self._tile_view)
        self._tab_widget.addTab(self._stack2d, "2D View")

        # 탭 2: 다중 뷰포트
        self._multi_viewport = MultiViewport()
        self._tab_widget.addTab(self._multi_viewport, "Multi View")

        # 탭 3: MPR
        self._mpr_widget = MPRWidget()
        self._tab_widget.addTab(self._mpr_widget, "MPR")

        # 탭 4: 3D Volume
        self._volume_widget = VolumeRenderWidget()
        self._tab_widget.addTab(self._volume_widget,
                                "3D Volume" if vtk_usable() else "3D (VTK 필요)")

        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        # 오른쪽 영역: [◀ 토글 띠 | 탭] - 패널을 접어도 띠는 창 왼쪽 끝에 남음
        right = QWidget()
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self._panel_toggle = QToolButton()
        self._panel_toggle.setObjectName("PanelToggle")
        self._panel_toggle.setFixedWidth(14)
        self._panel_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self._panel_toggle.setToolTip("시리즈 패널 접기/펼치기 (F2)")
        self._panel_toggle.setStyleSheet(
            "QToolButton#PanelToggle { background: #232323; color: #9a9a9a; border: none;"
            " border-right: 1px solid #333; font-size: 9px; padding: 0; }"
            "QToolButton#PanelToggle:hover { background: #094771; color: white; }")
        self._panel_toggle.clicked.connect(self.toggle_series_panel)
        right_layout.addWidget(self._panel_toggle)
        right_layout.addWidget(self._tab_widget)
        splitter.addWidget(right)

        left_panel.setMinimumWidth(220)
        splitter.setSizes([300, 1100])
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        splitter.splitterMoved.connect(self._on_splitter_moved)
        main_layout.addWidget(splitter)

        self._panel_width = 300
        self._panel_visible = True
        self._panel_anim = None

    def _init_menubar(self):
        """메뉴바"""
        menubar = self.menuBar()

        # File 메뉴
        file_menu = menubar.addMenu("&File")

        open_file = QAction("Open File... (DICOM, NIfTI, NRRD, MHA, NumPy, PNG/JPEG, STL)", self)
        open_file.setShortcut(QKeySequence("Ctrl+O"))
        open_file.triggered.connect(self._open_file)
        file_menu.addAction(open_file)

        cloud_actions = []
        for text, key in (("Open from Google Drive...", "google"), ("Open from OneDrive...", "onedrive")):
            action = QAction(text, self)
            action.triggered.connect(lambda _=False, k=key: self._open_cloud(k))
            cloud_actions.append(action)

        open_dir = QAction("Open DICOM Folder...", self)
        open_dir.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_dir.triggered.connect(self._open_directory)
        file_menu.addAction(open_dir)
        file_menu.addSeparator()
        for action in cloud_actions:
            file_menu.addAction(action)

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

        # 포맷 변환 (소스 형식 → 대상 형식)
        convert_menu = file_menu.addMenu("Convert / Export As")
        for entry in (
                ("DICOM → NIfTI...", "dicom", "nifti"),
                ("DICOM → NRRD...", "dicom", "nrrd"),
                ("DICOM → MetaImage (.mha)...", "dicom", "metaimage"),
                ("DICOM → NumPy...", "dicom", "numpy"),
                ("DICOM → PNG 시퀀스...", "dicom", "png"),
                None,
                ("NIfTI → DICOM...", "nifti", "dicom"),
                ("NIfTI → NumPy...", "nifti", "numpy"),
                ("NIfTI → NRRD...", "nifti", "nrrd"),
                ("NumPy → NIfTI...", "numpy", "nifti"),
                ("NRRD → NIfTI...", "nrrd", "nifti"),
                ("MetaImage → NIfTI...", "metaimage", "nifti"),
                ("PNG/JPEG 시퀀스 → NIfTI...", "image", "nifti"),
                None,
                ("다른 조합 (모든 형식)...", None, None)):
            if entry is None:
                convert_menu.addSeparator()
                continue
            text, source, target = entry
            action = convert_menu.addAction(text)
            action.triggered.connect(
                lambda _=False, src=source, tgt=target: self._open_convert(src, tgt))

        file_menu.addSeparator()
        file_menu.addAction(self._act_save_ann)
        file_menu.addAction(self._act_load_ann)
        file_menu.addAction(self._act_export_keys)
        file_menu.addSeparator()
        file_menu.addAction(self._act_send)
        file_menu.addAction(self._act_print)
        file_menu.addSeparator()
        file_menu.addAction(self._act_settings)
        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # View 메뉴
        view_menu = menubar.addMenu("&View")

        # 툴바와 같은 QAction을 공유 (단축키 중복 시 Qt가 둘 다 무시함)
        view_menu.addAction(self._act_reset)
        view_menu.addSeparator()
        for action in (self._act_flip_v, self._act_flip_h, self._act_rot_l,
                       self._act_rot_r, self._act_invert):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self._act_value_lens)
        view_menu.addAction(self._act_image_panel)
        view_menu.addSeparator()

        self._act_toggle_panel = QAction("Toggle Series Panel", self)
        self._act_toggle_panel.setShortcut(QKeySequence("F2"))
        self._act_toggle_panel.triggered.connect(self.toggle_series_panel)
        view_menu.addAction(self._act_toggle_panel)

        overlay_action = QAction("Toggle Overlay (정보 + 주석)", self)
        overlay_action.setShortcuts([QKeySequence("T"), QKeySequence("O")])
        overlay_action.triggered.connect(self._toggle_overlay)
        view_menu.addAction(overlay_action)
        view_menu.addAction(self._act_maximize)

        # Reading
        reading_menu = menubar.addMenu("&Reading")
        reading_menu.addAction(self._act_reading)

        # Tools 메뉴
        tools_menu = menubar.addMenu("&Tools")

        tags_action = QAction("DICOM Tags...", self)
        tags_action.setShortcut(QKeySequence("Ctrl+T"))
        tags_action.triggered.connect(self._show_tags)
        tools_menu.addAction(tags_action)

        tools_menu.addSeparator()

        tools_menu.addAction(self._act_anonymize)
        tools_menu.addAction(self._act_ai)
        tools_menu.addAction(self._act_console)

        tools_menu.addSeparator()

        clear_meas = QAction("Clear All Measurements", self)
        clear_meas.triggered.connect(
            lambda: self._target_viewport().clear_measurements())
        tools_menu.addAction(clear_meas)

        tools_menu.addSeparator()
        apply_hp = QAction("Apply Hanging Protocol", self)
        apply_hp.triggered.connect(lambda: self._apply_hanging(auto=False))
        tools_menu.addAction(apply_hp)
        save_hp = QAction("Save Layout as Hanging Protocol...", self)
        save_hp.triggered.connect(self._save_layout_as_protocol)
        tools_menu.addAction(save_hp)
        tools_menu.addAction(self._act_compare)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_key)
        tools_menu.addAction(self._act_key_view)

        file_menu.insertAction(export_video_action, self._act_capture)

        # Window presets 메뉴 (설정에서 추가/편집/삭제, 열 때마다 새로 구성)
        self._init_process_menu(menubar)
        from .clinical.menu import install as install_clinical
        self._clinical_menu = install_clinical(self, menubar)
        self._preset_menu = menubar.addMenu("&Presets")
        self._help_menu = menubar.addMenu("&Help")
        about = QAction(f"About {APP_NAME}", self)
        about.setMenuRole(QAction.AboutRole)  # macOS: 앱 메뉴(DabbaView → About)로 이동
        about.triggered.connect(lambda: AboutDialog(self).exec_())
        self._help_menu.addAction(about)
        github = QAction("GitHub 저장소 열기", self)
        github.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))
        self._help_menu.addAction(github)
        self._help_menu.addSeparator()
        deploy = QAction("🚀 Deploy Web...", self)
        deploy.setToolTip("DabbaView-Web(GitHub Pages)을 GitHub Actions로 다시 배포")
        deploy.triggered.connect(self._open_deploy_web)
        self._help_menu.addAction(deploy)
        self._preset_menu.aboutToShow.connect(self._rebuild_preset_menu)
        self._rebuild_preset_menu()

    def _init_toolbar(self):
        """도구 모음 (3줄): 도구 / 보기·동기화 / 출력·시네"""
        toolbar = QToolBar("Tools")
        toolbar.setIconSize(QSize(24, 24))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        tool_group = QActionGroup(self)
        tool_group.setExclusive(True)
        V = DicomViewport
        tools = [
            ("⬚ Select", V.TOOL_SELECT, "S",
             "Selector: 선택 전용 (좌클릭으로 뷰포트 선택)\n"
             "우클릭 드래그=W/L, 가운데=Pan, 휠=슬라이스는 항상 동작"),
            None,
            ("W/L", V.TOOL_WINDOW, "1", "윈도잉 (좌클릭 드래그)"),
            ("Pan", V.TOOL_PAN, "2", "팬 (좌클릭 드래그)"),
            ("Zoom", V.TOOL_ZOOM, "3", "줌 (좌클릭 드래그)"),
            None,
            ("Dist", V.TOOL_MEASURE, "4", "거리 측정 (클릭→클릭, Shift+클릭: 끝점만 다시 지정)"),
            ("Angle", V.TOOL_ANGLE, "5", "각도 측정 (3점 클릭)"),
            ("Cobb", V.TOOL_COBB, "B", "Cobb 각: 첫 번째 선 드래그 → 두 번째 선 드래그"),
            None,
            ("3D Cursor", V.TOOL_CURSOR3D, "6",
             "3D 커서: 클릭 위치의 환자 좌표(mm)\nCrosslink가 켜져 있으면 다른 뷰에도 전파"),
            ("Magnify", V.TOOL_MAGNIFY, "7", "돋보기: 누르고 있는 동안 확대 (휠로 2x/3x/4x)"),
            None,
            ("ROI", V.TOOL_ROI, "8", "Freehand ROI: 면적·Mean·SD·Min·Max"),
            ("Ellipse", V.TOOL_ELLIPSE, "E", "타원 ROI (Shift: 원): 면적·Mean·SD·Min·Max"),
            ("Area", V.TOOL_AREA, "9", "Freehand 면적 측정: 면적(mm²)·둘레"),
            None,
            ("Arrow", V.TOOL_ARROW, "0", "2D 화살표: 가리킬 곳에서 누르고 드래그 → 라벨"),
            ("Text", V.TOOL_TEXT, "A", "텍스트 메모: 클릭 → 내용·크기·색상"),
            None,
            ("📍 Landmark", V.TOOL_LANDMARK, "F",
             "랜드마크/Fiducial: 클릭한 위치(환자 좌표 mm)에 점 추가\n"
             "AI 패널 → Analysis → Landmarks에서 이름·내보내기(CSV/JSON)"),
            ("Profile", V.TOOL_PROFILE, "Shift+L",
             "라인 프로파일: 드래그한 선을 따라 픽셀 값 그래프 (하단 패널)"),
        ]
        self._tool_actions = {}
        for entry in tools:
            if entry is None:
                toolbar.addSeparator()
                continue
            label, tool_id, shortcut, tooltip = entry
            action = QAction(label, self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.setToolTip(f"{tooltip} ({shortcut})")
            action.triggered.connect(
                lambda checked, t=tool_id: self._set_tool_all(t))
            tool_group.addAction(action)
            toolbar.addAction(action)
            self._tool_actions[tool_id] = action
        self._tool_actions[V.TOOL_SELECT].setChecked(True)

        # ─── 둘째 줄: 이미지 조작 + 동기화 + Key Image / Tile ───
        self.addToolBarBreak()
        view_bar = QToolBar("View")
        view_bar.setMovable(False)
        self.addToolBar(view_bar)
        for action in (self._act_flip_v, self._act_flip_h, self._act_rot_l,
                       self._act_rot_r, self._act_invert, self._act_reset):
            view_bar.addAction(action)
        view_bar.addSeparator()

        # 크로스 레퍼런스 (GE AW의 Crosslink)
        self._sync_action = QAction("⌖ Crosslink", self)
        self._sync_action.setCheckable(True)
        self._sync_action.setShortcut(QKeySequence("C"))
        self._sync_action.setToolTip(
            "크로스 레퍼런스 (C)\n"
            "Select/W/L 도구에서 좌클릭/드래그 위치가 같은 좌표계(Frame of Reference)의\n"
            "다른 뷰포트·MPR에 십자선으로 표시되고 가장 가까운 슬라이스로 이동합니다.")
        self._sync_action.toggled.connect(self._cursor_sync.set_enabled)
        view_bar.addAction(self._sync_action)
        for action in (self._act_ref_lines, self._act_sync_scroll,
                       self._act_sync_window, self._act_value_lens):
            view_bar.addAction(action)
        view_bar.addSeparator()
        for action in (self._act_key, self._act_key_view, self._act_tile):
            view_bar.addAction(action)
        self._tile_grid = QComboBox()
        self._tile_grid.addItems([f"{n}x{n}" for n in range(2, 7)])
        self._tile_grid.setCurrentText("4x4")
        self._tile_grid.setToolTip("Tile 격자 크기")
        self._tile_grid.currentTextChanged.connect(
            lambda t: self._tile_view.set_grid(int(t.split("x")[0])))
        view_bar.addWidget(self._tile_grid)
        view_bar.addAction(self._act_compare)

        # ─── 셋째 줄: 출력 + 시네/슬라이스 ───
        self.addToolBarBreak()
        output_bar = QToolBar("Output")
        output_bar.setMovable(False)
        self.addToolBar(output_bar)
        for action in (self._act_reading, self._act_capture, self._act_image_panel,
                       self._act_anonymize, self._act_ai, self._act_send, self._act_print,
                       self._act_settings):
            output_bar.addAction(action)
        output_bar.addSeparator()

        # 시네 재생
        cine_action = QAction("▶ Play", self)
        cine_action.setShortcut(QKeySequence("P"))
        cine_action.setToolTip("시네 재생/정지 (P)")
        cine_action.triggered.connect(self._viewport.toggle_cine)
        output_bar.addAction(cine_action)

        # FPS 조절
        output_bar.addWidget(QLabel(" FPS: "))
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(15)
        self._fps_spin.valueChanged.connect(self._viewport.set_cine_fps)
        output_bar.addWidget(self._fps_spin)

        output_bar.addSeparator()

        # 슬라이스 슬라이더
        output_bar.addWidget(QLabel(" Slice: "))
        self._slice_slider = QSlider(Qt.Horizontal)
        self._slice_slider.setMinimum(0)
        self._slice_slider.setMaximum(0)
        self._slice_slider.setFixedWidth(220)
        self._slice_slider.valueChanged.connect(self._viewport.go_to_slice)
        output_bar.addWidget(self._slice_slider)

        self._slice_label = QLabel(" 0/0 ")
        output_bar.addWidget(self._slice_label)

    def _create_image_actions(self):
        """이미지 조작 액션 (메뉴·툴바 공유). 대상 = 현재 탭의 뷰포트"""
        def make(text, shortcut, tooltip, slot, checkable=False):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.setToolTip(f"{tooltip} ({shortcut})" if shortcut else tooltip)
            action.setCheckable(checkable)
            action.triggered.connect(slot)
            return action

        self._act_flip_v = make("⇅ Flip V", "V", "상하 반전",
                                lambda: self._target_viewport().flip_vertical())
        self._act_flip_h = make("⇆ Flip H", "H", "좌우 반전",
                                lambda: self._target_viewport().flip_horizontal())
        self._act_rot_l = make("↺ Rot L", "[", "왼쪽으로 90° 회전",
                               lambda: self._target_viewport().rotate_left())
        self._act_rot_r = make("↻ Rot R", "]", "오른쪽으로 90° 회전",
                               lambda: self._target_viewport().rotate_right())
        self._act_invert = make("◐ B/W Inverse", "I", "흑백 반전",
                                lambda: self._target_viewport().toggle_invert())
        self._act_reset = make("⟲ Reset", "Shift+R", "회전/반전 초기화 + 화면 맞춤",
                               lambda: self._target_viewport().reset_view())
        self._act_value_lens = make(
            "HU Lens", "L", "커서 옆에 픽셀 값 표시 (CT: HU, MR: SI, PET: SUVbw)\n"
                            "상태바에는 항상 좌표와 값이 표시됩니다",
            self._set_value_lens_all, checkable=True)
        self._act_capture = make("📷 Capture", "Ctrl+Shift+S",
                                 "현재 화면을 오버레이·측정선 포함해 이미지로 저장",
                                 self._capture_image)
        self._act_ref_lines = make("Ref Lines", "", "Reference Line: Multi View 다른 칸의 슬라이스 위치 표시",
                                   self._multi_viewport.set_reference_lines, checkable=True)
        self._act_sync_scroll = make("Sync Scroll", "", "Multi View 동기화 스크롤\n"
                                     "같은 좌표계·평행한 시리즈는 위치 기준, 비교(Compare) 칸은 간격 유지",
                                     self._multi_viewport.set_sync_scroll, checkable=True)
        self._act_sync_window = make("Sync W/L", "", "Multi View 동기화 윈도잉 (같은 모달리티)",
                                     self._multi_viewport.set_sync_window, checkable=True)
        self._act_key = make("★ Key", "K", "현재 영상을 Key Image로 표시/해제",
                             self._toggle_key_image)
        self._act_key_view = make("Key Images", "Shift+K", "Key Image만 모아보기 (Tile)",
                                  self._show_key_images)
        self._act_tile = make("▦ Tile", "Shift+T", "Stack ↔ Tile 모드 (여러 슬라이스를 격자로)",
                              self._set_tile_mode, checkable=True)
        self._act_compare = make("Compare", "", "같은 환자의 이전 검사와 나란히 비교\n"
                                 "(동기화 스크롤·윈도잉 자동 켜짐)", self._compare_prior)
        self._act_send = make("DICOM Send", "", "PACS 등으로 DICOM 전송 (C-STORE)",
                              lambda: self._open_network_dialog(DicomSendDialog))
        self._act_print = make("DICOM Print", "", "DICOM 프린터로 필름 인쇄",
                               lambda: self._open_network_dialog(DicomPrintDialog))
        self._act_settings = make("⚙ Settings", "", "마우스 매핑 / W/L 프리셋 / Hanging Protocol / DICOM 노드",
                                  lambda: self._open_settings())
        self._act_settings.setShortcut(QKeySequence.Preferences)
        self._act_settings.setMenuRole(QAction.PreferencesRole)
        self._act_save_ann = make("Save Annotations...", "", "주석·측정·Key Image를 JSON으로 저장",
                                  self._save_annotations)
        self._act_load_ann = make("Load Annotations...", "", "JSON 주석 파일 불러오기",
                                  self._load_annotations)
        self._act_export_keys = make("Export Key Images...", "", "Key Image를 PNG + 목록(JSON)으로 내보내기",
                                     self._export_key_images)
        self._act_anonymize = make("🕶 Anonymize", "", "선택적 익명화 후 저장 (개인/기관/검사/촬영 파라미터, 프리셋)",
                                   self._show_anonymize)
        self._act_reading = make("📝 Reading", "R", "판독 창 (판독문 작성·가져오기·인쇄)",
                                 self._open_reading)
        self._act_maximize = make("Maximize Viewport", "Space",
                                  "Multi View: 선택한 칸만 크게 ↔ 원래 배치",
                                  self._toggle_maximize)
        self._act_console = self._console.toggleViewAction()
        self._act_console.setText("🐍 Python Console")
        self._act_console.setShortcut(QKeySequence("F3"))
        self._act_console.setToolTip("Python 콘솔 (F3): app.current_array 등으로 분석")
        self._act_plots = self._plot_dock.toggleViewAction()
        self._act_plots.setText("📊 Histogram / Profile")
        self._act_plots.setToolTip("히스토그램 · 라인 프로파일 패널")
        self._act_ai = self._ai_panel.toggleViewAction()
        self._act_ai.setText("🧠 AI")
        self._act_ai.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self._act_ai.setToolTip("AI Research 패널 (Ctrl+Shift+A): 세그멘테이션 라벨링, 학습 데이터 "
                                "내보내기, MONAI Label·ONNX 모델, 데이터셋 관리")
        self._act_image_panel = self._info_panel.toggleViewAction()
        self._act_image_panel.setText("ⓘ Image")
        self._act_image_panel.setShortcut(QKeySequence("Ctrl+I"))
        self._act_image_panel.setToolTip("Image 정보 패널 (시퀀스 상세) (Ctrl+I)")
        # toggled는 도크가 실제로 보이기 전에 발생 → visibilityChanged에서 갱신
        self._info_panel.visibilityChanged.connect(
            lambda visible: visible and self._refresh_image_info())

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
        # 불러오기·표시에 실패한 파일 목록 (디코딩 실패는 어느 스레드에서든 보고됨)
        from . import dicom_loader
        from .load_errors import LoadErrorButton, LoadErrorLog
        self._load_errors = LoadErrorLog(self)
        dicom_loader.decode_error_listeners.append(self._load_errors.add_decode_error)
        self._statusbar.addPermanentWidget(LoadErrorButton(self._load_errors, self))

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
        # 격자 위 버튼으로 바꾼 레이아웃도 드롭다운에 반영
        self._multi_viewport.layout_changed.connect(
            lambda name: self._layout_combo.findText(name.upper()) >= 0
            and self._layout_combo.setCurrentText(name.upper()))
        self._multi_viewport.paths_dropped.connect(self._on_paths_dropped)

        self._viewport.slice_changed.connect(self._on_slice_changed)
        self._viewport.window_changed.connect(self._on_window_changed)
        self._viewport.zoom_changed.connect(self._on_zoom_changed)
        self._viewport.measurement_completed.connect(
            self._on_measurement_completed)

        for vp in self._all_viewports():
            vp.cursor_info.connect(self._status_pos.setText)
            vp.status_message.connect(
                lambda text: self._statusbar.showMessage(text, 8000))
            vp.slice_changed.connect(lambda *_: self._refresh_image_info())
        self._multi_viewport.active_viewport_changed.connect(
            lambda *_: (self._refresh_image_info(), self._ai_panel.on_series_changed()))
        self._seg.status.connect(lambda text: self._statusbar.showMessage(text, 6000))

    # ─── 파일 열기 ───

    def _open_file(self):
        # 여러 파일 선택 가능 (PNG/JPEG 여러 장 → 한 시리즈)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open File", self._last_dir(), OPEN_FILTERS)
        if paths:
            self.load_paths(paths)

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

    def open_series_from_folder(self, folder, series_uid):
        """폴더를 불러온 뒤 그 안의 series_uid 시리즈를 표시 (AI 워크리스트)"""
        self._pending_select_uid = series_uid
        self.load_paths([folder], remember=False)

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
        progress.setMinimumWidth(460)

        worker = DirectoryLoadWorker(paths, target_viewport, remember, self)
        worker.progress.connect(self._on_load_progress)
        worker.finished_loading.connect(self._on_load_finished)
        worker.finished.connect(worker.deleteLater)
        progress.canceled.connect(worker.cancel)

        self._load_worker = worker
        self._load_progress = progress
        self._load_label = "Loading DICOM files..."
        # 진행이 한동안 멈추면 어떤 파일에서 막혔는지 보여 주고 취소를 안내
        self._load_watchdog = QTimer(self)
        self._load_watchdog.timeout.connect(self._check_load_stall)
        self._load_watchdog.start(1000)
        worker.start()

    LOAD_STALL_S = 5

    def _check_load_stall(self):
        worker, progress = self._load_worker, self._load_progress
        if worker is None or progress is None:
            return
        idle = time.monotonic() - worker.last_progress
        if idle < self.LOAD_STALL_S:
            progress.setLabelText(self._load_label)
            return
        if not progress.isVisible():
            progress.show()
        slow = worker.loader.slow_files(self.LOAD_STALL_S)
        lines = [self._load_label, "",
                 f"⚠ {idle:.0f}초째 진행이 없습니다."]
        for path, seconds in slow[:3]:
            lines.append(f"   {os.path.basename(path)} — {seconds:.0f}초째 응답 없음")
        from .dicom_loader import FILE_TIMEOUT_S
        lines.append(f"{FILE_TIMEOUT_S:.0f}초가 지난 파일은 자동으로 건너뜁니다. "
                     "기다리기 싫으면 '취소'를 누르세요 — 지금까지 읽은 영상은 열립니다.")
        progress.setLabelText("\n".join(lines))

    def _on_load_progress(self, current, total):
        # 모달 진행창의 setValue가 이벤트를 처리하는 동안 완료 처리로 _load_progress가 None이 될 수 있음
        progress = self._load_progress
        if progress is None:
            return
        if total <= 0:
            self._load_label = "파일 목록 확인 중..."
            progress.setLabelText(self._load_label)
            progress.setRange(0, 0)   # 개수를 모르는 단계: 움직이는 막대
            return
        if progress.maximum() == 0:
            progress.setRange(0, 100)
        self._load_label = f"Loading DICOM files... ({current}/{total})"
        progress.setLabelText(self._load_label)
        progress.setValue(current * 100 // total)

    def _on_load_finished(self, loader, loaded):
        cancelled = self._load_worker.was_cancelled()
        target_viewport = self._load_worker.target_viewport
        loaded_paths = self._load_worker.paths
        remember = self._load_worker.remember
        self._load_worker = None
        self._load_watchdog.stop()
        if self._load_progress is not None:
            self._load_progress.close()
            self._load_progress = None

        if cancelled and not loader.series_dict:
            self._statusbar.showMessage("Loading cancelled", 5000)
            return

        if loaded == 0:
            if loader.load_errors:
                self._load_errors.reset(loader.load_errors)
                QMessageBox.warning(
                    self, "Warning",
                    f"영상을 하나도 불러오지 못했습니다. {len(loader.load_errors)}개 파일을 건너뛰었습니다.\n\n"
                    f"예: {os.path.basename(loader.load_errors[0][0])} — {loader.load_errors[0][1]}\n\n"
                    "상태바 오른쪽 아래 ⚠ 버튼으로 전체 목록을 볼 수 있습니다.")
            else:
                QMessageBox.warning(
                    self, "Warning",
                    "No DICOM files found in the selected folder.")
            return

        # 실제로 불러오기에 성공한 경로만 최근 목록에 기록
        if remember:
            self._add_recent_paths(loaded_paths)

        protocol_name = None
        if not loader.series_dict:
            # SEG / STL만 불러온 경우: 지금 보고 있는 시리즈 목록은 그대로
            notes = self._apply_loaded_extras(loader)
            self._statusbar.showMessage("  ·  ".join([f"Loaded {loaded} files"] + notes), 12000)
            return
        if target_viewport is None:
            self._loader = loader
            self._load_errors.reset(loader.load_errors)
            pending, self._pending_select_uid = self._pending_select_uid, None
            self._update_series_list(
                select_uid=pending if pending and loader.get_series_by_uid(pending) else None)
            if self._app_settings.auto_hanging():
                protocol_name = self._apply_hanging(auto=True)
        else:
            # 기존 목록에 추가하고, 드롭한 뷰포트를 활성화한 뒤 첫 새 시리즈 선택
            new_uids = self._loader.merge(loader)
            self._load_errors.extend(loader.load_errors)
            self._multi_viewport.set_active(target_viewport)
            self._update_series_list(select_uid=new_uids[0] if new_uids else None)
        self._report_library.set_studies(self._studies_for_matching())
        notes = self._apply_loaded_extras(loader)
        errors = loader.load_errors
        message = f"Loaded {loaded} files"
        if cancelled:
            message = f"불러오기 취소 — 지금까지 읽은 {loaded}개 파일만 표시"
        if errors:
            message += f" ({len(errors)}개 건너뜀 — 오른쪽 아래 ⚠ 버튼으로 목록 보기)"
        if protocol_name:
            message += f"  ·  Hanging Protocol: {protocol_name}"
        if notes:
            message += "  ·  " + "  ·  ".join(notes)
        self._statusbar.showMessage(message, 12000 if notes else 8000)

    # ─── DICOM 외 포맷: 마스크 / 라벨맵 / DICOM SEG / STL ───

    def _apply_loaded_extras(self, loader):
        """함께 불러온 마스크·라벨맵은 AI 오버레이로, SEG는 참조 시리즈에, STL은 3D 탭에"""
        notes = []
        for uid, mask in loader.volume_masks.items():
            series = self._loader.get_series_by_uid(uid)
            if series is not None and self._apply_mask(series, mask):
                notes.append(f"마스크: {series.description}")
        removed = False
        for label_series in loader.label_candidates:
            base = self._find_matching_series(label_series)
            if base is None:
                continue
            if self._apply_mask(base, label_series.array.astype("uint8")):
                self._loader.series_dict.pop(label_series.series_uid, None)
                removed = True
                notes.append(f"라벨맵 {label_series.description} → {base.description}")
        self._pending_segmentations.extend(loader.segmentations)
        notes += self._apply_pending_segmentations()
        if loader.meshes:
            notes += self._show_meshes(loader.meshes)
        if removed:
            current = self._current_series
            self._update_series_list(
                select_uid=current.series_uid if current is not None
                and self._loader.get_series_by_uid(current.series_uid) else None)
        if loader.load_errors:
            first = loader.load_errors[0]
            notes.append(f"예: {os.path.basename(first[0])}: {first[1][:80]}")
        return notes

    def _apply_mask(self, series, mask):
        """라벨 값마다 라벨이 없으면 만들고 AI 마스크로 표시 (기존 칠한 곳은 유지)"""
        import numpy as np
        case = self._seg.case(series)
        if not case.editable or tuple(mask.shape) != tuple(case.shape):
            return False
        for value in np.unique(mask):
            if value and self._labels.get(int(value)) is None:
                self._labels.ensure(f"Label {int(value)}", int(value))
        merged = mask.astype(np.uint8)
        if not case.is_empty():
            merged = case.mask.copy()
            merged[mask > 0] = mask[mask > 0]
        self._seg.set_mask(series, merged)
        return True

    def _find_matching_series(self, label_series):
        """같은 크기·위치·방향의 다른 시리즈 (라벨맵을 얹을 영상)"""
        import numpy as np
        ref = label_series.slices[0]
        for series in self._loader.get_series_list():
            if series is label_series or series.num_slices != label_series.num_slices:
                continue
            first = series.slices[0]
            try:
                same_size = (int(first.Rows), int(first.Columns)) == (int(ref.Rows), int(ref.Columns))
                same_pos = np.allclose([float(v) for v in first.ImagePositionPatient],
                                       [float(v) for v in ref.ImagePositionPatient], atol=1.0)
                same_dir = np.allclose([float(v) for v in first.ImageOrientationPatient],
                                       [float(v) for v in ref.ImageOrientationPatient], atol=1e-3)
            except (AttributeError, TypeError, ValueError):
                continue
            if same_size and same_pos and same_dir:
                return series
        return None

    def _apply_pending_segmentations(self):
        """DICOM SEG → 참조 시리즈 오버레이. 참조 시리즈가 아직 없으면 다음 로딩 때 다시 시도"""
        from .formats.seg_reader import SegmentationFile
        notes, still_pending = [], []
        for path in self._pending_segmentations:
            try:
                seg_file = SegmentationFile(path)
            except Exception as e:  # noqa: BLE001 - 손상된 SEG는 건너뜀
                notes.append(f"SEG 읽기 실패 {os.path.basename(path)}: {e}")
                continue
            target = next((s for s in self._loader.get_series_list()
                           if seg_file.matches(s)), None)
            if target is None:
                still_pending.append(path)
                notes.append(f"SEG {os.path.basename(path)}: 참조 시리즈를 불러오면 자동으로 표시")
                continue

            def label_for(number, info):
                label = self._labels.ensure(info["label"])
                if info.get("color"):
                    self._labels.set_color(label["id"], info["color"])
                return label["id"]
            try:
                mask = seg_file.to_mask(target, label_for)
            except ValueError as e:
                notes.append(f"SEG {os.path.basename(path)}: {e}")
                continue
            self._apply_mask(target, mask)
            if self._current_series is not target:
                self._select_series(target)
            notes.append(f"SEG: {seg_file.description} → {target.description} "
                         f"({len(seg_file.segments)}개 세그먼트)")
        self._pending_segmentations = still_pending
        return notes

    def _show_meshes(self, paths):
        if not vtk_usable():
            return ["STL 메시는 3D Volume 탭(VTK)이 필요합니다"]
        notes = []
        for path in paths:
            try:
                cells = self._volume_widget.add_mesh(path)
                notes.append(f"STL {os.path.basename(path)} ({cells:,} 삼각형)")
            except ValueError as e:
                notes.append(str(e))
        self._tab_widget.setCurrentWidget(self._volume_widget)
        return notes

    # ─── Process 메뉴 / 분석 ───

    def _init_process_menu(self, menubar):
        menu = menubar.addMenu("P&rocess")
        groups = (("gaussian", "median", "unsharp"), ("sobel", "canny"),
                  ("erosion", "dilation", "opening", "closing"))
        filters = menu.addMenu("Filters (→ 새 시리즈)")
        for i, group in enumerate(groups):
            if i:
                filters.addSeparator()
            for key in group:
                action = filters.addAction(FILTERS[key][0] + "...")
                action.triggered.connect(lambda _=False, k=key: self._open_filter(k))
        menu.addSeparator()
        menu.addAction(self._act_plots)
        hist = menu.addAction("Histogram")
        hist.triggered.connect(lambda: (self._plot_dock.show(), self._plot_dock.raise_(),
                                        self._plot_dock.tabs.setCurrentIndex(0),
                                        self._plot_dock.refresh_histogram()))
        profile = menu.addAction("Line Profile 도구 (Shift+L)")
        profile.triggered.connect(lambda: self.select_tool_by_id("profile"))
        menu.addSeparator()
        for text, key in (("Analyze Particles...", "particles"),
                          ("Surface Model (Marching Cubes)...", "surface"),
                          ("Image Registration...", "registration"),
                          ("Image Fusion...", "fusion"),
                          ("Landmarks / Fiducials...", "landmarks")):
            action = menu.addAction(text)
            action.triggered.connect(lambda _=False, k=key: self.show_analysis(k))
        menu.addSeparator()
        self._colormap_menu = menu.addMenu("Color Map (LUT)")
        self.rebuild_colormap_menu()
        menu.addSeparator()
        menu.addAction(self._act_console)
        self._macro_menu = menu.addMenu("Macros")
        self._macros.changed.connect(self._rebuild_macro_menu)
        self._rebuild_macro_menu()

    def _open_filter(self, key):
        from .analysis.process_dialog import FilterDialog
        vp = self._target_viewport()
        if vp.series is None:
            QMessageBox.information(self, "Process", "시리즈를 먼저 여세요.")
            return
        if any(a is not None and a.ndim != 2 for a in [vp.series.get_pixel_array(0)]):
            QMessageBox.information(self, "Process", "흑백 영상에만 적용할 수 있습니다.")
            return
        FilterDialog(self, key).exec_()

    def show_analysis(self, section):
        self._ai_panel.show()
        self._ai_panel.raise_()
        self._ai_panel.tabs.setCurrentWidget(self._analysis_tab)
        self._analysis_tab.show_section(section)

    def select_tool_by_id(self, name):
        V = DicomViewport
        tool = {"landmark": V.TOOL_LANDMARK, "profile": V.TOOL_PROFILE}[name]
        action = self._tool_actions.get(tool)
        if action is not None:
            action.trigger()

    def rebuild_colormap_menu(self):
        menu = self._colormap_menu
        menu.clear()
        current = self._target_viewport().colormap_name
        for name in colormaps.names():
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _=False, n=name: self.apply_colormap(n))
        menu.addSeparator()
        load = menu.addAction("LUT 파일 불러오기...")
        load.triggered.connect(lambda: self.show_analysis("colormap")
                               or self._analysis_tab._load_lut())

    def apply_colormap(self, name):
        try:
            lut = colormaps.get_lut(name)
        except (KeyError, ValueError):
            return
        self._target_viewport().set_colormap(name, lut)
        self._analysis_tab.sync_colormap(name)
        self.rebuild_colormap_menu()
        self._statusbar.showMessage(f"Color Map: {name}", 3000)

    def _rebuild_macro_menu(self):
        menu = self._macro_menu
        menu.clear()
        for name in self._macros.names():
            action = menu.addAction(name)
            action.triggered.connect(lambda _=False, n=name: (
                self._console.show(),
                self._console.execute(self._macros.load(n), label=f"[매크로] {n}")))
        menu.addSeparator()
        manage = menu.addAction("매크로 관리...")
        manage.triggered.connect(lambda: self.show_analysis("macros"))

    def add_derived_series(self, series, select=True):
        """처리·정합·콘솔 결과 시리즈를 목록에 추가"""
        self._loader.series_dict[series.series_uid] = series
        current = self._current_series
        keep = current.series_uid if current is not None else None
        self._update_series_list(select_uid=series.series_uid if select else keep)
        self._analysis_tab.refresh_series()
        self._statusbar.showMessage(f"새 시리즈: {series.description}", 6000)

    def _on_any_slice_changed(self, *_):
        dock = self._plot_dock
        if dock.isVisible() and dock.tabs.currentIndex() == 0 and \
                dock._source.currentIndex() in (0, 2):
            dock.refresh_histogram()

    def _open_cloud(self, key):
        """File → Open from Google Drive / OneDrive"""
        from .cloud.browser import open_from_cloud
        from .cloud.secure_store import SecureStore
        providers = getattr(self, "_cloud_providers", None)
        if providers is None:
            from .cloud.google_drive import GoogleDriveProvider
            from .cloud.onedrive import OneDriveProvider
            store = SecureStore(self._settings)
            providers = self._cloud_providers = {
                "google": GoogleDriveProvider(self._app_settings, store),
                "onedrive": OneDriveProvider(self._app_settings, store)}
        open_from_cloud(self, providers[key])

    def _open_deploy_web(self):
        from .deploy_web import DeployWebDialog
        DeployWebDialog(self._app_settings, self).exec_()

    def _open_convert(self, source_kind=None, target=None):
        ConvertDialog(self, source_kind, target).exec_()

    def _on_series_dropped(self, viewport_index, uid):
        """트리에서 Multi View 뷰포트로 시리즈를 드롭"""
        series = self._loader.get_series_by_uid(uid)
        if series is None:
            return
        self._multi_viewport.set_active(viewport_index)
        self._select_series(series)

    def _on_paths_dropped(self, viewport_index, paths):
        """외부 파일/폴더를 Multi View 뷰포트로 드롭"""
        self.load_paths(paths, target_viewport=viewport_index)

    def _on_cursor_synced(self, linked, skipped):
        if linked == 0 and skipped > 0:
            self._statusbar.showMessage(
                "Sync Cursor: 같은 좌표계(Frame of Reference)의 시리즈가 없습니다", 3000)

    def _update_series_list(self, select_uid=None):
        """썸네일 패널 + 트리 갱신 후 첫 시리즈(또는 select_uid)를 표시"""
        series_list = self._loader.get_series_list()
        self._series_tree.populate(series_list, select_uid=select_uid, emit=False)
        self._series_panel.populate(series_list, select_uid=select_uid)
        uid = select_uid or self._series_panel.current_uid()
        series = self._loader.get_series_by_uid(uid) if uid else None
        if series is not None:
            self._select_series(series)
        self._ai_panel._rebuild_worklist()  # 불러온 케이스 표시 갱신
        self._analysis_tab.refresh_series()

    def _on_series_highlighted(self, uid):
        """패널/트리에서 누름·방향키: 선택 표시만 (로드는 클릭을 뗄 때)"""
        if self._loader.get_series_by_uid(uid) is None:
            return
        self._series_panel.select_uid(uid)
        self._series_tree.select_uid(uid)

    def _on_series_selected(self, uid):
        """클릭(뗄 때)/Enter: 2D 뷰와 Multi View 활성 칸에 로드"""
        series = self._loader.get_series_by_uid(uid)
        if series:
            self._select_series(series)

    # ─── 시리즈 패널 접기/펼치기 ───

    PANEL_ANIM_MS = 200

    def _restore_series_panel(self):
        width = self._settings.value("series_panel_width", 300, type=int)
        self._panel_width = max(220, min(800, width))
        visible = self._settings.value("series_panel_visible", True, type=bool)
        self._splitter.setSizes([self._panel_width, 1100])
        if not visible:
            self._set_panel_collapsed_now()
        self._update_panel_toggle()

    def _set_panel_collapsed_now(self):
        self._left_panel.setMinimumWidth(0)
        total = sum(self._splitter.sizes())
        self._splitter.setSizes([0, total])
        self._panel_visible = False

    def is_series_panel_visible(self):
        return self._panel_visible

    def toggle_series_panel(self):
        self.set_series_panel_visible(not self._panel_visible)

    def set_series_panel_visible(self, visible, animate=True):
        """시리즈 패널을 슬라이드로 펼치거나 접음 (상태는 QSettings에 저장)"""
        if self._panel_anim is not None:
            self._panel_anim.stop()
            self._panel_anim = None
        sizes = self._splitter.sizes()
        total = sum(sizes)
        current = sizes[0]
        if not visible and current > 0:
            self._panel_width = current  # 펼칠 때 원래 너비로
        target = min(self._panel_width, max(0, total - 300)) if visible else 0
        self._left_panel.setMinimumWidth(0)  # 애니메이션 중에는 0까지 줄어들 수 있게
        self._panel_visible = visible
        self._update_panel_toggle()
        self._save_panel_state()

        def apply(width):
            self._splitter.setSizes([int(width), total - int(width)])

        def finished():
            self._panel_anim = None
            apply(target)
            if visible:
                self._left_panel.setMinimumWidth(220)

        if not animate:
            finished()
            return
        anim = QVariantAnimation(self)
        anim.setDuration(self.PANEL_ANIM_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(float(current))
        anim.setEndValue(float(target))
        anim.valueChanged.connect(apply)
        anim.finished.connect(finished)
        self._panel_anim = anim
        anim.start()

    def _on_splitter_moved(self, pos, index):
        """경계를 손으로 끌어서 접거나 넓힌 경우도 반영"""
        width = self._splitter.sizes()[0]
        if width > 0:
            self._panel_width = width
        visible = width > 0
        if visible != self._panel_visible:
            self._panel_visible = visible
            if visible:
                self._left_panel.setMinimumWidth(220)
            self._update_panel_toggle()
        self._save_panel_state()

    def _update_panel_toggle(self):
        self._panel_toggle.setText("◀" if self._panel_visible else "▶")
        self._act_toggle_panel.setText(
            "Hide Series Panel" if self._panel_visible else "Show Series Panel")

    def _save_panel_state(self):
        self._settings.setValue("series_panel_visible", self._panel_visible)
        self._settings.setValue("series_panel_width", int(self._panel_width))

    def _toggle_series_view(self, tree):
        self._series_stack.setCurrentWidget(self._series_tree if tree else self._series_panel)
        self._view_toggle.setText("▦" if tree else "☰")

    def _select_series(self, series):
        self._current_series = series
        # 썸네일 패널과 트리의 선택 표시를 맞춤 (시그널 없이)
        self._series_panel.select_uid(series.series_uid)
        self._series_tree.select_uid(series.series_uid)
        if self._stack2d.currentWidget() is self._tile_view:
            self._tile_view.set_series(series)
        self._viewport.set_series(series)
        self._multi_viewport.set_series_to_active(series)
        self._slice_slider.setMaximum(max(0, series.num_slices - 1))
        self._slice_slider.setValue(0)
        self._sync_volume_tabs()
        self._refresh_image_info()
        self._ai_panel.on_series_changed()

    def _on_tab_changed(self, index):
        self._sync_volume_tabs()
        self._refresh_image_info()
        self._ai_panel.on_series_changed()
        # 레이아웃 드롭다운 표시를 현재 화면에 맞춤
        current = self._tab_widget.currentWidget()
        if current is self._stack2d:
            self._layout_combo.setCurrentText("2D")
        elif current is self._multi_viewport:
            text = self._multi_viewport.current_layout.upper()
            if self._layout_combo.findText(text) >= 0:
                self._layout_combo.setCurrentText(text)

    # ─── 뷰포트 공통 설정 ───

    def _configure_viewports(self):
        """마우스 매핑·주석 저장소를 모든 뷰포트가 공유"""
        for vp in self._all_viewports():
            vp.set_mouse_bindings(self._app_settings.mouse)
            vp.set_annotation_store(self._annotation_store)
            vp.set_segmentation(self._seg)
            vp.set_landmark_store(self._landmarks)
            vp.profile_measured.connect(self._plot_dock.show_profile)
            vp.slice_changed.connect(self._on_any_slice_changed)
        self._tile_view.set_annotation_store(self._annotation_store)

    # ─── 레이아웃 / Hanging Protocol ───

    def _study_series(self, series=None):
        """series(기본: 현재 시리즈)와 같은 검사의 시리즈 목록 (트리 표시 순서)"""
        series = series or self._current_series
        if series is None:
            return []
        for _pname, _pid, studies in group_series(self._loader.get_series_list()):
            for _date, _time, _desc, group in studies:
                if any(s is series for s in group):
                    return group
        return [series]

    def _apply_layout_choice(self, choice):
        if self._current_series is None:
            return
        if choice == "2D":
            self._set_tile_mode(False)
            self._tab_widget.setCurrentWidget(self._stack2d)
            return
        if choice == "Default":
            self._apply_hanging(auto=False)
            return
        study = self._study_series()
        if choice == "ALL":
            self._multi_viewport.show_series(study)
        else:
            layout = choice.lower()
            rows, cols = LAYOUTS[layout]
            # 현재 시리즈부터 검사 순서대로 채움
            start = next((i for i, s in enumerate(study) if s is self._current_series), 0)
            ordered = study[start:] + study[:start]
            self._multi_viewport.show_series(ordered[:rows * cols], layout=layout)
        self._tab_widget.setCurrentWidget(self._multi_viewport)

    def _apply_hanging(self, auto=False):
        """현재 검사에 맞는 Hanging Protocol 적용 → 적용한 프로토콜 이름 (없으면 None)

        auto=False이고 맞는 프로토콜이 없으면 2x2로 앞에서부터 배치.
        """
        study = self._study_series()
        if not study:
            return None
        protocol = find_protocol(self._app_settings.hanging_protocols(), study)
        slots = assign_slots(protocol, study) if protocol else []
        if protocol and any(slots):
            layout = protocol.get("layout", "2x2")
            self._multi_viewport.show_series(slots, layout=layout if layout in LAYOUTS else None)
            self._tab_widget.setCurrentWidget(self._multi_viewport)
            self._statusbar.showMessage(f"Hanging Protocol: {protocol['name']}", 6000)
            return protocol["name"]
        if not auto:
            self._multi_viewport.show_series(study[:4], layout="2x2")
            self._tab_widget.setCurrentWidget(self._multi_viewport)
            self._statusbar.showMessage("맞는 Hanging Protocol이 없어 2x2 기본 배치", 6000)
        return None

    def _save_layout_as_protocol(self):
        mv = self._multi_viewport
        placed = [vp.series for vp in mv.visible_viewports]
        if not any(placed):
            QMessageBox.information(self, "Hanging Protocol",
                                    "Multi View에 시리즈를 배치한 뒤 저장하세요.")
            return
        name, ok = QInputDialog.getText(self, "Save Hanging Protocol", "프로토콜 이름:")
        if not ok or not name.strip():
            return
        protocol = protocol_from_layout(name.strip(), mv.current_layout, placed,
                                        [s for s in placed if s])
        protocols = [p for p in self._app_settings.hanging_protocols()
                     if p.get("name") != protocol["name"]]
        # 사용자 프로토콜을 기본 프로토콜보다 먼저 검사
        self._app_settings.save_hanging_protocols([protocol] + protocols)
        self._statusbar.showMessage(f"Hanging Protocol 저장: {protocol['name']}", 6000)

    # ─── 비교 (이전 검사) ───

    def _find_prior(self, series):
        """같은 환자의 다른 검사에서 가장 비슷한 시리즈 (없으면 None)"""
        if series is None:
            return None
        own_tokens = set((series.description or "").upper().split())
        orient = orientation_name(series.slices[0]) if series.slices else ""
        best, best_key = None, None
        for other in self._loader.get_series_list():
            if (other.patient_id != series.patient_id or other.study_uid == series.study_uid
                    or other.modality != series.modality):
                continue
            tokens = set((other.description or "").upper().split())
            same_orient = orientation_name(other.slices[0]) == orient if other.slices else False
            earlier = other.study_date <= series.study_date
            key = (same_orient, len(own_tokens & tokens), earlier, other.study_date)
            if best_key is None or key > best_key:
                best, best_key = other, key
        return best

    def _compare_prior(self):
        current = self._current_series
        prior = self._find_prior(current)
        if prior is None:
            QMessageBox.information(self, "Compare",
                                    "같은 환자의 다른 날짜 검사에서 비교할 시리즈를 찾지 못했습니다.")
            return
        mv = self._multi_viewport
        mv.show_series([current, prior], layout="1x2")
        mv.link_compare(0, 1)
        self._act_sync_scroll.setChecked(True)
        mv.set_sync_scroll(True)
        self._act_sync_window.setChecked(True)
        mv.set_sync_window(True)
        self._tab_widget.setCurrentWidget(mv)
        self._statusbar.showMessage(
            f"Compare: {current.study_date} {current.description}  ↔  "
            f"{prior.study_date} {prior.description}", 8000)

    # ─── Stack / Tile, Key Image ───

    def _set_tile_mode(self, on):
        self._act_tile.setChecked(bool(on))
        if on:
            if self._current_series is None:
                self._act_tile.setChecked(False)
                return
            self._tile_view.set_series(self._current_series,
                                       window=self._viewport.window_level,
                                       start_index=self._viewport.current_slice)
            self._stack2d.setCurrentWidget(self._tile_view)
            self._tab_widget.setCurrentWidget(self._stack2d)
        else:
            self._stack2d.setCurrentWidget(self._viewport)

    def _on_tile_activated(self, series, index):
        """Tile에서 더블클릭 → 해당 슬라이스를 Stack 모드로"""
        self._set_tile_mode(False)
        if series is not self._current_series:
            self._select_series(series)
        self._viewport.go_to_slice(index)

    def _toggle_key_image(self):
        vp = self._target_viewport()
        if self._stack2d.currentWidget() is self._tile_view and self._tile_view.items:
            # Tile 모드: 선택한 칸
            n = self._tile_view.selected_index
            if n is not None:
                series, index = self._tile_view.items[n]
                self._annotation_store.toggle_key_image(series, index)
            return
        vp.toggle_key_image()

    def _key_image_items(self):
        items = []
        for _key, info in self._annotation_store.key_images():
            series = self._loader.get_series_by_uid(info.get("series_uid"))
            if series is not None and 0 <= info.get("index", -1) < series.num_slices:
                items.append((series, info["index"]))
        return items

    def _show_key_images(self):
        items = self._key_image_items()
        if not items:
            QMessageBox.information(self, "Key Images", "표시된 Key Image가 없습니다. (K로 표시)")
            return
        self._tile_view.set_items(items, title="★ Key Images",
                                  window=self._viewport.window_level)
        self._act_tile.setChecked(True)
        self._stack2d.setCurrentWidget(self._tile_view)
        self._tab_widget.setCurrentWidget(self._stack2d)

    def _window_for(self, series):
        """출력용 W/L: 현재 보고 있는 시리즈면 화면 값, 아니면 DICOM 기본값"""
        vp = self._target_viewport()
        if vp.series is series:
            return vp.window_level
        return series.get_default_window()

    def _export_key_images(self):
        items = self._key_image_items()
        if not items:
            QMessageBox.information(self, "Export Key Images", "표시된 Key Image가 없습니다.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Export Key Images", self._last_dir())
        if not folder:
            return
        import json
        from PyQt5.QtGui import QImage
        exported = []
        for n, (series, index) in enumerate(items, 1):
            img = render_8bit(series, index, self._window_for(series))
            if img is None:
                continue
            h, w = img.shape
            name = "".join(c if c.isalnum() or c in "-_" else "_"
                           for c in (series.description or "series"))
            filename = f"key_{n:03d}_{name}_im{index + 1}.png"
            QImage(img.data, w, h, w, QImage.Format_Grayscale8).copy().save(
                os.path.join(folder, filename))
            ds = series.slices[index]
            exported.append({"file": filename, "series": series.description,
                             "series_number": series.series_number,
                             "image": index + 1,
                             "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", ""))})
        with open(os.path.join(folder, "key_images.json"), "w", encoding="utf-8") as f:
            json.dump(exported, f, ensure_ascii=False, indent=2)
        self._statusbar.showMessage(f"Key Image {len(exported)}장 내보냄: {folder}", 8000)

    # ─── 주석 저장 / 불러오기 ───

    def _save_annotations(self):
        if self._annotation_store.count() == 0 and not self._annotation_store.key_images():
            QMessageBox.information(self, "Save Annotations", "저장할 주석이 없습니다.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Annotations", os.path.join(self._last_dir(), "annotations.json"),
            "Annotations (*.json)")
        if path:
            self._annotation_store.save_json(path)
            self._statusbar.showMessage(f"주석 저장: {path}", 6000)

    def _load_annotations(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Annotations", self._last_dir(),
                                              "Annotations (*.json)")
        if not path:
            return
        try:
            count = self._annotation_store.load_json(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Load Annotations", str(e))
            return
        self._statusbar.showMessage(f"주석 {count}개 불러옴", 6000)

    # ─── 설정 / 네트워크 ───

    def _open_settings(self, tab="mouse"):
        dialog = SettingsDialog(self._app_settings, self, tab=tab)
        if dialog.exec_():
            self._rebuild_preset_menu()
            if self._app_settings.report_folder() != self._report_library.folder:
                self._report_library.set_folder(self._app_settings.report_folder())
            self._statusbar.showMessage("설정을 저장했습니다.", 4000)

    def _rebuild_preset_menu(self):
        menu = self._preset_menu
        menu.clear()
        for p in self._app_settings.window_presets():
            action = menu.addAction(f"{p['name']} (C:{p['center']:g} W:{p['width']:g})")
            action.triggered.connect(
                lambda checked=False, c=p["center"], w=p["width"]:
                self._target_viewport().set_window(c, w, user=True))
        menu.addSeparator()
        edit = menu.addAction("Edit Presets...")
        edit.triggered.connect(lambda: self._open_settings("presets"))

    def _open_network_dialog(self, dialog_class):
        vp = self._target_viewport()
        series = vp.series
        sources = {"image": [], "series": [], "key": []}
        if series is not None:
            window = self._window_for(series)
            sources["image"] = [(series, vp.current_slice, window)]
            sources["series"] = [(series, i, window) for i in range(series.num_slices)]
        sources["key"] = [(s, i, self._window_for(s)) for s, i in self._key_image_items()]
        if not any(sources.values()):
            QMessageBox.information(self, dialog_class.title, "보낼 영상이 없습니다.")
            return
        dialog = dialog_class(self._app_settings, sources, self,
                              open_settings=self._open_settings)
        dialog.exec_()

    # ─── 대상 뷰포트 / 전체 적용 ───

    def _all_viewports(self):
        return [self._viewport] + self._multi_viewport.viewports

    def _target_viewport(self):
        """이미지 조작 대상: Multi View 탭이면 활성 칸, 그 외에는 2D 뷰포트 (Stack)"""
        if self._tab_widget.currentWidget() is self._multi_viewport:
            return self._multi_viewport.active_viewport
        return self._viewport

    def _set_tool_all(self, tool):
        self._seg.set_tool(None)  # 툴바 도구를 고르면 세그멘테이션 도구 해제
        for vp in self._all_viewports():
            vp.set_tool(tool)

    def _set_value_lens_all(self, enabled):
        for vp in self._all_viewports():
            vp.set_value_lens(enabled)

    def _refresh_image_info(self):
        if not self._info_panel.isVisible():
            return
        vp = self._target_viewport()
        ds = vp.current_dataset()
        total = vp.series.num_slices if vp.series else 0
        self._info_panel.show_image(ds, vp.current_slice, total)

    def _capture_image(self):
        """현재 화면을 오버레이·측정선 포함해 저장 (Capture Image Only)"""
        current = self._tab_widget.currentWidget()
        if current is self._stack2d and self._stack2d.currentWidget() is self._tile_view:
            pixmap = self._tile_view.grab()
            name = "tile"
        elif current in (self._stack2d, self._multi_viewport):
            vp = self._target_viewport()
            if vp.series is None:
                QMessageBox.information(self, "Info", "캡처할 영상이 없습니다.")
                return
            pixmap = vp.capture()
            name = (vp.series.description or "capture").strip()
        else:
            pixmap = current.grab()
            name = self._tab_widget.tabText(self._tab_widget.currentIndex())
        name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Capture Image", os.path.join(self._last_dir(), f"{name}.png"),
            "PNG (*.png);;JPEG (*.jpg)")
        if not filepath:
            return
        if not filepath.lower().endswith((".png", ".jpg", ".jpeg")):
            filepath += ".png"
        if pixmap.save(filepath, quality=95):
            self._statusbar.showMessage(f"Captured: {filepath}", 5000)
        else:
            QMessageBox.warning(self, "Capture", f"저장하지 못했습니다:\n{filepath}")

    def closeEvent(self, event):
        self._ai_panel.shutdown()  # 편집한 마스크 저장
        self._series_tree.shutdown()
        self._series_panel.shutdown()
        super().closeEvent(event)

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
        """환자 정보 오버레이 + 측정/주석 표시 토글 (T / O)"""
        show = not self._viewport.overlay_visible
        for vp in self._all_viewports():
            vp.set_overlay_visible(show)
        self._statusbar.showMessage(f"Overlay {'ON' if show else 'OFF'}", 2000)

    def _toggle_maximize(self):
        """Space: Multi View에서 선택한 칸만 크게 ↔ 원래 배치"""
        if self._tab_widget.currentWidget() is not self._multi_viewport:
            self._statusbar.showMessage("Space: Multi View에서 선택한 칸을 크게 봅니다", 3000)
            return
        mv = self._multi_viewport
        if mv.num_visible <= 1 and not mv.is_maximized:
            return
        maximized = mv.toggle_maximize()
        self._statusbar.showMessage(
            "선택한 칸 최대화 (Space로 복귀)" if maximized else "원래 레이아웃", 3000)

    # ─── Reading (판독) ───

    def _studies_for_matching(self):
        return [{"study_uid": s.study_uid, "patient_id": s.patient_id,
                 "study_date": s.study_date}
                for s in {s.study_uid: s for s in self._loader.get_series_list()}.values()
                if s.study_uid]

    def _open_reading(self):
        series = self._target_viewport().series or self._current_series
        if series is None:
            QMessageBox.information(self, "Reading", "먼저 검사를 여세요.")
            return
        study_uid = series.study_uid
        dialog = self._reading_dialogs.get(study_uid)
        try:
            if dialog is not None:
                dialog.isVisible()  # 이미 삭제된 창이면 RuntimeError
        except RuntimeError:
            dialog = None
        if dialog is None:
            dialog = ReadingDialog(self._study_series(series), self._report_store,
                                   self._report_library, self._app_settings,
                                   self._studies_for_matching(), self)
            # 닫힌 창만 목록에서 제거 (늦게 온 시그널이 새 창을 지우지 않도록)
            dialog.finished.connect(
                lambda *_, d=dialog: self._reading_dialogs.get(study_uid) is d
                and self._reading_dialogs.pop(study_uid))

            dialog.setAttribute(Qt.WA_DeleteOnClose)
            self._reading_dialogs[study_uid] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

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
