# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
라벨 마스크 → DICOM Segmentation (SEG, BINARY) 객체

- 라벨마다 Segment 하나, 라벨이 있는 슬라이스만 프레임으로 저장
- 프레임마다 원본 영상(SOPInstanceUID) 참조 + ImagePositionPatient
- 환자/검사 정보는 원본에서 복사 (같은 Study에 새 Series로 저장)
"""
import datetime

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

SEG_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.66.4"

_COPY_FROM_SOURCE = (
    "PatientName", "PatientID", "PatientBirthDate", "PatientSex", "PatientAge",
    "StudyInstanceUID", "StudyDate", "StudyTime", "StudyID", "AccessionNumber",
    "ReferringPhysicianName", "StudyDescription", "FrameOfReferenceUID",
    "PositionReferenceIndicator",
)


def _code(value, scheme, meaning):
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def _rgb_to_cielab_dicom(rgb):
    """sRGB (0-255) → DICOM CIELab (0-65535 스케일)"""
    c = np.array(rgb, dtype=float) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]])
    xyz = m @ c / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    lab = np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])
    return [int(round(lab[0] * 65535 / 100)),
            int(round((lab[1] + 128) * 65535 / 255)),
            int(round((lab[2] + 128) * 65535 / 255))]


def build_segmentation(series, mask, labels, series_description="AI Segmentation",
                       series_number=None):
    """마스크 (k, row, col) + 라벨 목록 → SEG Dataset. 라벨이 하나도 없으면 ValueError"""
    series.sort_slices()
    sources = series.slices
    if mask.shape[0] != len(sources):
        raise ValueError("마스크 슬라이스 수가 시리즈와 다릅니다.")
    present = [l for l in labels if (mask == l["id"]).any()]
    if not present:
        raise ValueError("칠해진 라벨이 없습니다.")
    ref = sources[0]
    rows, cols = mask.shape[1:]
    now = datetime.datetime.now()

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = SEG_SOP_CLASS
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = SEG_SOP_CLASS

    for keyword in _COPY_FROM_SOURCE:
        if keyword in ref:
            setattr(ds, keyword, getattr(ref, keyword))
    for keyword in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex",
                    "StudyDate", "StudyTime", "StudyID", "AccessionNumber",
                    "ReferringPhysicianName"):
        if keyword not in ds:
            setattr(ds, keyword, "")
    if "StudyInstanceUID" not in ds:
        ds.StudyInstanceUID = generate_uid()

    ds.Modality = "SEG"
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = series_number or (int(getattr(ref, "SeriesNumber", 0) or 0) + 1000)
    ds.SeriesDescription = series_description
    ds.InstanceNumber = 1
    ds.ContentDate = now.strftime("%Y%m%d")
    ds.ContentTime = now.strftime("%H%M%S")
    ds.ContentLabel = "SEGMENTATION"
    ds.ContentDescription = series_description
    ds.ContentCreatorName = ""
    ds.Manufacturer = "DabbaView"
    ds.ManufacturerModelName = "DabbaView AI Research"
    ds.DeviceSerialNumber = "0"
    ds.SoftwareVersions = "0.2.0"
    ds.ImageType = ["DERIVED", "PRIMARY"]
    ds.SegmentationType = "BINARY"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = rows, cols
    ds.BitsAllocated = 1
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0
    ds.LossyImageCompression = "00"

    # 참조 시리즈
    ref_series = Dataset()
    ref_series.SeriesInstanceUID = ref.SeriesInstanceUID
    ref_series.ReferencedInstanceSequence = Sequence()
    for src in sources:
        item = Dataset()
        item.ReferencedSOPClassUID = src.SOPClassUID
        item.ReferencedSOPInstanceUID = src.SOPInstanceUID
        ref_series.ReferencedInstanceSequence.append(item)
    ds.ReferencedSeriesSequence = Sequence([ref_series])

    # Segment 정의
    ds.SegmentSequence = Sequence()
    for number, label in enumerate(present, start=1):
        seg = Dataset()
        seg.SegmentNumber = number
        seg.SegmentLabel = label["name"][:64]
        seg.SegmentAlgorithmType = "MANUAL"
        seg.SegmentedPropertyCategoryCodeSequence = Sequence(
            [_code("85756007", "SCT", "Tissue")])
        seg.SegmentedPropertyTypeCodeSequence = Sequence(
            [_code("85756007", "SCT", "Tissue")])
        seg.RecommendedDisplayCIELabValue = _rgb_to_cielab_dicom(label["color"])
        ds.SegmentSequence.append(seg)

    # 차원 구성: (Segment 번호, 영상 위치)
    dim_uid = generate_uid()
    ds.DimensionOrganizationSequence = Sequence([Dataset()])
    ds.DimensionOrganizationSequence[0].DimensionOrganizationUID = dim_uid
    ds.DimensionOrganizationType = "3D"
    idx_seg = Dataset()
    idx_seg.DimensionOrganizationUID = dim_uid
    idx_seg.DimensionIndexPointer = pydicom.tag.Tag("ReferencedSegmentNumber")
    idx_seg.FunctionalGroupPointer = pydicom.tag.Tag("SegmentIdentificationSequence")
    idx_seg.DimensionDescriptionLabel = "ReferencedSegmentNumber"
    idx_pos = Dataset()
    idx_pos.DimensionOrganizationUID = dim_uid
    idx_pos.DimensionIndexPointer = pydicom.tag.Tag("ImagePositionPatient")
    idx_pos.FunctionalGroupPointer = pydicom.tag.Tag("PlanePositionSequence")
    idx_pos.DimensionDescriptionLabel = "ImagePositionPatient"
    ds.DimensionIndexSequence = Sequence([idx_seg, idx_pos])

    # 공통 기능 그룹: 방향 + 픽셀 간격
    shared = Dataset()
    orient = Dataset()
    orient.ImageOrientationPatient = list(getattr(ref, "ImageOrientationPatient",
                                                  [1, 0, 0, 0, 1, 0]))
    shared.PlaneOrientationSequence = Sequence([orient])
    measures = Dataset()
    measures.PixelSpacing = list(getattr(ref, "PixelSpacing", [1, 1]))
    measures.SliceThickness = getattr(ref, "SliceThickness", 1) or 1
    if "SpacingBetweenSlices" in ref:
        measures.SpacingBetweenSlices = ref.SpacingBetweenSlices
    shared.PixelMeasuresSequence = Sequence([measures])
    ds.SharedFunctionalGroupsSequence = Sequence([shared])

    # 프레임별 기능 그룹 + 픽셀
    per_frame = Sequence()
    frames = []
    for number, label in enumerate(present, start=1):
        for k, src in enumerate(sources):
            frame = mask[k] == label["id"]
            if not frame.any():
                continue
            frames.append(frame)
            fg = Dataset()
            derivation = Dataset()
            source_img = Dataset()
            source_img.ReferencedSOPClassUID = src.SOPClassUID
            source_img.ReferencedSOPInstanceUID = src.SOPInstanceUID
            source_img.SpatialLocationsPreserved = "YES"
            source_img.PurposeOfReferenceCodeSequence = Sequence(
                [_code("121322", "DCM", "Source image for image processing operation")])
            derivation.SourceImageSequence = Sequence([source_img])
            derivation.DerivationCodeSequence = Sequence(
                [_code("113076", "DCM", "Segmentation")])
            fg.DerivationImageSequence = Sequence([derivation])
            content = Dataset()
            content.DimensionIndexValues = [number, k + 1]
            fg.FrameContentSequence = Sequence([content])
            position = Dataset()
            position.ImagePositionPatient = list(getattr(src, "ImagePositionPatient",
                                                         [0, 0, k]))
            fg.PlanePositionSequence = Sequence([position])
            seg_id = Dataset()
            seg_id.ReferencedSegmentNumber = number
            fg.SegmentIdentificationSequence = Sequence([seg_id])
            per_frame.append(fg)
    ds.PerFrameFunctionalGroupsSequence = per_frame
    ds.NumberOfFrames = len(frames)

    # 1비트 패킹 (프레임을 이어 붙여 비트 단위로, 바이트 안에서는 LSB부터)
    bits = np.concatenate([f.ravel() for f in frames]).astype(np.uint8)
    packed = np.packbits(bits, bitorder="little")
    if len(packed) % 2:
        packed = np.append(packed, np.uint8(0))
    ds.PixelData = packed.tobytes()
    ds["PixelData"].VR = "OB"
    return ds


def save_segmentation(path, series, mask, labels, **kwargs):
    ds = build_segmentation(series, mask, labels, **kwargs)
    ds.save_as(path, enforce_file_format=True)
    return ds
