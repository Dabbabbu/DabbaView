"""
INFINITT 스타일 시리즈 패널

세로 스크롤 카드 목록. 클릭 = 선택만(노란 테두리), 더블클릭/Enter/드래그 앤 드롭 = 뷰포트에 표시.
카드마다
  - 썸네일 (중간 슬라이스, 80x80) + 좌상단 '시리즈번호/총 슬라이스' (예: 4/31)
  - 시퀀스 설명 (SeriesDescription) + 방향·시퀀스 요약
  - 선택된 시리즈는 노란 테두리
검사(Study)마다 머리글 행으로 묶음. 카드를 Multi View 칸으로 드래그 가능.
"""
from PyQt5.QtWidgets import (QListWidget, QListWidgetItem, QStyledItemDelegate,
                             QStyle, QAbstractItemView)
from PyQt5.QtCore import Qt, QSize, QRectF, QPointF, QMimeData, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QImage, QFontMetrics

from . import dicom_info
from .series_tree import (SERIES_MIME_TYPE, MODALITY_COLORS, DEFAULT_MODALITY_COLOR,
                          ThumbnailWorker, group_series, format_dicom_date,
                          format_patient_name)

CARD_THUMB = 80
CARD_HEIGHT = CARD_THUMB + 12
HEADER_HEIGHT = 24
THUMB_PIXELS = CARD_THUMB * 2  # HiDPI 선명도용 2배 해상도로 생성

ROLE_KIND = Qt.UserRole
ROLE_UID = Qt.UserRole + 1
ROLE_NUMBER = Qt.UserRole + 2
ROLE_DESC = Qt.UserRole + 3
ROLE_DETAIL = Qt.UserRole + 4
ROLE_MODALITY = Qt.UserRole + 5

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
        if index.data(ROLE_KIND) == "header":
            return QSize(option.rect.width(), HEADER_HEIGHT)
        return QSize(option.rect.width(), CARD_HEIGHT)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(option.rect)

        if index.data(ROLE_KIND) == "header":
            font = QFont()
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#4fc1ff"))
            text = QFontMetrics(font).elidedText(index.data(Qt.DisplayRole), Qt.ElideRight,
                                                 int(rect.width() - 12))
            painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
            painter.setPen(QColor("#333"))
            painter.drawLine(QPointF(rect.left() + 4, rect.bottom()),
                             QPointF(rect.right() - 4, rect.bottom()))
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

        if selected:
            painter.setPen(QPen(SELECT_COLOR, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(card.adjusted(1, 1, -1, -1), 4, 4)
        painter.restore()


class SeriesPanel(QListWidget):
    """세로 스크롤 시리즈 카드 목록"""

    series_selected = pyqtSignal(str)   # 클릭 (선택만)
    series_activated = pyqtSignal(str)  # 더블클릭 / Enter → 뷰포트에 표시
    thumbnail_ready = pyqtSignal(str, QImage)

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
        self.itemDoubleClicked.connect(self._on_double_clicked)

    # 목록 구성
    def populate(self, series_list, select_uid=None):
        self.blockSignals(True)
        self.clear()
        self._items_by_uid = {}
        first = target = None
        for pname, pid, studies in group_series(series_list):
            for study_date, _time, study_desc, series_group in studies:
                header = QListWidgetItem(
                    f"{format_patient_name(pname)}  ·  "
                    f"{format_dicom_date(study_date) or '날짜 없음'}  {study_desc}".strip())
                header.setData(ROLE_KIND, "header")
                header.setFlags(Qt.ItemIsEnabled)
                header.setToolTip(f"Patient: {pname}  ID: {pid or '-'}")
                self.addItem(header)
                for s in series_group:
                    ds = s.slices[0] if s.slices else None
                    item = QListWidgetItem()
                    item.setData(ROLE_KIND, "series")
                    item.setData(ROLE_UID, s.series_uid)
                    item.setData(ROLE_NUMBER, series_number_label(s))
                    item.setData(ROLE_DESC, s.description or "(no description)")
                    detail = [dicom_info.orientation_name(ds) if ds is not None else ""]
                    if ds is not None and s.modality == "MR":
                        detail.append(dicom_info.mr_sequence_type(ds).split(" [")[0])
                    item.setData(ROLE_DETAIL, " · ".join(d for d in detail if d))
                    item.setData(ROLE_MODALITY, s.modality)
                    item.setData(Qt.DisplayRole, s.description)
                    if ds is not None:
                        item.setToolTip(dicom_info.sequence_tooltip(
                            ds, f"#{s.series_number}  {s.description}"
                            if s.series_number is not None else s.description,
                            s.num_slices))
                    self.addItem(item)
                    self._items_by_uid[s.series_uid] = item
                    first = first or item
                    if s.series_uid == select_uid:
                        target = item
        self.blockSignals(False)
        self._start_thumbnails(series_list)
        selected = target or first
        if selected is not None:
            self.setCurrentItem(selected)  # → series_selected

    def select_uid(self, uid):
        """시그널 없이 선택 표시만 변경"""
        item = self._items_by_uid.get(uid)
        if item is not None:
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

    def _on_double_clicked(self, item):
        if item.data(ROLE_KIND) == "series":
            self.series_activated.emit(item.data(ROLE_UID))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
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
