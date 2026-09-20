# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Phase (심장 위상 · 시간 위상) 나누기 - cine · perfusion처럼 한 위치에 여러 장이 있는 시리즈

위상 구분: TemporalPositionIdentifier (0020,0100) → TriggerTime (0018,1060) →
(태그가 없으면) 같은 위치에 여러 장이 있을 때 그 순서.
같은 위치(ImagePositionPatient를 법선에 투영한 값)끼리 묶고, 그 안에서 위상 값 순서가 위상 번호가 된다.
CardiacNumberOfImages (0018,1090)는 확인용으로만 쓴다.
"""
from .clinical.data import position_key

MIN_PHASES = 5   # cine · perfusion처럼 진짜 시간 위상만 (T1/T2 map, DWI b값은 제외)


def _order_value(ds, fallback):
    """위상 태그가 없을 때 같은 위치 안에서의 순서 (InstanceNumber → 불러온 순서)"""
    value = getattr(ds, "InstanceNumber", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _phase_value(ds):
    for keyword in ("TemporalPositionIdentifier", "TriggerTime"):
        value = getattr(ds, keyword, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None   # AcquisitionTime은 b값·TI가 다른 영상도 나눠 버려서 쓰지 않음


class PhaseMap:
    """위상 · 위치 표: 슬라이스 번호 ↔ (위치 번호, 위상 번호)"""

    def __init__(self, n_phases, n_positions, slice_of, of_slice, labels, unit):
        self.n_phases = n_phases
        self.n_positions = n_positions
        self.slice_of = slice_of      # (위치, 위상) → 슬라이스 번호
        self.of_slice = of_slice      # 슬라이스 번호 → (위치, 위상)
        self.labels = labels          # 위상 버튼에 쓸 글 (1, 2, 3 …)
        self.unit = unit              # 위상 값 단위 설명 (툴팁)

    def where(self, index):
        return self.of_slice.get(index)

    def slice_at(self, position, phase):
        """그 위치 · 위상의 슬라이스. 없으면 같은 위치에서 가장 가까운 위상"""
        hit = self.slice_of.get((position, phase))
        if hit is not None:
            return hit
        near = [(abs(p - phase), i) for (pos, p), i in self.slice_of.items() if pos == position]
        return min(near)[1] if near else None

    def step_position(self, index, direction):
        """위상은 그대로, 위치만 앞뒤로"""
        where = self.where(index)
        if where is None:
            return None
        position = max(0, min(self.n_positions - 1, where[0] + direction))
        return self.slice_at(position, where[1])

    def step_phase(self, index, direction=1):
        """위치는 그대로, 위상만 (끝에서 처음으로 돌아감 - 시네)"""
        where = self.where(index)
        if where is None:
            return None
        return self.slice_at(where[0], (where[1] + direction) % self.n_phases)


def phase_map(series):
    """→ PhaseMap (위상이 2개 이상일 때만). 결과는 시리즈에 저장해 두고 다시 씀"""
    cached = getattr(series, "_dv_phase_map", "miss")
    if cached != "miss":
        return cached
    result = _build(series)
    try:
        series._dv_phase_map = result
    except AttributeError:
        pass
    return result


def _build(series):
    slices = getattr(series, "slices", None) or []
    if len(slices) < 4:
        return None
    keys = [position_key(ds) for ds in slices]
    values = [_phase_value(ds) for ds in slices]
    if any(v is None for v in values):
        # 위상 태그가 없는 시리즈(일부 cine · 다중 프레임): 같은 위치가 여러 번 나오면 그 순서를 위상으로
        order_value = [_order_value(ds, i) for i, ds in enumerate(slices)]
        seen = {}
        values = []
        for k, ov in zip(keys, order_value):
            seen.setdefault(k, []).append(ov)
        rank = {k: {v: i for i, v in enumerate(sorted(vs))} for k, vs in seen.items()}
        for k, ov in zip(keys, order_value):
            values.append(float(rank[k][ov]))
    positions = sorted(set(keys))
    if len(positions) * 2 > len(slices):   # 위치마다 한두 장 = 위상 아님
        return None
    order = {k: i for i, k in enumerate(positions)}
    by_position = {}
    for i, (k, v) in enumerate(zip(keys, values)):
        by_position.setdefault(order[k], []).append((v, i))
    n_phases = max(len(v) for v in by_position.values())
    counts = sorted(len(v) for v in by_position.values())
    typical = counts[len(counts) // 2]   # 가운데 값 - 일부만 내보낸 검사(위치마다 장수가 다름)도 받아들임
    if n_phases < MIN_PHASES or typical < MIN_PHASES:   # b값 · TI · TE가 다른 파라미터 영상은 위상이 아님
        return None
    slice_of, of_slice = {}, {}
    for position, items in by_position.items():
        for phase, (_v, index) in enumerate(sorted(items)):
            slice_of[(position, phase)] = index
            of_slice[index] = (position, phase)
    unit = ("TemporalPositionIdentifier" if getattr(slices[0], "TemporalPositionIdentifier", None) is not None
            else "TriggerTime (ms)" if getattr(slices[0], "TriggerTime", None) is not None
            else "같은 위치의 영상 순서")
    labels = [str(i + 1) for i in range(n_phases)]
    return PhaseMap(n_phases, len(positions), slice_of, of_slice, labels, unit)
