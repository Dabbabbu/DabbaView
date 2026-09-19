# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
시리즈 → 3D 볼륨 (학습 데이터 내보내기 / 모델 추론용)

배열 순서: volume[k, row, col] (k = 정렬된 슬라이스 순서, 뷰포트 슬라이스 번호와 같음)
affine_lps: 복셀 인덱스 (col, row, k, 1) → 환자 좌표 LPS (mm)
"""
import numpy as np


class Volume:
    def __init__(self, array, spacing, affine_lps, series=None):
        self.array = array                  # (d, h, w) float32
        self.spacing = tuple(spacing)       # (Δk, Δrow, Δcol) mm
        self.affine_lps = affine_lps        # 4x4
        self.series = series

    @property
    def shape(self):
        return self.array.shape

    @property
    def affine_ras(self):
        """NIfTI 규약 (RAS+) affine"""
        return np.diag([-1.0, -1.0, 1.0, 1.0]) @ self.affine_lps

    def voxel_volume_mm3(self):
        return float(np.prod(self.spacing))


def series_shape(series):
    """시리즈의 (슬라이스 수, 행, 열). 크기가 다른 슬라이스가 섞여 있으면 None"""
    series.sort_slices()
    shapes = {(int(getattr(s, "Rows", 0)), int(getattr(s, "Columns", 0)))
              for s in series.slices}
    if len(shapes) != 1:
        return None
    rows, cols = shapes.pop()
    if rows <= 0 or cols <= 0:
        return None
    return series.num_slices, rows, cols


def series_spacing_affine(series):
    """(Δk, Δrow, Δcol), affine_lps. 공간 정보가 없으면 1mm 등간격 단위 행렬"""
    g = series.geometry
    n = series.num_slices
    if g is None:
        return (1.0, 1.0, 1.0), np.eye(4)
    d_row, d_col = (float(v) for v in g.spacings[0])
    if n > 1:
        step = (g.origins[-1] - g.origins[0]) / (n - 1)
    else:
        thickness = float(getattr(series.slices[0], "SliceThickness", 1) or 1)
        step = g.normals[0] * thickness
    d_k = float(np.linalg.norm(step)) or 1.0
    affine = np.eye(4)
    affine[:3, 0] = g.row_dirs[0] * d_col   # 열 인덱스(col) 증가 방향
    affine[:3, 1] = g.col_dirs[0] * d_row   # 행 인덱스(row) 증가 방향
    affine[:3, 2] = step
    affine[:3, 3] = g.origins[0]
    return (d_k, d_row, d_col), affine


def load_volume(series, progress=None):
    """시리즈 전체 픽셀(Rescale 적용)을 읽어 Volume 생성. 슬라이스 크기가 다르면 ValueError"""
    shape = series_shape(series)
    if shape is None:
        raise ValueError("슬라이스 크기가 서로 달라 3D 볼륨을 만들 수 없습니다.")
    arrays = series.get_all_pixel_arrays()
    if any(a is None for a in arrays):
        raise ValueError("일부 슬라이스의 픽셀을 읽지 못했습니다.")
    if any(a.ndim != 2 for a in arrays):
        raise ValueError("컬러/멀티프레임 영상은 지원하지 않습니다.")
    array = np.stack(arrays).astype(np.float32)
    spacing, affine = series_spacing_affine(series)
    return Volume(array, spacing, affine, series)
