# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 익명화 모듈
환자 식별 정보를 제거하거나 대체
"""
import os
import copy
from datetime import datetime

import pydicom
from pydicom.uid import generate_uid

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                              QLineEdit, QPushButton, QCheckBox,
                              QFileDialog, QProgressBar, QMessageBox,
                              QGroupBox, QGridLayout, QTableWidget,
                              QTableWidgetItem, QHeaderView)
from PyQt5.QtCore import Qt


# 익명화 대상 태그 목록
ANONYMIZE_TAGS = {
    'PatientName': 'ANONYMOUS',
    'PatientID': 'ANON000',
    'PatientBirthDate': '19000101',
    'PatientSex': 'O',
    'PatientAge': '000Y',
    'PatientAddress': '',
    'PatientTelephoneNumbers': '',
    'InstitutionName': 'ANONYMOUS',
    'InstitutionAddress': '',
    'ReferringPhysicianName': 'ANONYMOUS',
    'PerformingPhysicianName': 'ANONYMOUS',
    'OperatorsName': 'ANONYMOUS',
    'PhysiciansOfRecord': '',
    'StudyDescription': '',
    'SeriesDescription': '',
    'AccessionNumber': '',
    'OtherPatientIDs': '',
    'OtherPatientNames': '',
    'PatientInsurancePlanCodeSequence': '',
}

# 제거할 Private 태그 그룹
PRIVATE_TAG_GROUPS = [0x0009, 0x0019, 0x0029, 0x0039, 0x0049,
                       0x0051, 0x0071, 0x0073, 0x7FE1]


def anonymize_dataset(ds, patient_name="ANONYMOUS", patient_id="ANON000",
                       remove_private=True, new_uids=False):
    """단일 DICOM 데이터셋 익명화"""
    ds_anon = copy.deepcopy(ds)

    # 표준 태그 익명화
    for tag_name, default_value in ANONYMIZE_TAGS.items():
        if hasattr(ds_anon, tag_name):
            if tag_name == 'PatientName':
                setattr(ds_anon, tag_name, patient_name)
            elif tag_name == 'PatientID':
                setattr(ds_anon, tag_name, patient_id)
            else:
                setattr(ds_anon, tag_name, default_value)

    # Private 태그 제거
    if remove_private:
        ds_anon.remove_private_tags()

    # UID 재생성
    if new_uids:
        if hasattr(ds_anon, 'StudyInstanceUID'):
            ds_anon.StudyInstanceUID = generate_uid()
        if hasattr(ds_anon, 'SeriesInstanceUID'):
            ds_anon.SeriesInstanceUID = generate_uid()
        if hasattr(ds_anon, 'SOPInstanceUID'):
            ds_anon.SOPInstanceUID = generate_uid()

    return ds_anon


def anonymize_file(input_path, output_path, **kwargs):
    """DICOM 파일 익명화 후 저장"""
    ds = pydicom.dcmread(input_path, force=True)
    ds_anon = anonymize_dataset(ds, **kwargs)
    ds_anon.save_as(output_path)


class AnonymizeDialog(QDialog):
    """익명화 다이얼로그"""

    def __init__(self, series=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DICOM Anonymize")
        self.resize(500, 500)
        self._series = series
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # 익명화 설정
        settings_group = QGroupBox("Anonymization Settings")
        settings_layout = QGridLayout()

        settings_layout.addWidget(QLabel("Patient Name:"), 0, 0)
        self._name_input = QLineEdit("ANONYMOUS")
        settings_layout.addWidget(self._name_input, 0, 1)

        settings_layout.addWidget(QLabel("Patient ID:"), 1, 0)
        self._id_input = QLineEdit("ANON000")
        settings_layout.addWidget(self._id_input, 1, 1)

        self._remove_private = QCheckBox("Remove private tags")
        self._remove_private.setChecked(True)
        settings_layout.addWidget(self._remove_private, 2, 0, 1, 2)

        self._new_uids = QCheckBox("Generate new UIDs")
        self._new_uids.setChecked(False)
        settings_layout.addWidget(self._new_uids, 3, 0, 1, 2)

        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        # 태그 미리보기
        preview_group = QGroupBox("Tags to be anonymized")
        preview_layout = QVBoxLayout()
        self._tag_table = QTableWidget()
        self._tag_table.setColumnCount(3)
        self._tag_table.setHorizontalHeaderLabels(
            ["Tag", "Original", "Anonymized"])
        self._tag_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self._populate_preview()
        preview_layout.addWidget(self._tag_table)
        preview_group.setLayout(preview_layout)
        layout.addWidget(preview_group)

        # 진행 바
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # 버튼
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save Anonymized Files...")
        save_btn.clicked.connect(self._save_anonymized)
        btn_layout.addStretch()
        btn_layout.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _populate_preview(self):
        if not self._series or not self._series.slices:
            return
        ds = self._series.slices[0]
        rows = []
        for tag_name in ANONYMIZE_TAGS:
            if hasattr(ds, tag_name):
                original = str(getattr(ds, tag_name))[:50]
                anon = ANONYMIZE_TAGS[tag_name]
                if tag_name == 'PatientName':
                    anon = self._name_input.text()
                elif tag_name == 'PatientID':
                    anon = self._id_input.text()
                rows.append((tag_name, original, anon))

        self._tag_table.setRowCount(len(rows))
        for i, (tag, orig, anon) in enumerate(rows):
            self._tag_table.setItem(i, 0, QTableWidgetItem(tag))
            self._tag_table.setItem(i, 1, QTableWidgetItem(orig))
            self._tag_table.setItem(i, 2, QTableWidgetItem(anon))

    def _save_anonymized(self):
        if not self._series:
            return

        output_dir = QFileDialog.getExistingDirectory(
            self, "Select Output Folder")
        if not output_dir:
            return

        patient_name = self._name_input.text()
        patient_id = self._id_input.text()
        remove_private = self._remove_private.isChecked()
        new_uids = self._new_uids.isChecked()

        self._progress.setVisible(True)
        self._progress.setMaximum(len(self._series.slices))

        saved = 0
        for i in range(self._series.num_slices):
            try:
                # slices는 픽셀 없는 메타데이터라 저장용으로 전체 파일을 다시 읽음
                ds = self._series.get_full_dataset(i)
                ds_anon = anonymize_dataset(
                    ds, patient_name=patient_name,
                    patient_id=patient_id,
                    remove_private=remove_private,
                    new_uids=new_uids)

                filename = f"ANON_{i:04d}.dcm"
                output_path = os.path.join(output_dir, filename)
                ds_anon.save_as(output_path)
                saved += 1
            except Exception as e:
                print(f"Error anonymizing slice {i}: {e}")

            self._progress.setValue(i + 1)

        QMessageBox.information(
            self, "Complete",
            f"{saved}개 파일이 익명화되어 저장되었습니다.\n{output_dir}")
        self.accept()
