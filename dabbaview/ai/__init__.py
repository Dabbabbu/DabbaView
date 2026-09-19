# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
AI Research: 세그멘테이션 라벨링, 학습 데이터 내보내기, 전처리,
MONAI Label / ONNX 모델 연동, 데이터셋(워크리스트) 관리
"""
import os

from PyQt5.QtCore import QStandardPaths


def data_dir(*parts):
    """AI Research 저장 폴더 (앱 데이터 폴더/ai/...) - 없으면 만듦"""
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    path = os.path.join(base or os.path.expanduser("~/.dabbaview"), "ai", *parts)
    os.makedirs(path, exist_ok=True)
    return path
