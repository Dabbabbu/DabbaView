# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
분석 도구 (3D Slicer / ImageJ·Fiji 스타일)

영상 처리 필터, 히스토그램, 라인 프로파일, 입자 분석, 컬러맵, 영상 정합,
영상 융합, 랜드마크, 표면 모델, Python 콘솔, 매크로
"""
import itertools
import os

import numpy as np

_counter = itertools.count(1)

# 한글이 들어가는 그래프 제목·축 이름용 글꼴 (있는 것 중 첫 번째)
KOREAN_FONTS = ["Apple SD Gothic Neo", "AppleGothic", "Malgun Gothic", "NanumGothic",
                "Noto Sans CJK KR", "Noto Sans KR", "DejaVu Sans"]


def configure_matplotlib():
    """matplotlib 한글 글꼴 + 마이너스 기호 (한 번만)"""
    import matplotlib
    from matplotlib import font_manager
    available = {f.name for f in font_manager.fontManager.ttflist}
    family = [name for name in KOREAN_FONTS if name in available] or ["DejaVu Sans"]
    matplotlib.rcParams["font.family"] = family
    matplotlib.rcParams["axes.unicode_minus"] = False
    return family[0]


def derived_series(array, source, name, operation, modality=None):
    """처리 결과 배열 (k, row, col) → 원본과 같은 위치·간격의 새 시리즈 (원본 보존)"""
    from ..ai.volume import series_spacing_affine
    from ..formats.volume_series import VolumeSeries
    _spacing, affine = series_spacing_affine(source)
    base = getattr(source, "source_path", None) or os.path.dirname(
        str(getattr(source.slices[0], "filename", "") or "")) or "memory"
    # 같은 처리를 여러 번 해도 서로 다른 시리즈가 되도록 번호를 붙임
    path = f"{base}#{operation}#{next(_counter)}"
    series = VolumeSeries(np.asarray(array, dtype=np.float32), affine,
                          str(source.slices[0].PatientName) if source.slices else name,
                          path, "derived", modality=modality or source.modality,
                          description=name, canonical=False)
    # 원본과 같은 검사로 묶이도록 환자·검사 정보를 따라감
    ref = source.slices[0]
    for ds in series.slices:
        for keyword in ("PatientName", "PatientID", "StudyInstanceUID", "StudyDate",
                        "StudyTime", "StudyDescription", "FrameOfReferenceUID"):
            if keyword in ref:
                setattr(ds, keyword, getattr(ref, keyword))
            elif keyword == "StudyDescription":
                ds.StudyDescription = ""   # 원본에 없으면 내부 설명을 남기지 않음
        ds.SeriesNumber = int(getattr(ref, "SeriesNumber", 0) or 0) + 500
    series.derived_from = source.series_uid
    return series
