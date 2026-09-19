# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
볼륨 → 의료영상 파일 (포맷 변환 / Export As)

모든 writer는 ai.volume.Volume (array[k, row, col], affine_lps)을 받아
공간 정보(간격·방향·원점)를 보존해 저장한다.
"""
import datetime
import json
import os

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from ..ai import preprocess
from ..ai.volume import Volume
from .volume_series import encode_int16

_RAS_FROM_LPS = np.diag([-1.0, -1.0, 1.0, 1.0])

TARGETS = {
    # key: (표시 이름, 기본 확장자, 폴더 출력 여부)
    "nifti": ("NIfTI", ".nii.gz", False),
    "nrrd": ("NRRD", ".nrrd", False),
    "metaimage": ("MetaImage", ".mha", False),
    "numpy": ("NumPy", ".npz", False),
    "png": ("PNG 시퀀스", "", True),
    "dicom": ("DICOM 시리즈", "", True),
}

SOP_CLASSES = {
    "OT": "1.2.840.10008.5.1.4.1.1.7",    # Secondary Capture
    "MR": "1.2.840.10008.5.1.4.1.1.4",
    "CT": "1.2.840.10008.5.1.4.1.1.2",
}


class ConvertOptions:
    def __init__(self):
        self.resample = False
        self.target_spacing = (1.0, 1.0, 1.0)   # (Δk, Δrow, Δcol) mm
        self.dtype = "keep"                     # keep / int16 / float32 / uint8
        self.compress = True
        self.include_mask = False
        # PNG
        self.png_16bit = False
        self.window = (40.0, 400.0)
        # DICOM
        self.modality = "OT"
        self.patient_name = "ANONYMOUS"
        self.patient_id = "ANON000"
        self.series_description = "Converted by DabbaView"
        self.source_dataset = None              # 원본 DICOM (환자·검사 정보 복사용, 선택)


def cast(array, dtype):
    if dtype == "int16":
        return np.clip(np.round(array), -32768, 32767).astype(np.int16)
    if dtype == "float32":
        return array.astype(np.float32)
    if dtype == "uint8":
        lo, hi = float(array.min()), float(array.max())
        return np.round((array - lo) / max(hi - lo, 1e-6) * 255).astype(np.uint8)
    # keep: 정수 값이면 int16/uint16, 아니면 float32
    sample = array[::max(1, array.shape[0] // 16)]
    if np.all(np.mod(sample, 1) == 0):
        lo, hi = float(array.min()), float(array.max())
        if lo >= -32768 and hi <= 32767:
            return array.astype(np.int16)
        if lo >= 0 and hi <= 65535:
            return array.astype(np.uint16)
    return array.astype(np.float32)


def _ijk(array):
    """(k, row, col) → (col, row, k) = (i, j, k)"""
    return np.ascontiguousarray(array.transpose(2, 1, 0))


def _mask_path(path):
    for ext in (".nii.gz", ".nii", ".nrrd", ".mha", ".mhd", ".npy", ".npz"):
        if path.lower().endswith(ext):
            return path[:-len(ext)] + "_mask" + path[-len(ext):]
    return path + "_mask"


# ─── 파일 형식별 ───

def write_nifti(path, volume, array):
    import nibabel as nib
    affine = _RAS_FROM_LPS @ volume.affine_lps
    img = nib.Nifti1Image(_ijk(array), affine)
    img.set_qform(affine, code=1)
    img.set_sform(affine, code=1)
    img.header.set_xyzt_units("mm")
    nib.save(img, path)


def write_nrrd(path, volume, array, compress):
    import nrrd
    a = volume.affine_lps
    header = {
        "space": "left-posterior-superior",
        "space directions": a[:3, :3].T.tolist(),   # 축마다 한 행
        "space origin": a[:3, 3].tolist(),
        "kinds": ["domain", "domain", "domain"],
        "encoding": "gzip" if compress else "raw",
    }
    nrrd.write(path, _ijk(array), header, index_order="F")


def write_metaimage(path, volume, array, compress):
    import SimpleITK as sitk
    a = volume.affine_lps
    spacing = [float(np.linalg.norm(a[:3, i])) or 1.0 for i in range(3)]
    direction = a[:3, :3] / np.array(spacing)
    image = sitk.GetImageFromArray(np.ascontiguousarray(array))  # (k, j, i)
    image.SetSpacing(spacing)
    image.SetOrigin([float(v) for v in a[:3, 3]])
    image.SetDirection([float(v) for v in direction.flatten()])
    sitk.WriteImage(image, path, useCompression=bool(compress))


def write_numpy(path, volume, array, mask, compress):
    meta = {"order": "k,row,col", "spacing": [float(s) for s in volume.spacing],
            "affine_lps": volume.affine_lps.tolist()}
    if path.lower().endswith(".npz"):
        data = {"image": array, "spacing": np.array(volume.spacing),
                "affine_lps": volume.affine_lps}
        if mask is not None:
            data["mask"] = mask
        (np.savez_compressed if compress else np.savez)(path, **data)
    else:
        np.save(path, array)
        if mask is not None:
            np.save(path[:-4] + "_mask.npy", mask)
        with open(path[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)


def write_png(folder, volume, array, mask, options, progress, cancelled):
    from PIL import Image
    img_dir = os.path.join(folder, "images")
    os.makedirs(img_dir, exist_ok=True)
    offset = 0.0
    if options.png_16bit:
        offset = float(np.floor(array.min()))
    for k in range(array.shape[0]):
        if cancelled():
            return
        sl = array[k]
        if options.png_16bit:
            data = np.clip(np.round(sl - offset), 0, 65535).astype(np.uint16)
        else:
            center, width = options.window
            data = (np.clip((sl - (center - width / 2)) / max(width, 1e-6), 0, 1)
                    * 255).round().astype(np.uint8)
        Image.fromarray(data).save(os.path.join(img_dir, f"slice_{k + 1:04d}.png"))
        if mask is not None:
            mdir = os.path.join(folder, "masks")
            os.makedirs(mdir, exist_ok=True)
            Image.fromarray(mask[k].astype(np.uint8)).save(
                os.path.join(mdir, f"slice_{k + 1:04d}.png"))
        progress(f"PNG {k + 1}/{array.shape[0]}", (k + 1) / array.shape[0])
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"spacing": [float(s) for s in volume.spacing],
                   "affine_lps": volume.affine_lps.tolist(),
                   "bits": 16 if options.png_16bit else 8,
                   "value_offset": offset if options.png_16bit else None,
                   "window": None if options.png_16bit else list(options.window)},
                  f, indent=2)


_COPY_TAGS = ("PatientName", "PatientID", "PatientBirthDate", "PatientSex", "PatientAge",
              "StudyInstanceUID", "StudyDate", "StudyTime", "StudyID", "AccessionNumber",
              "StudyDescription", "ReferringPhysicianName", "Manufacturer",
              "InstitutionName", "BodyPartExamined")


def write_dicom(folder, volume, array, mask, options, labels, progress, cancelled):
    """DICOM 시리즈 (슬라이스마다 파일). mask가 있으면 같은 폴더에 DICOM SEG도 저장"""
    os.makedirs(folder, exist_ok=True)
    a = volume.affine_lps
    col_sp = float(np.linalg.norm(a[:3, 0])) or 1.0
    row_sp = float(np.linalg.norm(a[:3, 1])) or 1.0
    step = float(np.linalg.norm(a[:3, 2])) or 1.0
    row_dir, col_dir = a[:3, 0] / col_sp, a[:3, 1] / row_sp
    stored, slope, intercept, signed = encode_int16(array)
    now = datetime.datetime.now()
    src = options.source_dataset
    study_uid = generate_uid()
    series_uid = generate_uid()
    frame_uid = generate_uid()
    sop_class = SOP_CLASSES.get(options.modality, SOP_CLASSES["OT"])
    lo, hi = np.percentile(array[array.shape[0] // 2], [1, 99])
    written = []
    for k in range(array.shape[0]):
        if cancelled():
            return written
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = sop_class
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.SOPClassUID = sop_class
        ds.PatientName = options.patient_name
        ds.PatientID = options.patient_id
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.StudyInstanceUID = study_uid
        ds.StudyDate = now.strftime("%Y%m%d")
        ds.StudyTime = now.strftime("%H%M%S")
        ds.StudyID = ""
        ds.AccessionNumber = ""
        ds.ReferringPhysicianName = ""
        if src is not None:
            for keyword in _COPY_TAGS:
                if keyword in src:
                    setattr(ds, keyword, getattr(src, keyword))
        ds.Modality = options.modality
        ds.SeriesInstanceUID = series_uid
        ds.SeriesNumber = 900
        ds.SeriesDescription = options.series_description
        ds.FrameOfReferenceUID = (str(src.FrameOfReferenceUID)
                                  if src is not None and "FrameOfReferenceUID" in src
                                  and not options.resample else frame_uid)
        ds.PositionReferenceIndicator = ""
        ds.InstanceNumber = k + 1
        ds.ContentDate = now.strftime("%Y%m%d")
        ds.ContentTime = now.strftime("%H%M%S")
        ds.ImageType = ["DERIVED", "SECONDARY"]
        if options.modality == "OT":
            ds.ConversionType = "WSD"
        ipp = a @ np.array([0.0, 0.0, k, 1.0])
        ds.ImagePositionPatient = [round(float(v), 6) for v in ipp[:3]]
        ds.ImageOrientationPatient = [round(float(v), 8) for v in list(row_dir) + list(col_dir)]
        ds.PixelSpacing = [round(row_sp, 6), round(col_sp, 6)]
        ds.SliceThickness = round(step, 6)
        ds.SpacingBetweenSlices = round(step, 6)
        ds.SliceLocation = round(float(np.dot(ipp[:3], np.cross(row_dir, col_dir))), 6)
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows, ds.Columns = array.shape[1:]
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1 if signed else 0
        ds.RescaleIntercept = round(intercept, 6)
        ds.RescaleSlope = round(slope, 10)
        ds.RescaleType = "HU" if options.modality == "CT" else "US"
        ds.WindowCenter = round(float((lo + hi) / 2), 3)
        ds.WindowWidth = round(float(max(hi - lo, 1)), 3)
        if options.modality == "CT":
            ds.KVP = ""
        if options.modality == "MR":
            ds.ScanningSequence = "RM"
            ds.SequenceVariant = "NONE"
            ds.ScanOptions = ""
            ds.MRAcquisitionType = "3D"
            ds.EchoTime = ""
            ds.RepetitionTime = ""
            ds.EchoTrainLength = ""
        ds.PixelData = stored[k].tobytes()
        ds.save_as(os.path.join(folder, f"IM_{k + 1:04d}.dcm"), enforce_file_format=True)
        written.append(ds)
        progress(f"DICOM {k + 1}/{array.shape[0]}", (k + 1) / array.shape[0])
    if mask is not None and mask.any() and labels:
        from ..ai.dicom_seg import save_segmentation
        series = _WrittenSeries(written)
        save_segmentation(os.path.join(folder, "SEG.dcm"), series, mask, labels,
                          series_description="Segmentation")
    return written


class _WrittenSeries:
    """방금 쓴 DICOM 슬라이스들을 dicom_seg가 참조할 수 있게 하는 최소 객체"""

    def __init__(self, slices):
        self.slices = slices

    def sort_slices(self):
        pass


# ─── 변환 진입점 ───

def convert(volume, mask, target, out_path, options, labels=None,
            progress=None, cancelled=None):
    """Volume(+마스크) → target 형식. 저장한 경로 목록 반환"""
    progress = progress or (lambda *_: None)
    cancelled = cancelled or (lambda: False)
    if options.resample:
        progress("리샘플링 중...", 0.05)
        pre = preprocess.PreprocessOptions()
        pre.resample = True
        pre.target_spacing = options.target_spacing
        volume, mask = preprocess.apply(volume, mask, pre)
    if not options.include_mask:
        mask = None
    array = volume.array
    if target not in ("png", "dicom"):
        array = cast(array, options.dtype)
    mask = mask.astype(np.uint8) if mask is not None else None
    progress("저장 중...", 0.3)
    outputs = [out_path]
    if target == "nifti":
        write_nifti(out_path, volume, array)
        if mask is not None:
            write_nifti(_mask_path(out_path), volume, mask)
            outputs.append(_mask_path(out_path))
    elif target == "nrrd":
        write_nrrd(out_path, volume, array, options.compress)
        if mask is not None:
            write_nrrd(_mask_path(out_path), volume, mask, options.compress)
            outputs.append(_mask_path(out_path))
    elif target == "metaimage":
        write_metaimage(out_path, volume, array, options.compress)
        if mask is not None:
            write_metaimage(_mask_path(out_path), volume, mask, options.compress)
            outputs.append(_mask_path(out_path))
    elif target == "numpy":
        write_numpy(out_path, volume, array, mask, options.compress)
    elif target == "png":
        write_png(out_path, volume, array, mask, options, progress, cancelled)
    elif target == "dicom":
        write_dicom(out_path, volume, array.astype(np.float32), mask, options, labels,
                    progress, cancelled)
    else:
        raise ValueError(f"알 수 없는 형식: {target}")
    progress("완료", 1.0)
    return outputs


def volume_from_series(series):
    from ..ai.volume import load_volume
    return load_volume(series)


__all__ = ["TARGETS", "ConvertOptions", "convert", "volume_from_series", "Volume"]
