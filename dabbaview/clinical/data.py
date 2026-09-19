# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
다중 영상 시리즈 → 4D 배열 (파라미터 × 위치 × 행 × 열)

한 시리즈 안에 같은 위치의 영상이 여러 장인 경우(다중 b-value, 다중 TE/TI, cine 위상,
동적 조영)를 위치별로 묶고, 영상마다 파라미터(b-value, TE, TI, 시간)를 DICOM 태그에서 읽는다.
여러 시리즈(예: b-value마다 따로 저장)를 합칠 수도 있다.
"""
import re

import numpy as np

from .. import dicom_info

PARAMS = {
    "b": "b-value (s/mm²)",
    "te": "TE (ms)",
    "ti": "TI (ms)",
    "time": "시간 (s)",
    "phase": "심장 위상 (TriggerTime ms)",
    "index": "순서",
}


def _float(value):
    try:
        if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            value = value[0]
        return float(value)
    except (TypeError, ValueError, IndexError):
        return None


def _seconds(text):
    """DICOM TM (HHMMSS.frac) → 초"""
    text = str(text or "").strip()
    if len(text) < 4:
        return None
    try:
        hh, mm = int(text[0:2]), int(text[2:4])
        ss = float(text[4:]) if len(text) > 4 else 0.0
        return hh * 3600 + mm * 60 + ss
    except ValueError:
        return None


def slice_param(ds, kind):
    """영상 한 장의 파라미터 값 (없으면 None)"""
    if kind == "b":
        value = dicom_info.b_value(ds)
        if value is None:
            for tag in ((0x0019, 0x100C), (0x2001, 0x1003)):   # Siemens, Philips
                elem = ds.get(tag)
                if elem is not None:
                    value = _float(elem.value)
                    if value is not None:
                        break
        if value is None:   # 시리즈 설명·이미지 코멘트의 'b=800' 같은 표기
            text = f"{getattr(ds, 'ImageComments', '')} {getattr(ds, 'SequenceName', '')}"
            m = re.search(r"b\s*=?\s*(\d{2,4})", text)
            value = float(m.group(1)) if m else None
        return value
    if kind == "te":
        return _float(getattr(ds, "EchoTime", None))
    if kind == "ti":
        value = _float(getattr(ds, "InversionTime", None))
        if value is None:   # Siemens MOLLI: ImageComments "TI 250"
            m = re.search(r"TI\s*[:=]?\s*(\d+(\.\d+)?)", str(getattr(ds, "ImageComments", "")))
            value = float(m.group(1)) if m else None
        return value
    if kind == "phase":
        return _float(getattr(ds, "TriggerTime", None))
    if kind == "time":
        for keyword in ("AcquisitionTime", "ContentTime"):
            value = _seconds(getattr(ds, keyword, ""))
            if value is not None:
                return value
        value = _float(getattr(ds, "TriggerTime", None))
        return value / 1000.0 if value is not None else None
    return None


def position_key(ds, precision=0.5):
    """같은 위치 판정용 키 (법선 방향 위치를 precision mm 단위로 반올림)"""
    ipp = getattr(ds, "ImagePositionPatient", None)
    normal = dicom_info.slice_normal(ds)
    if ipp is None or normal is None:
        return round(float(getattr(ds, "SliceLocation", 0) or 0) / precision)
    return round(float(np.dot([float(v) for v in ipp], normal)) / precision)


class Stack:
    """4D 데이터: array[p, z, row, col] + 파라미터 값 values[p] + 위치별 영상 참조"""

    def __init__(self, array, values, kind, refs, positions):
        self.array = array          # float32 (P, Z, H, W)
        self.values = np.asarray(values, dtype=float)
        self.kind = kind
        self.refs = refs            # refs[p][z] = (series, 영상 번호)
        self.positions = positions  # 위치 키 (Z)

    @property
    def shape(self):
        return self.array.shape

    def ref(self, p, z):
        return self.refs[p][z]


def build_stack(series_list, kind, values_override=None, relative_time=True):
    """시리즈(들) → Stack. 위치마다 같은 파라미터 값 집합이 있어야 함

    values_override: 파라미터 값을 직접 지정 (영상 순서대로 같은 위치에서 반복되는 값 목록)
    """
    groups = {}   # 위치 → [(값, series, index)]
    for series in series_list:
        series.sort_slices()
        for i, ds in enumerate(series.slices):
            groups.setdefault(position_key(ds), []).append(
                (slice_param(ds, kind) if kind != "index" else None, series, i,
                 int(getattr(ds, "InstanceNumber", i) or i)))
    if not groups:
        raise ValueError("영상이 없습니다.")
    if kind == "b" and values_override is None:
        # GE 등: b0 영상에는 b-value 태그가 없고 나머지에만 있는 경우 → 위치마다 하나뿐인 빈 값 = b0
        if all(sum(v is None for v, *_ in items) == 1 and any(v is not None for v, *_ in items)
               for items in groups.values()):
            groups = {pos: [(0.0 if v is None else v, *rest) for v, *rest in items]
                      for pos, items in groups.items()}
    positions = sorted(groups)
    counts = {len(v) for v in groups.values()}
    if len(counts) != 1:
        raise ValueError(f"위치마다 영상 수가 다릅니다 ({sorted(counts)}). 시리즈를 확인하세요.")
    n = counts.pop()
    ordered = []
    for pos in positions:
        items = groups[pos]
        if values_override is None and all(v is not None for v, *_ in items):
            items = sorted(items, key=lambda t: (t[0], t[3]))
        else:
            items = sorted(items, key=lambda t: t[3])   # InstanceNumber 순서
        ordered.append(items)
    if values_override is not None:
        values = [float(v) for v in values_override]
        if len(values) != n:
            raise ValueError(f"값 {len(values)}개를 입력했지만 위치마다 영상이 {n}장입니다.")
    else:
        first = [t[0] for t in ordered[0]]
        if any(v is None for v in first):
            values = list(range(n))
            kind = "index"
        else:
            # 위치마다 획득 시각이 조금씩 다르므로 위치별 값을 평균
            values = list(np.mean([[t[0] for t in items] for items in ordered], axis=0))
    if kind == "time" and relative_time:
        values = list(np.asarray(values) - values[0])
    refs = [[(items[p][1], items[p][2]) for items in ordered] for p in range(n)]
    first_series, first_index = refs[0][0]
    shape = first_series.get_pixel_array(first_index).shape
    array = np.zeros((n, len(positions)) + shape, dtype=np.float32)
    for p in range(n):
        for z in range(len(positions)):
            series, index = refs[p][z]
            img = series.get_pixel_array(index)
            if img is None or img.shape != shape:
                raise ValueError("영상 크기가 서로 다릅니다.")
            array[p, z] = img
    return Stack(array, values, kind, refs, positions)


def detect(series, kind):
    """시리즈에서 kind 파라미터 값 목록 (중복 제거·정렬), 없으면 []"""
    values = {slice_param(ds, kind) for ds in series.slices}
    if kind == "b" and None in values and len(values) > 1:
        values.add(0.0)      # b0 영상에 태그가 없는 경우 (build_stack과 같은 규칙)
    values.discard(None)
    return sorted(values)


def parse_values(text):
    """'0, 50, 100 800' → [0, 50, 100, 800]"""
    parts = re.split(r"[,\s;]+", text.strip())
    return [float(p) for p in parts if p]
