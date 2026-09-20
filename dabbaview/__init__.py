# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""DabbaView - Python DICOM Viewer inspired by RadiAnt

버전은 여기의 __version__ 하나만 고치면 된다.
앱 번들(setup_app.py), 타이틀 바, About, 시작 화면, README가 모두 이 값을 쓴다.
"""
__version__ = "2.5.1"

APP_NAME = "DabbaView"
COPYRIGHT = "Copyright (c) 2026 Park Seongho (Dabbabbu)"
LICENSE_NAME = "GNU General Public License v3.0 (GPL-3.0)"
GITHUB_URL = "https://github.com/Dabbabbu/DabbaView"


def build_info():
    """(빌드 날짜, 커밋) - 앱 번들은 setup_app.py가 만든 _build_info에서, 소스 실행은 None"""
    try:
        from ._build_info import BUILD_DATE, GIT_COMMIT
        return BUILD_DATE, GIT_COMMIT
    except ImportError:
        return None, None
