# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
불러온 영상이 어디서 왔는지 - 폴더 이름 · 파일 이름 (시리즈 카드 · 창 제목 · 상태바에 표시)

압축파일에서 푼 것은 캐시 폴더 대신 '원래 압축파일 ▸ 안쪽 폴더'로 보여 준다.
"""
import os


def _filename(ds):
    name = getattr(ds, "filename", "") if ds is not None else ""
    return name if isinstance(name, str) else ""


def slice_file(series, index):
    """그 슬라이스의 파일 경로 ('' = 모름)"""
    try:
        return _filename(series.slices[index])
    except (AttributeError, IndexError, TypeError):
        return ""


def folder_of(series):
    """(짧은 이름, 전체 경로) — 예: ('65001_CS MRCP BH_MIP_Radials', '/Users/…/65001_CS …')"""
    first = slice_file(series, 0)
    if not first:
        return "", ""
    folder = os.path.dirname(first)
    from . import archives
    original = archives.original_for(folder)
    if original:
        archive, inner = original
        short = os.path.basename(archive) + (f" ▸ {inner}" if inner else "")
        return short, os.path.join(archive, inner) if inner else archive
    return os.path.basename(folder) or folder, folder
