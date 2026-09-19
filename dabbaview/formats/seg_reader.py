# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM Segmentation (SEG) → 참조 시리즈 위 라벨 마스크

프레임마다 원본 영상(ReferencedSOPInstanceUID)으로 슬라이스를 찾고,
참조가 없으면 ImagePositionPatient로 가장 가까운 슬라이스에 놓는다.
"""
import numpy as np
import pydicom

SEG_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.66.4"


def is_segmentation(ds):
    return str(getattr(ds, "SOPClassUID", "")) == SEG_SOP_CLASS or \
        str(getattr(ds, "Modality", "")) == "SEG"


def cielab_to_rgb(lab):
    """DICOM CIELab (0-65535) → sRGB (0-255)"""
    L = lab[0] * 100.0 / 65535
    a = lab[1] * 255.0 / 65535 - 128
    b = lab[2] * 255.0 / 65535 - 128
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    f = np.array([fx, fy, fz])
    xyz = np.where(f ** 3 > 0.008856, f ** 3, (f - 16 / 116) / 7.787)
    xyz *= np.array([0.95047, 1.0, 1.08883])
    m = np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415],
                  [0.0557, -0.2040, 1.0570]])
    c = m @ xyz
    c = np.where(c > 0.0031308, 1.055 * np.power(np.clip(c, 0, None), 1 / 2.4) - 0.055, 12.92 * c)
    return tuple(int(round(v)) for v in np.clip(c, 0, 1) * 255)


class SegmentationFile:
    """SEG 파일 해석 결과"""

    def __init__(self, path):
        self.path = path
        self.ds = pydicom.dcmread(path, force=True)
        ds = self.ds
        ref = getattr(ds, "ReferencedSeriesSequence", None)
        self.referenced_series_uid = str(ref[0].SeriesInstanceUID) if ref else None
        self.frame_of_reference_uid = str(getattr(ds, "FrameOfReferenceUID", ""))
        self.segments = {}
        for seg in getattr(ds, "SegmentSequence", []):
            color = None
            if "RecommendedDisplayCIELabValue" in seg:
                color = cielab_to_rgb([int(v) for v in seg.RecommendedDisplayCIELabValue])
            self.segments[int(seg.SegmentNumber)] = {
                "label": str(getattr(seg, "SegmentLabel", f"Segment {seg.SegmentNumber}")),
                "color": color}
        self.description = str(getattr(ds, "SeriesDescription", "") or "DICOM SEG")

    def _frames(self):
        pixels = self.ds.pixel_array
        if pixels.ndim == 2:
            pixels = pixels[None]
        if getattr(self.ds, "SegmentationType", "BINARY") == "FRACTIONAL":
            maximum = float(getattr(self.ds, "MaximumFractionalValue", 255) or 255)
            pixels = pixels >= maximum / 2
        return pixels > 0

    def matches(self, series):
        if self.referenced_series_uid:
            return series.series_uid == self.referenced_series_uid
        return bool(self.frame_of_reference_uid) and any(
            str(getattr(s, "FrameOfReferenceUID", "")) == self.frame_of_reference_uid
            for s in series.slices[:1])

    def to_mask(self, series, label_for_segment):
        """series 위 마스크 (k, row, col) uint8. label_for_segment(번호, 정보) → 라벨 id"""
        series.sort_slices()
        d = series.num_slices
        rows, cols = int(series.slices[0].Rows), int(series.slices[0].Columns)
        if (int(self.ds.Rows), int(self.ds.Columns)) != (rows, cols):
            raise ValueError("SEG 크기가 참조 영상과 다릅니다.")
        by_uid = {str(s.SOPInstanceUID): k for k, s in enumerate(series.slices)}
        geometry = series.geometry
        frames = self._frames()
        shared = getattr(self.ds, "SharedFunctionalGroupsSequence", [None])[0]
        per_frame = getattr(self.ds, "PerFrameFunctionalGroupsSequence", [])
        mask = np.zeros((d, rows, cols), dtype=np.uint8)
        label_ids = {n: label_for_segment(n, info) for n, info in self.segments.items()}
        placed = 0
        for i, frame in enumerate(frames):
            fg = per_frame[i] if i < len(per_frame) else None
            number = _frame_segment(fg, shared)
            k = _frame_slice(fg, by_uid, geometry)
            if number is None or k is None or not (0 <= k < d):
                continue
            mask[k][frame] = label_ids.get(number, number)
            placed += 1
        if placed == 0:
            raise ValueError("SEG 프레임을 참조 시리즈의 슬라이스에 맞추지 못했습니다.")
        return mask


def _frame_segment(fg, shared):
    for group in (fg, shared):
        seq = getattr(group, "SegmentIdentificationSequence", None) if group is not None else None
        if seq:
            return int(seq[0].ReferencedSegmentNumber)
    return None


def _frame_slice(fg, by_uid, geometry):
    if fg is None:
        return None
    for deriv in getattr(fg, "DerivationImageSequence", []):
        for src in getattr(deriv, "SourceImageSequence", []):
            k = by_uid.get(str(getattr(src, "ReferencedSOPInstanceUID", "")))
            if k is not None:
                return k
    pos = getattr(fg, "PlanePositionSequence", None)
    if pos and geometry is not None:
        point = [float(v) for v in pos[0].ImagePositionPatient]
        index, distance = geometry.nearest_slice(point)
        return index if abs(distance) < 2.0 else None
    return None
