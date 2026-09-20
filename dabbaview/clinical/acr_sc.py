# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
ACR 증빙 영상 → DICOM Secondary Capture

각 증빙 캡처를 표준 DICOM SC(1.2.840.10008.5.1.4.1.1.7)로 저장해 PACS로 보낼 수 있게 한다.
환자 · 검사 정보는 분석한 팬텀 영상에서 그대로 가져오고(같은 Study에 붙음), 시리즈는 새로 만든다.
"""
import datetime
import os

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
SERIES_NUMBER = 9001          # 증빙 시리즈 번호 (원본과 겹치지 않게 크게)
SERIES_DESCRIPTION = "ACR QC evidence (DabbaView)"

# 원본에서 그대로 가져올 항목 (환자 · 검사)
COPY_TAGS = ("PatientName", "PatientID", "PatientBirthDate", "PatientSex", "PatientAge",
             "StudyInstanceUID", "StudyDate", "StudyTime", "StudyID", "AccessionNumber",
             "ReferringPhysicianName", "StudyDescription", "InstitutionName",
             "Manufacturer", "ManufacturerModelName", "StationName", "DeviceSerialNumber")


def _source_dataset(tool):
    """분석에 쓴 팬텀 영상의 첫 데이터셋 (환자 · 검사 정보 원본)"""
    refs = (tool.sets or {}).get("T1") or []
    if not refs:
        return None
    series, k = refs[0]
    try:
        return series.slices[k]
    except (AttributeError, IndexError):
        return None


def write_evidence(items, folder, source_ds=None, info=None):
    """증빙 [(번호, 검사, 내용, 비고, PNG 경로)] → DICOM SC 파일들. 만든 경로 목록 반환"""
    import numpy as np
    import pydicom
    from PIL import Image
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    os.makedirs(folder, exist_ok=True)
    now = datetime.datetime.now()
    series_uid = generate_uid()
    made = []
    for n, (number, test, desc, note, path) in enumerate(items, start=1):
        image = Image.open(path).convert("RGB")
        pixels = np.asarray(image, dtype=np.uint8)

        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = SC_SOP_CLASS
        ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.file_meta.ImplementationVersionName = "DabbaView"
        ds.is_little_endian = True
        ds.is_implicit_VR = False

        ds.SpecificCharacterSet = "ISO_IR 192"     # UTF-8 (한글 설명 · 기관명)
        for tag in COPY_TAGS:                      # 환자 · 검사 정보는 원본 그대로
            value = getattr(source_ds, tag, None) if source_ds is not None else None
            if value not in (None, ""):
                setattr(ds, tag, value)
        if not getattr(ds, "PatientName", None):
            ds.PatientName = "ACR^PHANTOM"
        if not getattr(ds, "PatientID", None):
            ds.PatientID = "ACR_PHANTOM"
        if not getattr(ds, "StudyInstanceUID", None):
            ds.StudyInstanceUID = generate_uid()
        if info:
            ds.InstitutionName = info.get("hospital") or getattr(ds, "InstitutionName", "")
            ds.StationName = (info.get("unit") or getattr(ds, "StationName", ""))[:16]
            ds.OperatorsName = info.get("tester", "")

        ds.SOPClassUID = SC_SOP_CLASS
        ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
        ds.SeriesInstanceUID = series_uid
        ds.SeriesNumber = SERIES_NUMBER
        ds.SeriesDescription = SERIES_DESCRIPTION
        ds.Modality = "OT"                          # Secondary Capture
        ds.ConversionType = "WSD"                   # Workstation
        ds.ImageType = ["DERIVED", "SECONDARY", "OTHER"]
        ds.InstanceNumber = n
        label = f"{number:02d}" if isinstance(number, int) else str(number)
        ds.ImageComments = f"[{label}] {test} — {desc}" + (f" | {note}" if note else "")
        ds.ContentDate = now.strftime("%Y%m%d")
        ds.ContentTime = now.strftime("%H%M%S")
        ds.SeriesDate, ds.SeriesTime = ds.ContentDate, ds.ContentTime
        ds.SecondaryCaptureDeviceManufacturer = "DabbaView"
        ds.BurnedInAnnotation = "YES"               # 글자가 찍혀 있음

        ds.SamplesPerPixel = 3
        ds.PhotometricInterpretation = "RGB"
        ds.PlanarConfiguration = 0
        ds.Rows, ds.Columns = pixels.shape[0], pixels.shape[1]
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.PixelData = pixels.tobytes()

        out = os.path.join(folder, f"ACR_{label}.dcm")
        pydicom.dcmwrite(out, ds, write_like_original=False)
        made.append(out)
    return made
