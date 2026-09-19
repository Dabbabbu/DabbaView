# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
클라우드 저장소 연동: Google Drive (google-api-python-client) / OneDrive (msal + Graph)

각 사용자가 자기 OAuth 클라이언트 키를 Settings → Cloud에 입력한다 (앱에 내장된 키 없음).
로그인 토큰은 OS 키체인(macOS 키체인 / Windows 자격 증명 관리자)에 저장.
"""
from dataclasses import dataclass, field


@dataclass
class CloudItem:
    id: str
    name: str
    is_folder: bool
    size: int = 0
    modified: str = ""
    downloadable: bool = True
    extra: dict = field(default_factory=dict)


class CloudError(Exception):
    pass


class NotConfigured(CloudError):
    """클라이언트 키가 없음 (Settings → Cloud에서 입력 필요)"""


def safe_name(name):
    keep = "".join(c if c not in '\\/:*?"<>|\0' else "_" for c in name).strip(". ")
    return keep or "unnamed"
