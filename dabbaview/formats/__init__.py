# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 외 의료영상 포맷 불러오기(readers) / 변환(writers, convert_dialog)
"""

# 파일 열기 대화상자 필터
OPEN_FILTERS = ";;".join([
    "지원하는 모든 형식 (*.dcm *.DCM *.dicom *.nii *.nii.gz *.nrrd *.nhdr *.mha *.mhd "
    "*.npy *.npz *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.stl "
    "*.zip *.7z *.rar *.tar *.tgz *.gz *.bz2 *.xz *.iso)",
    "DICOM (*.dcm *.DCM *.dicom)",
    "NIfTI (*.nii *.nii.gz)",
    "NRRD (*.nrrd *.nhdr)",
    "MetaImage (*.mha *.mhd)",
    "NumPy (*.npy *.npz)",
    "이미지 시퀀스 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
    "STL 메시 (*.stl)",
    "압축파일 (*.zip *.7z *.rar *.tar *.tgz *.gz *.bz2 *.xz *.iso)",
    "All Files (*)",
])
