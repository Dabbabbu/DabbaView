# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM이 아닌 3D 볼륨(NIfTI, NRRD, MetaImage, NumPy, 이미지 시퀀스)을
DicomSeries처럼 쓰기 위한 메모리 시리즈

슬라이스마다 공간 정보(IPP/IOP/PixelSpacing 등)를 채운 메타데이터 Dataset을 만들어
뷰포트·MPR·3D·AI·내보내기 코드가 DICOM 시리즈와 똑같이 다룰 수 있게 한다.

array[k, row, col] (float32, 물리 값) + affine_lps: (col, row, k, 1) → LPS mm
"""
import hashlib
import os

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from ..dicom_loader import DicomSeries

SECONDARY_CAPTURE = "1.2.840.10008.5.1.4.1.1.7"

FORMAT_NAMES = {
    "nifti": "NIfTI", "nrrd": "NRRD", "metaimage": "MetaImage",
    "numpy": "NumPy", "image": "Image", "dicom": "DICOM",
}


def stable_uid(*parts):
    """같은 파일이면 같은 UID (다시 열어도 AI 마스크·주석이 이어지도록)"""
    return generate_uid(entropy_srcs=[str(p) for p in parts])


def canonicalize(array, affine_lps):
    """화면 표시가 DICOM 관례(방사선학적 방향)가 되도록 축 뒤집기

    - 행/열 축: 주성분이 L 또는 P면 + 방향, S면 - 방향 (화면 아래 = 발 쪽)
    - 슬라이스 축: 법선(행×열) 방향으로 증가하도록
    """
    array = np.asarray(array)
    affine = np.array(affine_lps, dtype=float)
    d, h, w = array.shape
    for axis, n, arr_axis in ((0, w, 2), (1, h, 1)):
        v = affine[:3, axis]
        major = int(np.argmax(np.abs(v)))
        want_positive = major != 2
        if (v[major] > 0) != want_positive and n > 1:
            array = np.flip(array, axis=arr_axis)
            affine[:3, 3] = affine[:3, 3] + v * (n - 1)
            affine[:3, axis] = -v
    normal = np.cross(affine[:3, 0], affine[:3, 1])
    if d > 1 and float(np.dot(affine[:3, 2], normal)) < 0:
        array = np.flip(array, axis=0)
        affine[:3, 3] = affine[:3, 3] + affine[:3, 2] * (d - 1)
        affine[:3, 2] = -affine[:3, 2]
    return np.ascontiguousarray(array), affine


def default_window(array, modality):
    if modality == "CT":
        return 40.0, 400.0
    sample = array[array.shape[0] // 2] if array.ndim == 3 else array
    lo, hi = np.percentile(sample, [1, 99])
    if hi <= lo:
        lo, hi = float(array.min()), float(array.max())
    return float((lo + hi) / 2), float(max(hi - lo, 1.0))


def guess_modality(array):
    """값 범위로 CT(HU) 여부 추정 - 나머지는 OT"""
    lo, hi = np.percentile(array[::max(1, array.shape[0] // 8)], [0.5, 99.5])
    if lo <= -500 and hi >= 100 and np.all(np.mod(array.flat[:4096], 1) == 0):
        return "CT"
    return "OT"


class VolumeSeries(DicomSeries):
    """메모리 볼륨 시리즈"""

    def __init__(self, array, affine_lps, name, source_path, source_format,
                 modality=None, description=None, index=0, canonical=True):
        array = np.asarray(array, dtype=np.float32)
        if array.ndim == 2:
            array = array[None]
        if array.ndim != 3:
            raise ValueError(f"3D 볼륨이 아닙니다: {array.shape}")
        if canonical:
            array, affine_lps = canonicalize(array, affine_lps)
        self._array = array
        self.affine_lps = np.array(affine_lps, dtype=float)
        self.source_path = os.path.abspath(source_path)
        self.source_format = source_format
        modality = modality or guess_modality(array)
        uid = stable_uid("series", self.source_path, index)
        super().__init__(uid, description or name, modality)
        self._build_slices(name, index)
        self._sorted = True

    # ─── 메타데이터 ───

    def _build_slices(self, name, index):
        d, h, w = self._array.shape
        a = self.affine_lps
        col_spacing = float(np.linalg.norm(a[:3, 0])) or 1.0
        row_spacing = float(np.linalg.norm(a[:3, 1])) or 1.0
        slice_step = float(np.linalg.norm(a[:3, 2])) or 1.0
        row_dir = a[:3, 0] / col_spacing
        col_dir = a[:3, 1] / row_spacing
        normal = np.cross(row_dir, col_dir)
        wc, ww = default_window(self._array, self.modality)
        path = self.source_path
        digest = hashlib.sha1(path.encode()).hexdigest()[:8].upper()
        study_uid = stable_uid("study", path)
        frame_uid = stable_uid("frame", path)
        fmt = FORMAT_NAMES.get(self.source_format, self.source_format)
        for k in range(d):
            ds = Dataset()
            ds.PatientName = name
            ds.PatientID = f"{fmt.upper()}-{digest}"
            ds.StudyInstanceUID = study_uid
            ds.StudyDescription = f"{fmt}: {os.path.basename(path)}"
            ds.StudyDate = ""
            ds.StudyTime = ""
            ds.SeriesInstanceUID = self.series_uid
            ds.SeriesDescription = self.description
            ds.SeriesNumber = index + 1
            ds.Modality = self.modality
            ds.SOPClassUID = SECONDARY_CAPTURE
            ds.SOPInstanceUID = stable_uid("sop", path, index, k)
            ds.InstanceNumber = k + 1
            ds.FrameOfReferenceUID = frame_uid
            ipp = a @ np.array([0.0, 0.0, k, 1.0])
            ds.ImagePositionPatient = [round(float(v), 6) for v in ipp[:3]]
            ds.ImageOrientationPatient = [round(float(v), 8) for v in
                                          list(row_dir) + list(col_dir)]
            ds.SliceLocation = round(float(np.dot(ipp[:3], normal)), 6)
            ds.PixelSpacing = [round(row_spacing, 6), round(col_spacing, 6)]
            ds.SliceThickness = round(slice_step, 6)
            ds.SpacingBetweenSlices = round(slice_step, 6)
            ds.Rows, ds.Columns = h, w
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.BitsAllocated = 16
            ds.BitsStored = 16
            ds.HighBit = 15
            ds.PixelRepresentation = 1
            ds.RescaleSlope = 1
            ds.RescaleIntercept = 0
            ds.WindowCenter = round(wc, 3)
            ds.WindowWidth = round(ww, 3)
            ds.ImageComments = f"Loaded from {fmt}: {path}"
            ds.filename = path
            self.slices.append(ds)

    # ─── 정렬 / 픽셀 (메모리 배열이 원본) ───

    def sort_slices(self):
        self._sorted = True  # 만들 때부터 법선 방향 순서

    @property
    def array(self):
        return self._array

    def _load_pixels(self, index):
        return self._array[index].astype(np.float64)

    def get_pixel_array(self, index):
        if index < 0 or index >= self._array.shape[0]:
            return None
        return self._array[index]

    def get_all_pixel_arrays(self, max_workers=None):
        return [self._array[k] for k in range(self._array.shape[0])]

    def get_volume_array(self):
        return self._array

    def decode_error(self, index):
        return None   # 메모리에 있는 배열이라 디코딩 실패 없음

    def get_full_dataset(self, index):
        """픽셀까지 포함한 DICOM Dataset (Send·익명화·Print 등에서 사용)"""
        if index < 0 or index >= len(self.slices):
            return None
        from copy import deepcopy
        ds = deepcopy(self.slices[index])
        if getattr(self, "_encoded", None) is None:
            self._encoded = encode_int16(self._array)  # 볼륨 전체 기준 (슬라이스 간 일관)
        stored, slope, intercept, signed = self._encoded
        ds.PixelRepresentation = 1 if signed else 0
        ds.RescaleSlope = round(slope, 10)
        ds.RescaleIntercept = round(intercept, 6)
        ds.PixelData = stored[index].tobytes()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        del ds.ImageComments
        return ds


def encode_int16(array):
    """물리 값 배열 → (16비트 저장값, slope, intercept, signed)

    정수 값이 int16 범위면 그대로(slope 1), 아니면 uint16 전 범위로 선형 변환.
    """
    array = np.asarray(array)
    lo, hi = float(array.min()), float(array.max())
    integral = np.all(np.mod(array[::max(1, array.shape[0] // 16)], 1) == 0)
    if integral and lo >= -32768 and hi <= 32767:
        return array.astype(np.int16), 1.0, 0.0, True
    if integral and lo >= 0 and hi <= 65535:
        return array.astype(np.uint16), 1.0, 0.0, False
    slope = (hi - lo) / 65535.0 if hi > lo else 1.0
    stored = np.round((array - lo) / slope).clip(0, 65535).astype(np.uint16)
    return stored, slope, lo, False
