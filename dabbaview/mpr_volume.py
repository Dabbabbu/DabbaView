# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
MPR·3D에 쓸 볼륨 고르기 - 어떤 슬라이스를 쌓아야 '공간'이 되는지 정한다

cine(심장 MRI 등)은 한 위치에 여러 시간 위상이 있다.
그대로 쌓으면 시간축이 공간축으로 둔갑해 재구성 영상이 늘어나거나 엉뚱하게 나온다.
여기서는 위상을 하나만 골라 '위치마다 한 장'으로 만든 뒤 넘긴다.

또 한 평면짜리 시리즈(심장 MRI의 2ch·3ch·4ch Long Axis)는 그 자체로는 재구성할 수 없다.
왜 안 되는지 설명을 돌려주고, 같은 방향의 다른 시리즈와 합치는 길을 열어 둔다.
"""
import numpy as np

from .geometry import build_series_geometry
from .phases import phase_map

MIN_POSITIONS = 2          # 볼륨이 되려면 서로 다른 위치가 적어도 둘
PARALLEL_TOLERANCE = 0.98  # 법선이 이만큼 같으면 '같은 방향'으로 보고 합칠 수 있다


class VolumePlan:
    """볼륨으로 쌓을 슬라이스 목록과, 안 되면 그 이유

    slices    : 쌓을 (series, index) 목록 — 공간 순서
    reason    : 볼륨을 못 만들 때 사람이 읽을 설명 (만들 수 있으면 "")
    hint      : 사용자가 다음에 할 수 있는 일 (없으면 "")
    n_phases  : 이 시리즈의 시간 위상 수 (1이면 cine 아님)
    phase     : 볼륨에 쓴 위상 번호
    """

    def __init__(self, slices=(), reason="", hint="", n_phases=1, phase=0):
        self.slices = list(slices)
        self.reason = reason
        self.hint = hint
        self.n_phases = n_phases
        self.phase = phase

    @property
    def ok(self):
        return len(self.slices) >= MIN_POSITIONS and not self.reason

    def __len__(self):
        return len(self.slices)


def _normal_of(series, index=0):
    """그 슬라이스가 놓인 평면의 법선 (못 구하면 None)"""
    geometry = series.geometry
    if geometry is None or index >= len(geometry.normals):
        return None
    return np.asarray(geometry.normals[index], dtype=float)


def is_parallel(a, b, tolerance=PARALLEL_TOLERANCE):
    """두 시리즈가 같은 방향으로 찍혔는가 (법선이 거의 같은 축)"""
    na, nb = _normal_of(a), _normal_of(b)
    if na is None or nb is None:
        return False
    return abs(float(na @ nb)) >= tolerance


def plan_for_series(series, phase=0):
    """시리즈 하나 → VolumePlan

    cine이면 위상 하나만 골라 위치마다 한 장씩 쓴다.
    """
    if series is None or series.num_slices == 0:
        return VolumePlan(reason="영상이 없습니다.")

    series.sort_slices()
    pmap = phase_map(series)
    n_phases = pmap.n_phases if pmap else 1

    if pmap and pmap.n_phases > 1:
        # cine · perfusion: 위치마다 고른 위상 한 장 (없으면 그 위치에서 가장 가까운 위상)
        want = max(0, min(n_phases - 1, int(phase)))
        picked = []
        for position in range(pmap.n_positions):
            index = pmap.slice_at(position, want)
            if index is not None:
                picked.append((series, index))
        if len(picked) < MIN_POSITIONS:
            return VolumePlan(
                reason=f"이 시리즈는 한 평면을 시간에 따라 {n_phases}번 찍은 것입니다"
                       f" (위치 1곳 · 위상 {n_phases}개).",
                hint="한 평면만으로는 다른 각도로 다시 자를 수 없습니다. "
                     "같은 방향으로 여러 위치를 찍은 시리즈를 고르거나, "
                     "시리즈 목록에서 같은 방향의 시리즈들을 함께 골라 'MPR로 합쳐 보기'를 쓰세요.",
                n_phases=n_phases, phase=want)
        return VolumePlan(picked, n_phases=n_phases, phase=want)

    # cine이 아닌 보통 시리즈: 공간 순서 그대로
    picked = [(series, i) for i in series.spatial_order()]
    if len(picked) < MIN_POSITIONS:
        return VolumePlan(
            reason="이 시리즈는 한 장짜리입니다.",
            hint="한 장만으로는 다른 각도로 다시 자를 수 없습니다. "
                 "같은 방향으로 여러 장을 찍은 시리즈를 고르거나, "
                 "시리즈 목록에서 같은 방향의 시리즈들을 함께 골라 'MPR로 합쳐 보기'를 쓰세요.",
            n_phases=n_phases)
    return VolumePlan(picked, n_phases=n_phases)


def plan_for_many(series_list, phase=0):
    """여러 시리즈를 하나의 볼륨으로 — 같은 방향(평행)일 때만

    심장 MRI의 2ch·4ch처럼 방향이 서로 다른 평면은 합쳐도 해부학이 되지 않으므로 거절한다.
    """
    chosen = [s for s in series_list if s is not None and s.num_slices > 0]
    if not chosen:
        return VolumePlan(reason="고른 시리즈가 없습니다.")
    if len(chosen) == 1:
        return plan_for_series(chosen[0], phase)

    first = chosen[0]
    odd = [s for s in chosen[1:] if not is_parallel(first, s)]
    if odd:
        names = ", ".join(s.description or "이름 없음" for s in odd[:3])
        return VolumePlan(
            reason=f"고른 시리즈의 촬영 방향이 서로 다릅니다 ({names} …).",
            hint="방향이 다른 평면(예: Short Axis와 4ch)은 합쳐도 3차원 해부가 되지 않습니다. "
                 "같은 방향으로 찍은 시리즈끼리만 합쳐 주세요.")

    picked = []
    for s in chosen:
        plan = plan_for_series(s, phase)
        if plan.slices:
            picked.extend(plan.slices)
        else:                       # 한 장짜리라도 방향이 같으면 볼륨에 보탠다
            s.sort_slices()
            picked.extend((s, i) for i in range(s.num_slices))
    if len(picked) < MIN_POSITIONS:
        return VolumePlan(reason="합쳐도 위치가 한 곳뿐이라 볼륨이 되지 않습니다.")

    # 법선 방향 위치순으로 다시 정렬 (시리즈가 섞여 있어도 공간 순서가 되게)
    normal = _normal_of(first)
    if normal is not None:
        def along(item):
            s, i = item
            origin = s.geometry.origins[i] if s.geometry is not None else np.zeros(3)
            return float(np.asarray(origin, dtype=float) @ normal)
        picked.sort(key=along)

    # 같은 위치가 겹치면(시리즈끼리 겹쳐 찍힘) 하나만 남긴다
    deduped, seen = [], []
    for item in picked:
        s, i = item
        if s.geometry is None:
            deduped.append(item)
            continue
        value = float(np.asarray(s.geometry.origins[i], dtype=float) @ (normal if normal is not None else np.zeros(3)))
        if any(abs(value - v) < 1e-3 for v in seen):
            continue
        seen.append(value)
        deduped.append(item)
    if len(deduped) < MIN_POSITIONS:
        return VolumePlan(reason="합쳐도 위치가 한 곳뿐이라 볼륨이 되지 않습니다.")
    return VolumePlan(deduped)


def build_volume(plan):
    """VolumePlan → (volume, spacing, geometry, datasets) / 못 만들면 (None, …)"""
    if not plan or not plan.slices:
        return None, np.ones(3), None, []

    arrays, datasets = [], []
    cache = {}
    for series, index in plan.slices:
        if id(series) not in cache:
            cache[id(series)] = series.get_all_pixel_arrays()
        arr = cache[id(series)][index]
        if arr is not None and arr.ndim == 2:
            arrays.append(arr)
            datasets.append(series.slices[index])
    if len(arrays) < MIN_POSITIONS:
        return None, np.ones(3), None, []

    # 크기가 다른 장은 뺀다 (첫 장 기준)
    ref = arrays[0].shape
    keep = [j for j, a in enumerate(arrays) if a.shape == ref]
    if len(keep) < MIN_POSITIONS:
        return None, np.ones(3), None, []

    volume = np.stack([arrays[j] for j in keep], axis=0)
    kept_ds = [datasets[j] for j in keep]
    geometry = build_series_geometry(kept_ds)
    if geometry is not None:
        d_row, d_col = geometry.spacings[0]
        d_slice = geometry.slice_spacing() or d_row
        spacing = np.array([d_slice, d_row, d_col], dtype=float)
    else:
        spacing = np.ones(3)
    return volume, spacing, geometry, kept_ds
