# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""스터디·시리즈 이름 바꾸기 / 환자 이름·ID 바꾸기 대화상자"""
from PyQt5.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                             QInputDialog, QLabel, QLineEdit, QMessageBox, QRadioButton,
                             QVBoxLayout)


class RenameDialog(QDialog):
    """StudyDescription / SeriesDescription 변경"""

    def __init__(self, title, field, current, n_files, backup, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        now = QLineEdit(current or "(비어 있음)")
        now.setReadOnly(True)
        form.addRow(f"현재 {field}:", now)
        self.name = QLineEdit(current or "")
        self.name.selectAll()
        form.addRow("새 이름:", self.name)
        self.modify = QCheckBox(f"DICOM 파일 원본도 수정 ({n_files:,}개 파일의 {field} 태그)")
        self.modify.setChecked(True)
        form.addRow(self.modify)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #999;")
        form.addRow(self.note)
        self._backup = backup
        self.modify.toggled.connect(self._sync)
        self._sync()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _sync(self):
        if self.modify.isChecked():
            self.note.setText("원본 파일을 바꿉니다. " + (
                "수정 전 원본을 <파일>.bak 으로 백업합니다 (Settings에서 끄기)."
                if self._backup else "백업이 꺼져 있습니다 (Settings에서 켜기)."))
        else:
            self.note.setText("원본 파일은 그대로 두고 Library에서만 표시 이름을 바꿉니다 "
                              "(이 스터디는 Library에 추가됩니다).")

    def _accept(self):
        if not self.name.text().strip():
            QMessageBox.information(self, self.windowTitle(), "새 이름을 입력하세요.")
            return
        if self.modify.isChecked() and QMessageBox.warning(
                self, self.windowTitle(),
                "원본 DICOM 파일이 수정됩니다. 되돌릴 수 없습니다. 계속하시겠습니까?"
                + ("\n\n(.bak 백업을 만듭니다)" if self._backup else "\n\n⚠ 백업 없이 수정합니다."),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.accept()

    def result_value(self):
        return self.name.text().strip(), self.modify.isChecked()


class PatientEditDialog(QDialog):
    """PatientName / PatientID 변경 - 익명화와 구분되는 별도 확인"""

    def __init__(self, name, pid, n_study_files, n_patient_files, n_studies, backup, parent=None):
        super().__init__(parent)
        self.setWindowTitle("환자 이름·ID 바꾸기")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        warn = QLabel("⚠ 환자 식별 정보를 다른 값으로 바꿉니다. <b>익명화가 아닙니다</b> — 개인정보를 "
                      "지우려면 Tools → Anonymize를 쓰세요. 잘못 바꾸면 다른 환자의 영상과 섞일 수 있습니다.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ffb347;")
        layout.addWidget(warn)
        form = QFormLayout()
        self.name = QLineEdit(name or "")
        self.pid = QLineEdit(pid or "")
        form.addRow("현재:", QLabel(f"{name or '-'}   (ID {pid or '-'})"))
        form.addRow("새 PatientName:", self.name)
        form.addRow("새 PatientID:", self.pid)
        layout.addLayout(form)
        self.scope_study = QRadioButton(f"이 스터디만 ({n_study_files:,}개 파일)")
        self.scope_patient = QRadioButton(f"이 환자의 불러온 모든 스터디 ({n_studies}개, {n_patient_files:,}개 파일)")
        self.scope_study.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.scope_study)
        group.addButton(self.scope_patient)
        layout.addWidget(self.scope_study)
        layout.addWidget(self.scope_patient)
        self.modify = QCheckBox("DICOM 파일 원본도 수정")
        self.modify.setChecked(True)
        layout.addWidget(self.modify)
        self._backup = backup
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._original = (name or "", pid or "")

    def _accept(self):
        name, pid = self.name.text().strip(), self.pid.text().strip()
        if (name, pid) == self._original:
            self.reject()
            return
        if not name or not pid:
            QMessageBox.information(self, self.windowTitle(), "이름과 ID를 모두 입력하세요.")
            return
        if self.modify.isChecked():
            if QMessageBox.warning(
                    self, self.windowTitle(),
                    "원본 DICOM 파일의 환자 이름·ID가 수정됩니다. 되돌릴 수 없습니다. 계속하시겠습니까?"
                    + ("\n\n(.bak 백업을 만듭니다)" if self._backup else "\n\n⚠ 백업 없이 수정합니다."),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            typed, ok = QInputDialog.getText(self, "한 번 더 확인",
                                             f"확인을 위해 새 PatientID를 그대로 입력하세요:\n{pid}")
            if not ok or typed.strip() != pid:
                QMessageBox.information(self, self.windowTitle(), "입력한 ID가 달라 취소했습니다.")
                return
        self.accept()

    def result_value(self):
        return (self.name.text().strip(), self.pid.text().strip(),
                "patient" if self.scope_patient.isChecked() else "study", self.modify.isChecked())

