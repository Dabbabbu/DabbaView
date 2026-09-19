# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""DICOM 원본 태그 수정 (스터디·시리즈 이름, 환자 이름·ID)

파일마다: 전체 읽기 → 태그 변경 → 같은 폴더의 임시 파일에 저장 → 다시 읽어 확인 →
원래 이름으로 바꿔치기(원자적). 백업을 켜면 처음 한 번 원본을 <파일>.bak 으로 복사.
픽셀 데이터(압축 포함)는 디코딩하지 않고 그대로 다시 씀. 전송 구문·인코딩도 그대로.
"""
import os
import shutil

import pydicom

EDITABLE = {"StudyDescription": "LO", "SeriesDescription": "LO",
            "PatientName": "PN", "PatientID": "LO"}


class EditCancelled(Exception):
    pass


def files_of(series_list):
    """시리즈들의 원본 파일 경로 (중복 없이, 멀티프레임은 파일 하나)"""
    seen, out = set(), []
    for s in series_list:
        for ds in s.slices:
            path = getattr(ds, "filename", None)
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def rewrite_tags(paths, changes, backup=True, progress=None, cancelled=None):
    """changes {키워드: 새 값} 을 각 파일에 적용 → (성공 수, [(경로, 오류)])"""
    for keyword in changes:
        if keyword not in EDITABLE:
            raise ValueError(f"바꿀 수 없는 태그: {keyword}")
    ok, errors = 0, []
    total = len(paths)
    for i, path in enumerate(paths):
        if cancelled is not None and cancelled():
            raise EditCancelled(f"{i}/{total}개 파일을 바꾼 뒤 취소했습니다.")
        if progress is not None:
            progress(i, total, path)
        tmp = path + ".dvtmp"
        try:
            ds = pydicom.dcmread(path, force=True)
            for keyword, value in changes.items():
                setattr(ds, keyword, value)
            ds.save_as(tmp, enforce_file_format=False)
            check = pydicom.dcmread(tmp, stop_before_pixels=True, force=True)
            for keyword, value in changes.items():
                if str(getattr(check, keyword, "")) != str(value):
                    raise ValueError(f"{keyword} 확인 실패")
            if backup and not os.path.exists(path + ".bak"):
                shutil.copy2(path, path + ".bak")
            os.replace(tmp, path)
            ok += 1
        except Exception as e:  # noqa: BLE001 - 이 파일만 건너뛰고 계속
            errors.append((path, f"{type(e).__name__}: {e}"))
            try:
                os.remove(tmp)
            except OSError:
                pass
    if progress is not None:
        progress(total, total, "")
    return ok, errors


def apply_in_memory(series_list, changes):
    """불러온 메타데이터에도 반영 (다시 열지 않아도 화면에 보이게)"""
    for s in series_list:
        for ds in s.slices:
            for keyword, value in changes.items():
                if keyword in ds:
                    del ds[keyword]   # 원본과 공유하는 요소를 바꾸지 않도록
                setattr(ds, keyword, value)
        if "SeriesDescription" in changes:
            s.description = str(changes["SeriesDescription"])
