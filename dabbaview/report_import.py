# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
기록 파일 가져오기 + 검사 자동 매칭

지원 형식
- 텍스트: .txt (UTF-8 / CP949 자동 판별), .rtf
- 이미지: .jpg .jpeg .png .bmp .tif .tiff, .pdf (페이지별 이미지로 렌더링)
- DICOM SR (Structured Report): .dcm → 본문 텍스트 추출

자동 매칭 규칙 (파일명 + 상위 폴더명 기준)
- DICOM SR: 파일 안의 StudyInstanceUID가 같으면 확실히 매칭
- 그 외: PatientID가 토큰으로 들어 있어야 함 (예: '1224059_20260917_report.txt')
  - 검사일(YYYYMMDD 또는 YYYY-MM-DD)도 있으면 그 검사에 매칭
  - 날짜가 없으면 그 환자의 검사가 하나뿐일 때만 매칭
"""
import os
import re

import pydicom

TEXT_EXTS = {".txt", ".text"}
RTF_EXTS = {".rtf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
DICOM_EXTS = {".dcm", ".dicom"}
ALL_EXTS = TEXT_EXTS | RTF_EXTS | IMAGE_EXTS | PDF_EXTS | DICOM_EXTS

FILE_FILTER = ("기록 (*.txt *.rtf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.pdf *.dcm);;"
               "All Files (*)")


def classify(path):
    """'text' | 'rtf' | 'image' | 'pdf' | 'dicom' | None"""
    ext = os.path.splitext(path)[1].lower()
    for kind, exts in (("text", TEXT_EXTS), ("rtf", RTF_EXTS), ("image", IMAGE_EXTS),
                       ("pdf", PDF_EXTS), ("dicom", DICOM_EXTS)):
        if ext in exts:
            return kind
    return None


# ─── 텍스트 ───

def read_text(path):
    """텍스트 파일 읽기 (UTF-8 → CP949 → Latin-1 순서로 시도)"""
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8-sig", "cp949", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_rtf(path):
    """RTF → 일반 텍스트 (\\ansicpg로 지정된 코드페이지 사용, 예: 949 = 한글)"""
    from striprtf.striprtf import rtf_to_text
    with open(path, "rb") as f:
        raw = f.read().decode("latin-1")  # RTF 자체는 ASCII + 이스케이프
    match = re.search(r"\\ansicpg(\d+)", raw)
    encoding = f"cp{match.group(1)}" if match else "cp1252"
    try:
        "".encode(encoding)
    except LookupError:
        encoding = "cp1252"
    return rtf_to_text(raw, encoding=encoding, errors="replace").strip()


# ─── DICOM SR ───

def _code_meaning(item, key):
    seq = getattr(item, key, None)
    if seq:
        return str(getattr(seq[0], "CodeMeaning", "") or "")
    return ""


def _sr_lines(items, depth=0):
    lines = []
    for item in items or []:
        name = _code_meaning(item, "ConceptNameCodeSequence")
        vtype = str(getattr(item, "ValueType", "") or "")
        indent = "  " * depth
        if vtype == "TEXT":
            value = str(getattr(item, "TextValue", "") or "")
            lines.append(f"{indent}{name}: {value}" if name else f"{indent}{value}")
        elif vtype == "CODE":
            lines.append(f"{indent}{name}: {_code_meaning(item, 'ConceptCodeSequence')}")
        elif vtype == "NUM":
            mv = getattr(item, "MeasuredValueSequence", None)
            if mv:
                unit = _code_meaning(mv[0], "MeasurementUnitsCodeSequence")
                lines.append(f"{indent}{name}: {mv[0].NumericValue} {unit}".rstrip())
        elif vtype in ("DATE", "TIME", "DATETIME", "PNAME", "UIDREF"):
            key = {"DATE": "Date", "TIME": "Time", "DATETIME": "DateTime",
                   "PNAME": "PersonName", "UIDREF": "UID"}[vtype]
            lines.append(f"{indent}{name}: {getattr(item, key, '')}")
        elif vtype == "CONTAINER" and name:
            lines.append("")
            lines.append(f"{indent}[{name}]")
        lines += _sr_lines(getattr(item, "ContentSequence", None), depth + (vtype == "CONTAINER"))
    return lines


def read_sr(path):
    """DICOM SR → (본문 텍스트, StudyInstanceUID, PatientID). SR이 아니면 ValueError"""
    ds = pydicom.dcmread(path, force=True)
    if str(getattr(ds, "Modality", "")) != "SR" and "ContentSequence" not in ds:
        raise ValueError("DICOM SR(Structured Report) 파일이 아닙니다.")
    title = _code_meaning(ds, "ConceptNameCodeSequence")
    lines = ([f"[{title}]"] if title else []) + _sr_lines(ds.get("ContentSequence"))
    text = "\n".join(lines).strip()
    return text, str(getattr(ds, "StudyInstanceUID", "") or ""), \
        str(getattr(ds, "PatientID", "") or "")


def sr_study_uid(path):
    """SR 파일의 StudyInstanceUID (빠르게, 픽셀 없음). 읽을 수 없으면 ''"""
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True,
                             specific_tags=["StudyInstanceUID", "Modality"])
        return str(getattr(ds, "StudyInstanceUID", "") or "")
    except Exception:
        return ""


# ─── 이미지 / PDF ───

def load_images(path):
    """이미지/PDF → QImage 목록 (PDF는 페이지마다 하나)"""
    from PyQt5.QtGui import QImage
    kind = classify(path)
    if kind == "image":
        image = QImage(path)
        if image.isNull():
            raise ValueError("이미지를 읽을 수 없습니다.")
        return [image]
    if kind == "pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        images = []
        try:
            for page in pdf:
                pil = page.render(scale=2).to_pil().convert("RGB")
                data = pil.tobytes("raw", "RGB")
                images.append(QImage(data, pil.width, pil.height, 3 * pil.width,
                                     QImage.Format_RGB888).copy())
        finally:
            pdf.close()
        return images
    raise ValueError("이미지/PDF 파일이 아닙니다.")


def load_text_content(path):
    """텍스트형 파일(txt/rtf/SR)의 본문 텍스트"""
    kind = classify(path)
    if kind == "text":
        return read_text(path)
    if kind == "rtf":
        return read_rtf(path)
    if kind == "dicom":
        return read_sr(path)[0]
    raise ValueError("텍스트 기록이 아닙니다.")


# ─── 자동 매칭 ───

def _name_text(path, root=None):
    """매칭에 쓰는 이름: 파일명 + (root 아래) 상위 폴더명"""
    parts = [os.path.basename(path)]
    parent = os.path.dirname(path)
    while parent and (root is None or os.path.abspath(parent) != os.path.abspath(root)):
        name = os.path.basename(parent)
        if not name:
            break
        parts.append(name)
        if root is None:
            break  # root가 없으면 바로 위 폴더까지만
        parent = os.path.dirname(parent)
    return " ".join(parts)


def _tokens(text):
    return set(t for t in re.split(r"[^0-9A-Za-z]+", text) if t)


def _dates_in(text):
    dates = set(re.findall(r"(?<!\d)(\d{8})(?!\d)", text))
    for y, m, d in re.findall(r"(?<!\d)(\d{4})[-._](\d{2})[-._](\d{2})(?!\d)", text):
        dates.add(f"{y}{m}{d}")
    return dates


def match_study(path, studies, root=None):
    """studies: [{'study_uid', 'patient_id', 'study_date'}] → 매칭된 study_uid 또는 None"""
    if classify(path) == "dicom":
        uid = sr_study_uid(path)
        return uid if any(s["study_uid"] == uid for s in studies) and uid else None
    text = _name_text(path, root)
    tokens = _tokens(text)
    dates = _dates_in(text)
    candidates = [s for s in studies
                  if len(s["patient_id"]) >= 3 and s["patient_id"] in tokens]
    dated = [s for s in candidates if s["study_date"] in dates]
    if len(dated) == 1:
        return dated[0]["study_uid"]
    if not dates and len(candidates) == 1:
        # 날짜가 없는 파일명: 그 환자의 검사가 하나뿐일 때만
        return candidates[0]["study_uid"]
    return None


def mentions_other_patient(path, study, studies):
    """파일명에 다른 환자의 ID가 있고 이 검사의 환자 ID는 없으면 True (잘못 연결 방지)"""
    tokens = _tokens(_name_text(path))
    if study["patient_id"] in tokens:
        return False
    return any(s["patient_id"] != study["patient_id"] and len(s["patient_id"]) >= 3
               and s["patient_id"] in tokens for s in studies)


def scan_folder(folder, studies, max_files=5000):
    """기록 폴더를 훑어 {study_uid: [paths]} (하위 폴더 포함)"""
    result = {}
    if not folder or not os.path.isdir(folder):
        return result
    count = 0
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name.startswith(".") or classify(name) is None:
                continue
            count += 1
            if count > max_files:
                return result
            path = os.path.join(root, name)
            uid = match_study(path, studies, root=folder)
            if uid:
                result.setdefault(uid, []).append(path)
    return result
