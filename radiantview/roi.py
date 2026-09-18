"""
Freehand ROI 계산: 면적/둘레(mm), 영역 내 픽셀 통계

좌표계: 뷰포트 이미지 좌표 (x, y) - 픽셀 i는 [i, i+1) 구간, 중심은 i + 0.5
"""
import cv2
import numpy as np


def polygon_area_mm2(points, spacing):
    """신발끈 공식으로 다각형 면적 (mm²). spacing = (행 간격, 열 간격)"""
    if len(points) < 3:
        return 0.0
    row_sp, col_sp = spacing
    pts = np.asarray(points, dtype=float)
    x, y = pts[:, 0] * col_sp, pts[:, 1] * row_sp
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def polygon_perimeter_mm(points, spacing, closed=True):
    if len(points) < 2:
        return 0.0
    row_sp, col_sp = spacing
    pts = np.asarray(points, dtype=float)
    if closed:
        pts = np.vstack([pts, pts[:1]])
    d = np.diff(pts, axis=0) * [col_sp, row_sp]
    return float(np.sum(np.hypot(d[:, 0], d[:, 1])))


def polygon_mask(points, shape):
    """다각형 내부에 중심이 있는 픽셀 마스크 (bool, shape=(rows, cols))"""
    mask = np.zeros(shape[:2], dtype=np.uint8)
    if len(points) < 3:
        return mask.astype(bool)
    # 픽셀 중심 좌표계로 옮기고 1/16 픽셀 정밀도로 채움
    shift = 4
    pts = np.round((np.asarray(points, dtype=float) - 0.5) * (1 << shift))
    cv2.fillPoly(mask, [pts.astype(np.int32)], 1, lineType=cv2.LINE_8,
                 shift=shift)
    return mask.astype(bool)


def roi_statistics(arr, points, spacing, factor=1.0):
    """ROI 통계 dict: area_mm2, perimeter_mm, pixels, mean, std, min, max

    arr: Rescale 적용된 2D 배열 (컬러 영상이면 통계 없이 면적만)
    factor: 표시 단위 환산 계수 (예: SUV)
    """
    stats = {
        "area_mm2": polygon_area_mm2(points, spacing),
        "perimeter_mm": polygon_perimeter_mm(points, spacing),
        "pixels": 0,
    }
    if arr is None or arr.ndim != 2:
        return stats
    mask = polygon_mask(points, arr.shape)
    values = arr[mask] * factor
    stats["pixels"] = int(values.size)
    if values.size:
        stats.update(mean=float(values.mean()), std=float(values.std()),
                     min=float(values.min()), max=float(values.max()))
    return stats
