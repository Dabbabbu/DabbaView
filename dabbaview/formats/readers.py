# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
의료영상 파일 → VolumeSeries

  NIfTI (.nii, .nii.gz)      nibabel
  NRRD (.nrrd, .nhdr)        pynrrd
  MetaImage (.mha, .mhd)     SimpleITK
  NumPy (.npy, .npz)         numpy (옆에 마스크·간격 정보가 있으면 함께)
  PNG/JPEG/BMP/TIFF          폴더(또는 선택한 파일들)를 한 시리즈로
  STL (.stl)                 3D 메시 - 3D Volume 탭 (여기서는 경로만 분류)

각 reader는 [LoadedVolume]을 반환한다. LoadedVolume.mask가 있으면 AI 세그멘테이션으로 표시.
"""
import json
import os
import re

import numpy as np

from .volume_series import VolumeSeries

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")
VOLUME_EXTENSIONS = (".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd", ".npy", ".npz")
MESH_EXTENSIONS = (".stl",)

# 4D 볼륨(fMRI, DWI 등)을 시리즈로 나눌 최대 개수
MAX_VOLUMES_4D = 64

_LABEL_NAME = re.compile(r"(label|mask|seg)", re.IGNORECASE)

_LPS_FROM_RAS = np.diag([-1.0, -1.0, 1.0, 1.0])


class LoadedVolume:
    def __init__(self, series, mask=None, label_candidate=False):
        self.series = series
        self.mask = mask                      # (k, row, col) uint8 또는 None
        self.label_candidate = label_candidate  # 라벨맵으로 보이는 볼륨 (다른 시리즈 오버레이 후보)


def file_kind(path):
    """'nifti' / 'nrrd' / 'metaimage' / 'numpy' / 'image' / 'mesh' / 'dicom'"""
    lower = path.lower()
    if lower.endswith((".nii", ".nii.gz")):
        return "nifti"
    if lower.endswith((".nrrd", ".nhdr")):
        return "nrrd"
    if lower.endswith((".mha", ".mhd")):
        return "metaimage"
    if lower.endswith((".npy", ".npz")):
        return "numpy"
    if lower.endswith(IMAGE_EXTENSIONS):
        return "image"
    if lower.endswith(MESH_EXTENSIONS):
        return "mesh"
    return "dicom"


def _stem(path):
    name = os.path.basename(path)
    for ext in (".nii.gz",) + VOLUME_EXTENSIONS + IMAGE_EXTENSIONS:
        if name.lower().endswith(ext):
            return name[:-len(ext)]
    return os.path.splitext(name)[0]


def _is_label_volume(array, path):
    """정수 값 몇 개뿐이고 이름에 label/mask/seg가 있으면 라벨맵으로 취급"""
    if not _LABEL_NAME.search(os.path.basename(path)):
        return False
    sample = array[::max(1, array.shape[0] // 16)]
    if not np.all(np.mod(sample, 1) == 0) or sample.min() < 0 or array.max() > 255:
        return False
    return len(np.unique(sample)) <= 64


def _volumes_from_ijk(data, affine_lps, path, fmt):
    """data[i, j, k(, t)] (i = 열, j = 행) → LoadedVolume 목록 (4D는 볼륨마다 하나)"""
    data = np.asarray(data)
    name = _stem(path)
    if data.ndim == 2:
        data = data[:, :, None]
    frames = [data] if data.ndim == 3 else [data[..., t] for t in
                                             range(min(data.shape[3], MAX_VOLUMES_4D))]
    if data.ndim > 4:
        raise ValueError(f"지원하지 않는 차원: {data.shape}")
    result = []
    for t, frame in enumerate(frames):
        array = np.ascontiguousarray(frame.transpose(2, 1, 0)).astype(np.float32)
        label = _is_label_volume(array, path)
        desc = name if len(frames) == 1 else f"{name} [t={t}]"
        series = VolumeSeries(array, affine_lps, name, path, fmt, description=desc, index=t,
                              modality="OT" if label else None)
        result.append(LoadedVolume(series, label_candidate=label))
    return result


def read_nifti(path):
    import nibabel as nib
    img = nib.load(path)
    data = np.asanyarray(img.dataobj)
    if data.dtype.names:  # RGB 구조체 등
        raise ValueError("컬러(RGB) NIfTI는 지원하지 않습니다.")
    affine = _LPS_FROM_RAS @ img.affine
    return _volumes_from_ijk(data.astype(np.float32, copy=False), affine, path, "nifti")


def read_nrrd(path):
    import nrrd
    data, header = nrrd.read(path, index_order="F")
    space = str(header.get("space", "left-posterior-superior")).lower()
    directions = header.get("space directions")
    spatial = [i for i in range(data.ndim)]
    affine = np.eye(4)
    if directions is not None:
        vectors = [np.asarray(v, dtype=float) if v is not None and not np.any(np.isnan(
            np.asarray(v, dtype=float))) else None for v in directions]
        spatial = [i for i, v in enumerate(vectors) if v is not None]
        for col, axis in enumerate(spatial[:3]):
            affine[:3, col] = vectors[axis][:3]
        origin = header.get("space origin")
        if origin is not None:
            affine[:3, 3] = np.asarray(origin, dtype=float)[:3]
    elif "spacings" in header:
        for col, sp in enumerate(header["spacings"][:3]):
            affine[col, col] = float(sp) if np.isfinite(sp) else 1.0
    if space in ("right-anterior-superior", "ras", "scanner-xyz", "3d-right-handed"):
        affine = _LPS_FROM_RAS @ affine
    elif space in ("left-anterior-superior", "las"):
        affine = np.diag([1.0, -1.0, 1.0, 1.0]) @ affine
    # 공간 축이 아닌 축(색 성분·시간 등)은 끝으로
    others = [i for i in range(data.ndim) if i not in spatial]
    data = np.transpose(data, spatial + others)
    if data.ndim == 4 and data.shape[3] in (3, 4) and "vector" in str(header.get("kinds", "")):
        data = data[..., :3].mean(axis=3)
    return _volumes_from_ijk(data.astype(np.float32, copy=False), affine, path, "nrrd")


def read_metaimage(path):
    import SimpleITK as sitk
    image = sitk.ReadImage(path)
    if image.GetNumberOfComponentsPerPixel() > 1:
        image = sitk.VectorMagnitude(image) if image.GetNumberOfComponentsPerPixel() > 3 \
            else sitk.VectorIndexSelectionCast(image, 0)
    array = sitk.GetArrayFromImage(image)          # (k, j, i) 또는 (j, i)
    dim = image.GetDimension()
    spacing = list(image.GetSpacing()) + [1.0] * (3 - dim)
    origin = list(image.GetOrigin()) + [0.0] * (3 - dim)
    direction = np.array(image.GetDirection(), dtype=float).reshape(dim, dim)
    d3 = np.eye(3)
    d3[:dim, :dim] = direction
    affine = np.eye(4)
    affine[:3, :3] = d3 @ np.diag(spacing[:3])      # ITK는 LPS
    affine[:3, 3] = origin[:3]
    if array.ndim == 2:
        array = array[None]
    if array.ndim == 4:                             # 4D: 첫 볼륨
        array = array[0]
    data = array.transpose(2, 1, 0)                  # → (i, j, k)
    return _volumes_from_ijk(data.astype(np.float32, copy=False), affine, path, "metaimage")


def _numpy_meta(path, name):
    """간격·affine 정보: <이름>.json 또는 AI 내보내기의 dataset.json"""
    folder = os.path.dirname(path)
    candidates = [os.path.splitext(path)[0] + ".json"]
    base = re.sub(r"_(image|mask)$", "", name)
    for up in (folder, os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))):
        candidates.append(os.path.join(up, "dataset.json"))
    for meta_path in candidates:
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if "affine_lps" in meta:
            return np.array(meta["affine_lps"], dtype=float)
        for case in meta.get("cases", []):
            if case.get("name") == base and case.get("spacing"):
                dk, dr, dc = (float(v) for v in case["spacing"])
                return np.diag([dc, dr, dk, 1.0])
    return None


def read_numpy(path):
    """배열 순서는 (슬라이스, 행, 열). 2D는 슬라이스 1장"""
    name = _stem(path)
    mask = None
    affine = None
    if path.lower().endswith(".npz"):
        with np.load(path, allow_pickle=False) as data:
            keys = list(data.keys())
            image_key = next((k for k in ("image", "img", "data", "volume", "arr_0")
                              if k in keys), keys[0] if keys else None)
            if image_key is None:
                raise ValueError("npz 안에 배열이 없습니다.")
            array = data[image_key]
            mask_key = next((k for k in ("mask", "label", "labels", "seg") if k in keys), None)
            if mask_key is not None:
                mask = data[mask_key]
            if "affine_lps" in keys:
                affine = np.array(data["affine_lps"], dtype=float)
            elif "spacing" in keys:
                dk, dr, dc = (float(v) for v in data["spacing"])
                affine = np.diag([dc, dr, dk, 1.0])
    else:
        array = np.load(path, allow_pickle=False)
        if name.endswith("_image"):
            mask_path = path[:-len("_image.npy")] + "_mask.npy"
            if os.path.exists(mask_path):
                mask = np.load(mask_path, allow_pickle=False)
    if affine is None:
        affine = _numpy_meta(path, name)
    array = np.asarray(array)
    if array.ndim == 4 and array.shape[-1] in (3, 4):  # 컬러 → 밝기
        array = array[..., :3].mean(axis=-1)
    if array.ndim == 4:
        array = array[0]
    if array.ndim == 2:
        array = array[None]
    if array.ndim != 3:
        raise ValueError(f"(슬라이스, 행, 열) 배열이 아닙니다: {array.shape}")
    if array.dtype == np.bool_:
        array = array.astype(np.uint8)
    if affine is None:
        affine = np.eye(4)
    # NumPy 배열은 이미 화면 순서 → 축 뒤집기 안 함
    label = mask is None and _is_label_volume(array.astype(np.float32), path)
    series = VolumeSeries(array, affine, name, path, "numpy", canonical=False,
                          modality="OT" if label else None)
    if mask is not None:
        mask = np.asarray(mask)
        if mask.ndim == 2:
            mask = mask[None]
        mask = mask.astype(np.uint8) if mask.shape == array.shape else None
    return [LoadedVolume(series, mask=mask, label_candidate=label)]


def _natural_key(path):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", os.path.basename(path))]


def _read_image(path):
    from PIL import Image
    with Image.open(path) as img:
        if img.mode in ("I;16", "I;16B", "I;16L", "I", "F", "L"):
            return np.asarray(img, dtype=np.float32)
        if img.mode == "P":  # 팔레트 PNG (VOC 마스크 등) - 인덱스 값 그대로
            return np.asarray(img, dtype=np.float32)
        return np.asarray(img.convert("L"), dtype=np.float32)


def _sequence_meta(folder):
    """Convert로 저장한 PNG 시퀀스의 meta.json (같은 폴더 또는 상위 폴더)"""
    for candidate in (os.path.join(folder, "meta.json"),
                      os.path.join(os.path.dirname(folder), "meta.json")):
        try:
            with open(candidate, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def read_image_sequence(paths, name=None):
    """이미지 파일들 → 크기별로 묶어 시리즈 (파일 이름 자연 정렬 = 슬라이스 순서)

    images/ 옆에 같은 파일 이름의 masks/ 폴더가 있으면 마스크로 함께 읽는다.
    meta.json이 있으면 간격·방향·16비트 값 오프셋을 되살린다.
    """
    paths = sorted(paths, key=_natural_key)
    groups = {}
    for p in paths:
        try:
            arr = _read_image(p)
        except OSError:
            continue
        groups.setdefault(arr.shape, []).append((p, arr))
    folder = os.path.dirname(paths[0]) if paths else ""
    meta = _sequence_meta(folder)
    base = name or os.path.basename(folder) or "Images"
    if base.lower() == "images" and meta:
        base = os.path.basename(os.path.dirname(folder)) or base
    affine = np.array(meta["affine_lps"], dtype=float) if meta.get("affine_lps") else np.eye(4)
    offset = float(meta.get("value_offset") or 0.0)
    mask_dir = os.path.join(os.path.dirname(folder), "masks")
    result = []
    for index, (shape, items) in enumerate(sorted(groups.items(), key=lambda g: -len(g[1]))):
        array = np.stack([a for _, a in items]) + offset
        desc = base if len(groups) == 1 else f"{base} ({shape[1]}x{shape[0]})"
        source = folder if len(items) > 1 else items[0][0]
        series = VolumeSeries(array, affine if index == 0 else np.eye(4), base, source,
                              "image", modality="OT", description=desc, index=index,
                              canonical=False)
        mask = None
        if os.path.basename(folder) == "images" and os.path.isdir(mask_dir):
            try:
                masks = [_read_image(os.path.join(mask_dir, os.path.basename(p)))
                         for p, _ in items]
                if all(m.shape == shape for m in masks):
                    mask = np.stack(masks).astype(np.uint8)
            except OSError:
                mask = None
        result.append(LoadedVolume(series, mask=mask))
    return result


def is_mask_image_folder(folder):
    """images/ 와 짝을 이루는 masks/ 폴더 (따로 시리즈로 만들지 않음)"""
    return (os.path.basename(folder) == "masks"
            and os.path.isdir(os.path.join(os.path.dirname(folder), "images")))


READERS = {
    "nifti": read_nifti,
    "nrrd": read_nrrd,
    "metaimage": read_metaimage,
    "numpy": read_numpy,
}


def read_volume_file(path):
    kind = file_kind(path)
    if kind == "image":
        return read_image_sequence([path])
    reader = READERS.get(kind)
    if reader is None:
        raise ValueError(f"지원하지 않는 형식: {path}")
    return reader(path)
