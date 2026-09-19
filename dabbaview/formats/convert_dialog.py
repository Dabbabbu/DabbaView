# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
포맷 변환 대화상자 (File → Convert / Export As)

소스: 현재 열린 시리즈 또는 파일/폴더 (DICOM, NIfTI, NRRD, MetaImage, NumPy, 이미지)
대상: NIfTI, NRRD, MetaImage, NumPy, PNG 시퀀스, DICOM 시리즈
"""
import os
import traceback

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QProgressBar, QPushButton, QRadioButton,
                             QVBoxLayout, QWidget)

from .writers import TARGETS, ConvertOptions, convert, volume_from_series

SOURCE_FILTERS = {
    "dicom": "DICOM (*.dcm *.DCM *.dicom *)",
    "nifti": "NIfTI (*.nii *.nii.gz)",
    "nrrd": "NRRD (*.nrrd *.nhdr)",
    "metaimage": "MetaImage (*.mha *.mhd)",
    "numpy": "NumPy (*.npy *.npz)",
    "image": "이미지 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
}

SAVE_FILTERS = {
    "nifti": "NIfTI 압축 (*.nii.gz);;NIfTI (*.nii)",
    "nrrd": "NRRD (*.nrrd)",
    "metaimage": "MetaImage 단일 파일 (*.mha);;MetaImage 헤더+raw (*.mhd)",
    "numpy": "NumPy 묶음 (*.npz);;NumPy 배열 (*.npy)",
}


class _Worker(QThread):
    progress = pyqtSignal(str, float)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.done.emit(self._fn(lambda t, f=0.0: self.progress.emit(t, float(f)),
                                    lambda: self._cancel))
        except Exception as e:  # noqa: BLE001 - 형식 오류 등을 대화상자에 표시
            traceback.print_exc()
            self.failed.emit(str(e) or type(e).__name__)


def series_format(series):
    return getattr(series, "source_format", "dicom")


class ConvertDialog(QDialog):
    def __init__(self, main_window, source_kind=None, target=None):
        super().__init__(main_window)
        self.main = main_window
        self.setWindowTitle("Convert / Export As")
        self.resize(620, 640)
        self._source_kind = source_kind
        self._file_loader = None
        self._worker = None
        self._build()
        self._select_initial(source_kind, target)

    # ─── UI ───

    def _build(self):
        layout = QVBoxLayout(self)

        src = QGroupBox("소스")
        sl = QVBoxLayout(src)
        self._src_current = QRadioButton()
        self._src_file = QRadioButton("파일 / 폴더:")
        sl.addWidget(self._src_current)
        file_row = QHBoxLayout()
        file_row.addWidget(self._src_file)
        self._file_path = QLineEdit()
        self._file_path.setReadOnly(True)
        file_row.addWidget(self._file_path, 1)
        pick_file = QPushButton("파일…")
        pick_file.clicked.connect(self._pick_file)
        pick_dir = QPushButton("폴더…")
        pick_dir.setToolTip("DICOM 폴더 또는 PNG/JPEG 시퀀스 폴더")
        pick_dir.clicked.connect(self._pick_folder)
        file_row.addWidget(pick_file)
        file_row.addWidget(pick_dir)
        sl.addLayout(file_row)
        self._series_combo = QComboBox()
        self._series_combo.setVisible(False)
        sl.addWidget(self._series_combo)
        self._src_info = QLabel()
        self._src_info.setStyleSheet("color: #888;")
        self._src_info.setWordWrap(True)
        sl.addWidget(self._src_info)
        for r in (self._src_current, self._src_file):
            r.toggled.connect(self._update_state)
        layout.addWidget(src)

        tgt = QGroupBox("대상")
        tl = QFormLayout(tgt)
        self._target = QComboBox()
        for key, (name, _ext, _folder) in TARGETS.items():
            self._target.addItem(name, key)
        self._target.currentIndexChanged.connect(self._update_state)
        tl.addRow("포맷:", self._target)
        out_row = QHBoxLayout()
        self._out_path = QLineEdit()
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._pick_output)
        out_row.addWidget(self._out_path, 1)
        out_row.addWidget(browse)
        tl.addRow("저장 위치:", out_row)
        layout.addWidget(tgt)

        opt = QGroupBox("옵션")
        ol = QFormLayout(opt)
        rs_row = QHBoxLayout()
        self._resample = QCheckBox("변경")
        rs_row.addWidget(self._resample)
        self._spacing = []
        for axis in ("x", "y", "z"):
            spin = QDoubleSpinBox()
            spin.setRange(0.05, 50)
            spin.setDecimals(3)
            spin.setValue(1.0)
            spin.setSuffix(" mm")
            rs_row.addWidget(QLabel(axis))
            rs_row.addWidget(spin)
            self._spacing.append(spin)
        ol.addRow("Voxel spacing:", rs_row)
        self._dtype = QComboBox()
        for label, key in (("원본 유지 (정수면 int16)", "keep"), ("int16", "int16"),
                           ("float32", "float32"), ("uint8 (0-255로 스케일)", "uint8")):
            self._dtype.addItem(label, key)
        ol.addRow("데이터 타입:", self._dtype)
        self._compress = QCheckBox("압축 (.nii.gz / NRRD gzip / MHA / .npz)")
        self._compress.setChecked(True)
        ol.addRow("", self._compress)
        self._include_mask = QCheckBox("AI 세그멘테이션 마스크도 저장 (_mask 파일 / DICOM SEG)")
        ol.addRow("", self._include_mask)

        self._png_box = QWidget()
        pl = QHBoxLayout(self._png_box)
        pl.setContentsMargins(0, 0, 0, 0)
        self._png_16 = QCheckBox("16비트 (원래 값)")
        self._png_16.setToolTip("끄면 아래 W/L로 8비트 변환")
        self._png_wc = QDoubleSpinBox()
        self._png_ww = QDoubleSpinBox()
        for spin, v in ((self._png_wc, 40), (self._png_ww, 400)):
            spin.setRange(-100000, 100000)
            spin.setValue(v)
        self._png_ww.setMinimum(1)
        pl.addWidget(self._png_16)
        pl.addWidget(QLabel("L"))
        pl.addWidget(self._png_wc)
        pl.addWidget(QLabel("W"))
        pl.addWidget(self._png_ww)
        ol.addRow("PNG:", self._png_box)

        self._dicom_box = QWidget()
        dl = QFormLayout(self._dicom_box)
        dl.setContentsMargins(0, 0, 0, 0)
        self._modality = QComboBox()
        self._modality.addItems(["OT", "MR", "CT"])
        dl.addRow("Modality:", self._modality)
        self._keep_patient = QCheckBox("원본 DICOM의 환자·검사 정보 유지")
        dl.addRow("", self._keep_patient)
        self._patient_name = QLineEdit("ANONYMOUS")
        self._patient_id = QLineEdit("ANON000")
        self._series_desc = QLineEdit("Converted by DabbaView")
        dl.addRow("Patient Name:", self._patient_name)
        dl.addRow("Patient ID:", self._patient_id)
        dl.addRow("Series Description:", self._series_desc)
        ol.addRow("DICOM:", self._dicom_box)
        layout.addWidget(opt)

        self._progress = QProgressBar()
        self._progress.setMaximum(1000)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._status = QLabel()
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        btns = QHBoxLayout()
        btns.addStretch()
        self._convert_btn = QPushButton("변환")
        self._convert_btn.setDefault(True)
        self._convert_btn.clicked.connect(self._start)
        self._close_btn = QPushButton("닫기")
        self._close_btn.clicked.connect(self._close)
        btns.addWidget(self._convert_btn)
        btns.addWidget(self._close_btn)
        layout.addLayout(btns)

    def _select_initial(self, source_kind, target):
        series = self.main._target_viewport().series
        if series is not None:
            fmt = series_format(series)
            self._src_current.setText(f"현재 시리즈: {series.description or '(설명 없음)'} "
                                      f"· {fmt.upper()} · {series.num_slices}장")
        else:
            self._src_current.setText("현재 시리즈: (열린 시리즈 없음)")
            self._src_current.setEnabled(False)
        use_current = series is not None and (source_kind is None
                                              or series_format(series) == source_kind)
        (self._src_current if use_current else self._src_file).setChecked(True)
        if target:
            self._target.setCurrentIndex(max(0, self._target.findData(target)))
        if series is not None:
            wc, ww = self.main._target_viewport().window_level
            self._png_wc.setValue(wc)
            self._png_ww.setValue(ww)
            self._modality.setCurrentText(series.modality if series.modality in ("MR", "CT")
                                          else "OT")
        self._update_state()
        if not use_current and source_kind:
            self._status.setText(f"{source_kind.upper()} 파일을 고르세요 (파일… / 폴더…).")

    def _source_series(self):
        if self._src_current.isChecked():
            return self.main._target_viewport().series
        if self._file_loader is None:
            return None
        return self._file_loader.get_series_by_uid(self._series_combo.currentData())

    def _update_state(self, *_):
        target = self._target.currentData()
        self._png_box.setVisible(target == "png")
        self._dicom_box.setVisible(target == "dicom")
        self._compress.setEnabled(target in ("nifti", "nrrd", "metaimage", "numpy"))
        self._dtype.setEnabled(target not in ("png", "dicom"))
        series = self._source_series()
        is_dicom = series is not None and series_format(series) == "dicom"
        self._keep_patient.setEnabled(is_dicom)
        self._keep_patient.setChecked(is_dicom and self._keep_patient.isChecked())
        has_mask = series is not None and self._mask_for(series) is not None
        self._include_mask.setEnabled(has_mask)
        if not has_mask:
            self._include_mask.setChecked(False)
        if series is not None:
            sp = series.slices[0]
            try:
                thickness = float(getattr(sp, "SpacingBetweenSlices", 0) or
                                  getattr(sp, "SliceThickness", 0) or 0)
                self._src_info.setText(
                    f"{series.num_slices}장 · {sp.Columns}x{sp.Rows} · 간격 "
                    f"{float(sp.PixelSpacing[1]):.3g} x {float(sp.PixelSpacing[0]):.3g} x "
                    f"{thickness:.3g} mm" + (" · 마스크 있음" if has_mask else ""))
            except (AttributeError, TypeError, ValueError, IndexError):
                self._src_info.setText(f"{series.num_slices}장")
        else:
            self._src_info.setText("")
        self._suggest_output()

    def _mask_for(self, series):
        if self._src_file.isChecked() and self._file_loader is not None:
            return self._file_loader.volume_masks.get(series.series_uid)
        seg = getattr(self.main, "_seg", None)
        if seg is None:
            return None
        case = seg.case(series)
        return None if not case.editable or case.is_empty() else case.mask

    def _suggest_output(self):
        series = self._source_series()
        target = self._target.currentData()
        if series is None or not target:
            return
        base_dir = os.path.dirname(self._out_path.text()) if self._out_path.text() else \
            self.main._last_dir()
        name = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in (series.description or "series")).strip("_") or "series"
        ext = TARGETS[target][1]
        if target == "nifti" and not self._compress.isChecked():
            ext = ".nii"
        if target == "numpy" and not self._compress.isChecked():
            ext = ".npy"
        self._out_path.setText(os.path.join(base_dir, name + ext))

    # ─── 선택 ───

    def _pick_file(self):
        kinds = [self._source_kind] if self._source_kind in SOURCE_FILTERS else []
        filters = [SOURCE_FILTERS[k] for k in kinds] + [
            "지원하는 모든 형식 (*.dcm *.nii *.nii.gz *.nrrd *.nhdr *.mha *.mhd *.npy *.npz "
            "*.png *.jpg *.jpeg *.tif *.tiff)", "모든 파일 (*)"]
        path, _ = QFileDialog.getOpenFileName(self, "소스 파일", self.main._last_dir(),
                                              ";;".join(filters))
        if path:
            self._load_source(path)

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "소스 폴더", self.main._last_dir())
        if path:
            self._load_source(path)

    def _load_source(self, path):
        from ..dicom_loader import DicomLoader
        self._file_path.setText(path)
        self._src_file.setChecked(True)

        def task(progress, cancelled):
            loader = DicomLoader()
            loader.load_paths([path], progress_callback=lambda c, t: progress(
                f"읽는 중 {c}/{t}", c / max(t, 1)))
            return loader

        def done(loader):
            self._file_loader = loader
            series_list = sorted(loader.get_series_list(), key=lambda s: -s.num_slices)
            self._series_combo.clear()
            for s in series_list:
                self._series_combo.addItem(f"{s.description or '(설명 없음)'} · "
                                           f"{series_format(s).upper()} · {s.num_slices}장",
                                           s.series_uid)
            self._series_combo.setVisible(len(series_list) > 1)
            if not series_list:
                self._status.setText("영상 시리즈를 찾지 못했습니다.")
            else:
                self._status.setText(f"시리즈 {len(series_list)}개를 찾았습니다.")
            try:
                self._series_combo.currentIndexChanged.disconnect()
            except TypeError:
                pass
            self._series_combo.currentIndexChanged.connect(self._update_state)
            self._update_state()
        self._run(f"읽는 중: {path}", task, done)

    def _pick_output(self):
        target = self._target.currentData()
        if TARGETS[target][2]:
            path = QFileDialog.getExistingDirectory(self, "저장할 폴더 (새 폴더 권장)",
                                                    os.path.dirname(self._out_path.text()))
        else:
            path, _ = QFileDialog.getSaveFileName(self, "저장", self._out_path.text(),
                                                  SAVE_FILTERS[target])
        if path:
            self._out_path.setText(path)

    # ─── 변환 ───

    def _options(self, series):
        o = ConvertOptions()
        o.resample = self._resample.isChecked()
        x, y, z = (s.value() for s in self._spacing)
        o.target_spacing = (z, y, x)
        o.dtype = self._dtype.currentData()
        o.compress = self._compress.isChecked()
        o.include_mask = self._include_mask.isChecked()
        o.png_16bit = self._png_16.isChecked()
        o.window = (self._png_wc.value(), self._png_ww.value())
        o.modality = self._modality.currentText()
        o.patient_name = self._patient_name.text().strip() or "ANONYMOUS"
        o.patient_id = self._patient_id.text().strip() or "ANON000"
        o.series_description = self._series_desc.text().strip() or "Converted by DabbaView"
        if self._keep_patient.isChecked() and series_format(series) == "dicom":
            o.source_dataset = series.slices[0]
        return o

    def _start(self):
        series = self._source_series()
        if series is None:
            QMessageBox.information(self, "Convert", "소스 시리즈를 고르세요.")
            return
        target = self._target.currentData()
        out = self._out_path.text().strip()
        if not out:
            QMessageBox.information(self, "Convert", "저장 위치를 정하세요.")
            return
        folder_output = TARGETS[target][2]
        if folder_output:
            if os.path.isdir(out) and os.listdir(out):
                out = os.path.join(out, _safe_name(series.description or "converted"))
            os.makedirs(out, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            out = _fix_extension(out, target, self._compress.isChecked())
        options = self._options(series)
        mask = self._mask_for(series) if options.include_mask else None
        mask = mask.copy() if mask is not None else None
        labels = self.main._labels.to_list() if hasattr(self.main, "_labels") else []

        def task(progress, cancelled):
            progress("볼륨 읽는 중...", 0.02)
            volume = volume_from_series(series)
            return convert(volume, mask, target, out, options, labels, progress, cancelled)

        def done(outputs):
            self._status.setText("완료: " + "\n".join(outputs))
            QMessageBox.information(self, "Convert", "변환했습니다.\n\n" + "\n".join(outputs))
        self._run(f"{TARGETS[target][0]}(으)로 변환 중...", task, done)

    def _run(self, text, fn, on_done):
        if self._worker is not None:
            return
        worker = _Worker(fn)
        worker.progress.connect(lambda t, f: (self._status.setText(t),
                                              self._progress.setValue(int(f * 1000))))
        worker.done.connect(lambda r: (self._finish(), on_done(r)))
        worker.failed.connect(lambda m: (self._finish(), self._status.setText(f"실패: {m}"),
                                         QMessageBox.warning(self, "Convert", m)))
        self._worker = worker
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._convert_btn.setEnabled(False)
        self._status.setText(text)
        worker.start()

    def _finish(self):
        if self._worker is not None:
            self._worker.wait(1000)
        self._worker = None
        self._progress.setVisible(False)
        self._convert_btn.setEnabled(True)

    def _close(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)
        self.reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(event)


def _safe_name(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text).strip("_") or "converted"


def _fix_extension(path, target, compress):
    lower = path.lower()
    if target == "nifti":
        if not lower.endswith((".nii", ".nii.gz")):
            path += ".nii.gz" if compress else ".nii"
    elif target == "nrrd" and not lower.endswith(".nrrd"):
        path += ".nrrd"
    elif target == "metaimage" and not lower.endswith((".mha", ".mhd")):
        path += ".mha"
    elif target == "numpy" and not lower.endswith((".npy", ".npz")):
        path += ".npz" if compress else ".npy"
    return path
