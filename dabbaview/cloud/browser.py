# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
클라우드 폴더 브라우저 (Google Drive / OneDrive 공통)

로그인 → 폴더 탐색 → 파일/폴더 선택(폴더는 하위 폴더까지) → 캐시를 거쳐 내려받기 → 불러오기
네트워크 작업은 모두 백그라운드 스레드에서.
"""
import traceback

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMessageBox, QProgressBar, QPushButton, QStyle,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import cache
from . import CloudError, NotConfigured, transfer

LARGE_DOWNLOAD = 2 * 1024 ** 3   # 2 GB 넘으면 확인


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return ""


class _Worker(QThread):
    progress = pyqtSignal(object)   # 문자열 또는 ("count", 완료, 전체, 문구)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.cancel = False

    def run(self):
        try:
            self.done.emit(self._fn(self.progress.emit, lambda: self.cancel))
        except CloudError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - 네트워크/인증 라이브러리 예외를 대화상자에 표시
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}")


class CloudBrowserDialog(QDialog):
    """provider: google_drive.GoogleDriveProvider / onedrive.OneDriveProvider"""

    def __init__(self, provider, main_window):
        super().__init__(main_window)
        self.provider = provider
        self.main = main_window
        self._worker = None
        self._stack = []          # 들어간 폴더들 (CloudItem)
        self._items = []
        self.downloaded = []
        self.setWindowTitle(f"Open from {provider.name}")
        self.resize(760, 560)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self._account = QLabel("로그인 중...")
        top.addWidget(self._account, 1)
        self._sign_out = QPushButton("로그아웃")
        self._sign_out.clicked.connect(self._do_sign_out)
        top.addWidget(self._sign_out)
        layout.addLayout(top)

        nav = QHBoxLayout()
        style = self.style()
        self._up = QPushButton(style.standardIcon(QStyle.SP_FileDialogToParent), "")
        self._up.setToolTip("상위 폴더")
        self._up.clicked.connect(self._go_up)
        self._home = QPushButton(style.standardIcon(QStyle.SP_DirHomeIcon), "")
        self._home.setToolTip("처음 (내 드라이브 / 공유)")
        self._home.clicked.connect(self._go_home)
        self._refresh = QPushButton(style.standardIcon(QStyle.SP_BrowserReload), "")
        self._refresh.setToolTip("새로 고침")
        self._refresh.clicked.connect(self._reload)
        self._path = QLabel()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("이름 필터")
        self._filter.setClearButtonEnabled(True)
        self._filter.setMaximumWidth(150)
        self._filter.textChanged.connect(self._apply_filter)
        # 전체 검색: '다른 컴퓨터(백업된 PC)' 폴더처럼 목록에 안 나오는 곳도 이름으로 찾음
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 폴더 이름으로 전체 검색 (Enter)")
        self._search.setToolTip("드라이브 전체에서 폴더 이름을 찾습니다.\n"
                                "'다른 컴퓨터'(백업된 PC)에 있는 폴더도 이렇게 찾을 수 있습니다.")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(260)
        self._search.returnPressed.connect(self._do_search)
        for w in (self._up, self._home, self._refresh):
            nav.addWidget(w)
        nav.addWidget(self._path, 1)
        nav.addWidget(self._search)
        nav.addWidget(self._filter)
        layout.addLayout(nav)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["이름", "크기", "수정"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._tree, 1)

        hint = QLabel("폴더는 더블클릭으로 들어갑니다. 파일·폴더를 골라(여러 개 가능) '열기'를 누르면 "
                      "내려받아 불러옵니다. 폴더를 고르면 하위 폴더까지 DICOM 파일을 모두 받습니다 "
                      "(로컬 Open Folder와 같은 기준).\n"
                      "목록에 없는 폴더(구글 드라이브 '다른 컴퓨터'에 백업된 PC 폴더 등)는 "
                      "위쪽 🔍 칸에 이름을 넣고 Enter로 찾으세요.\n"
                      f"한 번 받은 파일은 캐시에 보관되어 다시 열 때 내려받지 않습니다: {cache.cache_root()}")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        layout.addWidget(hint)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._status = QLabel()
        layout.addWidget(self._status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self._open_here = QPushButton("📂 현재 폴더 전체 열기")
        self._open_here.setToolTip("지금 보고 있는 폴더를 하위 폴더까지 통째로 내려받아 엽니다")
        self._open_here.clicked.connect(self._open_current_folder)
        buttons.addWidget(self._open_here)
        self._open = QPushButton("열기 (내려받아 불러오기)")
        self._open.setDefault(True)
        self._open.clicked.connect(self._download_selected)
        self._cancel = QPushButton("닫기")
        self._cancel.clicked.connect(self._close)
        buttons.addWidget(self._open)
        buttons.addWidget(self._cancel)
        layout.addLayout(buttons)
        self._set_busy(False)

    # ─── 백그라운드 ───

    def _run(self, text, fn, done):
        if self._worker is not None:
            return
        worker = _Worker(fn)
        worker.progress.connect(self._on_progress)
        worker.done.connect(lambda r: (self._finish(), done(r)))
        worker.failed.connect(self._failed)
        self._worker = worker
        self._status.setText(text)
        self._set_busy(True)
        worker.start()

    def _on_progress(self, value):
        if isinstance(value, tuple) and value and value[0] == "count":
            _tag, done, total, text = value
            self._progress.setRange(0, max(total, 1))
            self._progress.setValue(done)
            self._progress.setFormat(f"{done}/{total} files")
            self._status.setText(text)
        else:
            self._status.setText(str(value))

    def _finish(self):
        if self._worker is not None:
            self._worker.wait(2000)
        self._worker = None
        self._set_busy(False)

    def _failed(self, message):
        self._finish()
        self._status.setText(f"오류: {message.splitlines()[0] if message else ''}")
        QMessageBox.warning(self, self.provider.name, message)

    def _set_busy(self, busy):
        self._progress.setVisible(busy)
        if busy:
            self._progress.setRange(0, 0)   # 파일 수를 알기 전에는 움직이는 막대
            self._progress.setFormat("")
        for w in (self._up, self._home, self._refresh, self._open, self._sign_out, self._tree):
            w.setEnabled(not busy)
        self._open_here.setEnabled(not busy and bool(self._stack))
        self._cancel.setText("취소" if busy else "닫기")

    # ─── 로그인 / 탐색 ───

    def start(self):
        """로그인(필요하면 브라우저) 후 첫 화면. 키가 없으면 False"""
        if not self.provider.is_configured():
            return False
        provider = self.provider

        def task(progress, cancelled):
            progress("로그인 확인 중... (처음이면 브라우저에서 로그인하세요)")
            account = provider.sign_in(interactive=True)
            return account, provider.roots()

        def done(result):
            account, roots = result
            self._account.setText(f"✅ {self.provider.name}: {account or '로그인됨'}")
            self._stack = []
            self._show(roots, "/")
        self._run("로그인 중...", task, done)
        return True

    def _show(self, items, path):
        self._items = items
        self._path.setText(path)
        self._tree.clear()
        style = self.style()
        folder_icon = style.standardIcon(QStyle.SP_DirIcon)
        file_icon = style.standardIcon(QStyle.SP_FileIcon)
        for item in items:
            row = QTreeWidgetItem([item.name, "" if item.is_folder else human_size(item.size),
                                   item.modified])
            row.setIcon(0, folder_icon if item.is_folder else file_icon)
            row.setData(0, Qt.UserRole, item)
            if not item.is_folder and not item.downloadable:
                row.setDisabled(True)
                row.setToolTip(0, "Google 문서 형식 - 내려받을 수 없음")
            self._tree.addTopLevelItem(row)
        self._up.setEnabled(bool(self._stack))
        self._open_here.setEnabled(bool(self._stack))
        self._apply_filter(self._filter.text())
        folders = sum(1 for i in items if i.is_folder)
        self._status.setText(f"폴더 {folders}개 · 파일 {len(items) - folders}개")

    def _apply_filter(self, text):
        text = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            row.setHidden(bool(text) and text not in row.text(0).lower())

    def _do_search(self):
        """폴더 이름으로 드라이브 전체 검색 (목록에 안 나오는 '다른 컴퓨터' 폴더도 찾음)"""
        text = self._search.text().strip()
        if len(text) < 2:
            self._status.setText("두 글자 이상 입력하세요.")
            return
        provider = self.provider
        if not hasattr(provider, "search"):
            self._status.setText("이 서비스는 검색을 지원하지 않습니다.")
            return

        def task(progress, cancelled):
            progress(f"'{text}' 검색 중...")
            return provider.search(text)

        def done(items):
            self._stack.clear()          # 검색 결과는 경로가 없으므로 처음으로 되돌림
            self._show(items, f"검색 결과: '{text}' — 폴더 {len(items)}개 (더블클릭해서 열기)")
            if not items:
                self._status.setText(f"'{text}' 이름의 폴더를 찾지 못했습니다.")
        self._run(f"'{text}' 검색 중...", task, done)

    def _open_folder(self, folder, push=True):
        provider = self.provider

        def task(progress, cancelled):
            return provider.list_children(folder)

        def done(items):
            if push:
                self._stack.append(folder)
            self._show(items, "/" + "/".join(f.name for f in self._stack))
        self._run(f"'{folder.name}' 여는 중...", task, done)

    def _on_double_click(self, row, _col):
        item = row.data(0, Qt.UserRole)
        if item.is_folder:
            self._open_folder(item)
        else:
            self._download_selected()

    def _go_up(self):
        if not self._stack:
            return
        self._stack.pop()
        if self._stack:
            folder = self._stack.pop()
            self._open_folder(folder)
        else:
            self._go_home()

    def _go_home(self):
        provider = self.provider
        self._stack = []
        self._run("불러오는 중...", lambda p, c: provider.roots(), lambda roots: self._show(roots, "/"))

    def _reload(self):
        if self._stack:
            self._open_folder(self._stack.pop())
        else:
            self._go_home()

    def _do_sign_out(self):
        self.provider.sign_out()
        QMessageBox.information(self, self.provider.name,
                                "로그아웃했습니다. 저장된 토큰을 지웠습니다.")
        self.reject()

    # ─── 다운로드 ───

    def _download_selected(self):
        selected = [r.data(0, Qt.UserRole) for r in self._tree.selectedItems()]
        selected = [i for i in selected if i.is_folder or i.downloadable]
        if not selected:
            QMessageBox.information(self, self.provider.name, "열 파일이나 폴더를 고르세요.")
            return
        self._download(selected)

    def _open_current_folder(self):
        if self._stack:
            self._download([self._stack[-1]])

    def _download(self, items):
        """1단계: 폴더를 훑어 파일 목록 → (크면 확인) → 2단계: 캐시를 거쳐 내려받기"""
        provider = self.provider

        def plan_task(progress, cancelled):
            return transfer.plan(provider, items, progress, cancelled)

        def planned(result):
            files, skipped, tops = result
            if not files:
                QMessageBox.information(self, provider.name,
                                        "불러올 DICOM 파일이 없습니다." +
                                        (f" (다른 형식 {skipped}개는 건너뜀)" if skipped else ""))
                return
            to_fetch = [i for _rel, i in files if not transfer.cached_path(provider, i)]
            size = sum(i.size for i in to_fetch)
            if size > LARGE_DOWNLOAD and QMessageBox.question(
                    self, provider.name,
                    f"파일 {len(files)}개 중 {len(to_fetch)}개({human_size(size)})를 내려받아야 합니다. "
                    "계속할까요?") != QMessageBox.Yes:
                return
            self._skipped = skipped
            self._run(f"내려받는 중... 0/{len(files)} files",
                      lambda progress, cancelled: transfer.fetch(provider, files, tops,
                                                                 progress, cancelled),
                      fetched)

        def fetched(result):
            paths, stats = result
            self.downloaded = paths
            self.stats = dict(stats, skipped=getattr(self, "_skipped", 0))
            self.accept()
        self._run("폴더 확인 중...", plan_task, planned)

    def _close(self):
        if self._worker is not None:
            self._worker.cancel = True
            self._status.setText("취소하는 중...")
            return
        self.reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.cancel = True
            self._worker.wait(3000)
        super().closeEvent(event)


def open_from_cloud(main_window, provider):
    """메뉴 동작: 키 확인 → 브라우저 → 다운로드 → 불러오기"""
    if not provider.is_configured():
        help_text = provider.setup_help
        box = QMessageBox(main_window)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(provider.name)
        box.setText(f"{provider.name} 연동 키가 설정되지 않았습니다.")
        box.setInformativeText(help_text)
        open_settings = box.addButton("Settings 열기", QMessageBox.AcceptRole)
        box.addButton("닫기", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is open_settings:
            main_window._open_settings("cloud")
        return
    dialog = CloudBrowserDialog(provider, main_window)
    try:
        dialog.start()
    except NotConfigured:
        return
    if dialog.exec_() == QDialog.Accepted and dialog.downloaded:
        stats = getattr(dialog, "stats", {})
        main_window.statusBar().showMessage(
            f"{provider.name}: 파일 {stats.get('total', 0)}개 (캐시 {stats.get('hits', 0)}개, "
            f"내려받음 {cache.human_size(stats.get('bytes', 0))}"
            + (f", 건너뜀 {stats['skipped']}개" if stats.get("skipped") else "") + ") 불러오는 중...",
            10000)
        # 로컬 Open Folder와 같은 방식으로 (세션 폴더는 최근 목록에 남기지 않음)
        main_window.load_paths(dialog.downloaded, remember=False)
