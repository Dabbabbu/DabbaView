# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
학습 데이터 전처리 - 리샘플링 / 크롭 / 노이즈 제거 / 히스토그램 매칭 / 정규화

영상 볼륨과 마스크를 함께 변환한다 (마스크는 최근접 보간 → 라벨 값 유지).
적용 순서: 크롭 → 리샘플링 → 노이즈 제거 → 히스토그램 매칭 → 정규화
"""
import numpy as np
from scipy import ndimage

from .volume import Volume


class PreprocessOptions:
    def __init__(self):
        self.crop = False
        self.crop_margin_mm = 10.0
        self.resample = False
        self.target_spacing = (1.0, 1.0, 1.0)     # (Δk, Δrow, Δcol) mm
        self.denoise = None                       # None / "gaussian" / "median"
        self.gaussian_sigma = 1.0                 # 복셀 단위
        self.median_size = 3
        self.histogram_reference = None           # 기준 볼륨 배열 (np.ndarray)
        self.normalize = False
        self.window = (40.0, 400.0)               # (center, width) → 0~1

    def is_identity(self):
        return not (self.crop or self.resample or self.denoise
                    or self.histogram_reference is not None or self.normalize)

    def describe(self):
        steps = []
        if self.crop:
            steps.append(f"crop(label bbox + {self.crop_margin_mm:g}mm)")
        if self.resample:
            steps.append("resample({:g}x{:g}x{:g}mm)".format(*self.target_spacing))
        if self.denoise == "gaussian":
            steps.append(f"gaussian(σ={self.gaussian_sigma:g})")
        elif self.denoise == "median":
            steps.append(f"median({self.median_size})")
        if self.histogram_reference is not None:
            steps.append("histogram-match")
        if self.normalize:
            steps.append("normalize(W{1:g}/L{0:g} → 0-1)".format(*self.window))
        return steps


def crop_box(mask, spacing, margin_mm):
    """라벨 영역을 감싸는 (슬라이스들) 범위 + 여유. 라벨이 없으면 None"""
    if mask is None or not mask.any():
        return None
    idx = np.nonzero(mask)
    box = []
    for axis, sp in enumerate(spacing):
        margin = int(np.ceil(margin_mm / sp)) if sp > 0 else 0
        lo = max(0, int(idx[axis].min()) - margin)
        hi = min(mask.shape[axis], int(idx[axis].max()) + 1 + margin)
        box.append(slice(lo, hi))
    return tuple(box)


def histogram_match(source, reference, n_quantiles=1024):
    """source 값 분포를 reference 분포에 맞춤 (분위수 매핑)"""
    q = np.linspace(0, 1, n_quantiles)
    src_q = np.quantile(source, q)
    ref_q = np.quantile(reference, q)
    # 같은 값이 반복되는 분위수 구간은 interp가 처리하도록 단조 증가 보장
    src_q = np.maximum.accumulate(src_q + np.arange(n_quantiles) * 1e-9)
    return np.interp(source.ravel(), src_q, ref_q).reshape(source.shape).astype(np.float32)


def normalize_window(array, center, width):
    low = center - width / 2.0
    return np.clip((array - low) / max(width, 1e-6), 0.0, 1.0).astype(np.float32)


def apply(volume, mask, options, progress=None):
    """(Volume, mask) → 전처리된 (Volume, mask). 원본은 바꾸지 않음"""
    array = volume.array
    spacing = np.array(volume.spacing, dtype=float)
    affine = volume.affine_lps.copy()

    def step(name):
        if progress:
            progress(name)

    if options.crop:
        box = crop_box(mask, spacing, options.crop_margin_mm)
        if box is not None:
            step("크롭")
            array = array[box]
            mask = mask[box] if mask is not None else None
            # 원점 이동: 인덱스 (col, row, k) = (box[2].start, box[1].start, box[0].start)
            offset = np.array([box[2].start, box[1].start, box[0].start, 0.0])
            affine[:3, 3] = (affine @ np.append(offset[:3], 1.0))[:3]

    if options.resample:
        step("리샘플링")
        target = np.array(options.target_spacing, dtype=float)
        old_shape = np.array(array.shape, dtype=float)
        array = ndimage.zoom(array, spacing / target, order=1)
        if mask is not None:
            mask = ndimage.zoom(mask, spacing / target, order=0)
        # zoom은 양 끝 복셀 중심을 고정 → 실제 간격 = 원래 길이 / (새 샘플 수 - 1)
        new_shape = np.array(array.shape, dtype=float)
        new_spacing = np.where(new_shape > 1,
                               spacing * (old_shape - 1) / np.maximum(new_shape - 1, 1),
                               spacing)
        scale = new_spacing / spacing   # (Δk, Δrow, Δcol) 비율
        affine[:3, 0] *= scale[2]
        affine[:3, 1] *= scale[1]
        affine[:3, 2] *= scale[0]
        spacing = new_spacing

    if options.denoise == "gaussian":
        step("가우시안 필터")
        array = ndimage.gaussian_filter(array, sigma=options.gaussian_sigma)
    elif options.denoise == "median":
        step("미디안 필터")
        array = ndimage.median_filter(array, size=int(options.median_size))

    if options.histogram_reference is not None:
        step("히스토그램 매칭")
        array = histogram_match(array, options.histogram_reference)

    if options.normalize:
        step("정규화")
        array = normalize_window(array, *options.window)

    return Volume(np.asarray(array, dtype=np.float32), tuple(spacing), affine,
                  volume.series), mask
