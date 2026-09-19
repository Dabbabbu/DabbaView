# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Help → Open Datasets: 공개 의료영상 데이터셋·대회 링크 (브라우저로 열림)"""

# (분류, 이름, 설명, URL) - None은 메뉴 구분선
OPEN_DATASETS = [
    ("아카이브", "The Cancer Imaging Archive (TCIA)", "암 영상 DICOM 컬렉션",
     "https://www.cancerimagingarchive.net/"),
    ("아카이브", "MedPix (NLM)", "증례 영상 데이터베이스", "https://medpix.nlm.nih.gov/"),
    ("아카이브", "MIMIC-CXR", "흉부 X선 + 판독문 (PhysioNet)",
     "https://physionet.org/content/mimic-cxr/"),
    ("아카이브", "NLST (National Lung Screening Trial)", "저선량 폐 CT 검진",
     "https://cdas.cancer.gov/nlst/"),
    ("아카이브", "UK Biobank Imaging", "대규모 코호트 영상", "https://www.ukbiobank.ac.uk/"),
    ("아카이브", "OpenNeuro", "뇌 영상 (BIDS)", "https://openneuro.org/"),
    None,
    ("대회·벤치마크", "Grand Challenge", "의료영상 AI 대회", "https://grand-challenge.org/"),
    ("대회·벤치마크", "Medical Segmentation Decathlon", "10개 장기·병변 세그멘테이션",
     "http://medicaldecathlon.com/"),
    ("대회·벤치마크", "ACDC (Cardiac)", "심장 MRI 세그멘테이션·진단",
     "https://www.creatis.insa-lyon.fr/Challenge/acdc/"),
    ("대회·벤치마크", "BraTS (Brain Tumor)", "뇌종양 MRI 세그멘테이션", "https://www.synapse.org/brats"),
    ("대회·벤치마크", "AMOS (Abdominal Multi-Organ)", "복부 다장기 CT/MR",
     "https://amos22.grand-challenge.org/"),
    None,
    ("목록", "Awesome DICOM (GitHub)", "DICOM 도구·데이터 모음",
     "https://github.com/open-dicom/awesome-dicom"),
    ("목록", "Awesome Medical Imaging Datasets", "의료영상 데이터셋 목록",
     "https://github.com/m-aryayi/Medical-Imaging-Datasets"),
]
