# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
시리즈 목록 트리: Patient → Study → Series
- 시리즈마다 대표(중간) 슬라이스 썸네일 + 모달리티 배지
- 툴팁에 시퀀스 파라미터 요약
"""
import threading
import time

import cv2
import numpy as np
from PyQt5.QtWidgets import (QTreeWidget, QTreeWidgetItem, QHeaderView,
                             QAbstractItemView, QApplication)
from PyQt5.QtCore import Qt, QSize, QRectF, QMimeData, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QDrag, QFont, QIcon, QImage, QPainter, QPixmap

from . import dicom_info


# 모달리티별 배지 색상 (없는 모달리티는 회색)
MODALITY_COLORS = {
    'CT': '#3B8FD9',
    'MR': '#8E5CD9',
    'CR': '#D9922B', 'DX': '#D9922B', 'DR': '#D9922B',
    'US': '#2FA66F',
    'PT': '#D9483B', 'NM': '#D9483B',
    'XA': '#C95C9E', 'RF': '#C95C9E',
    'MG': '#B8A42A',
}
DEFAULT_MODALITY_COLOR = '#6E6E6E'

BADGE_SIZE = QSize(30, 16)
THUMB_SIZE = 64

ROLE_SERIES_UID = Qt.UserRole

# 트리 → 뷰포트 드래그 앤 드롭용 MIME 타입 (데이터: SeriesInstanceUID, utf-8)
SERIES_MIME_TYPE = "application/x-dabbaview-series-uid"


def _series_icon(modality, thumbnail=None):
    """썸네일(없으면 빈 칸) 좌하단에 모달리티 배지를 얹은 아이콘 (HiDPI 대응)"""
    modality = (modality or '?')[:3]
    scale = 2
    size = THUMB_SIZE
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(QColor('#111111'))
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    if thumbnail is not None and not thumbnail.isNull():
        scale = min(size / thumbnail.width(), size / thumbnail.height())
        tw, th = thumbnail.width() * scale, thumbnail.height() * scale
        p.drawImage(QRectF((size - tw) / 2, (size - th) / 2, tw, th), thumbnail)
    else:
        p.setPen(QColor('#555555'))
        font = QFont()
        font.setPixelSize(9)
        p.setFont(font)
        p.drawText(QRectF(0, 0, size, size - 14), Qt.AlignCenter, "…")
    p.setPen(QColor('#333333'))
    p.drawRect(QRectF(0.5, 0.5, size - 1, size - 1))

    badge = QRectF(2, size - BADGE_SIZE.height() - 2,
                   BADGE_SIZE.width(), BADGE_SIZE.height())
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(MODALITY_COLORS.get(modality, DEFAULT_MODALITY_COLOR)))
    p.drawRoundedRect(badge, 4, 4)
    font = QFont()
    font.setPixelSize(10)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor('white'))
    p.drawText(badge, Qt.AlignCenter, modality)
    p.end()
    return QIcon(pixmap)


def make_thumbnail(series, size=THUMB_SIZE):
    """시리즈 중간 슬라이스를 기본 윈도로 8비트 변환한 QImage (실패 시 None)

    Pixel Spacing 비율을 반영해 size x size 안에 맞춤. 워커 스레드에서 호출 가능.
    """
    if series.num_slices == 0:
        return None
    index = series.num_slices // 2
    arr = series.get_pixel_array(index)
    if arr is None:
        return None
    if arr.ndim == 3 and arr.shape[2] in (3, 4):
        img = np.clip(arr[..., :3], 0, 255).astype(np.uint8)
    else:
        if arr.ndim == 3:
            arr = arr[0]
        wc, ww = series.get_default_window()
        img = np.clip((arr - (wc - ww / 2)) / max(ww, 1) * 255, 0, 255).astype(np.uint8)
    h, w = img.shape[:2]
    sp = dicom_info.pixel_spacing(series.slices[index]) or (1.0, 1.0)
    phys_w, phys_h = w * sp[1], h * sp[0]
    scale = size / max(phys_w, phys_h)
    tw, th = max(1, round(phys_w * scale)), max(1, round(phys_h * scale))
    img = np.ascontiguousarray(cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA))
    if img.ndim == 3:
        qimg = QImage(img.data, tw, th, 3 * tw, QImage.Format_RGB888)
    else:
        qimg = QImage(img.data, tw, th, tw, QImage.Format_Grayscale8)
    return qimg.copy()  # numpy 버퍼와 분리


class ThumbnailWorker(QThread):
    """시리즈 썸네일을 백그라운드에서 생성 (UI 스레드 비차단)"""

    thumbnail_ready = pyqtSignal(str, QImage)

    def __init__(self, series_list, parent=None, size=THUMB_SIZE):
        super().__init__(parent)
        self._series_list = list(series_list)
        self._size = size
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        for series in self._series_list:
            if self._cancel.is_set():
                return
            try:
                image = make_thumbnail(series, self._size)
            except Exception:
                image = None
            if image is not None and not self._cancel.is_set():
                self.thumbnail_ready.emit(series.series_uid, image)


def format_dicom_date(value):
    """YYYYMMDD → YYYY-MM-DD (형식이 다르면 그대로)"""
    value = (value or '').strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def format_patient_name(value):
    """DICOM PN (Family^Given^...) → 사람이 읽기 쉬운 형태"""
    parts = [p for p in str(value or '').split('^') if p]
    return ' '.join(parts) if parts else 'Unknown'


def _series_sort_key(series):
    number = series.series_number
    return (number is None, number if number is not None else 0,
            series.description)


def group_series(series_list):
    """시리즈를 환자 → 검사 단위로 묶음

    반환: [(patient_name, patient_id, [(study_date, study_time, study_desc,
            [series, ...]), ...]), ...]  (정렬 완료)
    """
    patients = {}
    for s in series_list:
        pkey = (s.patient_id, s.patient_name)
        # StudyInstanceUID가 없으면 날짜+설명으로 대체
        skey = s.study_uid or (s.study_date, s.study_description)
        patients.setdefault(pkey, {}).setdefault(skey, []).append(s)

    result = []
    for (pid, pname), studies in patients.items():
        study_rows = []
        for series in studies.values():
            first = series[0]
            series.sort(key=_series_sort_key)
            study_rows.append((first.study_date, first.study_time,
                               first.study_description, series))
        # 최신 검사가 위로
        study_rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        result.append((pname, pid, study_rows))
    result.sort(key=lambda r: (format_patient_name(r[0]).lower(), r[1]))
    return result


class ClickToLoadMixin:
    """클릭 = 로드, 드래그 = 드래그 앤 드롭 (시리즈 패널·트리 공용)

    - 누를 때: 선택 표시만 하고 로드할 시리즈 UID를 기억
    - startDragDistance 이상 움직이면: 로드 취소, 드래그 시작
    - 같은 항목 위에서 떼면(= 클릭): series_activated → 활성 뷰포트에 로드
    - 더블클릭의 두 번째 클릭은 같은 시리즈를 다시 로드하지 않음 (뷰 상태 유지)

    사용 클래스는 _series_uid_at(pos)와 series_activated 시그널을 제공해야 함.
    """

    def _click_init(self):
        self._pending_uid = None
        self._press_pos = None
        self._last_activation = (None, 0.0)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pending_uid = self._series_uid_at(event.pos())
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._pending_uid is not None and event.buttons() & Qt.LeftButton
                and (event.pos() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            uid, self._pending_uid = self._pending_uid, None
            self.start_series_drag(uid)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        uid, self._pending_uid = self._pending_uid, None
        super().mouseReleaseEvent(event)
        if (event.button() == Qt.LeftButton and uid is not None
                and self._series_uid_at(event.pos()) == uid):
            self._activate(uid)

    def mouseDoubleClickEvent(self, event):
        # 두 번째 누름도 클릭처럼 처리 (뗄 때 로드, 단 방금 로드한 시리즈면 무시)
        self.mousePressEvent(event)

    def _activate(self, uid):
        last_uid, last_time = self._last_activation
        now = time.monotonic()
        if uid == last_uid and (now - last_time) * 1000 < QApplication.doubleClickInterval():
            return
        self._last_activation = (uid, now)
        self.series_activated.emit(uid)

    def start_series_drag(self, uid):
        """시리즈 드래그 시작 (MIME: SERIES_MIME_TYPE)"""
        mime = QMimeData()
        mime.setData(SERIES_MIME_TYPE, uid.encode("utf-8"))
        mime.setText(uid)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.CopyAction)


class SeriesTreeWidget(ClickToLoadMixin, QTreeWidget):
    """Patient → Study → Series 계층 트리"""

    series_selected = pyqtSignal(str)   # 누름 / 방향키 (선택 표시)
    series_activated = pyqtSignal(str)  # 클릭(뗄 때) / Enter → 뷰포트에 로드

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Series", "Images"])
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setUniformRowHeights(False)  # 시리즈 행만 썸네일 높이
        self.setIndentation(14)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.currentItemChanged.connect(self._on_current_item_changed)
        self._click_init()
        self._items_by_uid = {}
        self._modality_by_uid = {}
        self._thumbnails = {}  # uid → QImage (트리를 다시 그려도 재사용)
        self._thumb_worker = None
        self._external_thumbnails = False  # True면 set_thumbnail로만 받음
        # 시리즈 항목을 Multi View 뷰포트로 드래그 (환자/검사 항목은 드래그 불가)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)

    def mimeTypes(self):
        return [SERIES_MIME_TYPE]

    def mimeData(self, items):
        uids = [i.data(0, ROLE_SERIES_UID) for i in items
                if i.data(0, ROLE_SERIES_UID)]
        if not uids:
            return None
        mime = QMimeData()
        mime.setData(SERIES_MIME_TYPE, uids[0].encode('utf-8'))
        mime.setText(uids[0])
        return mime

    def select_uid(self, uid):
        """시그널 없이 해당 시리즈 항목을 선택 상태로 표시"""
        it = self.invisibleRootItem()
        stack = [it]
        while stack:
            item = stack.pop()
            if item.data(0, ROLE_SERIES_UID) == uid:
                self.blockSignals(True)
                self.setCurrentItem(item)
                self.blockSignals(False)
                return
            stack.extend(item.child(i) for i in range(item.childCount()))

    def populate(self, series_list, select_uid=None, emit=True):
        """트리를 다시 구성하고 전부 펼친 뒤 시리즈 선택

        select_uid가 있으면 그 시리즈, 없으면 첫 시리즈 선택.
        """
        self.blockSignals(True)
        self.clear()
        self._items_by_uid = {}
        self._modality_by_uid = {}
        first_series_item = None
        target_item = None

        bold = QFont()
        bold.setBold(True)
        dim = QColor('#9a9a9a')

        for pname, pid, studies in group_series(series_list):
            patient_text = format_patient_name(pname)
            if pid:
                patient_text += f"  (ID: {pid})"
            patient_item = QTreeWidgetItem([patient_text, ""])
            patient_item.setFont(0, bold)
            patient_item.setToolTip(0, f"Patient: {pname}\nID: {pid or '-'}")
            patient_item.setFlags(Qt.ItemIsEnabled)
            self.addTopLevelItem(patient_item)

            for study_date, study_time, study_desc, series_group in studies:
                date_text = format_dicom_date(study_date) or "날짜 없음"
                study_text = date_text
                if study_desc:
                    study_text += f"  {study_desc}"
                study_item = QTreeWidgetItem(
                    [study_text, f"{len(series_group)} series"])
                study_item.setForeground(1, dim)
                study_item.setToolTip(
                    0, f"Study Date: {date_text}\n"
                       f"Description: {study_desc or '-'}")
                study_item.setFlags(Qt.ItemIsEnabled)
                patient_item.addChild(study_item)

                for s in series_group:
                    label = s.description or "(no description)"
                    if s.series_number is not None:
                        label = f"#{s.series_number}  {label}"
                    ds = s.slices[0] if s.slices else None
                    detail = self._short_detail(ds)
                    if detail:
                        label += f"\n{detail}"
                    item = QTreeWidgetItem([label, str(s.num_slices)])
                    item.setIcon(0, _series_icon(s.modality,
                                                 self._thumbnails.get(s.series_uid)))
                    item.setData(0, ROLE_SERIES_UID, s.series_uid)
                    item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                    tooltip = (dicom_info.sequence_tooltip(
                        ds, s.description, s.num_slices) if ds is not None else "")
                    if s.series_number is not None:
                        tooltip = f"Series #{s.series_number}\n{tooltip}"
                    item.setToolTip(0, tooltip)
                    item.setToolTip(1, tooltip)
                    study_item.addChild(item)
                    self._items_by_uid[s.series_uid] = item
                    self._modality_by_uid[s.series_uid] = s.modality
                    if first_series_item is None:
                        first_series_item = item
                    if s.series_uid == select_uid:
                        target_item = item

        self.expandAll()
        self.blockSignals(False)

        self._start_thumbnails(series_list)

        selected = target_item or first_series_item
        if selected is not None:
            # emit=True: currentItemChanged → series_selected 로 시리즈 표시
            self.blockSignals(not emit)
            self.setCurrentItem(selected)
            self.blockSignals(False)

    @staticmethod
    def _short_detail(ds):
        """트리 두 번째 줄: 방향 · 시퀀스 요약 (예: 'Axial · 2D FSE')"""
        if ds is None:
            return ""
        parts = [dicom_info.orientation_name(ds)]
        if dicom_info.tag(ds, "Modality") == "MR":
            seq = dicom_info.mr_sequence_type(ds).split(" [")[0]
            parts.append(seq)
        return " · ".join(p for p in parts if p)

    # ─── 썸네일 ───

    def use_external_thumbnails(self, enabled=True):
        self._external_thumbnails = enabled

    def set_thumbnail(self, uid, image):
        self._on_thumbnail_ready(uid, image)

    def _start_thumbnails(self, series_list):
        if self._external_thumbnails:
            return
        if self._thumb_worker is not None:
            self._thumb_worker.cancel()
        pending = [s for s in series_list if s.series_uid not in self._thumbnails]
        if not pending:
            self._thumb_worker = None
            return
        worker = ThumbnailWorker(pending, self)
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
        item = self._items_by_uid.get(uid)
        if item is not None:
            item.setIcon(0, _series_icon(self._modality_by_uid.get(uid), image))

    def thumbnail_for(self, uid):
        return self._thumbnails.get(uid)

    def shutdown(self):
        """앱 종료 시 썸네일 스레드 정리"""
        if self._thumb_worker is not None:
            self._thumb_worker.cancel()
            self._thumb_worker.wait(3000)

    def _series_uid_at(self, pos):
        item = self.itemAt(pos)
        return item.data(0, ROLE_SERIES_UID) if item is not None else None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self.currentItem()
            uid = item.data(0, ROLE_SERIES_UID) if item is not None else None
            if uid:
                self.series_activated.emit(uid)
                return
        super().keyPressEvent(event)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        uid = current.data(0, ROLE_SERIES_UID)
        if uid:
            self.series_selected.emit(uid)
