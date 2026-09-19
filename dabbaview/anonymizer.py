# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 익명화 모듈
카테고리별 선택적 익명화 (개인정보 / 기관정보 / 검사정보 / 촬영 파라미터)
+ Private 태그 제거, UID 재생성, 프리셋
"""
import os
import copy

import pydicom
from pydicom.uid import generate_uid

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                              QLineEdit, QPushButton, QCheckBox, QWidget,
                              QFileDialog, QProgressBar, QMessageBox,
                              QGroupBox, QGridLayout, QTreeWidget,
                              QTreeWidgetItem, QHeaderView, QScrollArea,
                              QSplitter, QApplication)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush, QFont

# 처리 방식
REPLACE = "replace"   # 입력한 값으로 대체 (환자명/ID)
EMPTY = "empty"       # 값 비움 (필수(Type 2) 태그 - 태그는 남김)
DELETE = "delete"     # 태그 삭제

# (카테고리 key, 제목, [(항목 key, 라벨, [(태그, 처리)])])
CATEGORIES = [
    ("personal", "개인정보", [
        ("patient_name", "환자명 (PatientName)", [("PatientName", REPLACE)]),
        ("patient_id", "환자 ID (PatientID)", [("PatientID", REPLACE)]),
        ("birth_date", "생년월일 (PatientBirthDate)",
         [("PatientBirthDate", EMPTY), ("PatientBirthTime", DELETE)]),
        ("age", "나이 (PatientAge)", [("PatientAge", DELETE)]),
        ("sex", "성별 (PatientSex)", [("PatientSex", EMPTY)]),
        ("contact", "주소/전화번호",
         [("PatientAddress", DELETE), ("PatientTelephoneNumbers", DELETE),
          ("CountryOfResidence", DELETE), ("RegionOfResidence", DELETE),
          ("PatientMotherBirthName", DELETE)]),
        ("other_ids", "기타 환자 식별정보 (OtherPatientIDs 등)",
         [("OtherPatientIDs", DELETE), ("OtherPatientNames", DELETE),
          ("OtherPatientIDsSequence", DELETE), ("PatientBirthName", DELETE),
          ("MedicalRecordLocator", DELETE), ("PatientInsurancePlanCodeSequence", DELETE),
          ("MilitaryRank", DELETE), ("PatientComments", DELETE),
          ("IssuerOfPatientID", DELETE), ("AdmissionID", DELETE),
          ("IssuerOfAdmissionID", DELETE)]),
    ]),
    ("institution", "기관정보", [
        ("institution", "병원명 (InstitutionName/Address)",
         [("InstitutionName", DELETE), ("InstitutionAddress", DELETE),
          ("InstitutionalDepartmentName", DELETE), ("StationName", DELETE)]),
        ("physicians", "의뢰의/촬영자/기록의 이름",
         [("ReferringPhysicianName", EMPTY), ("PerformingPhysicianName", DELETE),
          ("OperatorsName", DELETE), ("PhysiciansOfRecord", DELETE),
          ("NameOfPhysiciansReadingStudy", DELETE), ("RequestingPhysician", DELETE),
          ("ReferringPhysicianAddress", DELETE),
          ("ReferringPhysicianTelephoneNumbers", DELETE)]),
        ("accession", "Accession Number · 오더 정보",
         [("AccessionNumber", EMPTY), ("StudyID", EMPTY), ("RequestedProcedureID", DELETE),
          ("PerformedProcedureStepID", DELETE), ("ScheduledProcedureStepID", DELETE),
          ("RequestAttributesSequence", DELETE), ("ReferencedStudySequence", DELETE),
          ("ReferencedPatientSequence", DELETE)]),
    ]),
    ("study", "검사정보", [
        ("study_desc", "Study Description",
         [("StudyDescription", DELETE), ("RequestedProcedureDescription", DELETE),
          ("AdmittingDiagnosesDescription", DELETE)]),
        ("series_desc", "Series Description",
         [("SeriesDescription", DELETE), ("ProtocolName", DELETE)]),
        ("dates", "Study/Series Date/Time",
         [("StudyDate", EMPTY), ("StudyTime", EMPTY), ("SeriesDate", DELETE),
          ("SeriesTime", DELETE), ("AcquisitionDate", DELETE),
          ("AcquisitionTime", DELETE), ("AcquisitionDateTime", DELETE),
          ("ContentDate", DELETE), ("ContentTime", DELETE)]),
    ]),
    ("acquisition", "촬영 파라미터", [
        ("timing", "TR, TE, TI, FA, ETL, NEX, Bandwidth",
         [("RepetitionTime", DELETE), ("EchoTime", DELETE), ("InversionTime", DELETE),
          ("FlipAngle", DELETE), ("EchoTrainLength", DELETE),
          ("NumberOfAverages", DELETE), ("PixelBandwidth", DELETE)]),
        ("geometry", "Slice Thickness, Spacing, FoV, Matrix",
         [("SliceThickness", DELETE), ("SpacingBetweenSlices", DELETE),
          ("ReconstructionDiameter", DELETE), ("PercentPhaseFieldOfView", DELETE),
          ("AcquisitionMatrix", DELETE)]),
        ("hardware", "코일, 시퀀스명, 장비 모델",
         [("ReceiveCoilName", DELETE), ("TransmitCoilName", DELETE),
          ("SequenceName", DELETE), ("ScanningSequence", DELETE),
          ("SequenceVariant", DELETE), ("ScanOptions", DELETE),
          ("ManufacturerModelName", DELETE), ("DeviceSerialNumber", DELETE),
          ("SoftwareVersions", DELETE)]),
    ]),
]

ALL_ITEMS = [item for _, _, items in CATEGORIES for item, _, _ in items]
_ITEM_TAGS = {item: tags for _, _, items in CATEGORIES for item, _, tags in items}

UID_TAGS = ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
            "FrameOfReferenceUID")
# 시퀀스 안에서 원본 영상·검사를 가리키는 UID (새 UID로 바꿀 때 함께 바꿔 연결 유지)
REFERENCE_UID_TAGS = UID_TAGS + ("ReferencedSOPInstanceUID", "ReferencedFrameOfReferenceUID",
                                 "RelatedFrameOfReferenceUID")


def _items_of(*categories):
    return {item for key, _, items in CATEGORIES if key in categories
            for item, _, _ in items}


# 프리셋: (라벨, 설명, 선택 항목, Private 태그 제거, UID 재생성)
PRESETS = {
    "minimal": ("최소 익명화", "개인정보만 제거, 나머지 보존",
                _items_of("personal"), False, False),
    "standard": ("표준 익명화", "개인정보 + 기관정보 제거, Private 태그 제거",
                 _items_of("personal", "institution"), True, False),
    "full": ("완전 익명화", "전부 제거 + Private 태그 제거 + UID 재생성",
             set(ALL_ITEMS), True, True),
    "research": ("연구용", "개인정보·기관정보 제거, 검사정보·촬영 파라미터·Private 태그 보존,\n"
                 "UID 재생성 (원본 PACS와 연결 끊기)",
                 _items_of("personal", "institution"), False, True),
}
DEFAULT_PRESET = "standard"


class AnonymizeOptions:
    """익명화 설정 - 선택 항목 + 대체값 + Private/UID 옵션"""

    def __init__(self, items=None, patient_name="ANONYMOUS", patient_id="ANON000",
                 remove_private=True, new_uids=False):
        self.items = set(PRESETS[DEFAULT_PRESET][2] if items is None else items)
        self.patient_name = patient_name
        self.patient_id = patient_id
        self.remove_private = remove_private
        self.new_uids = new_uids

    def planned_changes(self, ds):
        """ds에 적용될 변경 [(item, 태그, 처리, 원래 값, 새 값)] - 데이터셋에 있는 태그만"""
        changes = []
        for item in ALL_ITEMS:
            if item not in self.items:
                continue
            for keyword, action in _ITEM_TAGS[item]:
                if keyword not in ds:
                    continue
                if action == REPLACE:
                    new = (self.patient_name if keyword == "PatientName"
                           else self.patient_id)
                else:
                    new = "" if action == EMPTY else None
                changes.append((item, keyword, action, ds.data_element(keyword), new))
        return changes


class UIDMapper:
    """원본 UID → 새 UID. 같은 시리즈/검사의 파일들이 같은 새 UID를 갖도록 공유"""

    def __init__(self):
        self._map = {}

    def __call__(self, uid):
        uid = str(uid)
        if uid not in self._map:
            self._map[uid] = generate_uid()
        return self._map[uid]


def anonymize_dataset(ds, options=None, uid_mapper=None, **kwargs):
    """단일 DICOM 데이터셋 익명화 (원본은 그대로, 사본 반환)

    options: AnonymizeOptions. 없으면 kwargs(patient_name, patient_id,
    remove_private, new_uids)로 표준 프리셋 설정을 만든다.
    """
    if options is None:
        options = AnonymizeOptions(**kwargs)
    ds_anon = copy.deepcopy(ds)
    _apply(ds_anon, options)       # 최상위 + 시퀀스 안 (오더·참조 정보에 환자 식별자가 들어 있을 수 있음)

    if options.remove_private:
        ds_anon.remove_private_tags()

    if options.new_uids:
        mapper = uid_mapper or UIDMapper()
        _map_uids(ds_anon, mapper)
        meta = getattr(ds_anon, "file_meta", None)
        if meta is not None and "MediaStorageSOPInstanceUID" in meta \
                and "SOPInstanceUID" in ds_anon:
            meta.MediaStorageSOPInstanceUID = ds_anon.SOPInstanceUID

    if options.items or options.remove_private:
        ds_anon.PatientIdentityRemoved = "YES"
    return ds_anon


def _apply(dataset, options, depth=0):
    for _, keyword, action, _, new in options.planned_changes(dataset):
        if action == DELETE:
            del dataset[keyword]
        else:
            setattr(dataset, keyword, new)
    if depth > 8:
        return
    for elem in list(dataset):
        if elem.VR == "SQ" and elem.tag in dataset:
            for item in elem.value:
                _apply(item, options, depth + 1)
                if options.remove_private:
                    item.remove_private_tags()


def _map_uids(dataset, mapper, depth=0):
    for keyword in REFERENCE_UID_TAGS:
        if keyword in dataset and getattr(dataset, keyword):
            setattr(dataset, keyword, mapper(getattr(dataset, keyword)))
    if depth > 8:
        return
    for elem in dataset:
        if elem.VR == "SQ":
            for item in elem.value:
                _map_uids(item, mapper, depth + 1)


def anonymize_file(input_path, output_path, **kwargs):
    """DICOM 파일 익명화 후 저장"""
    ds = pydicom.dcmread(input_path, force=True)
    ds_anon = anonymize_dataset(ds, **kwargs)
    ds_anon.save_as(output_path, enforce_file_format=True)


def _format_value(element):
    if element.VR == "SQ":
        return f"(Sequence, {len(element.value)} item)"
    text = str(element.value)
    return text if len(text) <= 60 else text[:57] + "..."


class AnonymizeDialog(QDialog):
    """익명화 다이얼로그 - 카테고리별 선택 + 프리셋 + 실시간 미리보기"""

    CHANGED_BG = QColor(255, 196, 0, 70)

    def __init__(self, series=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DICOM Anonymize")
        self.resize(1000, 680)
        self._series = series
        self._item_checks = {}      # item → QCheckBox
        self._category_checks = {}  # category → QCheckBox (tristate)
        self._updating = False
        self._init_ui()
        self.apply_preset(DEFAULT_PRESET)

    # ─── UI ───

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # 프리셋
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("프리셋:"))
        for key, (label, desc, *_rest) in PRESETS.items():
            btn = QPushButton(label)
            btn.setToolTip(desc)
            btn.clicked.connect(lambda _, k=key: self.apply_preset(k))
            preset_row.addWidget(btn)
        preset_row.addStretch()
        layout.addLayout(preset_row)
        self._preset_label = QLabel()
        self._preset_label.setStyleSheet("color: #888;")
        layout.addWidget(self._preset_label)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_options())
        splitter.addWidget(self._build_preview())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 600])
        layout.addWidget(splitter, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        btn_layout = QHBoxLayout()
        self._summary = QLabel()
        btn_layout.addWidget(self._summary)
        btn_layout.addStretch()
        save_btn = QPushButton("Save Anonymized Files...")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save_anonymized)
        btn_layout.addWidget(save_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _build_options(self):
        panel = QWidget()
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)

        # 대체값
        replace_group = QGroupBox("대체값")
        grid = QGridLayout(replace_group)
        grid.addWidget(QLabel("Patient Name:"), 0, 0)
        self._name_input = QLineEdit("ANONYMOUS")
        grid.addWidget(self._name_input, 0, 1)
        grid.addWidget(QLabel("Patient ID:"), 1, 0)
        self._id_input = QLineEdit("ANON000")
        grid.addWidget(self._id_input, 1, 1)
        self._name_input.textChanged.connect(self._refresh_preview)
        self._id_input.textChanged.connect(self._refresh_preview)
        vbox.addWidget(replace_group)

        # 카테고리 체크박스
        content = QWidget()
        cvbox = QVBoxLayout(content)
        cvbox.setContentsMargins(0, 0, 0, 0)
        for cat_key, title, items in CATEGORIES:
            group = QGroupBox()
            gl = QVBoxLayout(group)
            header = QCheckBox(title)
            header.setTristate(True)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            header.clicked.connect(lambda _, c=cat_key: self._on_category_clicked(c))
            self._category_checks[cat_key] = header
            gl.addWidget(header)
            for item, label, tags in items:
                cb = QCheckBox(label)
                cb.setToolTip("태그: " + ", ".join(t for t, _ in tags))
                cb.setStyleSheet("margin-left: 18px;")
                cb.toggled.connect(self._on_item_toggled)
                self._item_checks[item] = cb
                gl.addWidget(cb)
            cvbox.addWidget(group)

        extra = QGroupBox("기타")
        el = QVBoxLayout(extra)
        self._remove_private = QCheckBox("Private 태그 제거 (제조사 전용 태그)")
        self._remove_private.setToolTip("제조사 private 태그에는 환자 정보가 들어 있을 수 있지만,\n"
                                        "b-value 등 연구용 파라미터가 들어 있기도 합니다.")
        self._new_uids = QCheckBox("UID 재생성 (Study/Series/SOP/Frame of Reference)")
        self._new_uids.setToolTip("시리즈 안의 파일들은 같은 새 Study/Series UID를 공유합니다.")
        for cb in (self._remove_private, self._new_uids):
            cb.toggled.connect(self._on_item_toggled)
            el.addWidget(cb)
        cvbox.addWidget(extra)
        cvbox.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        vbox.addWidget(scroll, 1)
        return panel

    def _build_preview(self):
        group = QGroupBox("미리보기 (첫 번째 영상 기준)")
        vbox = QVBoxLayout(group)
        self._only_changed = QCheckBox("변경될 태그만 보기")
        self._only_changed.toggled.connect(self._refresh_preview)
        vbox.addWidget(self._only_changed)
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Tag", "Original", "Anonymized"])
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self._tree.setAlternatingRowColors(False)
        vbox.addWidget(self._tree)
        return group

    # ─── 옵션 ↔ 체크박스 ───

    def options(self):
        return AnonymizeOptions(
            items={i for i, cb in self._item_checks.items() if cb.isChecked()},
            patient_name=self._name_input.text(),
            patient_id=self._id_input.text(),
            remove_private=self._remove_private.isChecked(),
            new_uids=self._new_uids.isChecked())

    def apply_preset(self, key):
        label, desc, items, remove_private, new_uids = PRESETS[key]
        self._updating = True
        for item, cb in self._item_checks.items():
            cb.setChecked(item in items)
        self._remove_private.setChecked(remove_private)
        self._new_uids.setChecked(new_uids)
        self._updating = False
        self._preset_label.setText(f"{label}: {desc.replace(chr(10), ' ')}")
        self._on_item_toggled()

    def _on_category_clicked(self, category):
        header = self._category_checks[category]
        # 부분 선택 상태에서 클릭하면 전체 선택
        checked = header.checkState() != Qt.Unchecked
        items = [i for key, _, its in CATEGORIES if key == category for i, _, _ in its]
        self._updating = True
        for item in items:
            self._item_checks[item].setChecked(checked)
        self._updating = False
        self._on_item_toggled()

    def _on_item_toggled(self, *_):
        if self._updating:
            return
        for cat_key, _, items in CATEGORIES:
            states = [self._item_checks[i].isChecked() for i, _, _ in items]
            header = self._category_checks[cat_key]
            header.blockSignals(True)
            header.setCheckState(Qt.Checked if all(states) else
                                 Qt.Unchecked if not any(states) else Qt.PartiallyChecked)
            header.blockSignals(False)
        self._refresh_preview()

    # ─── 미리보기 ───

    def _preview_dataset(self):
        if not self._series or not self._series.slices:
            return None
        return self._series.slices[0]

    def _refresh_preview(self, *_):
        self._tree.clear()
        ds = self._preview_dataset()
        if ds is None:
            self._summary.setText("")
            return
        options = self.options()
        planned = {(c[0], c[1]): c for c in options.planned_changes(ds)}
        only_changed = self._only_changed.isChecked()
        changed_count = 0
        bold = QFont()
        bold.setBold(True)

        def add_row(parent, tag, original, anonymized, changed):
            row = QTreeWidgetItem(parent, [tag, original, anonymized])
            if changed:
                for col in range(3):
                    row.setBackground(col, QBrush(self.CHANGED_BG))
                row.setFont(2, bold)
            else:
                for col in range(3):
                    row.setForeground(col, QBrush(QColor(140, 140, 140)))
            return row

        for cat_key, title, items in CATEGORIES:
            category = QTreeWidgetItem([title, "", ""])
            category.setFirstColumnSpanned(True)
            category.setFont(0, bold)
            n_changed = 0
            for item, _, tags in items:
                for keyword, _ in tags:
                    if keyword not in ds:
                        continue
                    original = _format_value(ds.data_element(keyword))
                    change = planned.get((item, keyword))
                    if change is None:
                        if not only_changed:
                            add_row(category, keyword, original, original, False)
                        continue
                    action, new = change[2], change[4]
                    shown = ("(삭제)" if action == DELETE else
                             "(빈 값)" if new == "" else str(new))
                    add_row(category, keyword, original, shown, True)
                    n_changed += 1
            if category.childCount():
                category.setText(0, f"{title}  ({n_changed}개 변경)")
                self._tree.addTopLevelItem(category)
                category.setExpanded(True)
            changed_count += n_changed

        # Private 태그 / UID
        private = [el for el in ds if el.tag.is_private]
        uids = [k for k in UID_TAGS if k in ds]
        if private or uids:
            other = QTreeWidgetItem(["기타", "", ""])
            other.setFirstColumnSpanned(True)
            other.setFont(0, bold)
            if private and (options.remove_private or not only_changed):
                add_row(other, "Private 태그", f"{len(private)}개",
                        "(전부 삭제)" if options.remove_private else f"{len(private)}개",
                        options.remove_private)
                changed_count += len(private) if options.remove_private else 0
            for keyword in uids:
                if options.new_uids or not only_changed:
                    value = str(getattr(ds, keyword))
                    add_row(other, keyword, value,
                            "(새 UID)" if options.new_uids else value, options.new_uids)
                    changed_count += 1 if options.new_uids else 0
            if other.childCount():
                self._tree.addTopLevelItem(other)
                other.setExpanded(True)

        self._summary.setText(f"변경될 태그: {changed_count}개 · "
                              f"대상 파일: {self._series.num_slices}개")

    # ─── 저장 ───

    def _save_anonymized(self):
        if not self._series:
            return
        output_dir = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if not output_dir:
            return

        options = self.options()
        uid_mapper = UIDMapper()  # 시리즈 전체가 같은 새 Study/Series UID 공유
        self._progress.setVisible(True)
        self._progress.setMaximum(self._series.num_slices)

        saved, errors = 0, []
        for i in range(self._series.num_slices):
            try:
                # slices는 픽셀 없는 메타데이터라 저장용으로 전체 파일을 다시 읽음
                ds = self._series.get_full_dataset(i)
                ds_anon = anonymize_dataset(ds, options, uid_mapper)
                ds_anon.save_as(os.path.join(output_dir, f"ANON_{i:04d}.dcm"),
                                enforce_file_format=True)
                saved += 1
            except Exception as e:  # noqa: BLE001 - 한 장 실패해도 나머지는 저장
                errors.append(f"{i}: {e}")
            self._progress.setValue(i + 1)
            QApplication.processEvents()

        message = f"{saved}개 파일이 익명화되어 저장되었습니다.\n{output_dir}"
        if errors:
            message += f"\n\n실패 {len(errors)}개:\n" + "\n".join(errors[:5])
        QMessageBox.information(self, "Complete", message)
        self.accept()
