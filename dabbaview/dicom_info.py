# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 메타데이터 해석: 시퀀스 파라미터 요약, GE 스타일 오버레이 문구, SUV 계산

모든 함수는 픽셀 없는 메타데이터 Dataset으로 동작한다.
"""
import math
from datetime import datetime

import numpy as np


# ─── 기본 태그 읽기 ───

def tag(ds, keyword, default=""):
    """태그 값을 문자열로 (다중값은 첫 값). 없으면 default"""
    value = getattr(ds, keyword, None)
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        value = value[0] if len(value) else default
    text = str(value).strip()
    return text if text else default


def tag_float(ds, keyword):
    """태그 값을 float로 (없거나 변환 불가면 None)"""
    try:
        return float(tag(ds, keyword, None))
    except (TypeError, ValueError):
        return None


def fmt_num(value, digits=1):
    """12.0 → '12', 12.5 → '12.5' (불필요한 소수점 제거)"""
    if value is None:
        return ""
    text = f"{value:.{digits}f}"
    if "." in text:  # 소수부의 0만 제거 (120 → '120', 12.50 → '12.5')
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def fmt_date(value):
    value = (value or "").strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def fmt_time(value):
    value = (value or "").strip().split(".")[0]
    if len(value) >= 6 and value[:6].isdigit():
        return f"{value[:2]}:{value[2:4]}:{value[4:6]}"
    if len(value) == 4 and value.isdigit():
        return f"{value[:2]}:{value[2:]}"
    return value


def format_person_name(value):
    parts = [p for p in str(value or "").split("^") if p]
    return " ".join(parts)


# ─── 공간 정보 ───

def pixel_spacing(ds):
    """(행 간격, 열 간격) mm. 없으면 None"""
    ps = getattr(ds, "PixelSpacing", None) or getattr(ds, "ImagerPixelSpacing", None)
    try:
        row, col = float(ps[0]), float(ps[1])
        if row > 0 and col > 0:
            return row, col
    except (TypeError, ValueError, IndexError):
        pass
    return None


def slice_normal(ds):
    try:
        iop = [float(v) for v in ds.ImageOrientationPatient]
        n = np.cross(iop[:3], iop[3:])
        return n / np.linalg.norm(n)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def orientation_name(ds):
    """Axial / Sagittal / Coronal / Oblique"""
    n = slice_normal(ds)
    if n is None:
        return ""
    axis = int(np.argmax(np.abs(n)))
    if abs(n[axis]) < 0.8:
        return "Oblique"
    return ("Sagittal", "Coronal", "Axial")[axis]


# 환자 좌표계(LPS): +x = Left, +y = Posterior, +z = Superior
_AXIS_LETTERS = (("R", "L"), ("A", "P"), ("I", "S"))


def patient_position_text(point):
    """환자 좌표(mm) → 'R 12.3  A 45.6  S 78.9' (GE 방식 방향 표기)"""
    parts = []
    for axis, value in enumerate(point):
        neg, pos = _AXIS_LETTERS[axis]
        parts.append(f"{pos if value >= 0 else neg} {abs(value):.1f}")
    return "  ".join(parts)


def slice_position_text(ds):
    """슬라이스 위치: SliceLocation 대신 IPP를 법선 주축에 투영 (예: 'S 30.0')"""
    n = slice_normal(ds)
    try:
        ipp = [float(v) for v in ds.ImagePositionPatient]
    except (AttributeError, TypeError, ValueError):
        loc = tag_float(ds, "SliceLocation")
        return f"{fmt_num(loc)}" if loc is not None else ""
    if n is None:
        return ""
    axis = int(np.argmax(np.abs(n)))
    value = ipp[axis]
    neg, pos = _AXIS_LETTERS[axis]
    return f"{pos if value >= 0 else neg} {abs(value):.1f}"


# ─── 시퀀스 해석 ───

_SCAN_SEQ = {"SE": "Spin Echo", "IR": "Inversion Recovery",
             "GR": "Gradient Echo", "EP": "Echo Planar", "RM": "Research"}


def _multi(ds, keyword):
    value = getattr(ds, keyword, None)
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in value.replace("\\", " ").split() if v]
    try:
        return [str(v) for v in value]
    except TypeError:
        return [str(value)]


def mr_sequence_type(ds):
    """MRAcquisitionType + ScanningSequence + SequenceVariant + ETL 로 요약

    예: '2D FSE', '3D GRE (SPGR)', '2D SE-EPI', '2D FLAIR (IR-FSE)'
    """
    seqs = set(_multi(ds, "ScanningSequence"))
    variants = set(_multi(ds, "SequenceVariant"))
    options = set(_multi(ds, "ScanOptions"))
    etl = tag_float(ds, "EchoTrainLength") or 1
    dim = tag(ds, "MRAcquisitionType")

    if "EP" in seqs:
        base = "SE-EPI" if "SE" in seqs else ("GRE-EPI" if "GR" in seqs else "EPI")
    elif "GR" in seqs:
        base = "GRE"
        if "SP" in variants:
            base += " (SPGR)"
        elif "SS" in variants or "TRSS" in variants:
            base += " (SSFP)"
    elif "SE" in seqs:
        base = "FSE" if etl > 1 else "SE"
    else:
        base = ""
    if "IR" in seqs:
        base = f"IR-{base}" if base else "IR"

    parts = [p for p in (dim, base) if p]
    text = " ".join(parts)
    extra = []
    if "FS" in options:
        extra.append("FatSat")
    if "PFP" in options or "PFF" in options:
        extra.append("Partial Fourier")
    name = tag(ds, "SequenceName")
    if name:
        extra.append(name)
    if extra:
        text += f" [{', '.join(extra)}]" if text else ", ".join(extra)
    return text


def acquisition_matrix(ds):
    """AcquisitionMatrix (freq rows, freq cols, phase rows, phase cols) → 'FxP'"""
    value = getattr(ds, "AcquisitionMatrix", None)
    try:
        nums = [int(v) for v in value if int(v) > 0]
        if len(nums) >= 2:
            return f"{nums[0]}x{nums[1]}"
    except (TypeError, ValueError):
        pass
    return ""


def recon_matrix(ds):
    rows, cols = tag(ds, "Rows"), tag(ds, "Columns")
    return f"{cols}x{rows}" if rows and cols else ""


def field_of_view_mm(ds):
    """표시 영상 FoV (가로 mm, 세로 mm)"""
    sp = pixel_spacing(ds)
    try:
        rows, cols = int(ds.Rows), int(ds.Columns)
    except (AttributeError, TypeError, ValueError):
        return None
    if sp is None:
        return None
    return cols * sp[1], rows * sp[0]


def b_value(ds):
    """확산 b-value (표준 태그 또는 GE private 0043,1039)"""
    value = tag_float(ds, "DiffusionBValue")
    if value is not None:
        return value
    elem = ds.get((0x0043, 0x1039)) if hasattr(ds, "get") else None
    if elem is None:
        return None
    try:
        raw = elem.value
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "ignore").split("\\")
        first = float(raw[0] if isinstance(raw, (list, tuple)) or
                      type(raw).__name__ == "MultiValue" else raw)
        # GE는 b-value에 1e9 단위 오프셋을 더해 저장하는 경우가 있음
        return first % 1e9 if first >= 1e9 else first
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def sequence_summary(ds):
    """시퀀스/영상 파라미터 요약 [(라벨, 값), ...] - 툴팁/정보 패널용"""
    rows = []

    def add(label, value):
        if value not in (None, ""):
            rows.append((label, value))

    modality = tag(ds, "Modality")
    add("Modality", modality)
    add("Orientation", orientation_name(ds))

    if modality == "MR":
        add("Sequence", mr_sequence_type(ds))
        tr, te, ti = (tag_float(ds, k) for k in
                      ("RepetitionTime", "EchoTime", "InversionTime"))
        add("TR / TE / TI", " / ".join(fmt_num(v) if v is not None else "-"
                                       for v in (tr, te, ti)) + " ms")
        fa = tag_float(ds, "FlipAngle")
        add("Flip Angle", f"{fmt_num(fa)}°" if fa is not None else "")
        etl = tag_float(ds, "EchoTrainLength")
        add("ETL", fmt_num(etl, 0) if etl else "")
        nex = tag_float(ds, "NumberOfAverages")
        add("NEX / NSA", fmt_num(nex, 2) if nex is not None else "")
        bw = tag_float(ds, "PixelBandwidth")
        add("Bandwidth", f"{fmt_num(bw, 2)} Hz/px" if bw is not None else "")
        b = b_value(ds)
        add("b-value", f"{fmt_num(b, 0)} s/mm²" if b else "")
        field = tag_float(ds, "MagneticFieldStrength")
        add("Field", f"{fmt_num(field)} T" if field else "")
        add("Coil", tag(ds, "ReceiveCoilName"))
    elif modality == "CT":
        kvp = tag_float(ds, "KVP")
        add("kVp", fmt_num(kvp, 0) if kvp else "")
        ma = tag_float(ds, "XRayTubeCurrent")
        add("mA", fmt_num(ma, 0) if ma else "")
        mas = tag_float(ds, "Exposure")
        add("mAs", fmt_num(mas, 0) if mas else "")
        add("Kernel", tag(ds, "ConvolutionKernel"))
        pitch = tag_float(ds, "SpiralPitchFactor")
        add("Pitch", fmt_num(pitch, 3) if pitch else "")
        ctdi = tag_float(ds, "CTDIvol")
        add("CTDIvol", f"{fmt_num(ctdi, 2)} mGy" if ctdi else "")
    elif modality in ("PT", "NM"):
        add("Units", tag(ds, "Units"))
        add("Corrections", "\\".join(_multi(ds, "CorrectedImage")))

    st = tag_float(ds, "SliceThickness")
    sb = tag_float(ds, "SpacingBetweenSlices")
    add("Slice Thk / Spacing",
        " / ".join(fmt_num(v, 2) if v is not None else "-" for v in (st, sb)) + " mm"
        if st is not None or sb is not None else "")
    acq, recon = acquisition_matrix(ds), recon_matrix(ds)
    add("Matrix", f"{acq} (recon {recon})" if acq else recon)
    fov = field_of_view_mm(ds)
    rd = tag_float(ds, "ReconstructionDiameter")
    if fov:
        add("FoV", f"{fmt_num(fov[0])} x {fmt_num(fov[1])} mm"
                   + (f" (recon Ø {fmt_num(rd)} mm)" if rd else ""))
    sp = pixel_spacing(ds)
    add("Pixel Spacing", f"{fmt_num(sp[0], 3)} x {fmt_num(sp[1], 3)} mm" if sp else "")
    return rows


def sequence_tooltip(ds, series_description="", num_images=0):
    lines = []
    if series_description:
        lines.append(series_description)
    lines += [f"{label}: {value}" for label, value in sequence_summary(ds)]
    if num_images:
        lines.append(f"Images: {num_images}")
    return "\n".join(lines)


# ─── 픽셀 값 단위 (HU / SI / SUV) ───

def _dicom_time_seconds(date_text, time_text):
    time_text = (time_text or "").strip()
    if not time_text:
        return None
    try:
        hh, mm = int(time_text[:2]), int(time_text[2:4] or 0)
        ss = float(time_text[4:] or 0)
    except ValueError:
        return None
    day = 0.0
    if date_text and len(date_text) >= 8:
        try:
            day = datetime.strptime(date_text[:8], "%Y%m%d").toordinal() * 86400.0
        except ValueError:
            pass
    return day + hh * 3600 + mm * 60 + ss


def suv_factor(ds):
    """SUVbw 환산 계수 (픽셀값[Bq/ml] × 계수 = SUV). 정보 부족하면 None

    SUVbw = C(Bq/ml) × 체중(g) / 붕괴 보정된 투여량(Bq)
    DecayCorrection=START 인 경우: 투여량을 SeriesTime(스캔 시작)까지 붕괴 보정.
    """
    if tag(ds, "Modality") != "PT" or tag(ds, "Units").upper() != "BQML":
        return None
    weight = tag_float(ds, "PatientWeight")
    seq = getattr(ds, "RadiopharmaceuticalInformationSequence", None)
    if not weight or not seq:
        return None
    info = seq[0]
    dose = tag_float(info, "RadionuclideTotalDose")
    half_life = tag_float(info, "RadionuclideHalfLife")
    if not dose or not half_life:
        return None
    decay = tag(ds, "DecayCorrection", "START").upper()
    if decay == "START":
        start_dt = tag(info, "RadiopharmaceuticalStartDateTime")
        if start_dt:
            inj = _dicom_time_seconds(start_dt[:8], start_dt[8:])
        else:
            inj = _dicom_time_seconds(tag(ds, "SeriesDate"),
                                      tag(info, "RadiopharmaceuticalStartTime"))
        scan = _dicom_time_seconds(tag(ds, "SeriesDate"), tag(ds, "SeriesTime"))
        if inj is None or scan is None:
            return None
        elapsed = scan - inj
        if elapsed < 0:  # 날짜 없이 자정을 넘긴 경우
            elapsed += 86400
        dose = dose * math.pow(2.0, -elapsed / half_life)
    elif decay != "ADMIN":
        return None
    return weight * 1000.0 / dose


def value_label(ds):
    """(단위 라벨, 환산 계수) - CT: HU, PT: SUVbw 또는 Bq/ml, 그 외: SI"""
    modality = tag(ds, "Modality")
    if modality == "CT":
        return "HU", 1.0
    if modality == "PT":
        factor = suv_factor(ds)
        if factor:
            return "SUVbw", factor
        return tag(ds, "Units", "Value"), 1.0
    return "SI", 1.0


# ─── GE 스타일 오버레이 ───

def overlay_corners(ds, image_index, num_images, window=None):
    """네 모서리 오버레이 문구 {'tl': [...], 'tr': [...], 'bl': [...]}

    (우하단은 스케일 바를 뷰포트에서 그림)
    """
    tl, tr, bl = [], [], []

    # 좌상단: 기관, 환자, 성별|나이|체중
    tl.append(tag(ds, "InstitutionName"))
    name = format_person_name(tag(ds, "PatientName"))
    pid = tag(ds, "PatientID")
    tl.append(f"{name}  {pid}".strip())
    age = tag(ds, "PatientAge")
    if len(age) == 4 and age[:3].isdigit():
        age = f"{int(age[:3])}{age[3]}"
    weight = tag_float(ds, "PatientWeight")
    demo = [tag(ds, "PatientSex"), age, f"{fmt_num(weight)}kg" if weight else ""]
    tl.append("|".join(v for v in demo if v))

    # 우상단: 날짜/시간, 시리즈/영상 번호, 위치, FoV, Matrix, 두께/간격
    date = (tag(ds, "AcquisitionDate") or tag(ds, "ContentDate")
            or tag(ds, "SeriesDate") or tag(ds, "StudyDate"))
    time = (tag(ds, "AcquisitionTime") or tag(ds, "ContentTime")
            or tag(ds, "SeriesTime") or tag(ds, "StudyTime"))
    tr.append(f"{fmt_date(date)} {fmt_time(time)}".strip())
    srs = tag(ds, "SeriesNumber")
    inst = tag(ds, "InstanceNumber")
    tr.append(f"Srs:{srs}|Img:{inst}  [{image_index + 1}/{num_images}]"
              if srs or inst else f"[{image_index + 1}/{num_images}]")
    pos = slice_position_text(ds)
    tr.append(f"SP {pos}" if pos else "")
    rd = tag_float(ds, "ReconstructionDiameter")
    fov = field_of_view_mm(ds)
    if rd:
        tr.append(f"ACQ FoV {fmt_num(rd / 10)}cm")
    elif fov:
        tr.append(f"ACQ FoV {fmt_num(fov[0] / 10)}cm")
    acq = acquisition_matrix(ds)
    tr.append(f"Matrix {acq or recon_matrix(ds)}")
    st, sb = tag_float(ds, "SliceThickness"), tag_float(ds, "SpacingBetweenSlices")
    thick = []
    if st is not None:
        thick.append(f"SL {fmt_num(st, 2)}mm")
    if sb is not None:
        thick.append(f"SP {fmt_num(sb, 2)}mm")
    tr.append("|".join(thick))

    # 좌하단: 시퀀스, 장비, FoV, 코일, 파라미터, W/L
    desc = tag(ds, "SeriesDescription") or tag(ds, "ProtocolName")
    b = b_value(ds)
    if b:
        desc = f"{desc}  b={fmt_num(b, 0)}".strip()
    bl.append(desc)
    model = tag(ds, "ManufacturerModelName")
    station = tag(ds, "StationName")
    bl.append("|".join(v for v in (model, station) if v))
    if fov:
        bl.append(f"FoV {fmt_num(fov[0] / 10)}x{fmt_num(fov[1] / 10)}cm")
    bl.append(tag(ds, "ReceiveCoilName"))
    modality = tag(ds, "Modality")
    if modality == "MR":
        params = []
        for label, key in (("TR", "RepetitionTime"), ("TE", "EchoTime"),
                           ("TI", "InversionTime")):
            v = tag_float(ds, key)
            if v:
                params.append(f"{label}:{fmt_num(v)}")
        bl.append(" ".join(params))
        params = []
        fa, etl, nex = (tag_float(ds, k) for k in
                        ("FlipAngle", "EchoTrainLength", "NumberOfAverages"))
        if fa is not None:
            params.append(f"FA:{fmt_num(fa)}")
        if etl and etl > 1:
            params.append(f"ETL:{fmt_num(etl, 0)}")
        if nex is not None:
            params.append(f"NEX:{fmt_num(nex, 2)}")
        bl.append(" ".join(params))
    elif modality == "CT":
        kvp, ma = tag_float(ds, "KVP"), tag_float(ds, "XRayTubeCurrent")
        bl.append(" ".join(p for p in (f"{fmt_num(kvp, 0)}kV" if kvp else "",
                                       f"{fmt_num(ma, 0)}mA" if ma else "") if p))
        bl.append(tag(ds, "ConvolutionKernel"))
    if window is not None:
        bl.append(f"W:{window[1]:.0f} L:{window[0]:.0f}")

    return {"tl": [l for l in tl if l], "tr": [l for l in tr if l],
            "bl": [l for l in bl if l]}


def nice_scale_length_mm(max_mm):
    """max_mm 이하의 보기 좋은 스케일 바 길이 (mm)"""
    for value in (500, 200, 100, 50, 20, 10, 5, 2, 1):
        if value <= max_mm:
            return value
    return None
