"""
DICOM 영상 좌표 ↔ 환자 좌표(mm) 변환

DICOM 규약 (PS3.3 C.7.6.2.1.1):
  P = IPP + r * (col * Δcol) + c * (row * Δrow)
  - IPP: ImagePositionPatient, 첫 픽셀(0,0) 중심의 환자 좌표
  - r, c: ImageOrientationPatient의 행 방향(열 인덱스 증가 방향) / 열 방향 코사인
  - PixelSpacing = [Δrow(행 간격), Δcol(열 간격)]
"""
import numpy as np


class SeriesGeometry:
    """시리즈 슬라이스별 공간 정보 (정렬된 slices 순서와 동일)"""

    def __init__(self, frame_of_reference_uid, origins, row_dirs, col_dirs,
                 spacings, shapes):
        self.frame_of_reference_uid = frame_of_reference_uid
        self.origins = origins      # (n, 3)
        self.row_dirs = row_dirs    # (n, 3) 열 인덱스(x) 증가 방향
        self.col_dirs = col_dirs    # (n, 3) 행 인덱스(y) 증가 방향
        self.normals = np.cross(row_dirs, col_dirs)
        self.spacings = spacings    # (n, 2) [Δrow, Δcol]
        self.shapes = shapes        # (n, 2) [rows, cols]

    @property
    def num_slices(self):
        return len(self.origins)

    def pixel_to_patient(self, index, col, row):
        """슬라이스 index의 (col, row) 픽셀 → 환자 좌표 (mm)"""
        d_row, d_col = self.spacings[index]
        return (self.origins[index]
                + self.row_dirs[index] * (col * d_col)
                + self.col_dirs[index] * (row * d_row))

    def patient_to_pixel(self, index, point):
        """환자 좌표 → 슬라이스 index 평면에 투영한 (col, row), 평면까지 거리(mm)"""
        v = np.asarray(point, dtype=float) - self.origins[index]
        d_row, d_col = self.spacings[index]
        col = float(v @ self.row_dirs[index]) / d_col
        row = float(v @ self.col_dirs[index]) / d_row
        dist = float(v @ self.normals[index])
        return col, row, dist

    def nearest_slice(self, point):
        """점에서 가장 가까운 슬라이스 (index, 평면까지 거리 mm)"""
        v = np.asarray(point, dtype=float) - self.origins
        dists = np.einsum('ij,ij->i', v, self.normals)
        index = int(np.argmin(np.abs(dists)))
        return index, float(dists[index])

    def slice_spacing(self):
        """인접 슬라이스 간 대표 간격(mm). 단일 슬라이스면 None"""
        if self.num_slices < 2:
            return None
        steps = np.abs(np.diff(self.origins @ self.normals[0]))
        steps = steps[steps > 1e-3]
        return float(np.median(steps)) if len(steps) else None

    def is_linkable_with(self, other):
        return (other is not None
                and self.frame_of_reference_uid
                and self.frame_of_reference_uid == other.frame_of_reference_uid)


def build_series_geometry(slices):
    """메타데이터 Dataset 목록으로 SeriesGeometry 생성

    공간 정보(IPP/IOP/PixelSpacing/FrameOfReferenceUID)가 하나라도 없으면 None.
    """
    if not slices:
        return None
    for_uid = str(getattr(slices[0], 'FrameOfReferenceUID', '') or '')
    if not for_uid:
        return None
    origins, rows, cols, spacings, shapes = [], [], [], [], []
    try:
        for ds in slices:
            ipp = [float(v) for v in ds.ImagePositionPatient]
            iop = [float(v) for v in ds.ImageOrientationPatient]
            ps = [float(v) for v in ds.PixelSpacing]
            if len(ipp) != 3 or len(iop) != 6 or len(ps) != 2 or min(ps) <= 0:
                return None
            origins.append(ipp)
            r, c = np.array(iop[:3]), np.array(iop[3:])
            rows.append(r / np.linalg.norm(r))
            cols.append(c / np.linalg.norm(c))
            spacings.append(ps)
            shapes.append([int(ds.Rows), int(ds.Columns)])
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None
    return SeriesGeometry(for_uid, np.array(origins), np.array(rows),
                          np.array(cols), np.array(spacings), np.array(shapes))
