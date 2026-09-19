# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
AI 학습용 데이터 내보내기

형식: NIfTI(.nii.gz) / NumPy(.npy) / PNG 시퀀스 / COCO / Pascal VOC / DICOM SEG
분할: train/val/test - 케이스가 여러 개면 케이스 단위, 하나면 슬라이스 단위(2D 형식)

폴더 구조 (분할 사용 시 {split} = train/val/test):
  nifti/{split}/images/case_001.nii.gz, nifti/{split}/labels/case_001.nii.gz
  numpy/{split}/case_001_image.npy, case_001_mask.npy
  png/{split}/images/case_001_0001.png, png/{split}/masks/case_001_0001.png
  coco/{split}/images/*.png, coco/annotations/instances_{split}.json
  voc/JPEGImages, voc/SegmentationClass, voc/Annotations, voc/ImageSets/Segmentation/{split}.txt
  dicom_seg/case_001_seg.dcm
  dataset.json (라벨, 분할, 전처리, 간격)

내보낸 파일에는 환자 정보가 들어가지 않는다 (DICOM SEG는 원본 검사 정보를 참조하므로 예외).
"""
import json
import os
import random
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from scipy import ndimage

from . import nifti, preprocess
from .dicom_seg import save_segmentation
from .volume import load_volume

FORMATS = {
    "nifti": "NIfTI (.nii.gz)",
    "numpy": "NumPy (.npy)",
    "png": "PNG 시퀀스",
    "coco": "COCO (JSON)",
    "voc": "Pascal VOC",
    "dicom_seg": "DICOM SEG",
}
SLICE_FORMATS = ("png", "coco", "voc")
SPLITS = ("train", "val", "test")


class ExportOptions:
    def __init__(self):
        self.formats = {"nifti"}
        self.split = None                 # None 또는 (train, val, test) 비율
        self.seed = 42
        self.only_labeled_slices = False  # 2D 형식: 라벨이 있는 슬라이스만
        self.preprocess = preprocess.PreprocessOptions()


class Cancelled(Exception):
    pass


def assign_splits(names, ratios, seed):
    """이름 목록을 비율대로 train/val/test에 배정 (재현 가능한 셔플)"""
    items = list(names)
    random.Random(seed).shuffle(items)
    total = sum(ratios) or 1.0
    n = len(items)
    n_train = int(round(n * ratios[0] / total))
    n_val = int(round(n * ratios[1] / total))
    if n >= 3:
        # 비율이 0이 아니면 최소 1개씩
        if ratios[1] > 0 and n_val == 0:
            n_val = 1
        if ratios[2] > 0 and n - n_train - n_val == 0:
            n_train -= 1
    n_train = max(0, min(n, n_train))
    n_val = max(0, min(n - n_train, n_val))
    result = {}
    for i, name in enumerate(items):
        result[name] = ("train" if i < n_train else
                        "val" if i < n_train + n_val else "test")
    return result


def _window_to_uint8(array, window, normalized):
    if normalized:
        return (np.clip(array, 0, 1) * 255).round().astype(np.uint8)
    center, width = window
    low = center - width / 2
    return (np.clip((array - low) / max(width, 1e-6), 0, 1) * 255).round().astype(np.uint8)


def _palette(labels):
    pal = [0, 0, 0] * 256
    for l in labels:
        pal[l["id"] * 3:l["id"] * 3 + 3] = list(l["color"])
    return pal


def _components(mask2d, labels):
    """(라벨, bbox(x, y, w, h), 면적, 폴리곤들) - 라벨별 연결 영역"""
    import cv2
    result = []
    for l in labels:
        binary = mask2d == l["id"]
        if not binary.any():
            continue
        comp, n = ndimage.label(binary)
        for i in range(1, n + 1):
            region = (comp == i).astype(np.uint8)
            ys, xs = np.nonzero(region)
            bbox = [int(xs.min()), int(ys.min()),
                    int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
            contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            polys = [c.reshape(-1).astype(float).tolist() for c in contours if len(c) >= 3]
            result.append((l, bbox, int(region.sum()), polys))
    return result


class DatasetWriter:
    """케이스를 하나씩 받아 선택한 형식으로 기록하고 마지막에 요약 파일을 씀"""

    def __init__(self, out_dir, options, labels):
        self.out = out_dir
        self.options = options
        self.labels = [dict(l) for l in labels]
        self.summary = {"labels": {str(l["id"]): l["name"] for l in self.labels},
                        "formats": sorted(options.formats),
                        "preprocessing": options.preprocess.describe(),
                        "cases": []}
        self._coco = {s: {"images": [], "annotations": [],
                          "categories": [{"id": l["id"], "name": l["name"],
                                          "supercategory": "label"} for l in self.labels]}
                      for s in SPLITS + (None,)}
        self._coco_ann_id = 1
        self._coco_img_id = 1
        self._voc_sets = {s: [] for s in SPLITS + (None,)}

    def _dir(self, *parts):
        path = os.path.join(self.out, *[p for p in parts if p])
        os.makedirs(path, exist_ok=True)
        return path

    def write_case(self, name, volume, mask, split, window, slice_splits=None, series=None,
                   raw_mask=None):
        """split: 케이스 분할(3D 형식), slice_splits: 슬라이스별 분할(2D 형식, 없으면 split)"""
        fmts = self.options.formats
        normalized = self.options.preprocess.normalize
        has_mask = mask is not None and mask.any()
        info = {"name": name, "split": split, "shape": list(volume.shape),
                "spacing": [round(float(s), 6) for s in volume.spacing],
                "labeled": bool(has_mask)}

        if "nifti" in fmts:
            img_dir = self._dir("nifti", split, "images")
            image_dtype = np.float32 if normalized else _image_dtype(volume.array)
            nifti.save(os.path.join(img_dir, f"{name}.nii.gz"),
                       volume.array.astype(image_dtype), volume.affine_ras)
            if mask is not None:
                nifti.save(os.path.join(self._dir("nifti", split, "labels"),
                                        f"{name}.nii.gz"), mask.astype(np.uint8),
                           volume.affine_ras)

        if "numpy" in fmts:
            d = self._dir("numpy", split)
            np.save(os.path.join(d, f"{name}_image.npy"), volume.array.astype(np.float32))
            if mask is not None:
                np.save(os.path.join(d, f"{name}_mask.npy"), mask.astype(np.uint8))

        if "dicom_seg" in fmts and series is not None:
            seg_mask = raw_mask if raw_mask is not None else mask
            if seg_mask is not None and seg_mask.any():
                d = self._dir("dicom_seg")
                save_segmentation(os.path.join(d, f"{name}_seg.dcm"), series,
                                  seg_mask, self.labels,
                                  series_description=f"AI Segmentation ({name})")

        if fmts & set(SLICE_FORMATS):
            for k in range(volume.shape[0]):
                m = mask[k] if mask is not None else None
                if self.options.only_labeled_slices and (m is None or not m.any()):
                    continue
                s = slice_splits[k] if slice_splits else split
                self._write_slice(name, k, m, s,
                                  _window_to_uint8(volume.array[k], window, normalized))
        self.summary["cases"].append(info)

    def _write_slice(self, name, k, m, split, img8):
        fmts = self.options.formats
        stem = f"{name}_{k + 1:04d}"
        h, w = img8.shape
        if "png" in fmts:
            Image.fromarray(img8).save(os.path.join(self._dir("png", split, "images"),
                                                    stem + ".png"))
            if m is not None:
                Image.fromarray(m.astype(np.uint8)).save(
                    os.path.join(self._dir("png", split, "masks"), stem + ".png"))
        comps = _components(m, self.labels) if (m is not None and
                                                fmts & {"coco", "voc"}) else []
        if "coco" in fmts:
            Image.fromarray(img8).save(os.path.join(self._dir("coco", split, "images"),
                                                    stem + ".png"))
            coco = self._coco[split]
            image_id = self._coco_img_id
            self._coco_img_id += 1
            coco["images"].append({"id": image_id, "file_name": stem + ".png",
                                   "width": w, "height": h, "case": name, "slice": k + 1})
            for label, bbox, area, polys in comps:
                coco["annotations"].append({
                    "id": self._coco_ann_id, "image_id": image_id,
                    "category_id": label["id"], "bbox": bbox, "area": area,
                    "segmentation": polys, "iscrowd": 0})
                self._coco_ann_id += 1
        if "voc" in fmts:
            Image.fromarray(img8).save(os.path.join(self._dir("voc", "JPEGImages"),
                                                    stem + ".png"))
            if m is not None:
                seg = Image.fromarray(m.astype(np.uint8), mode="P")
                seg.putpalette(_palette(self.labels))
                seg.save(os.path.join(self._dir("voc", "SegmentationClass"), stem + ".png"))
            root = ET.Element("annotation")
            ET.SubElement(root, "folder").text = "JPEGImages"
            ET.SubElement(root, "filename").text = stem + ".png"
            size = ET.SubElement(root, "size")
            ET.SubElement(size, "width").text = str(w)
            ET.SubElement(size, "height").text = str(h)
            ET.SubElement(size, "depth").text = "1"
            ET.SubElement(root, "segmented").text = "1" if m is not None else "0"
            for label, (x, y, bw, bh), _area, _polys in comps:
                obj = ET.SubElement(root, "object")
                ET.SubElement(obj, "name").text = label["name"]
                ET.SubElement(obj, "pose").text = "Unspecified"
                ET.SubElement(obj, "truncated").text = "0"
                ET.SubElement(obj, "difficult").text = "0"
                box = ET.SubElement(obj, "bndbox")
                ET.SubElement(box, "xmin").text = str(x + 1)
                ET.SubElement(box, "ymin").text = str(y + 1)
                ET.SubElement(box, "xmax").text = str(x + bw)
                ET.SubElement(box, "ymax").text = str(y + bh)
            ET.ElementTree(root).write(
                os.path.join(self._dir("voc", "Annotations"), stem + ".xml"),
                encoding="utf-8", xml_declaration=True)
            self._voc_sets[split].append(stem)

    def finish(self):
        fmts = self.options.formats
        if "coco" in fmts:
            d = self._dir("coco", "annotations")
            for split, coco in self._coco.items():
                if coco["images"]:
                    with open(os.path.join(d, f"instances_{split or 'all'}.json"),
                              "w", encoding="utf-8") as f:
                        json.dump(coco, f, ensure_ascii=False)
        if "voc" in fmts:
            d = self._dir("voc", "ImageSets", "Segmentation")
            for split, stems in self._voc_sets.items():
                if stems:
                    with open(os.path.join(d, f"{split or 'all'}.txt"), "w") as f:
                        f.write("\n".join(stems) + "\n")
        with open(os.path.join(self.out, "dataset.json"), "w", encoding="utf-8") as f:
            json.dump(self.summary, f, ensure_ascii=False, indent=2)


def _image_dtype(array):
    """정수 값이면 int16 (용량 절약), 아니면 float32"""
    if np.all(np.mod(array, 1) == 0) and array.min() >= -32768 and array.max() <= 32767:
        return np.int16
    return np.float32


def export_cases(cases, out_dir, options, labels, progress=None, cancelled=None):
    """cases: [(이름, series, mask 또는 None, window)] → out_dir에 내보내기. 요약 dict 반환"""
    names = [c[0] for c in cases]
    case_splits = {n: None for n in names}
    single_slice_split = False
    if options.split:
        if len(cases) > 1:
            case_splits = assign_splits(names, options.split, options.seed)
        else:
            single_slice_split = True

    writer = DatasetWriter(out_dir, options, labels)
    for i, (name, series, mask, window) in enumerate(cases):
        if cancelled and cancelled():
            raise Cancelled()
        if progress:
            progress(f"{name}: 볼륨 읽는 중 ({i + 1}/{len(cases)})", i / len(cases))
        volume = load_volume(series)
        pre_volume, pre_mask = preprocess.apply(
            volume, mask, options.preprocess,
            progress=(lambda step: progress(f"{name}: {step}", i / len(cases)))
            if progress else None)
        slice_splits = None
        if single_slice_split:
            ks = [str(k) for k in range(pre_volume.shape[0])]
            assigned = assign_splits(ks, options.split, options.seed)
            slice_splits = [assigned[str(k)] for k in range(pre_volume.shape[0])]
        if progress:
            progress(f"{name}: 파일 쓰는 중", (i + 0.5) / len(cases))
        writer.write_case(name, pre_volume, pre_mask,
                          case_splits[name] if not single_slice_split else None,
                          window, slice_splits=slice_splits, series=series,
                          raw_mask=mask)
    if single_slice_split:
        writer.summary["split_unit"] = "slice"
    elif options.split:
        writer.summary["split_unit"] = "case"
    writer.finish()
    if progress:
        progress("완료", 1.0)
    return writer.summary
