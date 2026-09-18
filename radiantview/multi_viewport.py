"""
다중 뷰포트 레이아웃 매니저
1x1, 1x2, 2x2 등 레이아웃 전환 지원
"""
from PyQt5.QtWidgets import (QWidget, QGridLayout, QVBoxLayout,
                              QHBoxLayout, QPushButton, QButtonGroup,
                              QFrame, QSizePolicy)
from PyQt5.QtCore import Qt, QEvent, pyqtSignal

from .viewport import DicomViewport
from .series_tree import SERIES_MIME_TYPE

ACTIVE_BORDER = ("#007acc", 2)
DROP_TARGET_BORDER = ("#ffb000", 4)


class MultiViewport(QWidget):
    """다중 뷰포트 관리 위젯"""

    active_viewport_changed = pyqtSignal(int)  # viewport index
    series_dropped = pyqtSignal(int, str)      # viewport index, SeriesInstanceUID
    paths_dropped = pyqtSignal(int, list)      # viewport index, 파일/폴더 경로

    LAYOUT_1x1 = "1x1"
    LAYOUT_1x2 = "1x2"
    LAYOUT_2x1 = "2x1"
    LAYOUT_2x2 = "2x2"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._viewports = []
        self._active_index = 0
        self._current_layout = self.LAYOUT_1x1
        self._series_list = []

        self._init_ui()

    def _init_ui(self):
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # 레이아웃 선택 버튼
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(2)

        self._btn_group = QButtonGroup(self)
        layouts = [
            (self.LAYOUT_1x1, "▣"),
            (self.LAYOUT_1x2, "◫"),
            (self.LAYOUT_2x1, "◩"),
            (self.LAYOUT_2x2, "⊞"),
        ]
        for layout_id, icon in layouts:
            btn = QPushButton(icon)
            btn.setFixedSize(32, 28)
            btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton { background: #333; color: #ccc; border: 1px solid #555;
                              font-size: 14px; }
                QPushButton:checked { background: #007acc; color: white; }
                QPushButton:hover { background: #444; }
            """)
            btn.setProperty("layout_id", layout_id)
            btn.clicked.connect(lambda checked, lid=layout_id: self.set_layout(lid))
            self._btn_group.addButton(btn)
            btn_layout.addWidget(btn)
            if layout_id == self.LAYOUT_1x1:
                btn.setChecked(True)

        btn_layout.addStretch()
        self._main_layout.addLayout(btn_layout)

        # 뷰포트 그리드 컨테이너
        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(2)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.addWidget(self._grid_container)

        # 4개 뷰포트 미리 생성
        for i in range(4):
            vp = DicomViewport()
            vp.setProperty("vp_index", i)
            vp.setMinimumSize(200, 200)  # 2x2 레이아웃이 작은 화면에도 들어가도록
            vp.setAcceptDrops(True)
            vp.installEventFilter(self)
            self._viewports.append(vp)

        self._apply_layout()

    def eventFilter(self, obj, event):
        """뷰포트 클릭 시 활성 뷰포트 변경 + 드래그 앤 드롭 처리"""
        if obj not in self._viewports:
            return super().eventFilter(obj, event)
        etype = event.type()
        idx = self._viewports.index(obj)

        if etype == QEvent.MouseButtonPress:
            self.set_active(idx)

        elif etype in (QEvent.DragEnter, QEvent.DragMove):
            if self._accepts(event.mimeData()):
                event.acceptProposedAction()
                obj.set_highlight(*DROP_TARGET_BORDER)
            else:
                event.ignore()
            return True

        elif etype == QEvent.DragLeave:
            self._highlight_active()
            return True

        elif etype == QEvent.Drop:
            self._highlight_active()
            mime = event.mimeData()
            if mime.hasFormat(SERIES_MIME_TYPE):
                uid = bytes(mime.data(SERIES_MIME_TYPE)).decode('utf-8')
                event.acceptProposedAction()
                self.series_dropped.emit(idx, uid)
            elif mime.hasUrls():
                paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
                if paths:
                    event.acceptProposedAction()
                    self.paths_dropped.emit(idx, paths)
            return True

        return super().eventFilter(obj, event)

    @staticmethod
    def _accepts(mime):
        if mime.hasFormat(SERIES_MIME_TYPE):
            return True
        return mime.hasUrls() and any(u.isLocalFile() for u in mime.urls())

    def set_layout(self, layout_id):
        """레이아웃 변경"""
        self._current_layout = layout_id
        self._apply_layout()

    def _apply_layout(self):
        """현재 레이아웃 적용"""
        # 기존 위젯 제거
        for vp in self._viewports:
            vp.setParent(None)

        configs = {
            self.LAYOUT_1x1: [(0, 0, 1, 1)],
            self.LAYOUT_1x2: [(0, 0, 1, 1), (0, 1, 1, 1)],
            self.LAYOUT_2x1: [(0, 0, 1, 1), (1, 0, 1, 1)],
            self.LAYOUT_2x2: [(0, 0, 1, 1), (0, 1, 1, 1),
                              (1, 0, 1, 1), (1, 1, 1, 1)],
        }

        positions = configs.get(self._current_layout, [(0, 0, 1, 1)])
        for i, (row, col, rspan, cspan) in enumerate(positions):
            self._grid_layout.addWidget(self._viewports[i],
                                        row, col, rspan, cspan)
            self._viewports[i].show()

        # 사용하지 않는 뷰포트 숨기기
        for i in range(len(positions), 4):
            self._viewports[i].hide()

        self._highlight_active()

    def set_active(self, index):
        """활성 뷰포트 설정"""
        if 0 <= index < len(self._viewports):
            self._active_index = index
            self._highlight_active()
            self.active_viewport_changed.emit(index)

    def _highlight_active(self):
        """활성 뷰포트 테두리 강조"""
        # 1x1에서는 테두리 불필요
        multi = self.num_visible > 1
        for i, vp in enumerate(self._viewports):
            if multi and i == self._active_index:
                vp.set_highlight(*ACTIVE_BORDER)
            else:
                vp.set_highlight(None)

    @property
    def viewports(self):
        return list(self._viewports)

    @property
    def active_viewport(self):
        """현재 활성 뷰포트 반환"""
        return self._viewports[self._active_index]

    def get_viewport(self, index):
        if 0 <= index < len(self._viewports):
            return self._viewports[index]
        return None

    def set_series_to_active(self, series):
        """활성 뷰포트에 시리즈 설정"""
        self.active_viewport.set_series(series)

    def set_series_to_viewport(self, index, series):
        """특정 뷰포트에 시리즈 설정"""
        vp = self.get_viewport(index)
        if vp:
            vp.set_series(series)

    @property
    def num_visible(self):
        configs = {
            self.LAYOUT_1x1: 1,
            self.LAYOUT_1x2: 2,
            self.LAYOUT_2x1: 2,
            self.LAYOUT_2x2: 4,
        }
        return configs.get(self._current_layout, 1)
