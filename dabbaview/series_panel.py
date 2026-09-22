# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
INFINITT 스타일 시리즈 패널

세로 스크롤 카드 목록. 클릭(뗄 때)/Enter = 활성 뷰포트에 로드,
누른 채 끌면 로드하지 않고 드래그 앤 드롭 (원하는 칸에 놓기).
카드마다
  - 썸네일 (중간 슬라이스, 80x80) + 좌상단 '시리즈번호/총 슬라이스' (예: 4/31)
  - 시퀀스 설명 (SeriesDescription) + 방향·시퀀스 요약
  - 선택된 시리즈는 노란 테두리
환자 머리글(▼ 이름 · 검사 수 · 영상 수) 아래 검사 머리글(▼ 날짜 설명), 그 아래 카드.
▶/▼를 누르면 접기/펼치기, 환자 머리글의 나머지 부분을 누르면 그 환자만 펼치고
첫 시리즈를 로드 (환자 간 빠른 전환). 기본은 선택된 시리즈의 환자만 펼침.
카드를 Multi View 칸으로 드래그 가능.
"""
from PyQt5.QtWidgets import (QListWidget, QListWidgetItem, QStyledItemDelegate,
                             QStyle, QAbstractItemView, QMenu)
from PyQt5.QtCore import Qt, QSize, QRectF, QPointF, QMimeData, QEvent, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFont, QPainter, QPen, QImage, QFontMetrics

from . import dicom_info
from .series_tree import (SERIES_MIME_TYPE, MODALITY_COLORS, DEFAULT_MODALITY_COLOR,
                          ThumbnailWorker, group_series, format_dicom_date,
                          format_patient_name, ClickToLoadMixin)

CARD_THUMB = 80
CARD_HEIGHT = CARD_THUMB + 12
HEADER_HEIGHT = 24
PATIENT_HEADER_HEIGHT = 30
TOGGLE_WIDTH = 22   # 머리글 왼쪽 ▶/▼ 영역 (여기를 누르면 접기/펼치기만)
THUMB_PIXELS = CARD_THUMB * 2  # HiDPI 선명도용 2배 해상도로 생성

ROLE_KIND = Qt.UserRole
ROLE_UID = Qt.UserRole + 1
ROLE_NUMBER = Qt.UserRole + 2
ROLE_DESC = Qt.UserRole + 3
ROLE_DETAIL = Qt.UserRole + 4
ROLE_MODALITY = Qt.UserRole + 5
ROLE_GROUP = Qt.UserRole + 6      # 환자 키 / 검사 키 (머리글·카드 모두)
ROLE_STUDY = Qt.UserRole + 7
ROLE_EXPANDED = Qt.UserRole + 8   # 머리글이 펼쳐져 있는지
ROLE_SOURCE = Qt.UserRole + 9     # 불러온 폴더 이름 (카드 맨 아래 📁 줄)

SELECT_COLOR = QColor("#ffd400")


def series_number_label(series):
    """'시리즈번호/총 슬라이스' (번호가 없으면 '-')"""
    number = dicom_info.tag(series.slices[0], "SeriesNumber") if series.slices else ""
    return f"{number or '-'}/{series.num_slices}"


class SeriesCardDelegate(QStyledItemDelegate):

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    def sizeHint(self, option, index):
        kind = index.data(ROLE_KIND)
        if kind == "patient":
            return QSize(option.rect.width(), PATIENT_HEADER_HEIGHT)
        if kind == "header":
            return QSize(option.rect.width(), HEADER_HEIGHT)
        return QSize(option.rect.width(), CARD_HEIGHT)

    @staticmethod
    def _paint_header(painter, rect, index, patient):
        arrow = "▼" if index.data(ROLE_EXPANDED) else "▶"
        font = QFont()
        font.setPointSize(11 if patient else 10)
        font.setBold(True)
        painter.setFont(font)
        if patient:
            painter.fillRect(rect.adjusted(0, 1, 0, -1), QColor("#1f2a33"))
        indent = 4 if patient else 14
        painter.setPen(QColor("#e8e8e8") if patient else QColor("#4fc1ff"))
        painter.drawText(QRectF(rect.left() + indent, rect.top(), TOGGLE_WIDTH, rect.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, arrow)
        text_rect = rect.adjusted(indent + TOGGLE_WIDTH - 4, 0, -6, 0)
        text = index.data(Qt.DisplayRole) or ""
        extra = index.data(ROLE_DETAIL) or ""
        metrics = QFontMetrics(font)
        if extra:
            small = QFont(font)
            small.setBold(False)
            small.setPointSize(9)
            extra_w = QFontMetrics(small).horizontalAdvance(extra) + 8
            name = metrics.elidedText(text, Qt.ElideRight,
                                      int(max(40, text_rect.width() - extra_w)))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, name)
            painter.setFont(small)
            painter.setPen(QColor("#9ab"))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignRight, extra)
        else:
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                             metrics.elidedText(text, Qt.ElideRight, int(text_rect.width())))
        painter.setPen(QColor("#333"))
        painter.drawLine(QPointF(rect.left() + 4, rect.bottom()),
                         QPointF(rect.right() - 4, rect.bottom()))

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(option.rect)

        kind = index.data(ROLE_KIND)
        if kind in ("patient", "header"):
            self._paint_header(painter, rect, index, kind == "patient")
            painter.restore()
            return

        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        card = rect.adjusted(3, 3, -3, -3)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#2b2a1c") if selected else
                         QColor("#262626") if hover else QColor("#1b1b1b"))
        painter.drawRoundedRect(card, 4, 4)

        # 썸네일
        thumb_rect = QRectF(card.left() + 4, card.top() + 3, CARD_THUMB, CARD_THUMB)
        painter.fillRect(thumb_rect, QColor("#000"))
        image = self._panel.thumbnail_for(index.data(ROLE_UID))
        if image is not None and not image.isNull():
            scale = min(CARD_THUMB / image.width(), CARD_THUMB / image.height())
            w, h = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF(thumb_rect.center().x() - w / 2,
                                     thumb_rect.center().y() - h / 2, w, h), image)
        else:
            painter.setPen(QColor("#555"))
            painter.drawText(thumb_rect, Qt.AlignCenter, "…")

        # 썸네일 위 '번호/총수' (INFINITT 방식)
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        number = index.data(ROLE_NUMBER)
        painter.setPen(QColor(0, 0, 0, 220))
        painter.drawText(QPointF(thumb_rect.left() + 4, thumb_rect.top() + 14), number)
        painter.setPen(SELECT_COLOR)
        painter.drawText(QPointF(thumb_rect.left() + 3, thumb_rect.top() + 13), number)

        # 모달리티 배지
        modality = (index.data(ROLE_MODALITY) or "?")[:3]
        badge = QRectF(thumb_rect.left() + 2, thumb_rect.bottom() - 16, 28, 14)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(MODALITY_COLORS.get(modality, DEFAULT_MODALITY_COLOR)))
        painter.drawRoundedRect(badge, 3, 3)
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(badge, Qt.AlignCenter, modality)

        # 설명 (최대 2줄) + 요약
        text_rect = QRectF(thumb_rect.right() + 8, card.top() + 4,
                           card.right() - thumb_rect.right() - 12, CARD_THUMB)
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#f0f0f0"))
        desc_rect = QRectF(text_rect.left(), text_rect.top(), text_rect.width(), 36)
        painter.drawText(desc_rect, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                         index.data(ROLE_DESC))
        font.setBold(False)
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QColor("#9a9a9a"))
        detail = QFontMetrics(font).elidedText(index.data(ROLE_DETAIL) or "",
                                               Qt.ElideRight, int(text_rect.width()))
        painter.drawText(QRectF(text_rect.left(), text_rect.top() + 40,
                                text_rect.width(), 16), Qt.AlignLeft, detail)
        source = index.data(ROLE_SOURCE)
        if source:                                   # 불러온 폴더 (어디서 왔는지)
            painter.setPen(QColor("#7fa7c9"))
            folder = QFontMetrics(font).elidedText(f"📁 {source}", Qt.ElideMiddle,
                                                   int(text_rect.width()))
            painter.drawText(QRectF(text_rect.left(), text_rect.top() + 58,
                                    text_rect.width(), 16), Qt.AlignLeft, folder)

        missing = self._panel.failed_count(index.data(ROLE_UID))
        if missing:                                  # 같은 폴더에서 읽지 못한 파일 → 일부가 빠졌을 수 있음
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            label = f"⚠ {missing:,} 누락"
            width = QFontMetrics(font).horizontalAdvance(label) + 8
            warn = QRectF(card.right() - width - 4, card.top() + 4, width, 14)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#5a3d10"))
            painter.drawRoundedRect(warn, 3, 3)
            painter.setPen(QColor("#ffcf7a"))
            painter.drawText(warn, Qt.AlignCenter, label)

        if selected:
            painter.setPen(QPen(SELECT_COLOR, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(card.adjusted(1, 1, -1, -1), 4, 4)
        painter.restore()


class SeriesPanel(ClickToLoadMixin, QListWidget):
    """세로 스크롤 시리즈 카드 목록"""

    series_selected = pyqtSignal(str)   # 누름 / 방향키 (선택 표시)
    series_activated = pyqtSignal(str)  # 클릭(뗄 때) / Enter → 뷰포트에 로드
    thumbnail_ready = pyqtSignal(str, QImage)
    summary_changed = pyqtSignal(dict)  # {"patients", "studies", "series", "images"}
    rename_requested = pyqtSignal(str, str)   # ("study"|"series"|"patient", UID)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(SeriesCardDelegate(self))
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setUniformItemSizes(False)
        self.setMouseTracking(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setStyleSheet("QListWidget { background: #141414; border: none; }"
                           "QListWidget::item { border: none; }")
        self._items_by_uid = {}
        self._thumbnails = {}
        self._thumb_worker = None
        self.currentItemChanged.connect(self._on_current_changed)
        self.itemClicked.connect(self._on_item_clicked)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._click_init()
        self._headers = []            # [(머리글 item, 종류, 키)]
        self._collapsed = set()       # 접힌 머리글 키 ("P:..." 환자, "S:..." 검사)
        self._first_uid = {}          # 환자 키 → 첫 시리즈 UID
        self.summary = {"patients": 0, "studies": 0, "series": 0, "images": 0}

    # 목록 구성
    def populate(self, series_list, select_uid=None):
        self.blockSignals(True)
        self.clear()
        self._items_by_uid = {}
        self._folder_by_uid = {}
        self._series_by_uid = {s.series_uid: s for s in series_list}
        self._headers = []
        self._first_uid = {}
        first = target = None
        groups = group_series(series_list)
        n_studies = n_images = 0
        for pname, pid, studies in groups:
            pkey = f"P:{pid}|{pname}"
            p_images = sum(s.num_slices for *_rest, g in studies for s in g)
            patient = QListWidgetItem(
                format_patient_name(pname) + (f"  ({pid})" if pid else ""))
            patient.setData(ROLE_KIND, "patient")
            patient.setData(ROLE_UID, studies[0][3][0].study_uid if studies and studies[0][3] else "")
            patient.setData(ROLE_GROUP, pkey)
            n = len(studies)
            patient.setData(ROLE_DETAIL, f"{n} stud{'ies' if n != 1 else 'y'} · {p_images:,} images")
            patient.setFlags(Qt.ItemIsEnabled)
            patient.setToolTip(f"Patient: {pname}  ID: {pid or '-'}\n"
                               "▶/▼: 접기·펼치기  |  이름 클릭: 이 환자의 첫 시리즈 열기")
            self.addItem(patient)
            self._headers.append((patient, "patient", pkey))
            for study_date, _time, study_desc, series_group in studies:
                first_series = series_group[0]
                skey = "S:" + (first_series.study_uid or f"{pkey}|{study_date}|{study_desc}")
                header = QListWidgetItem(
                    f"{format_dicom_date(study_date) or '날짜 없음'}  {study_desc}".strip())
                header.setData(ROLE_KIND, "header")
                header.setData(ROLE_GROUP, pkey)
                header.setData(ROLE_STUDY, skey)
                header.setData(ROLE_DETAIL, f"{len(series_group)} series")
                header.setData(ROLE_UID, first_series.study_uid or "")
                header.setFlags(Qt.ItemIsEnabled)
                header.setToolTip(f"Patient: {pname}  ID: {pid or '-'}")
                self.addItem(header)
                self._headers.append((header, "study", skey))
                n_studies += 1
                for s in series_group:
                    ds = s.slices[0] if s.slices else None
                    item = QListWidgetItem()
                    item.setData(ROLE_KIND, "series")
                    item.setData(ROLE_UID, s.series_uid)
                    item.setData(ROLE_GROUP, pkey)
                    item.setData(ROLE_STUDY, skey)
                    item.setData(ROLE_NUMBER, series_number_label(s))
                    item.setData(ROLE_DESC, s.description or "(no description)")
                    detail = [dicom_info.orientation_name(ds) if ds is not None else ""]
                    if ds is not None and s.modality == "MR":
                        detail.append(dicom_info.mr_sequence_type(ds).split(" [")[0])
                    item.setData(ROLE_DETAIL, " · ".join(d for d in detail if d))
                    item.setData(ROLE_MODALITY, s.modality)
                    item.setData(Qt.DisplayRole, s.description)
                    from .source_info import folder_of
                    short, full = folder_of(s)
                    item.setData(ROLE_SOURCE, short)
                    self._folder_by_uid[s.series_uid] = full
                    if ds is not None:
                        item.setToolTip(dicom_info.sequence_tooltip(
                            ds, f"#{s.series_number}  {s.description}"
                            if s.series_number is not None else s.description,
                            s.num_slices) + (f"\n\n📁 {full}" if full else ""))
                    self.addItem(item)
                    self._items_by_uid[s.series_uid] = item
                    self._first_uid.setdefault(pkey, s.series_uid)
                    n_images += s.num_slices
                    first = first or item
                    if s.series_uid == select_uid:
                        target = item
        selected = target or first
        # 기본: 선택된 시리즈의 환자만 펼치고 나머지 환자는 접음
        current_patient = selected.data(ROLE_GROUP) if selected is not None else None
        self._collapsed = {key for _item, kind, key in self._headers
                           if kind == "patient" and key != current_patient}
        self._apply_visibility()
        self.blockSignals(False)
        self.summary = {"patients": len(groups), "studies": n_studies,
                        "series": len(self._items_by_uid), "images": n_images}
        self.summary_changed.emit(dict(self.summary))
        self._start_thumbnails(series_list)
        if selected is not None:
            self.setCurrentItem(selected)  # → series_selected

    # 접기 / 펼치기
    def set_failed_folders(self, counts):
        """폴더 → 읽지 못한 파일 수. 그 폴더의 시리즈 카드에 ⚠ (영상 일부가 빠졌을 수 있음)"""
        self._failed_folders = dict(counts)
        self.viewport().update()

    def failed_count(self, uid):
        folder = getattr(self, "_folder_by_uid", {}).get(uid)
        return getattr(self, "_failed_folders", {}).get(folder, 0) if folder else 0

    def _apply_visibility(self):
        for row in range(self.count()):
            item = self.item(row)
            kind = item.data(ROLE_KIND)
            pkey, skey = item.data(ROLE_GROUP), item.data(ROLE_STUDY)
            if kind == "patient":
                item.setData(ROLE_EXPANDED, pkey not in self._collapsed)
                continue
            patient_open = pkey not in self._collapsed
            if kind == "header":
                item.setData(ROLE_EXPANDED, skey not in self._collapsed)
                item.setHidden(not patient_open)
            else:
                item.setHidden(not patient_open or skey in self._collapsed)
        self.viewport().update()

    def is_expanded(self, key):
        return key not in self._collapsed

    def set_expanded(self, key, expanded):
        (self._collapsed.discard if expanded else self._collapsed.add)(key)
        self._apply_visibility()

    def toggle(self, key):
        self.set_expanded(key, key in self._collapsed)

    def collapse_all(self):
        """모든 환자·검사 접기 (선택 표시는 유지)"""
        self._collapsed = {key for _item, _kind, key in self._headers}
        self._apply_visibility()

    def expand_all(self):
        self._collapsed = set()
        self._apply_visibility()
        item = self.currentItem()
        if item is not None:
            self.scrollToItem(item)

    def patient_keys(self):
        return [key for _item, kind, key in self._headers if kind == "patient"]

    def reveal(self, uid):
        """시리즈가 보이도록 그 환자·검사를 펼침"""
        item = self._items_by_uid.get(uid)
        if item is None:
            return
        pkey, skey = item.data(ROLE_GROUP), item.data(ROLE_STUDY)
        if pkey in self._collapsed or skey in self._collapsed:
            self._collapsed.discard(pkey)
            self._collapsed.discard(skey)
            self._apply_visibility()

    def open_patient(self, pkey):
        """환자 간 빠른 전환: 이 환자만 펼치고 첫 시리즈 로드"""
        self._collapsed = {key for _item, kind, key in self._headers
                           if kind == "patient" and key != pkey}
        self._apply_visibility()
        uid = self._first_uid.get(pkey)
        if uid:
            self.select_uid(uid)
            header = next((item for item, kind, key in self._headers
                           if kind == "patient" and key == pkey), None)
            if header is not None:
                self.scrollToItem(header, QAbstractItemView.PositionAtTop)
            self.series_activated.emit(uid)

    def _on_item_clicked(self, item):
        kind = item.data(ROLE_KIND)
        if kind not in ("patient", "header"):
            return
        # 더블클릭의 두 번째 클릭으로 다시 토글되지 않게
        import time
        from PyQt5.QtWidgets import QApplication
        now = time.monotonic()
        last_item, last_time = getattr(self, "_last_header_click", (None, 0.0))
        self._last_header_click = (item, now)
        if item is last_item and (now - last_time) * 1000 < QApplication.doubleClickInterval():
            return
        # 누른 위치 (ClickToLoadMixin이 기록) — 없으면 현재 커서 위치
        pos = self._press_pos or self.viewport().mapFromGlobal(QCursor.pos())
        rect = self.visualItemRect(item)
        indent = 4 if kind == "patient" else 14
        on_arrow = pos.x() < rect.left() + indent + TOGGLE_WIDTH
        if kind == "header":
            self.toggle(item.data(ROLE_STUDY))
        elif on_arrow:
            self.toggle(item.data(ROLE_GROUP))
        else:
            self.open_patient(item.data(ROLE_GROUP))

    def select_uid(self, uid):
        """시그널 없이 선택 표시만 변경"""
        item = self._items_by_uid.get(uid)
        if item is not None:
            self.reveal(uid)
            self.blockSignals(True)
            self.setCurrentItem(item)
            self.scrollToItem(item)
            self.blockSignals(False)
            self.viewport().update()

    def current_uid(self):
        item = self.currentItem()
        return item.data(ROLE_UID) if item is not None else None

    def item_for(self, uid):
        return self._items_by_uid.get(uid)

    def _on_current_changed(self, current, previous):
        if current is not None and current.data(ROLE_KIND) == "series":
            self.series_selected.emit(current.data(ROLE_UID))

    def _series_uid_at(self, pos):
        item = self.itemAt(pos)
        if item is not None and item.data(ROLE_KIND) == "series":
            return item.data(ROLE_UID)
        return None

    # ─── 이름 바꾸기 (우클릭, F2) ───
    def _study_uid_of(self, item):
        if item is None:
            return ""
        if item.data(ROLE_KIND) == "series":
            series = self._series_by_uid.get(item.data(ROLE_UID))
            return series.study_uid if series is not None else ""
        return item.data(ROLE_UID) or ""

    def _context_menu(self, pos):
        from .platform_keys import RENAME_LABEL, RENAME_LABEL_SHIFT
        item = self.itemAt(pos)
        if item is None:
            return
        kind = item.data(ROLE_KIND)
        menu = QMenu(self)
        study_uid = self._study_uid_of(item)
        if kind == "series":
            uid = item.data(ROLE_UID)
            menu.addAction(f"Rename Series… ({RENAME_LABEL_SHIFT})", lambda: self.rename_requested.emit("series", uid))
        if study_uid and kind in ("series", "header"):
            menu.addAction(f"Rename Study… ({RENAME_LABEL})", lambda: self.rename_requested.emit("study", study_uid))
        if study_uid:
            menu.addSeparator()
            menu.addAction("Edit Patient Name/ID…", lambda: self.rename_requested.emit("patient", study_uid))
        menu.exec_(self.viewport().mapToGlobal(pos))

    def event(self, event):
        # F2는 창 전체에서 '시리즈 패널 접기'지만, 이 목록에 포커스가 있으면 이름 바꾸기
        from .platform_keys import is_rename_key
        if event.type() == QEvent.ShortcutOverride and is_rename_key(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        from .platform_keys import is_open_key, is_rename_key
        if is_rename_key(event):   # macOS Return · Windows F2
            item = self.currentItem()
            if item is not None and item.data(ROLE_KIND) == "series" \
                    and event.modifiers() & Qt.ShiftModifier:
                self.rename_requested.emit("series", item.data(ROLE_UID))
            elif self._study_uid_of(item):
                self.rename_requested.emit("study", self._study_uid_of(item))
            return
        if is_open_key(event):   # macOS ⌘Return · 그 밖의 OS Return
            item = self.currentItem()
            if item is not None and item.data(ROLE_KIND) == "series":
                self.series_activated.emit(item.data(ROLE_UID))
                return
        super().keyPressEvent(event)

    # 드래그 (Multi View 칸으로)
    def mimeTypes(self):
        return [SERIES_MIME_TYPE]

    def mimeData(self, items):
        uids = [i.data(ROLE_UID) for i in items if i.data(ROLE_KIND) == "series"]
        if not uids:
            return None
        mime = QMimeData()
        mime.setData(SERIES_MIME_TYPE, uids[0].encode("utf-8"))
        mime.setText(uids[0])
        return mime

    # 썸네일
    def thumbnail_for(self, uid):
        return self._thumbnails.get(uid)

    def _start_thumbnails(self, series_list):
        if self._thumb_worker is not None:
            self._thumb_worker.cancel()
        pending = [s for s in series_list if s.series_uid not in self._thumbnails]
        if not pending:
            self._thumb_worker = None
            return
        worker = ThumbnailWorker(pending, self, size=THUMB_PIXELS)
        worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._thumb_worker = worker
        worker.start()

    def _on_worker_finished(self, worker):
        if self._thumb_worker is worker:
            self._thumb_worker = None
        worker.deleteLater()

    def _on_thumbnail_ready(self, uid, image):
        self._thumbnails[uid] = image
        self.thumbnail_ready.emit(uid, image)
        item = self._items_by_uid.get(uid)
        if item is not None:
            self.update(self.indexFromItem(item))

    def shutdown(self):
        if self._thumb_worker is not None:
            self._thumb_worker.cancel()
            self._thumb_worker.wait(3000)
