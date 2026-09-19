# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
영상 융합 (Image Fusion) - 기준 시리즈 위에 두 번째 시리즈를 컬러로 겹쳐 보기

두 번째 시리즈를 기준 슬라이스의 픽셀 위치(환자 좌표)에서 다시 샘플링하므로
해상도·슬라이스 간격·방향이 달라도 같은 위치끼리 겹친다 (CT + PET 등).
Blend: Overlay(알파) / Add / Multiply / Checkerboard
"""
import numpy as np
from scipy import ndimage

BLEND_MODES = ["Overlay", "Add", "Multiply", "Checkerboard"]


class FusionLayer:
    def __init__(self, volume, window, lut, opacity=0.5, mode="Overlay", checker=32):
        self.volume = volume        # ai.volume.Volume (두 번째 시리즈)
        self.window = window        # (center, width)
        self.lut = lut              # (256, 3) uint8 또는 None(흑백)
        self.opacity = opacity
        self.mode = mode
        self.checker = checker      # Checkerboard 칸 크기 (픽셀)
        self._inv = np.linalg.inv(volume.affine_lps)
        self._cache = {}

    @property
    def series(self):
        return self.volume.series

    def sample_slice(self, base_affine, k, shape):
        """기준 슬라이스 k (base_affine: (col,row,k)→LPS)의 각 픽셀 위치에서 값 (h, w)"""
        key = (k, shape, base_affine.tobytes())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        h, w = shape
        rows, cols = np.mgrid[0:h, 0:w]
        idx = np.stack([cols.ravel(), rows.ravel(), np.full(h * w, k), np.ones(h * w)])
        world = base_affine @ idx
        vox = self._inv @ world            # (col, row, k) in fusion
        coords = [vox[2], vox[1], vox[0]]  # (k, row, col)
        # 부동소수 오차로 가장자리 픽셀이 밖으로 판정되지 않도록 반 칸 여유
        shape = self.volume.array.shape
        valid = np.ones(h * w, dtype=bool)
        for axis, c in enumerate(coords):
            valid &= (c >= -0.5) & (c <= shape[axis] - 0.5)
        values = ndimage.map_coordinates(self.volume.array, coords, order=1, mode="nearest")
        values[~valid] = np.nan
        out = values.reshape(h, w)
        if len(self._cache) > 32:
            self._cache.clear()
        self._cache[key] = out
        return out

    def rgb(self, values):
        """샘플 값 → (RGB uint8, 유효 여부 bool)"""
        center, width = self.window
        valid = np.isfinite(values)
        norm = np.clip((np.nan_to_num(values, nan=center - width) - (center - width / 2))
                       / max(width, 1e-6), 0, 1)
        g = (norm * 255).astype(np.uint8)
        rgb = self.lut[g] if self.lut is not None else np.repeat(g[..., None], 3, axis=2)
        return rgb, valid


def composite(base_rgb, layer, fused_values):
    """기준 RGB (h, w, 3) uint8 + 융합 층 → 합성 RGB"""
    top, valid = layer.rgb(fused_values)
    base = base_rgb.astype(np.float32)
    top_f = top.astype(np.float32)
    a = float(layer.opacity)
    if layer.mode == "Add":
        out = np.clip(base + a * top_f, 0, 255)
    elif layer.mode == "Multiply":
        out = base * ((1 - a) + a * top_f / 255.0)
    elif layer.mode == "Checkerboard":
        h, w = valid.shape
        n = max(2, int(layer.checker))
        yy, xx = np.mgrid[0:h, 0:w]
        cells = ((yy // n + xx // n) % 2).astype(bool)
        out = base.copy()
        out[cells] = top_f[cells]
    else:  # Overlay (알파 블렌딩)
        out = base * (1 - a) + top_f * a
    out = np.where(valid[..., None], out, base)
    return out.clip(0, 255).astype(np.uint8)
