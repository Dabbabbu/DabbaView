# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
컬러맵 (LUT, Look-Up Table): 8비트 값 → RGB (256, 3) uint8

기본 제공: Gray, Hot, Cool, Jet, Viridis, Magma, Inferno, Plasma, Bone, Rainbow, Fire(ImageJ)
사용자 LUT: ImageJ .lut (768바이트 이진 / 32바이트 헤더 + 768), 텍스트 (R G B 또는 i R G B 256줄)
"""
import os

import numpy as np

BUILTIN = ["Gray", "Hot", "Cool", "Jet", "Viridis", "Magma", "Inferno", "Plasma", "Bone",
           "Rainbow", "Fire"]

_MPL_NAMES = {"Hot": "hot", "Cool": "cool", "Jet": "jet", "Viridis": "viridis",
              "Magma": "magma", "Inferno": "inferno", "Plasma": "plasma", "Bone": "bone",
              "Rainbow": "rainbow"}

_custom = {}   # 이름 → LUT (사용자가 불러온 것)


def _fire():
    """ImageJ 'Fire' LUT (제어점 보간)"""
    r = [0, 0, 1, 25, 49, 73, 98, 122, 146, 162, 173, 184, 195, 207, 217, 229, 240, 252,
         255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255]
    g = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 14, 35, 57, 79, 101, 117, 133, 147, 161, 175,
         190, 205, 219, 234, 248, 255, 255, 255, 255]
    b = [0, 61, 96, 130, 165, 192, 220, 227, 210, 181, 151, 122, 93, 64, 35, 5, 0, 0, 0, 0,
         0, 0, 0, 0, 0, 0, 0, 35, 98, 160, 223, 255]
    x = np.linspace(0, 255, 32)
    i = np.arange(256)
    return np.stack([np.interp(i, x, c) for c in (r, g, b)], axis=1).round().astype(np.uint8)


def get_lut(name):
    """이름 → (256, 3) uint8. Gray면 None (흑백 그대로)"""
    if not name or name == "Gray":
        return None
    if name in _custom:
        return _custom[name]
    if name == "Fire":
        return _fire()
    import matplotlib
    cmap = matplotlib.colormaps[_MPL_NAMES.get(name, name.lower())]
    return (cmap(np.linspace(0, 1, 256))[:, :3] * 255).round().astype(np.uint8)


def load_lut(path):
    """LUT 파일 읽기 → (이름, LUT). 형식이 맞지 않으면 ValueError"""
    with open(path, "rb") as f:
        data = f.read()
    lut = None
    if len(data) in (768, 800):          # ImageJ 이진 LUT (R 256 + G 256 + B 256)
        raw = np.frombuffer(data[-768:], dtype=np.uint8)
        lut = raw.reshape(3, 256).T.copy()
    else:
        rows = []
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.replace(",", " ").replace("\t", " ").split()
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                continue                  # 머리글
            if len(nums) >= 3:
                rows.append(nums[-3:])
        if len(rows) >= 2:
            arr = np.array(rows, dtype=float)
            if arr.max() <= 1.0:
                arr *= 255
            i = np.linspace(0, 255, len(arr))
            lut = np.stack([np.interp(np.arange(256), i, arr[:, c]) for c in range(3)],
                           axis=1).round().clip(0, 255).astype(np.uint8)
    if lut is None or lut.shape != (256, 3):
        raise ValueError("LUT 형식을 알 수 없습니다 (ImageJ .lut 또는 R G B 텍스트).")
    name = os.path.splitext(os.path.basename(path))[0]
    _custom[name] = lut
    return name, lut


def names():
    return BUILTIN + [n for n in _custom if n not in BUILTIN]


def apply(gray8, lut):
    """(h, w) uint8 → (h, w, 3) uint8"""
    return lut[gray8]
