# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
영상 → 8비트 그레이스케일 렌더링 (Key Image 내보내기, DICOM Print 공용)
"""
import numpy as np


def render_8bit(series, index, window=None, inverted=False):
    """(rows, cols) uint8 배열. 컬러 영상은 휘도로 변환. 실패 시 None"""
    arr = series.get_pixel_array(index)
    if arr is None:
        return None
    if arr.ndim == 3 and arr.shape[2] in (3, 4):
        rgb = np.clip(arr[..., :3], 0, 255)
        img = (rgb @ np.array([0.299, 0.587, 0.114])).astype(np.uint8)
    else:
        if arr.ndim == 3:
            arr = arr[0]
        wc, ww = window or series.get_default_window()
        ww = max(float(ww), 1.0)
        img = np.clip((arr - (wc - ww / 2)) / ww * 255, 0, 255).astype(np.uint8)
    if inverted:
        img = 255 - img
    return np.ascontiguousarray(img)
