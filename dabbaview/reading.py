# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Reading(기록) 팝업 - INFINITT PACS 기록 창 형태

- 타이틀: 모달리티, 환자명, ID, 성별, 검사일시, Study Description, Body Part, Clinical Info
- 본문: 기록 편집기 ('====== [Conclusion] =======' 구분선으로 결론 구분)
- 서명: Creator / Approver / Approver2 / My Comment
- 하단: Study Comment, Exam Date(상태), Report Date
- 버튼: Edit, Import, JSON 열기/저장, Copy, Print, Save, Approve, Close
- 탭: Report | Series(시퀀스 요약) | 가져온/자동 매칭된 기록 파일(텍스트·이미지·PDF·SR)

기록은 StudyInstanceUID별 JSON으로 저장 (이 컴퓨터의 앱 데이터 폴더).
"""
import json
import os
import re
from datetime import datetime

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                             QTabWidget, QWidget, QScrollArea, QFileDialog,
                             QMessageBox, QApplication, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView)
from PyQt5.QtCore import Qt, QObject, QFileSystemWatcher, QTimer, pyqtSignal, QStandardPaths
from PyQt5.QtGui import (QPixmap, QSyntaxHighlighter, QTextCharFormat, QColor,
                         QFont, QTextDocument)

from . import dicom_info
from . import report_import as ri

CONCLUSION_DIVIDER = "====== [Conclusion] ======="
_DIVIDER_RE = re.compile(r"^\s*=+\s*\[\s*conclusion\s*\]\s*=+\s*$", re.IGNORECASE | re.MULTILINE)
REPORT_TEMPLATE = f"\n\n{CONCLUSION_DIVIDER}\n1. "


def default_reports_dir():
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return os.path.join(base or os.path.expanduser("~/.dabbaview"), "reports")


def split_report(text):
    """본문 → (소견, 결론). 구분선이 없으면 결론은 ''"""
    match = _DIVIDER_RE.search(text)
    if not match:
        return text.strip(), ""
    return text[:match.start()].strip(), text[match.end():].strip()


def study_info(series_list):
    """검사 대표 정보 dict (첫 시리즈의 첫 영상 기준)"""
    ds = series_list[0].slices[0] if series_list and series_list[0].slices else None
    t = (lambda k: dicom_info.tag(ds, k)) if ds is not None else (lambda k: "")
    clinical = (t("AdditionalPatientHistory") or t("StudyComments")
                or t("ReasonForTheRequestedProcedure") or t("RequestedProcedureDescription"))
    modalities = sorted({s.modality for s in series_list if s.modality})
    return {
        "study_uid": series_list[0].study_uid if series_list else "",
        "patient_name": dicom_info.format_person_name(t("PatientName")),
        "patient_id": t("PatientID"),
        "sex": t("PatientSex"),
        "age": t("PatientAge"),
        "modality": "/".join(modalities),
        "study_date": t("StudyDate"),
        "exam_datetime": f"{dicom_info.fmt_date(t('StudyDate'))} "
                         f"{dicom_info.fmt_time(t('StudyTime'))}".strip(),
        "study_description": t("StudyDescription"),
        "body_part": t("BodyPartExamined"),
        "clinical_info": clinical,
        "study_comment": t("StudyComments") or t("AdditionalPatientHistory"),
        "accession": t("AccessionNumber"),
    }


class ReportStore:
    """StudyInstanceUID별 기록 JSON 저장소"""

    def __init__(self, directory=None):
        self.directory = directory or default_reports_dir()

    def _path(self, study_uid):
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", study_uid)
        return os.path.join(self.directory, f"{safe}.json")

    def load(self, study_uid):
        path = self._path(study_uid)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, report):
        os.makedirs(self.directory, exist_ok=True)
        path = self._path(report["study_uid"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 저장 중 중단돼도 기존 파일은 보존
        return path


class ReportLibrary(QObject):
    """기록 폴더를 감시해서 불러온 검사와 파일을 자동 매칭"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folder = ""
        self._studies = []
        self._matches = {}
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._schedule_rescan)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)  # 파일 복사가 끝날 때까지 잠시 대기
        self._timer.timeout.connect(self.rescan)

    @property
    def folder(self):
        return self._folder

    def set_folder(self, folder):
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        self._folder = folder or ""
        if self._folder and os.path.isdir(self._folder):
            dirs = [self._folder] + [os.path.join(r, d) for r, ds, _ in os.walk(self._folder)
                                     for d in ds][:200]
            self._watcher.addPaths(dirs)
        self.rescan()

    def set_studies(self, studies):
        """studies: [{'study_uid', 'patient_id', 'study_date'}]"""
        self._studies = list(studies)
        self.rescan()

    def _schedule_rescan(self, *_):
        self._timer.start()

    def rescan(self):
        self._matches = ri.scan_folder(self._folder, self._studies)
        # 새로 생긴 하위 폴더도 감시
        if self._folder and os.path.isdir(self._folder):
            watched = set(self._watcher.directories())
            for root, dirs, _ in os.walk(self._folder):
                for d in dirs:
                    path = os.path.join(root, d)
                    if path not in watched and len(watched) < 200:
                        self._watcher.addPath(path)
                        watched.add(path)
        self.changed.emit()

    def files_for(self, study_uid):
        return list(self._matches.get(study_uid, []))


