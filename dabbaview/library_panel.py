# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""왼쪽 패널 'Library' 탭 (Zotero 스타일)

  검색 (환자명·ID·설명·메모·#태그) · 태그 필터 칩
  컬렉션 트리: 전체 / 컬렉션 없음 / 사용자 컬렉션 (하위 컬렉션, 끌어다 놓아 추가)
  스터디 목록: 더블클릭·Enter·📂 = 열기, 여러 개 선택, 컬렉션으로 끌기
  메모: 굵게·기울임·목록, 태그, 빠른 태그, 자동 저장, Markdown/TXT 내보내기
"""
import json
import os

from PyQt5.QtCore import QMimeData, QPoint, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QKeySequence, QTextListFormat
from PyQt5.QtWidgets import (QAbstractItemView, QColorDialog, QFileDialog, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QShortcut,
                             QSplitter, QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from .library import _date, normalize_tag

STUDY_MIME = "application/x-dabbaview-library-studies"
ALL, UNFILED = "__all__", "unfiled"
ROLE_ID = Qt.UserRole


def _chip_style(color, checked):
    return (f"QToolButton {{ border: 1px solid {color}; border-radius: 9px; padding: 1px 7px;"
            f" color: {'#111' if checked else color}; background: {color if checked else 'transparent'};"
            " font-size: 11px; }")


class CollectionTree(QTreeWidget):
    studies_dropped = pyqtSignal(list, str)   # 스터디 UID들, 컬렉션 id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setIndentation(14)

    def _target(self, pos):
        item = self.itemAt(pos)
        cid = item.data(0, ROLE_ID) if item is not None else None
        return cid if cid not in (None, ALL, UNFILED) else None

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(STUDY_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(STUDY_MIME) and self._target(event.pos()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        cid = self._target(event.pos())
        if cid and event.mimeData().hasFormat(STUDY_MIME):
            uids = json.loads(bytes(event.mimeData().data(STUDY_MIME)).decode("utf-8"))
            self.studies_dropped.emit(uids, cid)
            event.acceptProposedAction()
        else:
            event.ignore()


class StudyList(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels(["환자", "검사일", "설명", "태그"])
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setUniformRowHeights(True)

    def mimeTypes(self):
        return [STUDY_MIME]

    def mimeData(self, items):
        mime = QMimeData()
        uids = [i.data(0, ROLE_ID) for i in items]
        mime.setData(STUDY_MIME, json.dumps(uids).encode("utf-8"))
        return mime


class LibraryPanel(QWidget):
    open_requested = pyqtSignal(str)   # StudyInstanceUID
    export_requested = pyqtSignal(list, object)   # 선택 UID들, 현재 컬렉션 id
    rename_requested = pyqtSignal(str, str)   # ("study"|"patient", StudyInstanceUID)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._current = None           # 메모를 보여 주는 스터디
        self._filter_tags = set()
        self._collection = ALL
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 환자명·ID·설명·메모·#태그")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_studies)
        top.addWidget(self.search, 1)
        add = QToolButton()
        add.setText("＋")
        add.setToolTip("새 컬렉션")
        add.clicked.connect(lambda: self.new_collection(None))
        top.addWidget(add)
        more = QToolButton()
        more.setText("⋯")
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        menu.addAction("📤 Export… (PDF·Word·Excel·이미지·CSV)", self.request_export)
        menu.addSeparator()
        menu.addAction("선택 스터디 메모 내보내기 (Markdown)…", lambda: self.export_notes("md", True))
        menu.addAction("선택 스터디 메모 내보내기 (TXT)…", lambda: self.export_notes("txt", True))
        menu.addAction("전체 메모 내보내기 (Markdown)…", lambda: self.export_notes("md", False))
        menu.addSeparator()
        menu.addAction("라이브러리 내보내기 (JSON)…", self.export_library)
        menu.addAction("라이브러리 가져오기 (JSON, 병합)…", self.import_library)
        menu.addSeparator()
        menu.addAction("빠른 태그 편집…", self.edit_quick_tags)
        menu.addAction("라이브러리 파일 위치 열기", self._reveal_file)
        more.setMenu(menu)
        top.addWidget(more)
        layout.addLayout(top)

        self.tag_bar = QHBoxLayout()
        self.tag_bar.setSpacing(3)
        tag_row = QWidget()
        tag_row.setLayout(self.tag_bar)
        layout.addWidget(tag_row)

        split = QSplitter(Qt.Vertical)
        self.collections = CollectionTree()
        self.collections.itemSelectionChanged.connect(self._on_collection)
        self.collections.studies_dropped.connect(self._on_drop)
        self.collections.setContextMenuPolicy(Qt.CustomContextMenu)
        self.collections.customContextMenuRequested.connect(self._collection_menu)
        split.addWidget(self.collections)

        self.studies = StudyList()
        self.studies.itemSelectionChanged.connect(self._on_study_selection)
        self.studies.itemDoubleClicked.connect(lambda item, _c: self.open_requested.emit(item.data(0, ROLE_ID)))
        self.studies.setContextMenuPolicy(Qt.CustomContextMenu)
        self.studies.customContextMenuRequested.connect(self._study_menu)
        split.addWidget(self.studies)

        split.addWidget(self._build_note_pane())
        split.setSizes([150, 260, 260])
        layout.addWidget(split, 1)

        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(700)
        self._note_timer.timeout.connect(self._save_note)
        store.changed.connect(self._schedule_refresh)
        self.refresh()

    # ─── 메모 영역 ───
    def _build_note_pane(self):
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        self.note_title = QLabel("스터디를 선택하세요")
        self.note_title.setWordWrap(True)
        self.note_title.setStyleSheet("color: #ddd; font-weight: bold;")
        v.addWidget(self.note_title)
        self.note_path = QLabel("")
        self.note_path.setWordWrap(True)
        self.note_path.setStyleSheet("color: #888; font-size: 10px;")
        v.addWidget(self.note_path)
        row = QHBoxLayout()
        self.open_button = QPushButton("📂 열기")
        self.open_button.clicked.connect(lambda: self._current and self.open_requested.emit(self._current))
        finder = QPushButton("Finder")
        finder.clicked.connect(self._reveal_study)
        row.addWidget(self.open_button)
        row.addWidget(finder)
        row.addStretch()
        v.addLayout(row)
        self.quick_row = QHBoxLayout()
        self.quick_row.setSpacing(3)
        v.addLayout(self.quick_row)
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("태그: interesting, teaching (Enter로 저장)")
        self.tag_edit.editingFinished.connect(self._save_tags)
        v.addWidget(self.tag_edit)
        fmt = QHBoxLayout()
        fmt.setSpacing(2)
        for text, tip, slot in (("B", "굵게 (⌘B)", self._bold), ("I", "기울임 (⌘I)", self._italic),
                                ("• 목록", "글머리 기호 목록", lambda: self._list(QTextListFormat.ListDisc)),
                                ("1. 목록", "번호 목록", lambda: self._list(QTextListFormat.ListDecimal))):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            if text in ("B", "I"):
                f = b.font()
                f.setBold(text == "B")
                f.setItalic(text == "I")
                b.setFont(f)
            fmt.addWidget(b)
        fmt.addStretch()
        self.note_status = QLabel("")
        self.note_status.setStyleSheet("color: #7a7; font-size: 10px;")
        fmt.addWidget(self.note_status)
        v.addLayout(fmt)
        self.note = QTextEdit()
        self.note.setPlaceholderText("메모 (굵게·기울임·목록 가능, #태그는 자동으로 태그가 됩니다)")
        self.note.setAcceptRichText(True)
        self.note.textChanged.connect(self._on_note_edited)
        # ⌘B / ⌘I는 메모 입력 중에만 (창 전체 단축키 ⌘I = Image 정보 패널과 겹치지 않게)
        for key, slot in (("Ctrl+B", self._bold), ("Ctrl+I", self._italic)):
            QShortcut(QKeySequence(key), self.note, slot, context=Qt.WidgetShortcut)
        v.addWidget(self.note, 1)
        self._set_note_enabled(False)
        return pane

    def _set_note_enabled(self, enabled):
        for w in (self.note, self.tag_edit, self.open_button):
            w.setEnabled(enabled)

    def _bold(self):
        fmt = self.note.currentCharFormat()
        fmt.setFontWeight(QFont.Normal if fmt.fontWeight() > QFont.Normal else QFont.Bold)
        self.note.mergeCurrentCharFormat(fmt)

    def _italic(self):
        fmt = self.note.currentCharFormat()
        fmt.setFontItalic(not fmt.fontItalic())
        self.note.mergeCurrentCharFormat(fmt)

    def _list(self, style):
        cursor = self.note.textCursor()
        if cursor.currentList() is not None and cursor.currentList().format().style() == style:
            block = cursor.blockFormat()
            cursor.currentList().remove(cursor.block())
            block.setIndent(0)
            cursor.setBlockFormat(block)
        else:
            cursor.createList(style)
        self.note.setFocus()

    def _on_note_edited(self):
        if self._current is not None and not getattr(self, "_loading_note", False):
            self.note_status.setText("편집 중…")
            self._note_timer.start()

    def _save_note(self):
        if self._current is None:
            return
        self._saving = True
        self.store.set_note(self._current, self.note.toHtml(), self.note.toPlainText())
        self._saving = False
        self.note_status.setText("✓ 저장됨")

    def flush(self):
        """열려 있는 메모를 바로 저장 (창을 닫거나 스터디를 바꿀 때)"""
        if self._note_timer.isActive():
            self._note_timer.stop()
            self._save_note()

    def _save_tags(self):
        if self._current is None:
            return
        tags = [t for t in self.tag_edit.text().replace("#", " ").replace(",", " ").split() if t]
        self.store.set_tags(self._current, tags)

    # ─── 목록 ───
    def _schedule_refresh(self):
        if not getattr(self, "_saving", False):
            QTimer.singleShot(0, self.refresh)
        else:
            QTimer.singleShot(0, self._refresh_studies)

    def refresh(self):
        self._refresh_collections()
        self._refresh_tag_bar()
        self._refresh_quick_tags()
        self._refresh_studies()

    def _refresh_collections(self):
        tree = self.collections
        tree.blockSignals(True)
        tree.clear()
        root_all = QTreeWidgetItem([f"📚 전체 스터디 ({len(self.store.studies)})"])
        root_all.setData(0, ROLE_ID, ALL)
        unfiled = sum(1 for s in self.store.studies.values() if not s.get("collections"))
        root_unfiled = QTreeWidgetItem([f"📥 컬렉션 없음 ({unfiled})"])
        root_unfiled.setData(0, ROLE_ID, UNFILED)
        tree.addTopLevelItem(root_all)
        tree.addTopLevelItem(root_unfiled)
        selected = None

        def add(parent_item, parent_id):
            nonlocal selected
            for c in self.store.children(parent_id):
                item = QTreeWidgetItem([f"📁 {c['name']} ({self.store.count_in(c['id'])})"])
                item.setData(0, ROLE_ID, c["id"])
                item.setToolTip(0, "스터디를 여기로 끌어다 놓으면 이 컬렉션에 추가 (여러 컬렉션 가능)")
                (parent_item.addChild if parent_item else tree.addTopLevelItem)(item)
                if c["id"] == self._collection:
                    selected = item
                add(item, c["id"])
        add(None, None)
        tree.expandAll()
        if self._collection == UNFILED:
            selected = root_unfiled
        tree.setCurrentItem(selected or root_all)
        if selected is None and self._collection not in (ALL, UNFILED):
            self._collection = ALL
        tree.blockSignals(False)

    def _refresh_tag_bar(self):
        while self.tag_bar.count():
            w = self.tag_bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        tags = self.store.all_tags()
        self._filter_tags &= set(tags)
        for tag in tags[:14]:
            color = self.store.tag_color(tag)
            b = QToolButton()
            b.setText(f"#{tag}")
            b.setCheckable(True)
            b.setChecked(tag in self._filter_tags)
            b.setStyleSheet(_chip_style(color, tag in self._filter_tags))
            b.setToolTip("태그로 거르기 (여러 개 = 모두 포함) · 우클릭: 색 바꾸기")
            b.toggled.connect(lambda on, t=tag: self._toggle_filter(t, on))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _p, t=tag: self._pick_tag_color(t))
            self.tag_bar.addWidget(b)
        if not tags:
            hint = QLabel("태그 없음 — 메모 아래 빠른 태그나 #태그로 추가")
            hint.setStyleSheet("color: #777; font-size: 10px;")
            self.tag_bar.addWidget(hint)
        self.tag_bar.addStretch()

    def _refresh_quick_tags(self):
        while self.quick_row.count():
            w = self.quick_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        label = QLabel("빠른 태그:")
        label.setStyleSheet("color: #888; font-size: 10px;")
        self.quick_row.addWidget(label)
        entry = self.store.get(self._current) if self._current else None
        on = set(entry.get("tags", [])) if entry else set()
        for tag in self.store.quick_tags:
            b = QToolButton()
            b.setText(f"#{tag}")
            b.setCheckable(True)
            b.setChecked(tag in on)
            b.setEnabled(entry is not None)
            b.setStyleSheet(_chip_style(self.store.tag_color(tag), tag in on))
            b.clicked.connect(lambda _c=False, t=tag: self.store.toggle_tag(self.selected_uids(), t))
            self.quick_row.addWidget(b)
        self.quick_row.addStretch()

    def _toggle_filter(self, tag, on):
        (self._filter_tags.add if on else self._filter_tags.discard)(tag)
        self._refresh_tag_bar()
        self._refresh_studies()

    def _pick_tag_color(self, tag):
        color = QColorDialog.getColor(QColor(self.store.tag_color(tag)), self, f"#{tag} 색")
        if color.isValid():
            self.store.set_tag_color(tag, color.name())

    def _refresh_studies(self):
        keep = set(self.selected_uids()) or ({self._current} if self._current else set())
        collection = None if self._collection == ALL else self._collection
        uids = self.store.search(self.search.text(), collection, self._filter_tags)
        lst = self.studies
        lst.blockSignals(True)
        lst.clear()
        for uid in uids:
            s = self.store.studies[uid]
            folder = s.get("folder", "")
            missing = bool(folder) and not os.path.isdir(folder)
            item = QTreeWidgetItem([
                ("⚠ " if missing else "") + f"{s.get('patient_name', '') or '-'}",
                _date(s.get("study_date")), s.get("description", "") or ", ".join(s.get("modalities", [])),
                " ".join(f"#{t}" for t in s.get("tags", []))])
            item.setData(0, ROLE_ID, uid)
            tip = (f"{s.get('patient_name', '')}  ID {s.get('patient_id', '')}\n"
                   f"{_date(s.get('study_date'))}  {s.get('description', '')}\n"
                   f"{', '.join(s.get('modalities', []))} · 시리즈 {s.get('series', '?')} · 영상 {s.get('images', '?')}\n"
                   f"폴더: {folder}" + ("\n⚠ 폴더를 찾을 수 없습니다" if missing else "")
                   + "\n더블클릭 / Enter: 열기")
            for c in range(4):
                item.setToolTip(c, tip)
            if missing:
                for c in range(4):
                    item.setForeground(c, QColor("#888"))
            if s.get("tags"):
                item.setForeground(3, QColor(self.store.tag_color(s["tags"][0])))
            lst.addTopLevelItem(item)
            if uid in keep:
                item.setSelected(True)
        for c in range(3):
            lst.resizeColumnToContents(c)
        lst.blockSignals(False)
        self._show_note(self._current if self._current in uids else
                        (self.selected_uids()[0] if self.selected_uids() else None))

    def selected_uids(self):
        return [i.data(0, ROLE_ID) for i in self.studies.selectedItems()]

    def select_study(self, uid):
        """외부에서 선택 (라이브러리에 방금 추가한 스터디 등)"""
        self._collection = ALL
        self.search.clear()
        self._filter_tags.clear()
        self.refresh()
        for i in range(self.studies.topLevelItemCount()):
            item = self.studies.topLevelItem(i)
            if item.data(0, ROLE_ID) == uid:
                self.studies.setCurrentItem(item)
                self.studies.scrollToItem(item)
                break

    def _on_collection(self):
        item = self.collections.currentItem()
        self._collection = item.data(0, ROLE_ID) if item is not None else ALL
        self._refresh_studies()

    def _on_study_selection(self):
        uids = self.selected_uids()
        self._show_note(uids[0] if len(uids) == 1 else (uids[0] if uids else None))

    def _show_note(self, uid):
        if uid == self._current and uid is not None:
            self._refresh_quick_tags()
            entry = self.store.get(uid)
            if entry is not None and not self.tag_edit.hasFocus():
                self.tag_edit.setText(", ".join(entry.get("tags", [])))
            return
        self.flush()
        self._current = uid
        entry = self.store.get(uid) if uid else None
        self._loading_note = True
        if entry is None:
            self.note_title.setText("스터디를 선택하세요")
            self.note_path.setText("")
            self.note.clear()
            self.tag_edit.clear()
            self._set_note_enabled(False)
        else:
            self.note_title.setText(f"{entry.get('patient_name', '')} ({entry.get('patient_id', '')}) · "
                                    f"{_date(entry.get('study_date'))} {entry.get('description', '')}")
            folder = entry.get("folder", "")
            self.note_path.setText(("⚠ 없음: " if folder and not os.path.isdir(folder) else "") + folder)
            if entry.get("note_html"):
                self.note.setHtml(entry["note_html"])
            else:
                self.note.setPlainText(entry.get("note_text", ""))
            self.tag_edit.setText(", ".join(entry.get("tags", [])))
            self._set_note_enabled(True)
        self._loading_note = False
        self.note_status.setText("")
        self._refresh_quick_tags()

    def event(self, event):
        from PyQt5.QtCore import QEvent
        if (event.type() == QEvent.ShortcutOverride and event.key() == Qt.Key_F2
                and self.studies.hasFocus()):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F2 and self.studies.hasFocus() and self._current:
            self.rename_requested.emit("study", self._current)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.studies.hasFocus() and self._current:
            self.open_requested.emit(self._current)
            return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.studies.hasFocus():
            self.remove_selected()
            return
        super().keyPressEvent(event)

    # ─── 컬렉션 ───
    def new_collection(self, parent):
        name, ok = QInputDialog.getText(self, "새 컬렉션", "이름 (예: Cardiac Cases, Teaching Cases):")
        if ok and name.strip():
            c = self.store.add_collection(name, parent)
            self._collection = c["id"]
            self.refresh()

    def _on_drop(self, uids, cid):
        n = self.store.add_to_collection(uids, cid)
        c = self.store.collection(cid)
        self.note_status.setText(f"'{c['name']}'에 {n}개 추가" if c else "")

    def _collection_menu(self, pos: QPoint):
        item = self.collections.itemAt(pos)
        cid = item.data(0, ROLE_ID) if item is not None else None
        menu = QMenu(self)
        menu.addAction("새 컬렉션…", lambda: self.new_collection(None))
        if cid not in (None, ALL, UNFILED):
            c = self.store.collection(cid)
            menu.addAction("하위 컬렉션 만들기…", lambda: self.new_collection(cid))
            menu.addAction("이름 바꾸기…", lambda: self._rename(cid))
            menu.addSeparator()
            menu.addAction("위로", lambda: self.store.move_collection(cid, -1))
            menu.addAction("아래로", lambda: self.store.move_collection(cid, 1))
            move = menu.addMenu("다른 컬렉션 안으로")
            move.addAction("(최상위)", lambda: self.store.set_parent(cid, None))
            for other in self.store.collections:
                if other["id"] not in self.store.descendants(cid):
                    move.addAction(other["name"], lambda _=False, o=other["id"]: self.store.set_parent(cid, o))
            menu.addSeparator()
            menu.addAction("📤 이 컬렉션 Export…", lambda: self.export_requested.emit([], cid))
            menu.addAction("컬렉션 메모 내보내기 (Markdown)…",
                           lambda: self.export_notes("md", False, self.store.search("", cid)))
            menu.addAction(f"'{c['name']}' 삭제 (스터디는 라이브러리에 남음)", lambda: self._delete(cid))
        menu.exec_(self.collections.viewport().mapToGlobal(pos))

    def _rename(self, cid):
        c = self.store.collection(cid)
        name, ok = QInputDialog.getText(self, "이름 바꾸기", "컬렉션 이름:", text=c["name"])
        if ok:
            self.store.rename_collection(cid, name)

    def _delete(self, cid):
        c = self.store.collection(cid)
        sub = len(self.store.descendants(cid)) - 1
        if QMessageBox.question(self, "컬렉션 삭제",
                                f"'{c['name']}'" + (f"과 하위 컬렉션 {sub}개" if sub else "")
                                + "를 삭제할까요?\n스터디와 메모는 라이브러리에 그대로 남습니다.") == QMessageBox.Yes:
            self._collection = ALL
            self.store.delete_collection(cid)

    # ─── 스터디 메뉴 ───
    def _study_menu(self, pos):
        uids = self.selected_uids()
        if not uids:
            return
        menu = QMenu(self)
        menu.addAction("📂 열기", lambda: self.open_requested.emit(uids[0]))
        menu.addAction("Rename Study… (F2)", lambda: self.rename_requested.emit("study", uids[0]))
        menu.addAction("Edit Patient Name/ID…", lambda: self.rename_requested.emit("patient", uids[0]))
        menu.addAction("Finder에서 보기", self._reveal_study)
        add = menu.addMenu("컬렉션에 추가")
        for c in self.store.collections:
            add.addAction(c["name"], lambda _=False, cid=c["id"]: self.store.add_to_collection(uids, cid))
        if not self.store.collections:
            add.addAction("(컬렉션 없음 — 새로 만들기)", lambda: self.new_collection(None))
        if self._collection not in (ALL, UNFILED):
            c = self.store.collection(self._collection)
            menu.addAction(f"'{c['name']}'에서 빼기",
                           lambda: self.store.remove_from_collection(uids, self._collection))
        tags = menu.addMenu("태그")
        for tag in sorted(set(self.store.quick_tags) | set(self.store.all_tags())):
            tags.addAction(f"#{tag}", lambda _=False, t=tag: self.store.toggle_tag(uids, t))
        tags.addAction("새 태그…", lambda: self._new_tag(uids))
        menu.addAction("메모 내보내기 (Markdown)…", lambda: self.export_notes("md", True))
        menu.addAction("📤 Export…", self.request_export)
        menu.addSeparator()
        menu.addAction(f"라이브러리에서 삭제 ({len(uids)}개)", self.remove_selected)
        menu.exec_(self.studies.viewport().mapToGlobal(pos))

    def _new_tag(self, uids):
        tag, ok = QInputDialog.getText(self, "새 태그", "태그 이름 (예: interesting):")
        if ok and normalize_tag(tag):
            self.store.toggle_tag(uids, tag)

    def request_export(self):
        self.flush()
        collection = self._collection if self._collection not in (ALL, UNFILED) else None
        self.export_requested.emit(self.selected_uids(), collection)

    def remove_selected(self):
        uids = self.selected_uids()
        if uids and QMessageBox.question(
                self, "라이브러리에서 삭제",
                f"{len(uids)}개 스터디를 라이브러리에서 뺄까요?\n메모·태그도 함께 삭제됩니다 (영상 파일은 그대로).") \
                == QMessageBox.Yes:
            self._current = None
            for uid in uids:
                self.store.remove_study(uid)

    def _reveal_study(self):
        entry = self.store.get(self._current) if self._current else None
        folder = entry.get("folder") if entry else ""
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _reveal_file(self):
        folder = os.path.dirname(self.store.path)
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ─── 내보내기 / 가져오기 ───
    def export_notes(self, fmt, selected_only, uids=None):
        self.flush()
        if uids is None:
            uids = self.selected_uids() if selected_only else self.store.search(
                self.search.text(), None if self._collection == ALL else self._collection, self._filter_tags)
        if not uids:
            QMessageBox.information(self, "메모 내보내기", "내보낼 스터디가 없습니다.")
            return
        ext = "md" if fmt == "md" else "txt"
        path, _ = QFileDialog.getSaveFileName(self, "메모 내보내기", f"dabbaview_notes.{ext}",
                                              "Markdown (*.md)" if ext == "md" else "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.store.export_notes(uids, fmt))
            self.note_status.setText(f"{len(uids)}개 내보냄")

    def export_library(self):
        self.flush()
        path, _ = QFileDialog.getSaveFileName(self, "라이브러리 내보내기", "dabbaview_library.json",
                                              "JSON (*.json)")
        if path:
            self.store.export_json(path)

    def import_library(self):
        path, _ = QFileDialog.getOpenFileName(self, "라이브러리 가져오기", "", "JSON (*.json)")
        if not path:
            return
        try:
            studies, collections = self.store.import_json(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "가져오기", str(e))
            return
        QMessageBox.information(self, "가져오기", f"새 스터디 {studies}개, 새 컬렉션 {collections}개를 병합했습니다.")

    def edit_quick_tags(self):
        text, ok = QInputDialog.getText(self, "빠른 태그", "쉼표로 구분 (최대 6개):",
                                        text=", ".join(self.store.quick_tags))
        if ok:
            tags = [normalize_tag(t) for t in text.split(",") if normalize_tag(t)][:6]
            self.store.quick_tags = tags or list(self.store.quick_tags)
            self.store._changed()
