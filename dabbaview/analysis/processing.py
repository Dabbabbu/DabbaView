# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
영상 처리 필터 (ImageJ Process 메뉴에 해당)

모든 필터는 2D 슬라이스마다(기본) 또는 3D 볼륨 전체에 적용할 수 있고,
결과는 항상 새 배열 → 새 시리즈 (원본 보존).
"""
import numpy as np
from scipy import ndimage

FILTERS = {
    # key: (메뉴 이름, [(파라미터, 라벨, 기본값, 최소, 최대, 소수 자리)])
    "gaussian": ("Gaussian Blur", [("sigma", "Sigma (픽셀)", 1.5, 0.1, 50, 2)]),
    "median": ("Median Filter", [("size", "커널 크기 (픽셀, 홀수)", 3, 1, 31, 0)]),
    "unsharp": ("Unsharp Mask (샤프닝)", [("sigma", "Sigma (픽셀)", 2.0, 0.1, 50, 2),
                                         ("amount", "강도 (Mask weight)", 0.6, 0.0, 5.0, 2)]),
    "sobel": ("Edge Detection: Sobel", []),
    "canny": ("Edge Detection: Canny", [("sigma", "Sigma", 1.5, 0.1, 20, 2),
                                        ("low", "Low threshold (백분위)", 10, 0, 100, 0),
                                        ("high", "High threshold (백분위)", 25, 0, 100, 0)]),
    "erosion": ("Morphology: Erosion", [("radius", "반지름 (픽셀)", 1, 1, 30, 0)]),
    "dilation": ("Morphology: Dilation", [("radius", "반지름 (픽셀)", 1, 1, 30, 0)]),
    "opening": ("Morphology: Opening", [("radius", "반지름 (픽셀)", 1, 1, 30, 0)]),
    "closing": ("Morphology: Closing", [("radius", "반지름 (픽셀)", 1, 1, 30, 0)]),
}

MORPHOLOGY = ("erosion", "dilation", "opening", "closing")


def _disk(radius, ndim):
    r = int(radius)
    grid = np.ogrid[tuple(slice(-r, r + 1) for _ in range(ndim))]
    return sum(g * g for g in grid) <= r * r


def _filter_block(block, key, p):
    """block: 2D 또는 3D float32 배열"""
    if key == "gaussian":
        return ndimage.gaussian_filter(block, p["sigma"])
    if key == "median":
        size = int(p["size"]) | 1
        return ndimage.median_filter(block, size=size)
    if key == "unsharp":
        blurred = ndimage.gaussian_filter(block, p["sigma"])
        # ImageJ: (원본 - weight*흐림) / (1 - weight)
        weight = min(float(p["amount"]), 0.99) if p["amount"] < 1 else None
        if weight is not None:
            return (block - weight * blurred) / (1 - weight)
        return block + p["amount"] * (block - blurred)
    if key == "sobel":
        grads = [ndimage.sobel(block, axis=a) for a in range(block.ndim)]
        return np.sqrt(sum(g * g for g in grads))
    if key == "canny":
        return _canny(block, p)
    if key in MORPHOLOGY:
        footprint = _disk(p["radius"], block.ndim)
        op = {"erosion": ndimage.grey_erosion, "dilation": ndimage.grey_dilation,
              "opening": ndimage.grey_opening, "closing": ndimage.grey_closing}[key]
        return op(block, footprint=footprint)
    raise ValueError(f"알 수 없는 필터: {key}")


def _canny(block, p):
    from skimage import feature
    if block.ndim == 3:
        return np.stack([_canny(s, p) for s in block])
    grad = np.hypot(ndimage.sobel(ndimage.gaussian_filter(block, p["sigma"]), 0),
                    ndimage.sobel(ndimage.gaussian_filter(block, p["sigma"]), 1))
    lo, hi = np.percentile(grad, [p["low"], p["high"]]) if grad.any() else (0, 0)
    hi = max(hi, lo + 1e-6)
    edges = feature.canny(block, sigma=p["sigma"], low_threshold=lo, high_threshold=hi)
    return edges.astype(np.float32) * 1000.0   # 윈도에서 잘 보이도록 0/1000


def apply_filter(volume, key, params, mode="2d", slices=None, progress=None, cancelled=None):
    """volume (d, h, w) → 결과 (d, h, w) float32. mode: '2d'(슬라이스마다) / '3d'

    slices: 2D 모드에서 처리할 슬라이스 목록 (None = 전체, 나머지는 원본 그대로)
    """
    volume = np.asarray(volume, dtype=np.float32)
    if mode == "3d" and key != "canny":
        if progress:
            progress(0, 1)
        out = _filter_block(volume, key, params)
        if progress:
            progress(1, 1)
        return np.asarray(out, dtype=np.float32)
    out = volume.copy()
    indices = range(volume.shape[0]) if slices is None else slices
    total = len(indices)
    for i, k in enumerate(indices):
        if cancelled and cancelled():
            return None
        out[k] = _filter_block(volume[k], key, params)
        if progress:
            progress(i + 1, total)
    return out


def describe(key, params):
    name = FILTERS[key][0]
    args = ", ".join(f"{k}={v:g}" for k, v in params.items())
    return f"{name}" + (f" ({args})" if args else "")