class _DividerHighlighter(QSyntaxHighlighter):
    """'====== [Conclusion] =======' 줄 강조"""

    def highlightBlock(self, text):
        if _DIVIDER_RE.match(text):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#4fc1ff"))
            fmt.setFontWeight(QFont.Bold)
            self.setFormat(0, len(text), fmt)


class ImageViewer(QScrollArea):
    """기록 이미지/PDF 뷰어 (Ctrl+휠·버튼으로 확대/축소)"""

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self._images = images
        self._zoom = 1.0
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._labels = []
        for _ in images:
            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            self._layout.addWidget(label)
            self._labels.append(label)
        self.setWidget(self._container)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignCenter)
        QTimer.singleShot(0, self.fit_width)

    def fit_width(self):
        if self._images:
            avail = max(100, self.viewport().width() - 30)
            self.set_zoom(avail / max(1, self._images[0].width()))

    def set_zoom(self, zoom):
        self._zoom = max(0.1, min(8.0, zoom))
        for label, image in zip(self._labels, self._images):
            w = max(1, int(image.width() * self._zoom))
            label.setPixmap(QPixmap.fromImage(image).scaledToWidth(w, Qt.SmoothTransformation))

    @property
    def zoom(self):
        return self._zoom

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.set_zoom(self._zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15))
            return
        super().wheelEvent(event)


