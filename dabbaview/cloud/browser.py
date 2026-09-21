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
from .transfer import _human_time

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
        self._base_title = f"Open from {provider.name}"
        self.setWindowTitle(self._base_title)
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
        self._tree.itemSelectionChanged.connect(self._update_open_buttons)
        self._tree.setMinimumHeight(300)          # 폴더를 한눈에 보도록
        layout.addWidget(self._tree, 1)

        # 긴 설명은 목록 공간을 잡아먹어서 빼고, 필요한 안내는 각 위젯 툴팁으로 옮김
        self._tree.setToolTip(
            "폴더는 더블클릭으로 들어갑니다. 파일·폴더를 골라(여러 개 가능) '선택 항목 열기'를 누르면 "
            "내려받아 불러옵니다.\n"
            "목록에 없는 폴더(구글 드라이브 '다른 컴퓨터'에 백업된 PC 폴더 등)는 위쪽 🔍 칸에서 찾으세요.\n"
            f"한 번 받은 파일은 캐시에 남아 다시 받지 않습니다: {cache.cache_root()}")
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
        self._summary_detail.setHeaderLabels(["확장자 (많은 순)", "개수", "용량"])
        self._summary_detail.setMaximumHeight(120)
        self._summary_detail.setVisible(False)
        self._summary_detail.setRootIsDecorated(False)
        self._summary_detail.itemChanged.connect(self._on_ext_toggled)
        layout.addWidget(self._summary_detail)
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("받을 파일:"))
        self._type_all = QRadioButton("전부 받기")
        self._type_all.setChecked(True)
        self._type_pick = QRadioButton("유형 골라서 받기")
        self._type_pick.setToolTip("같은 검사가 DICOM·JPG 등 여러 형식으로 중복돼 있을 때, 필요한 유형만 받습니다")
        type_group = QButtonGroup(self)
        type_group.addButton(self._type_all)
        type_group.addButton(self._type_pick)
        self._type_group = type_group
        self._type_pick.toggled.connect(self._on_type_mode)
        type_row.addWidget(self._type_all)
        type_row.addWidget(self._type_pick)
        type_row.addStretch(1)
        layout.addLayout(type_row)
        self._choice_label = QLabel("")
        self._choice_label.setStyleSheet("color:#cfe0f5;")
        layout.addWidget(self._choice_label)

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
        self._fast_open.toggled.connect(lambda *_: self._update_mode_hint())
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
        status_row = QHBoxLayout()
        self._spinner = QLabel("")          # 회전 표시: 폭을 고정해 글자가 밀리지 않게
        self._spinner.setFixedWidth(18)
        self._spinner.setAlignment(Qt.AlignCenter)
        self._status = QLabel()
        status_row.addWidget(self._spinner)
        status_row.addWidget(self._status, 1)
        layout.addLayout(status_row)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self._open_here = QPushButton("📂 이 폴더 전체 열기")
        self._open_here.setToolTip("지금 들어와 있는 폴더를 하위 폴더까지 통째로 엽니다 (고를 필요 없음)")
        self._open_here.clicked.connect(self._open_current_folder)
        buttons.addWidget(self._open_here)
        self._open = QPushButton("선택 항목 열기")
        self._open.setToolTip("위 목록에서 고른 폴더·파일만 엽니다 (여러 개 고를 수 있음)")
        self._open.setDefault(True)
        self._open.clicked.connect(self._download_selected)
        self._cancel = QPushButton("닫기")
        self._cancel.clicked.connect(self._close)
        buttons.addWidget(self._open)
        buttons.addWidget(self._cancel)
        layout.addLayout(buttons)
        self._fix_width()
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
        """고른 폴더를 훑고 나면 각 방식의 예상 용량을 버튼 이름에 붙여 준다"""
        if getattr(self, "_by_ext", None):
            self._update_choice()      # 고른 유형 기준으로 계산 (아래 계산보다 정확)
            return
        summary = getattr(self, "_summary", None)
        total = (summary or {}).get("bytes", 0)
        files = (summary or {}).get("files", 0)
        if total and files and not (summary or {}).get("listing"):
            head = min(total, files * transfer.HEAD_BYTES)
            self._fast_open.setText(f"⚡ 빠른 열기 (권장 · 약 {human_size(head)})")
            self._full_open.setText(f"⬇ 전체 다운로드 (오프라인 대비 · 약 {human_size(total)})")
        else:
            self._fast_open.setText("⚡ 빠른 열기 (권장)")
            self._full_open.setText("⬇ 전체 다운로드 (오프라인 대비)")

    def _fix_width(self):
        """글자가 길어져도 창 크기가 변하지 않게 (라벨이 창을 밀지 못하도록)"""
        from PyQt5.QtWidgets import QSizePolicy
        for label in (self._status, self._summary_label, self._dest_hint):
            label.setWordWrap(False)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setMinimumWidth(880)
        self.resize(980, 720)

    ROW_PX = 24          # 표 한 줄 높이(대략)

    def _set_detail_space(self, expanded):
        """확장자 표를 펼치면 5~6줄이 보이도록 폴더 목록 높이를 양보한다"""
        if expanded:
            self._summary_detail.setMinimumHeight(self.ROW_PX * 6 + 28)   # 6줄 + 머리글
            self._summary_detail.setMaximumHeight(self.ROW_PX * 9 + 28)
            self._tree.setMinimumHeight(self.ROW_PX * 5)                  # 폴더도 5줄은 유지
        else:
            self._summary_detail.setMinimumHeight(0)
            self._summary_detail.setMaximumHeight(120)
            self._tree.setMinimumHeight(300)

    def _toggle_summary(self, on):
        self._summary_toggle.setText("▾ 접기" if on else "▸ 자세히")
        self._summary_detail.setVisible(on)
        self._set_detail_space(on)
        if on:
            self._scan_current()          # 하위 폴더 내용까지 훑어서 채움
        else:
            self._cancel_scan()

    # ─── 하위 폴더 내용 미리 보기 ───
    def _cancel_scan(self):
        worker = getattr(self, "_scan_worker", None)
        if worker is not None:
            worker.cancel = True
            self._scan_worker = None

    def _scan_current(self):
        """지금 보고 있는 폴더(또는 고른 폴더들)의 하위까지 훑어 확장자·용량을 보여 준다"""
        targets = [i for i in self._selected_items() if i.is_folder] or \
                  ([self._stack[-1]] if self._stack else
                   [i for i in self._items if i.is_folder])
        if not targets:
            return
        self._cancel_scan()
        provider = self.provider
        worker = _Worker(lambda progress, cancelled:
                         transfer.plan(provider, targets, progress, cancelled))
        self._scan_worker = worker
        worker.progress.connect(self._on_scan_progress)
        worker.done.connect(lambda _r: setattr(self, "_scan_worker", None))
        worker.failed.connect(lambda _m: setattr(self, "_scan_worker", None))
        self._status.setText("하위 폴더 내용을 확인하는 중…")
        worker.start()

    def _on_scan_progress(self, value):
        if not (isinstance(value, tuple) and value):
            return
        if value[0] == "summary":
            self._stop_ticker()
            self._prog = None
            summary = dict(value[1], listing=False)
            self._show_summary(summary)
            self._status.setText(
                f"하위 포함: 폴더 {summary['folders']:,}개 · 파일 {summary['files']:,}개 · "
                f"{human_size(summary['bytes'])}"
                + (f"  ·  그림 {summary['mixed_images']:,}장 제외" if summary.get("mixed_images") else ""))
            return
        if value[0] != "scan" or len(value) < 4:
            return
        listed, found, files = value[1], value[2], value[3]
        elapsed = value[4] if len(value) > 4 else 0
        by_ext = value[6] if len(value) > 6 else {}
        all_bytes = value[7] if len(value) > 7 else 0
        import time as _t
        self._prog = {"kind": "scan", "done": listed, "total": found, "files": files,
                      "bytes": all_bytes, "start": _t.monotonic() - elapsed}
        self._fill_ext_table(by_ext)          # 표 맨 밑에 진행률 줄이 함께 그려짐
        self._start_ticker()
        self._tick_progress()

    DICOM_EXTS = (".dcm", ".dicom", ".ima", "(확장자 없음)")

    def _fill_ext_table(self, by_ext):
        """확장자별 개수·용량 표 (빈도순) — 체크로 받을 유형을 고른다"""
        self._by_ext = dict(by_ext)
        self._summary_detail.blockSignals(True)
        self._summary_detail.clear()
        picking = self._type_pick.isChecked()
        # 새 확장자가 나중에 발견돼도 기본은 '받음' — 사용자가 끈 것만 기억한다
        excluded = getattr(self, "_ext_excluded", None)
        if excluded is None:
            excluded = self._ext_excluded = set()
        chosen = {e for e in by_ext if e not in excluded}
        self._ext_chosen = chosen if picking else None
        for ext, (count, size) in sorted(by_ext.items(), key=lambda kv: (-kv[1][0], kv[0])):
            row = QTreeWidgetItem([ext, f"{count:,}개", human_size(size)])
            if picking:
                row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
                row.setCheckState(0, Qt.Checked if ext in chosen else Qt.Unchecked)
            else:       # 전부 받기: 체크칸 없이 목록만
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
            row.setData(0, Qt.UserRole, ext)
            row.setTextAlignment(1, Qt.AlignRight)
            row.setTextAlignment(2, Qt.AlignRight)
            self._summary_detail.addTopLevelItem(row)
        for i in range(3):
            self._summary_detail.resizeColumnToContents(i)
        self._add_scan_row()
        self._summary_detail.blockSignals(False)
        self._update_choice()

    def _add_scan_row(self):
        """훑는 중이면 표 맨 밑에 진행률을 한 줄 넣는다 (얼마나 남았는지 바로 보이게)"""
        state = getattr(self, "_prog", None)
        scanning = getattr(self, "_scan_worker", None) is not None
        if not scanning or not state or state.get("kind") != "scan":
            return
        done, total = state.get("done", 0), state.get("total", 0)
        percent = (done * 100.0 / total) if total else 0.0
        from .transfer import _human_time
        elapsed = state.get("start")
        import time as _t
        secs = (_t.monotonic() - elapsed) if elapsed else 0
        left = ""
        if done and done < total and secs > 1:
            left = f" · 남은 시간 약 {_human_time((total - done) * secs / done)}"
        row = QTreeWidgetItem([f"⏳ 확인 중… {percent:.0f}%",
                               f"폴더 {done:,}/{total:,}",
                               f"파일 {state.get('files', 0):,}개{left}"])
        row.setFlags(Qt.ItemIsEnabled)
        from PyQt5.QtGui import QBrush, QColor
        for col in range(3):
            row.setForeground(col, QBrush(QColor("#8fb3e0")))
        self._summary_detail.addTopLevelItem(row)
        self._summary_detail.scrollToItem(row)

    def _on_ext_toggled(self, *_args):
        chosen, excluded = set(), set(getattr(self, "_ext_excluded", set()))
        for i in range(self._summary_detail.topLevelItemCount()):
            row = self._summary_detail.topLevelItem(i)
            ext = row.data(0, Qt.UserRole)
            if ext is None:      # 진행률 줄
                continue
            if row.checkState(0) == Qt.Checked:
                chosen.add(ext)
                excluded.discard(ext)
            else:
                excluded.add(ext)
        self._ext_chosen = chosen
        self._ext_excluded = excluded
        self._update_choice()

    def _chosen_counts(self):
        """고른 유형의 (개수, 용량)"""
        by_ext = getattr(self, "_by_ext", {}) or {}
        if self._type_pick.isChecked():
            excluded = getattr(self, "_ext_excluded", set()) or set()
            chosen = {e for e in by_ext if e not in excluded}
        else:
            chosen = set(by_ext)
        count = sum(v[0] for k, v in by_ext.items() if k in chosen)
        size = sum(v[1] for k, v in by_ext.items() if k in chosen)
        return count, size

    def _update_choice(self):
        """고른 유형의 개수·용량·예상 시간을 요약 줄과 버튼에 반영"""
        by_ext = getattr(self, "_by_ext", {}) or {}
        if not by_ext:
            return
        count, size = self._chosen_counts()
        total_count = sum(v[0] for v in by_ext.values())
        speed = self._known_speed()
        full_eta = f"  ·  예상 {_human_time(size / speed)}" if size and speed else ""
        head = min(size, count * transfer.HEAD_BYTES)
        head_eta = f"  ·  예상 {_human_time(head / speed)}" if head and speed else ""
        if self._type_pick.isChecked():
            excluded = getattr(self, "_ext_excluded", set()) or set()
            names = ", ".join(sorted(e for e in by_ext if e not in excluded))
            self._choice_label.setText(
                f"고른 유형: {count:,}개 / 전체 {total_count:,}개  ·  {human_size(size)}"
                + (f"  ·  {names}" if names else "  ·  (아무것도 고르지 않음)"))
        else:
            self._choice_label.setText(f"전부 받기: {total_count:,}개  ·  {human_size(size)}")
        self._fast_open.setText(f"⚡ 빠른 열기 (권장 · 약 {human_size(head)}{head_eta})")
        self._full_open.setText(f"⬇ 전체 다운로드 (오프라인 대비 · {human_size(size)}{full_eta})")

    def _known_speed(self):
        """최근에 관찰한 다운로드 속도 (없으면 8 MB/s로 어림)"""
        return getattr(self, "_speed_hint", 0) or 8 * 1024 * 1024

    def _on_type_mode(self, picking):
        """전부 받기 ↔ 유형 골라서 받기"""
        self._ext_excluded = set()                     # 처음엔 모두 받는 상태에서 시작
        self._ext_chosen = None
        if picking:
            self._summary_toggle.setChecked(True)      # 표를 펼치고 하위 폴더까지 훑기
            self._summary_detail.setVisible(True)
            self._set_detail_space(True)
        self._fill_ext_table(getattr(self, "_by_ext", {}) or {})

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
        self._fill_ext_table(summary.get("by_ext", {}))
        self._summary_toggle.setVisible(bool(summary.get("by_ext")) or bool(summary.get("folders")))
        self._update_mode_hint()

    def _on_progress(self, value):
        if isinstance(value, tuple) and value and value[0] == "summary":
            self._show_summary(value[1])
            return
        self._on_progress_bar(value)

    def _start_ticker(self):
        """파일 하나가 끝날 때까지 화면이 멈춰 보이지 않게 0.5초마다 갱신"""
        if getattr(self, "_ticker", None) is None:
            from PyQt5.QtCore import QTimer
            self._ticker = QTimer(self)
            self._ticker.setInterval(500)
            self._ticker.timeout.connect(self._tick_progress)
        if not self._ticker.isActive():
            self._ticker.start()

    def _stop_ticker(self):
        if getattr(self, "_ticker", None) is not None:
            self._ticker.stop()
        if hasattr(self, "_spinner"):
            self._spinner.setText("")
        self.setWindowTitle(self._base_title)

    SPINNER = "◐◓◑◒"

    def _tick_progress(self):
        """마지막으로 받은 수치에 '지금까지 흐른 시간'을 더해 다시 그린다"""
        state = getattr(self, "_prog", None)
        if not state:
            return
        self._spin = (getattr(self, "_spin", 0) + 1) % len(self.SPINNER)
        mark = self.SPINNER[self._spin]
        import time as _t
        elapsed = _t.monotonic() - state["start"]
        done, total = state["done"], state["total"]
        if state["kind"] == "count":
            percent = (done * 100.0 / total) if total else 0.0
            self._progress.setFormat(f"{done:,} / {total:,} 파일 ({percent:.1f}%)")
            parts = [state["title"], f"{done:,} / {total:,} 파일 ({percent:.1f}%)"]
            if state.get("bytes"):
                parts.append(f"{human_size(state['bytes'])} 받음")
                speed = state["bytes"] / max(0.001, elapsed)
                parts.append(f"{human_size(speed)}/s")
            speed_files = done / max(0.001, elapsed)
            if done and done < total and speed_files > 0:
                parts.append(f"남은 시간 약 {_human_time((total - done) / speed_files)}")
            parts.append(f"경과 {_human_time(elapsed)}")
            parts.extend(state.get("extra") or [])
            self._spinner.setText(mark)
            self._status.setText("  ·  ".join(parts))
            self.setWindowTitle(f"{self._base_title} — {percent:.1f}%  ({done:,}/{total:,})")
        else:   # 폴더 훑는 중
            self._spinner.setText(mark)
            self.setWindowTitle(f"{self._base_title} — 폴더 확인 {done:,}/{total:,}")
            self._status.setText(
                f"하위 폴더 확인 중…  폴더 {done:,}/{total:,}  ·  "
                f"파일 {state['files']:,}개  ·  {human_size(state.get('bytes', 0))}  ·  "
                f"경과 {_human_time(elapsed)}")

    def _on_progress_bar(self, value):
        if isinstance(value, tuple) and value and value[0] == "count":
            _tag, done, total, text = value
            self._progress.setRange(0, max(total, 1))
            self._progress.setValue(done)
            parts = str(text).split("  ·  ")
            head = parts[0] if parts else "내려받는 중"
            # 보내는 쪽이 준 세부 정보(받은 용량·캐시 수) 중 겹치지 않는 것만 이어 붙임
            extra = [p.strip() for p in parts[1:]
                     if any(k in p for k in ("받음", "캐시", "/s"))]
            state = getattr(self, "_prog", None) or {}
            if state.get("kind") != "count":
                state = {}                      # 훑기 → 다운로드로 넘어가면 처음부터
            self._prog = {"kind": "count", "done": done, "total": total,
                          "title": head or "내려받는 중", "extra": extra,
                          "bytes": 0, "hits": 0,
                          "start": state.get("start") or __import__("time").monotonic()}
            self._start_ticker()
            self._tick_progress()
        elif isinstance(value, tuple) and value and value[0] == "scan":
            # 폴더 훑는 중: 확인한 폴더 / 지금까지 찾은 폴더 (하위로 들어가며 전체가 늘어남)
            # 보내는 쪽 항목 수가 달라져도 깨지지 않게 위치로 읽는다
            listed, found, files = value[1], value[2], value[3]
            elapsed = value[4] if len(value) > 4 else 0
            by_ext = value[6] if len(value) > 6 else None
            all_bytes = value[7] if len(value) > 7 else 0
            self._progress.setRange(0, max(found, 1))
            self._progress.setValue(listed)
            self._progress.setFormat(f"폴더 {listed:,}/{found:,} (%p%)")
            import time as _t
            self._prog = {"kind": "scan", "done": listed, "total": found, "files": files,
                          "bytes": all_bytes, "start": _t.monotonic() - elapsed}
            if by_ext:
                self._fill_ext_table(by_ext)
            self._start_ticker()
            self._tick_progress()
        else:
            self._status.setText(str(value))

    def _finish(self):
        self._stop_ticker()
        self._prog = None
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
        self._update_open_buttons()
        self._apply_filter(self._filter.text())
        self._cancel_scan()
        self._show_listing_summary(items)
        folders = sum(1 for i in items if i.is_folder)
        self._status.setText(
            "▸ 자세히를 누르면 하위 폴더 내용(확장자·용량)까지 확인합니다" if folders
            else "")
        if self._summary_toggle.isChecked():
            self._scan_current()

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

    def _update_open_buttons(self):
        picked = [i for i in self._selected_items() if i.is_folder or i.downloadable]
        self._open.setEnabled(bool(picked))
        if picked:
            names = ", ".join(i.name for i in picked[:3])
            if len(picked) > 3:
                names += f" 외 {len(picked) - 3}개"
            self._open.setText(f"선택 항목 열기 ({len(picked)}개)")
            self._open.setToolTip(f"고른 항목만 엽니다: {names}")
        else:
            self._open.setText("선택 항목 열기")
            self._open.setToolTip("위 목록에서 폴더·파일을 고르면 그것만 엽니다")
        # '이 폴더 전체 열기'가 어디를 말하는지 이름으로 분명히
        here = self._stack[-1].name if self._stack else ""
        folders = [i.name for i in self._items if i.is_folder]
        if here:
            self._open_here.setText(f"📂 '{here}' 폴더 전체 열기")
            inside = ", ".join(folders[:3]) + (f" 외 {len(folders) - 3}개" if len(folders) > 3 else "")
            self._open_here.setToolTip(
                f"지금 들어와 있는 '{here}' 폴더를 하위까지 통째로 엽니다"
                + (f"\n포함되는 하위 폴더: {inside}" if folders else ""))
        else:
            self._open_here.setText("📂 이 폴더 전체 열기")

    def _selected_items(self):
        return [r.data(0, Qt.UserRole) for r in self._tree.selectedItems()]

    def _download_selected(self):
        selected = self._selected_items()
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
            chosen = getattr(self, "_ext_chosen", None) if self._type_pick.isChecked() else None
            if chosen is not None:      # 고른 확장자만 내려받음 (중복 형식 제외)
                def keep(rel):
                    ext = (os.path.splitext(rel)[1] or "(확장자 없음)").lower()
                    return ext in chosen
                before = len(files)
                files = [(rel, item) for rel, item in files if keep(rel)]
                skipped += before - len(files)
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
            state = getattr(self, "_prog", None) or {}
            import time as _t
            elapsed = _t.monotonic() - (state.get("start") or _t.monotonic())
            if stats.get("bytes") and elapsed > 2:
                self._speed_hint = stats["bytes"] / elapsed
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
    # 메인 창에 딸린 창이 아니라 '독립 창'으로 → 창 전환(⌘`)·미션 컨트롤에 따로 나오고,
    # 메인 창 뒤로 숨지 않는다
    dialog.setParent(None)
    dialog.setWindowFlags(Qt.Window)
    # 부모가 없으면 메인 창의 어두운 테마를 못 물려받으므로 그대로 복사
    if hasattr(main_window, "styleSheet"):
        dialog.setStyleSheet(main_window.styleSheet())
        dialog.setPalette(main_window.palette())
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    if hasattr(main_window, "register_popup"):
        main_window.register_popup(dialog, "☁")   # 메인 창 옆 탭으로 표시
