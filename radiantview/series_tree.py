"""
시리즈 목록 트리: Patient → Study → Series
"""
from PyQt5.QtWidgets import (QTreeWidget, QTreeWidgetItem, QHeaderView,
                             QAbstractItemView)
from PyQt5.QtCore import Qt, QSize, QRectF, QMimeData, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap


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

ROLE_SERIES_UID = Qt.UserRole

# 트리 → 뷰포트 드래그 앤 드롭용 MIME 타입 (데이터: SeriesInstanceUID, utf-8)
SERIES_MIME_TYPE = "application/x-radiantview-series-uid"


def _badge_icon(modality, _cache={}):
    """모달리티 약어가 적힌 색상 배지 아이콘 (HiDPI 대응)"""
    modality = (modality or '?')[:3]
    if modality in _cache:
        return _cache[modality]
    scale = 2
    pixmap = QPixmap(BADGE_SIZE * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(MODALITY_COLORS.get(modality, DEFAULT_MODALITY_COLOR)))
    rect = QRectF(0, 0, BADGE_SIZE.width(), BADGE_SIZE.height())
    p.drawRoundedRect(rect, 4, 4)
    font = QFont()
    font.setPixelSize(10)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor('white'))
    p.drawText(rect, Qt.AlignCenter, modality)
    p.end()
    icon = QIcon(pixmap)
    _cache[modality] = icon
    return icon


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


class SeriesTreeWidget(QTreeWidget):
    """Patient → Study → Series 계층 트리"""

    series_selected = pyqtSignal(str)  # SeriesInstanceUID

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Series", "Images"])
        self.setIconSize(BADGE_SIZE)
        self.setUniformRowHeights(True)
        self.setIndentation(14)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.currentItemChanged.connect(self._on_current_item_changed)
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

    def populate(self, series_list, select_uid=None):
        """트리를 다시 구성하고 전부 펼친 뒤 시리즈 선택

        select_uid가 있으면 그 시리즈, 없으면 첫 시리즈 선택.
        """
        self.blockSignals(True)
        self.clear()
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
                    item = QTreeWidgetItem([label, str(s.num_slices)])
                    item.setIcon(0, _badge_icon(s.modality))
                    item.setData(0, ROLE_SERIES_UID, s.series_uid)
                    item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                    item.setToolTip(
                        0, f"{s.modality or '?'}  Series #{s.series_number if s.series_number is not None else '-'}\n"
                           f"{s.description or '(no description)'}\n"
                           f"{s.num_slices} images")
                    study_item.addChild(item)
                    if first_series_item is None:
                        first_series_item = item
                    if s.series_uid == select_uid:
                        target_item = item

        self.expandAll()
        self.blockSignals(False)

        selected = target_item or first_series_item
        if selected is not None:
            # currentItemChanged → series_selected 로 시리즈 표시
            self.setCurrentItem(selected)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        uid = current.data(0, ROLE_SERIES_UID)
        if uid:
            self.series_selected.emit(uid)