class ReadingDialog(QDialog):
    """기록 팝업 (검사 하나)"""

    saved = pyqtSignal(str)  # study_uid

    def __init__(self, series_list, store, library=None, app_settings=None,
                 all_studies=None, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowMinMaxButtonsHint, True)
        self.resize(760, 820)
        self._series_list = list(series_list)
        self._store = store
        self._library = library
        self._settings = app_settings
        self._all_studies = all_studies or []
        self._info = study_info(self._series_list)
        self._attachments = []   # 연결한 파일 경로 (수동 Import)
        self._file_tabs = {}     # path → 탭 위젯
        self._report = store.load(self._info["study_uid"]) or {}
        self._build_ui()
        self._load_report()
        if library is not None:
            library.changed.connect(self._refresh_matched_files)
        self._refresh_matched_files()

    # ─── UI ───
    def _build_ui(self):
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)
        # 붙인 파일 탭: 마우스를 올리면 X (Report · Series 탭은 닫지 않음)
        from .panel_close import HoverCloseTabs
        HoverCloseTabs(self._tabs.tabBar(), self._close_file_tab,
                       closable=lambda i: self._tabs.widget(i) in self._file_tabs.values())

        # Report 탭
        page = QWidget()
        form = QVBoxLayout(page)
        self._body = QPlainTextEdit()
        self._body.setFont(QFont("Menlo", 12) if QFont("Menlo").exactMatch() else QFont())
        self._highlighter = _DividerHighlighter(self._body.document())
        form.addWidget(self._body, 1)

        sign = QGridLayout()
        self._creator = QLineEdit()
        self._approver = QLineEdit()
        self._approver2 = QLineEdit()
        self._my_comment = QLineEdit()
        for row, (label, widget) in enumerate((("Creator:", self._creator),
                                               ("Approver:", self._approver),
                                               ("Approver2:", self._approver2),
                                               ("My Comment:", self._my_comment))):
            sign.addWidget(QLabel(label), row, 0)
            sign.addWidget(widget, row, 1)
        # 하단 정보도 서명란과 같은 표에 두어 정렬
        self._study_comment = QLabel()
        self._exam_date = QLabel()
        self._report_date = QLabel()
        for row, (label, widget) in enumerate((("Study Comment:", self._study_comment),
                                               ("Exam Date:", self._exam_date),
                                               ("Report Date:", self._report_date)), start=4):
            sign.addWidget(QLabel(label), row, 0)
            widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
            sign.addWidget(widget, row, 1)
        sign.setColumnStretch(1, 1)
        form.addLayout(sign)
        self._tabs.addTab(page, "Report")

        # Series 탭: 시리즈 목록 + 시퀀스 요약
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["#", "Series", "Images", "Parameters"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for s in self._series_list:
            ds = s.slices[0] if s.slices else None
            summary = ", ".join(f"{k} {v}" for k, v in dicom_info.sequence_summary(ds)
                                if k not in ("Modality",)) if ds is not None else ""
            r = table.rowCount()
            table.insertRow(r)
            for c, value in enumerate((s.series_number if s.series_number is not None else "",
                                       f"{s.modality}  {s.description}", s.num_slices, summary)):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        self._series_table = table
        self._tabs.addTab(table, f"Series ({len(self._series_list)})")

        # 버튼
        buttons = QHBoxLayout()
        self._btn_edit = QPushButton("Edit")
        self._btn_edit.setCheckable(True)
        self._btn_edit.toggled.connect(self._set_editable)
        self._btn_import = QPushButton("Import…")
        self._btn_import.clicked.connect(self._import_files)
        self._btn_json_open = QPushButton("JSON 열기…")
        self._btn_json_open.setToolTip("기록 JSON 파일 불러오기 (이 앱에서 저장한 형식)")
        self._btn_json_open.clicked.connect(self._open_json)
        self._btn_json_save = QPushButton("JSON 저장…")
        self._btn_json_save.setToolTip("현재 기록을 JSON 파일로 저장 (환자·검사 정보, 서명, 날짜 포함)")
        self._btn_json_save.clicked.connect(self._save_json)
        self._btn_copy = QPushButton("Copy")
        self._btn_copy.clicked.connect(self.copy_to_clipboard)
        self._btn_print = QPushButton("Print…")
        self._btn_print.clicked.connect(self._print)
        self._btn_save = QPushButton("Save")
        self._btn_save.clicked.connect(self.save)
        self._btn_approve = QPushButton("Approve")
        self._btn_approve.clicked.connect(self.approve)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        for b in (self._btn_edit, self._btn_import, self._btn_json_open, self._btn_json_save,
                  self._btn_copy, self._btn_print):
            buttons.addWidget(b)
        buttons.addStretch()
        for b in (self._btn_save, self._btn_approve, close):
            buttons.addWidget(b)
        layout.addLayout(buttons)

    # ─── 데이터 ───
    def _title(self):
        i = self._info
        parts = [i["modality"], i["patient_name"], i["patient_id"], i["sex"],
                 i["exam_datetime"], i["study_description"], i["body_part"], i["clinical_info"]]
        return ", ".join(p for p in parts if p)

    def _load_report(self):
        r = self._report
        self.setWindowTitle(self._title())
        self._body.setPlainText(r.get("text", REPORT_TEMPLATE))
        creator_default = (self._settings.report_creator() if self._settings else "")
        self._creator.setText(r.get("creator", creator_default))
        self._approver.setText(r.get("approver", ""))
        self._approver2.setText(r.get("approver2", ""))
        self._my_comment.setText(r.get("my_comment", ""))
        self._attachments = list(r.get("attachments", []))
        for path in self._attachments:
            self._add_file_tab(path, manual=True)
        self._study_comment.setText(self._info["study_comment"] or "-")
        self._update_status_labels()
        approved = r.get("status") == "Approved"
        self._btn_edit.setChecked(not approved)
        self._set_editable(not approved)
        self._dirty_baseline = self.report_text()

    def _update_status_labels(self):
        r = self._report
        status = r.get("status", "Draft" if r else "New")
        self._exam_date.setText(f"{self._info['exam_datetime'] or '-'}  ({status})")
        self._report_date.setText(r.get("report_datetime", "-"))

    def _set_editable(self, editable):
        for w in (self._body, self._creator, self._approver, self._approver2, self._my_comment):
            w.setReadOnly(not editable)
        self._btn_save.setEnabled(editable)
        self._btn_import.setEnabled(editable)

    def report_text(self):
        return self._body.toPlainText()

    def is_modified(self):
        return self.report_text() != getattr(self, "_dirty_baseline", "")

    def _collect(self):
        text = self.report_text()
        findings, conclusion = split_report(text)
        report = dict(self._info)
        report.update({
            "text": text, "findings": findings, "conclusion": conclusion,
            "creator": self._creator.text().strip(),
            "approver": self._approver.text().strip(),
            "approver2": self._approver2.text().strip(),
            "my_comment": self._my_comment.text().strip(),
            "attachments": self._attachments,
            "status": self._report.get("status", "Draft"),
            "approved_datetime": self._report.get("approved_datetime", ""),
        })
        return report

    def save(self):
        report = self._collect()
        report["report_datetime"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if report["status"] == "New":
            report["status"] = "Draft"
        self._store.save(report)
        self._report = report
        if self._settings and report["creator"]:
            self._settings.set_report_creator(report["creator"])
        self._dirty_baseline = self.report_text()
        self._update_status_labels()
        self.saved.emit(report["study_uid"])
        return report

    def approve(self):
        if not self.report_text().strip() or self.report_text().strip() == REPORT_TEMPLATE.strip():
            QMessageBox.information(self, "Approve", "기록 내용이 비어 있습니다.")
            return
        if not self._approver.text().strip():
            self._approver.setText(self._creator.text().strip())
        self._report["status"] = "Approved"
        self._report["approved_datetime"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        self._btn_edit.setChecked(False)

    # ─── 파일 탭 (가져오기 / 자동 매칭) ───
    def _add_file_tab(self, path, manual):
        if path in self._file_tabs:
            return self._file_tabs[path]
        name = os.path.basename(path)
        kind = ri.classify(path)
        try:
            if not os.path.exists(path):
                raise FileNotFoundError("파일을 찾을 수 없습니다 (이동/삭제됨).")
            if kind in ("image", "pdf"):
                widget = ImageViewer(ri.load_images(path))
            else:
                widget = QPlainTextEdit(ri.load_text_content(path))
        except Exception as e:
            widget = QLabel(f"{name}\n\n{e}")
            widget.setAlignment(Qt.AlignCenter)
        label = f"{'📎' if manual else '🔗'} {name}"
        index = self._tabs.addTab(widget, label)
        self._tabs.setTabToolTip(index, f"{path}\n{'직접 연결' if manual else '기록 폴더에서 자동 매칭'}")
        self._file_tabs[path] = widget
        return widget

    def _close_file_tab(self, index):
        widget = self._tabs.widget(index)
        for path, w in list(self._file_tabs.items()):
            if w is widget:
                del self._file_tabs[path]
        self._tabs.removeTab(index)

    def _refresh_matched_files(self):
        if self._library is None:
            return
        for path in self._library.files_for(self._info["study_uid"]):
            self._add_file_tab(path, manual=False)

    def import_file(self, path, insert_text=True):
        """파일을 이 검사에 연결. 텍스트/SR은 본문에 삽입, 이미지/PDF는 탭으로 표시"""
        kind = ri.classify(path)
        if kind is None:
            raise ValueError("지원하지 않는 형식입니다.")
        if insert_text and kind in ("text", "rtf", "dicom"):
            content = ri.load_text_content(path)
            block = f"\n----- {os.path.basename(path)} -----\n{content.strip()}\n"
            cursor = self._body.textCursor()
            if not self._body.hasFocus():
                # 편집 중이 아니면 소견 끝(Conclusion 구분선 바로 앞)에 넣음
                match = _DIVIDER_RE.search(self.report_text())
                cursor.setPosition(match.start() if match else len(self.report_text()))
                if match:
                    block += "\n"
            cursor.insertText(block)
        if path not in self._attachments:
            self._attachments.append(path)
        return self._add_file_tab(path, manual=True)

    def _import_files(self):
        start = (self._library.folder if self._library and self._library.folder
                 else os.path.expanduser("~"))
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Report", start, ri.FILE_FILTER)
        for path in paths:
            if ri.mentions_other_patient(path, self._info, self._all_studies):
                answer = QMessageBox.warning(
                    self, "Import Report",
                    f"파일명에 다른 환자의 ID가 있습니다:\n{os.path.basename(path)}\n\n"
                    f"현재 검사({self._info['patient_id']})에 연결할까요?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    continue
            try:
                self.import_file(path)
            except Exception as e:
                QMessageBox.warning(self, "Import Report", f"{os.path.basename(path)}\n{e}")

    # ─── JSON 파일 ───

    def _save_json(self):
        report = self._collect()
        report["report_datetime"] = (self._report.get("report_datetime")
                                     or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        safe = re.sub(r"[^0-9A-Za-z._-]", "_",
                      f"{self._info.get('patient_id', '')}_{self._info.get('study_date', '')}")
        path, _ = QFileDialog.getSaveFileName(self, "기록 JSON 저장",
                                              os.path.join(os.path.expanduser("~"),
                                                           f"report_{safe}.json"),
                                              "JSON (*.json)")
        if not path:
            return None
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return path

    def _open_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "기록 JSON 열기", os.path.expanduser("~"),
                                              "JSON (*.json)")
        if path:
            self.load_json(path)

    def load_json(self, path):
        """JSON 기록을 편집기에 불러옴 (다른 검사 것이면 확인)"""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not ("text" in data or "findings" in data):
                raise ValueError("기록 JSON 형식이 아닙니다 (text 항목 없음).")
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "기록 JSON", f"{os.path.basename(path)}\n{e}")
            return False
        other_study = data.get("study_uid") and data["study_uid"] != self._info.get("study_uid")
        other_patient = data.get("patient_id") and data["patient_id"] != self._info.get("patient_id")
        if other_study or other_patient:
            who = f"{data.get('patient_name', '')} ({data.get('patient_id', '')}) " \
                  f"{data.get('study_date', '')}"
            if QMessageBox.warning(
                    self, "기록 JSON",
                    f"다른 {'환자' if other_patient else '검사'}의 기록입니다:\n{who}\n\n"
                    "현재 검사에 불러올까요?", QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                return False
        text = data.get("text")
        if text is None:
            text = data.get("findings", "")
            if data.get("conclusion"):
                text += f"\n\n{CONCLUSION_DIVIDER}\n" + data["conclusion"]
        self._btn_edit.setChecked(True)
        self._body.setPlainText(text)
        for key, widget in (("creator", self._creator), ("approver", self._approver),
                            ("approver2", self._approver2), ("my_comment", self._my_comment)):
            if data.get(key):
                widget.setText(data[key])
        return True

    # ─── 출력 ───
    def plain_report(self):
        lines = [self._title(), "", self.report_text().strip(), ""]
        for label, widget in (("Creator", self._creator), ("Approver", self._approver),
                              ("Approver2", self._approver2), ("My Comment", self._my_comment)):
            lines.append(f"{label}: {widget.text()}")
        lines += ["", f"Study Comment: {self._study_comment.text()}",
                  f"Exam Date: {self._exam_date.text()}",
                  f"Report Date: {self._report_date.text()}"]
        return "\n".join(lines)

    def copy_to_clipboard(self):
        QApplication.clipboard().setText(self.plain_report())

    def document(self):
        """인쇄용 문서"""
        import html
        esc = html.escape
        body = esc(self.report_text().strip()).replace("\n", "<br>")
        rows = "".join(f"<tr><td><b>{esc(k)}</b></td><td>{esc(v)}</td></tr>" for k, v in (
            ("Creator", self._creator.text()), ("Approver", self._approver.text()),
            ("Approver2", self._approver2.text()), ("My Comment", self._my_comment.text()),
            ("Study Comment", self._study_comment.text()), ("Exam Date", self._exam_date.text()),
            ("Report Date", self._report_date.text())))
        doc = QTextDocument()
        doc.setHtml(f"<h3>{esc(self._title())}</h3><p>{body}</p><hr><table>{rows}</table>")
        return doc

    def print_to(self, printer):
        self.document().print_(printer)

    def _print(self):
        from PyQt5.QtPrintSupport import QPrinter, QPrintDialog
        printer = QPrinter(QPrinter.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec_() == QDialog.Accepted:
            self.print_to(printer)

    def closeEvent(self, event):
        if self.is_modified() and not self._body.isReadOnly():
            answer = QMessageBox.question(
                self, "Reading", "저장하지 않은 변경이 있습니다. 저장할까요?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save:
                self.save()
        super().closeEvent(event)
