# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
NIfTI-1 (.nii / .nii.gz) 읽기·쓰기 - 외부 라이브러리 없이 numpy만 사용

배열은 뷰어 순서 (k, row, col). 파일에는 NIfTI 규약대로 첫 축(i = col)이
가장 빠르게 변하도록 저장되며, affine은 RAS+ (i, j, k) → mm.
"""
import gzip
import struct

import numpy as np

_DTYPES = {  # NIfTI datatype code → numpy dtype
    2: np.uint8, 4: np.int16, 8: np.int32, 16: np.float32, 64: np.float64,
    256: np.int8, 512: np.uint16, 768: np.uint32, 1024: np.int64, 1280: np.uint64,
}
_CODES = {np.dtype(v): k for k, v in _DTYPES.items()}

NIFTI_UNITS_MM = 2


def _quaternion(affine):
    """affine의 회전 부분 → (b, c, d, qfac, pixdim) (NIfTI qform)"""
    m = affine[:3, :3].astype(float)
    pixdim = np.linalg.norm(m, axis=0)
    pixdim[pixdim == 0] = 1.0
    r = m / pixdim
    qfac = 1.0
    if np.linalg.det(r) < 0:
        qfac = -1.0
        r[:, 2] = -r[:, 2]
    # 직교 행렬로 보정 (SVD)
    u, _, vt = np.linalg.svd(r)
    r = u @ vt
    a = 1.0 + r[0, 0] + r[1, 1] + r[2, 2]
    if a > 0.5:
        a = 0.5 * np.sqrt(a)
        b = 0.25 * (r[2, 1] - r[1, 2]) / a
        c = 0.25 * (r[0, 2] - r[2, 0]) / a
        d = 0.25 * (r[1, 0] - r[0, 1]) / a
    else:
        xd = 1.0 + r[0, 0] - (r[1, 1] + r[2, 2])
        yd = 1.0 + r[1, 1] - (r[0, 0] + r[2, 2])
        zd = 1.0 + r[2, 2] - (r[0, 0] + r[1, 1])
        if xd > 1.0:
            b = 0.5 * np.sqrt(xd)
            c = 0.25 * (r[0, 1] + r[1, 0]) / b
            d = 0.25 * (r[0, 2] + r[2, 0]) / b
            a = 0.25 * (r[2, 1] - r[1, 2]) / b
        elif yd > 1.0:
            c = 0.5 * np.sqrt(yd)
            b = 0.25 * (r[0, 1] + r[1, 0]) / c
            d = 0.25 * (r[1, 2] + r[2, 1]) / c
            a = 0.25 * (r[0, 2] - r[2, 0]) / c
        else:
            d = 0.5 * np.sqrt(zd)
            b = 0.25 * (r[0, 2] + r[2, 0]) / d
            c = 0.25 * (r[1, 2] + r[2, 1]) / d
            a = 0.25 * (r[1, 0] - r[0, 1]) / d
        if a < 0:
            b, c, d = -b, -c, -d
    return float(b), float(c), float(d), qfac, pixdim


def to_bytes(array, affine_ras, description=""):
    """(k, row, col) 배열 → NIfTI-1 단일 파일(.nii) 바이트"""
    array = np.asarray(array)
    if array.dtype == np.bool_:
        array = array.astype(np.uint8)
    if array.dtype not in _CODES:
        array = array.astype(np.float32)
    code = _CODES[array.dtype]
    d, h, w = array.shape
    b, c, dq, qfac, pixdim = _quaternion(affine_ras)

    header = bytearray(348)
    struct.pack_into("<i", header, 0, 348)
    struct.pack_into("<c", header, 38, b"r")
    struct.pack_into("<8h", header, 40, 3, w, h, d, 1, 1, 1, 1)
    struct.pack_into("<hhh", header, 70, code, array.dtype.itemsize * 8, 0)
    struct.pack_into("<8f", header, 76, qfac, pixdim[0], pixdim[1], pixdim[2], 1, 1, 1, 1)
    struct.pack_into("<f", header, 108, 352.0)            # vox_offset
    struct.pack_into("<ff", header, 112, 1.0, 0.0)        # scl_slope, scl_inter
    struct.pack_into("<B", header, 123, NIFTI_UNITS_MM)   # xyzt_units
    if array.size:
        struct.pack_into("<ff", header, 124, float(array.max()), float(array.min()))
    desc = description.encode("ascii", "replace")[:79]
    header[148:148 + len(desc)] = desc
    struct.pack_into("<hh", header, 252, 1, 1)            # qform_code, sform_code (scanner)
    struct.pack_into("<3f", header, 256, b, c, dq)
    struct.pack_into("<3f", header, 268, *affine_ras[:3, 3])
    for row in range(3):
        struct.pack_into("<4f", header, 280 + 16 * row, *affine_ras[row, :4])
    header[344:348] = b"n+1\0"
    # C 순서 (k, row, col) 바이트 = col이 가장 빠름 = NIfTI (i, j, k)
    return bytes(header) + b"\0\0\0\0" + np.ascontiguousarray(array).astype(
        array.dtype.newbyteorder("<")).tobytes()


def save(path, array, affine_ras, description=""):
    data = to_bytes(array, affine_ras, description)
    if path.endswith(".gz"):
        with gzip.open(path, "wb", compresslevel=6) as f:
            f.write(data)
    else:
        with open(path, "wb") as f:
            f.write(data)


def from_bytes(data):
    """NIfTI-1 바이트 → (배열 (k, row, col), affine_ras). gzip 자동 판별"""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    endian = "<" if struct.unpack_from("<i", data, 0)[0] == 348 else ">"
    if struct.unpack_from(endian + "i", data, 0)[0] != 348:
        raise ValueError("NIfTI-1 파일이 아닙니다.")
    dims = struct.unpack_from(endian + "8h", data, 40)
    code, _bitpix = struct.unpack_from(endian + "hh", data, 70)
    pixdim = struct.unpack_from(endian + "8f", data, 76)
    vox_offset = int(struct.unpack_from(endian + "f", data, 108)[0])
    slope, inter = struct.unpack_from(endian + "ff", data, 112)
    qform_code, sform_code = struct.unpack_from(endian + "hh", data, 252)
    if code not in _DTYPES:
        raise ValueError(f"지원하지 않는 NIfTI 데이터 형식: {code}")
    ndim = dims[0]
    shape = [max(1, v) for v in dims[1:1 + ndim]]
    # 3D 초과(4D 등)는 첫 볼륨만
    w, h, d = (shape + [1, 1, 1])[:3]
    dtype = np.dtype(_DTYPES[code]).newbyteorder(endian)
    count = w * h * d
    array = np.frombuffer(data, dtype=dtype, count=count, offset=vox_offset)
    array = array.reshape(d, h, w).astype(dtype.newbyteorder("="))
    if slope not in (0.0, 1.0) or inter != 0.0:
        array = array * (slope or 1.0) + inter

    if sform_code > 0:
        affine = np.eye(4)
        for row in range(3):
            affine[row] = struct.unpack_from(endian + "4f", data, 280 + 16 * row)
    elif qform_code > 0:
        b, c, dq = struct.unpack_from(endian + "3f", data, 256)
        a = np.sqrt(max(0.0, 1.0 - (b * b + c * c + dq * dq)))
        r = np.array([
            [a * a + b * b - c * c - dq * dq, 2 * (b * c - a * dq), 2 * (b * dq + a * c)],
            [2 * (b * c + a * dq), a * a + c * c - b * b - dq * dq, 2 * (c * dq - a * b)],
            [2 * (b * dq - a * c), 2 * (c * dq + a * b), a * a + dq * dq - c * c - b * b]])
        qfac = -1.0 if pixdim[0] < 0 else 1.0
        affine = np.eye(4)
        affine[:3, :3] = r @ np.diag([pixdim[1], pixdim[2], pixdim[3] * qfac])
        affine[:3, 3] = struct.unpack_from(endian + "3f", data, 268)
    else:
        affine = np.diag([pixdim[1] or 1.0, pixdim[2] or 1.0, pixdim[3] or 1.0, 1.0])
    return array, affine


def load(path):
    with open(path, "rb") as f:
        return from_bytes(f.read())
