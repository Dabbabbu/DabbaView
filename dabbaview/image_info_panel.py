# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Image 정보 패널 (GE AW의 Image 버튼): 현재 영상의 시퀀스/획득 상세 정보
"""
from PyQt5.QtWidgets import (QDockWidget, QTableWidget, QTableWidgetItem,
                             QHeaderView, QAbstractItemView)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont

from . import dicom_info


def image_info_rows(ds, image_index, num_images):
    """[(섹션, 라벨, 값)] 목록"""
    t = dicom_info.tag
    rows = []

    def add(section, label, value):
        if value not in (None, ""):
            rows.append((section, label, value))

    add("Patient", "Name", dicom_info.format_person_name(t(ds, "PatientName")))
    add("Patient", "ID", t(ds, "PatientID"))
    add("Patient", "Sex / Age",
        " / ".join(v for v in (t(ds, "PatientSex"), t(ds, "PatientAge")) if v))
    weight = dicom_info.tag_float(ds, "PatientWeight")
    add("Patient", "Weight", f"{dicom_info.fmt_num(weight)} kg" if weight else "")

    add("Study", "Institution", t(ds, "InstitutionName"))
    add("Study", "Date", f"{dicom_info.fmt_date(t(ds, 'StudyDate'))} "
                         f"{dicom_info.fmt_time(t(ds, 'StudyTime'))}".strip())
    add("Study", "Description", t(ds, "StudyDescription"))
    add("Study", "Accession", t(ds, "AccessionNumber"))

    add("Series", "Number", t(ds, "SeriesNumber"))
    add("Series", "Description", t(ds, "SeriesDescription"))
    add("Series", "Protocol", t(ds, "ProtocolName"))
    add("Series", "Body Part", t(ds, "BodyPartExamined"))
    add("Series", "Patient Position", t(ds, "PatientPosition"))
    add("Series", "Scanner", " ".join(v for v in (
        t(ds, "Manufacturer"), t(ds, "ManufacturerModelName")) if v))
    add("Series", "Station", t(ds, "StationName"))
    add("Series", "Software", t(ds, "SoftwareVersions"))

    for label, value in dicom_info.sequence_summary(ds):
        add("Acquisition", label, value)

    add("Image", "Image", f"{image_index + 1} / {num_images}"
                          f"  (Instance {t(ds, 'InstanceNumber', '-')})")
    add("Image", "Slice Position", dicom_info.slice_position_text(ds))
    try:
        ipp = ", ".join(f"{float(v):.2f}" for v in ds.ImagePositionPatient)
        add("Image", "Position (LPS mm)", ipp)
    except (AttributeError, TypeError, ValueError):
        pass
    add("Image", "Acquisition Time", dicom_info.fmt_time(t(ds, "AcquisitionTime")))
    add("Image", "Image Type", "\\".join(str(v) for v in getattr(ds, "ImageType", [])))
    return rows


class ImageInfoPanel(QDockWidget):
    """현재 뷰포트 영상의 상세 정보 (슬라이스/시리즈가 바뀌면 갱신)"""

    def __init__(self, parent=None):
        super().__init__("Image Info", parent)
        self.setObjectName("ImageInfoPanel")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Item", "Value"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setWordWrap(True)
        self.setWidget(self._table)
        self.setMinimumWidth(280)

    def show_image(self, ds, image_index=0, num_images=0):
        table = self._table
        table.setRowCount(0)
        if ds is None:
            return
        section_font = QFont()
        section_font.setBold(True)
        last_section = None
        for section, label, value in image_info_rows(ds, image_index, num_images):
            if section != last_section:
                row = table.rowCount()
                table.insertRow(row)
                header = QTableWidgetItem(section)
                header.setFont(section_font)
                header.setForeground(QColor("#4fc1ff"))
                table.setItem(row, 0, header)
                table.setSpan(row, 0, 1, 2)
                last_section = section
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(label))
            table.setItem(row, 1, QTableWidgetItem(str(value)))
        table.resizeRowsToContents()

    def rows_text(self):
        """테스트/디버그용: 표 내용을 문자열 목록으로"""
        out = []
        for r in range(self._table.rowCount()):
            a = self._table.item(r, 0)
            b = self._table.item(r, 1)
            out.append(f"{a.text() if a else ''}: {b.text() if b else ''}")
        return out
