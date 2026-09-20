# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
클라우드 폴더 브라우저 (Google Drive / OneDrive 공통)

로그인 → 폴더 탐색 → 파일/폴더 선택(폴더는 하위 폴더까지) → 캐시를 거쳐 내려받기 → 불러오기
네트워크 작업은 모두 백그라운드 스레드에서.
"""
import os
import traceback

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMessageBox, QProgressBar, QPushButton,
                             QRadioButton, QStyle,
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
        sum_row = QHBoxLayout()
        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet("color:#cfe0f5;")
        self._summary_label.setWordWrap(True)
        self._summary_toggle = QPushButton("▸ 자세히")
        self._summary_toggle.setToolTip("어떤 확장자의 파일이 몇 개씩 있는지 보여 줍니다")
        self._summary_toggle.setCheckable(True)
        self._summary_toggle.setVisible(False)
        self._summary_toggle.toggled.connect(self._toggle_summary)
        sum_row.addWidget(self._summary_label, 1)
        sum_row.addWidget(self._summary_toggle)
        layout.addLayout(sum_row)
        self._summary_detail = QTreeWidget()
        self._summary_detail.setColumnCount(3)
        self._summary_detail.setHeaderLabels(["확장자", "개수", "용량"])
        self._summary_detail.setMaximumHeight(150)
        self._summary_detail.setVisible(False)
        self._summary_detail.setRootIsDecorated(False)
        layout.addWidget(self._summary_detail)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("열기 방식:"))
        self._fast_open = QRadioButton("⚡ 빠른 열기 (권장)")
        self._fast_open.setChecked(True)
        self._fast_open.setToolTip(
            "인터넷이 되는 환경이라면 이쪽이 항상 낫습니다.\n"
            " · 파일 앞부분(64 KB)만 받아 시리즈 목록을 바로 띄웁니다\n"
            " · 시리즈를 열면 나머지를 백그라운드로 미리 받아 스크롤이 끊기지 않습니다\n"
            " · 안 보는 시리즈는 아예 받지 않습니다 (시간·디스크 절약)\n"
            "(동기화 폴더로 열 때는 불가능하고, API로 연결했을 때만 됩니다)")
        self._full_open = QRadioButton("⬇ 전체 다운로드 (오프라인 대비)")
        self._full_open.setToolTip(
            "고른 폴더의 영상을 모두 내려받은 뒤 엽니다.\n"
            "인터넷이 없는 곳에서 볼 예정이거나, 원본을 그대로 보관할 때 고르세요.\n"
            "시간과 디스크 공간이 더 듭니다.")
        group = QButtonGroup(self)
        group.addButton(self._fast_open)
        group.addButton(self._full_open)
        self._mode_group = group
        mode_row.addWidget(self._fast_open)
        mode_row.addWidget(self._full_open)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)
        self._mode_hint = QLabel()
        self._mode_hint.setStyleSheet("color:#8a9;font-size:11px;")
        self._mode_hint.setWordWrap(True)
        layout.addWidget(self._mode_hint)
        self._fast_open.toggled.connect(lambda *_: self._update_mode_hint())
        self._update_mode_hint()
        dest_row = QHBoxLayout()
        self._dest_hint = QLabel()
        self._dest_hint.setWordWrap(True)
        open_dest = QPushButton("폴더 열기")
        open_dest.setToolTip("받은 파일이 저장되는 폴더를 Finder/탐색기로 엽니다")
        open_dest.clicked.connect(self._open_dest_folder)
        change_dest = QPushButton("변경…")
        change_dest.setToolTip("Settings ▸ Cloud에서도 바꿀 수 있습니다")
        change_dest.clicked.connect(self._change_dest_folder)
        self._refresh_dest_hint()
        dest_row.addWidget(self._dest_hint, 1)
        dest_row.addWidget(open_dest)
        dest_row.addWidget(change_dest)
        layout.addLayout(dest_row)
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

    # ─── 저장 폴더 ───
    def _download_dir(self):
        settings = getattr(self.main, "_app_settings", None)
        if settings is not None and hasattr(settings, "cloud_download_dir"):
            return settings.cloud_download_dir()
        from ..app_settings import default_download_dir
        return default_download_dir()

    def _disk_free(self, folder=None):
        """저장 폴더가 있는 디스크의 (남은 용량, 전체 용량). 폴더가 아직 없으면 상위로 올라가며 확인"""
        import shutil
        path = folder or self._download_dir()
        while path and not os.path.isdir(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        try:
            usage = shutil.disk_usage(path or os.path.expanduser("~"))
            return usage.free, usage.total
        except OSError:
            return 0, 0

    def _dest_text(self, prefix="저장 위치"):
        folder = self._download_dir()
        free, total = self._disk_free(folder)
        text = f"{prefix}: {folder}"
        if total:
            text += f"  ·  남은 공간 {human_size(free)} / {human_size(total)}"
        return text

    def _refresh_dest_hint(self, prefix="저장 위치"):
        free, _total = self._disk_free()
        self._dest_hint.setText(self._dest_text(prefix))
        low = free and free < 5 * 1024 ** 3        # 5 GB 미만이면 눈에 띄게
        self._dest_hint.setStyleSheet("color:#ffb84d;" if low else "color:#9ab;")

    def _open_dest_folder(self):
        import subprocess
        import sys as _sys
        folder = self._download_dir()
        os.makedirs(folder, exist_ok=True)
        if _sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        elif _sys.platform == "win32":
            os.startfile(folder)   # noqa: S606 - 사용자가 고른 폴더 열기
        else:
            subprocess.Popen(["xdg-open", folder])

    def _change_dest_folder(self):
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, "받은 파일을 저장할 폴더",
                                                  self._download_dir())
        if not folder:
            return
        settings = getattr(self.main, "_app_settings", None)
        if settings is not None and hasattr(settings, "set_cloud_download_dir"):
            settings.set_cloud_download_dir(folder)
        self._refresh_dest_hint()

    def _update_mode_hint(self):
        """고른 방식에 따라 무슨 일이 일어나는지 한 줄로 안내 + 예상 용량"""
        summary = getattr(self, "_summary", None)
        total = (summary or {}).get("bytes", 0)
        if self._fast_open.isChecked():
            head = min(total, (summary or {}).get("files", 0) * transfer.HEAD_BYTES)
            text = ("    지금은 메타데이터만 받습니다"
                    + (f" (약 {human_size(head)})" if head else "")
                    + " → 목록이 바로 뜨고, 여는 영상만 그때 받습니다."
                    "  인터넷 없이 보려면 연 뒤 File ▸ ☁ 클라우드 영상 전체 받기.")
        else:
            text = ("    고른 폴더의 영상을 모두 받은 뒤 엽니다"
                    + (f" (약 {human_size(total)})" if total else "")
                    + " → 다 받을 때까지 기다려야 하지만, 인터넷 없이도 볼 수 있습니다.")
        self._mode_hint.setText(text)

    def _toggle_summary(self, on):
        self._summary_toggle.setText("▾ 접기" if on else "▸ 자세히")
        self._summary_detail.setVisible(on)

    def _show_listing_summary(self, items):
        """지금 보고 있는 폴더의 내용 요약 (하위 폴더는 들어가야 알 수 있음)

        '열기'로 훑고 나면 하위까지 합친 요약으로 바뀐다.
        """
        files = [i for i in items if not i.is_folder]
        folders = len(items) - len(files)
        by_ext = {}
        for item in files:
            ext = (os.path.splitext(item.name)[1] or "(확장자 없음)").lower()
            entry = by_ext.setdefault(ext, [0, 0])
            entry[0] += 1
            entry[1] += getattr(item, "size", 0) or 0
        self._show_summary({"folders": folders, "files": len(files), "skipped": 0,
                            "bytes": sum(getattr(i, "size", 0) or 0 for i in files),
                            "by_ext": by_ext, "mixed_images": 0, "listing": True})

    def _show_summary(self, summary):
        """폴더 N개 · 파일 N개 · 용량 · (자세히: 확장자별 개수·용량)"""
        self._summary = summary
        parts = [f"폴더 {summary['folders']:,}개",
                 f"파일 {summary['files']:,}개",
                 f"용량 {human_size(summary['bytes'])}"]
        if summary.get("mixed_images"):
            parts.append(f"그림 {summary['mixed_images']:,}장 제외(DICOM과 섞임)")
        elif summary.get("skipped"):
            parts.append(f"지원 안 함 {summary['skipped']:,}개 제외")
        title = "이 폴더:" if summary.get("listing") else "선택한 폴더(하위 포함):"
        if summary.get("listing") and summary["folders"]:
            parts.append("하위 폴더 내용은 '열기'를 누르면 합쳐서 보여 줍니다")
        self._summary_label.setText(f"{title}  " + "  ·  ".join(parts))
        self._summary_detail.clear()
        for ext, (count, size) in sorted(summary.get("by_ext", {}).items(),
                                         key=lambda kv: -kv[1][0]):
            row = QTreeWidgetItem([ext, f"{count:,}개", human_size(size)])
            row.setTextAlignment(1, Qt.AlignRight)
            row.setTextAlignment(2, Qt.AlignRight)
            self._summary_detail.addTopLevelItem(row)
        for i in range(3):
            self._summary_detail.resizeColumnToContents(i)
        self._summary_toggle.setVisible(bool(summary.get("by_ext")))
        self._update_mode_hint()

    def _on_progress(self, value):
        if isinstance(value, tuple) and value and value[0] == "summary":
            self._show_summary(value[1])
            return
        self._on_progress_bar(value)

    def _on_progress_bar(self, value):
        if isinstance(value, tuple) and value and value[0] == "count":
            _tag, done, total, text = value
            self._progress.setRange(0, max(total, 1))
            self._progress.setValue(done)
            self._progress.setFormat(f"{done:,}/{total:,} 파일 (%p%)")
            self._status.setText(text)
        elif isinstance(value, tuple) and value and value[0] == "scan":
            # 폴더 훑는 중: 확인한 폴더 / 지금까지 찾은 폴더 (하위로 들어가며 전체가 늘어남)
            _tag, listed, found, files, elapsed, where = value
            from .transfer import _human_time
            self._progress.setRange(0, max(found, 1))
            self._progress.setValue(listed)
            self._progress.setFormat(f"폴더 {listed:,}/{found:,} (%p%)")
            text = (f"폴더 확인 중  ·  {listed:,}/{found:,} 폴더  ·  파일 {files:,}개 찾음  ·  "
                    f"경과 {_human_time(elapsed)}")
            if where:
                text += f"  ·  {where}"
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
        self._show_listing_summary(items)

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
            if self._fast_open.isChecked() and hasattr(provider, "download_head"):
                size = min(size, len(to_fetch) * transfer.HEAD_BYTES)   # 헤더만 받음
            free, _total = self._disk_free()
            if free and size > free * 0.95:
                QMessageBox.warning(
                    self, provider.name,
                    f"받을 용량이 {human_size(size)}인데 저장 폴더의 남은 공간은 "
                    f"{human_size(free)}뿐입니다.\n\n"
                    "다른 폴더를 고르거나(변경… 버튼), 폴더를 나눠서 여세요.")
                return
            if size > LARGE_DOWNLOAD and QMessageBox.question(
                    self, provider.name,
                    f"파일 {len(files)}개 중 {len(to_fetch)}개({human_size(size)})를 내려받아야 합니다. "
                    "계속할까요?") != QMessageBox.Yes:
                return
            self._skipped = skipped
            dest = self._download_dir()
            self._refresh_dest_hint()
            fast = self._fast_open.isChecked() and hasattr(provider, "download_head")
            if fast:
                self._run(f"빠른 열기 - 메타데이터 받는 중... 0/{len(files)}",
                          lambda progress, cancelled: transfer.fetch_heads(
                              provider, files, tops, progress, cancelled, dest_root=dest),
                          fetched)
            else:
                self._run(f"내려받는 중... 0/{len(files)} files",
                          lambda progress, cancelled: transfer.fetch(provider, files, tops,
                                                                     progress, cancelled,
                                                                     dest_root=dest),
                          fetched)

        def fetched(result):
            paths, stats = result
            self.downloaded = paths
            self.stats = dict(stats, skipped=getattr(self, "_skipped", 0))
            self._dest_hint.setText(
                f"저장한 곳: {stats.get('folder', '')}  ·  "
                f"남은 공간 {human_size(self._disk_free()[0])}")
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

    def finished(result):
        # 창을 닫은 뒤 처리 (모달이 아니므로 탐색·다운로드 중에도 뷰어를 쓸 수 있음)
        main_window._cloud_dialog = None
        dialog.deleteLater()
        if result != QDialog.Accepted or not dialog.downloaded:
            return
        stats = getattr(dialog, "stats", {})
        main_window.statusBar().showMessage(
            f"{provider.name}: 파일 {stats.get('total', 0)}개 (캐시 {stats.get('hits', 0)}개, "
            f"내려받음 {cache.human_size(stats.get('bytes', 0))}"
            + (f", 건너뜀 {stats['skipped']}개" if stats.get("skipped") else "") + ") 불러오는 중...",
            10000)
        # 로컬 Open Folder와 같은 방식으로 (세션 폴더는 최근 목록에 남기지 않음)
        main_window.load_paths(dialog.downloaded, remember=False)

    dialog.finished.connect(finished)
    main_window._cloud_dialog = dialog      # 참조 유지 (없으면 바로 사라짐)
    dialog.setModal(False)                  # 창을 띄워 둔 채 다른 작업 가능
    dialog.setWindowFlags(Qt.Window)        # 보통 창 — 메인 창 위에 늘 붙어 있지 않음
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
