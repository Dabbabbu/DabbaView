# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
새 버전 확인 - GitHub Releases의 최신 태그와 지금 버전 비교

시작할 때 한 번, 백그라운드 스레드에서 확인한다 (실패하면 조용히 넘어감).
보내는 것은 GET 요청 하나뿐이고 사용자 정보는 담지 않는다.
"""
import functools
import json
import re
import ssl
import sys
import urllib.request

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from . import __version__

API_URL = "https://api.github.com/repos/Dabbabbu/DabbaView/releases/latest"
RELEASES_PAGE = "https://github.com/Dabbabbu/DabbaView/releases/latest"
TIMEOUT_S = 6


def parse_version(text):
    """'v2.8.0' → (2, 8, 0). 숫자가 아니면 None"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def is_newer(latest, current=__version__):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


@functools.lru_cache(maxsize=1)
def _ssl_context():
    """파이썬 기본 인증서 저장소가 비어 있는 설치본(python.org · 번들 앱)에서도 되게 certifi 사용"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi가 없으면 기본 설정
        return None


def pick_asset(assets, platform=None):
    """이 컴퓨터에 맞는 설치 파일 → (이름, 내려받기 주소) 또는 None

    Windows: 설치 프로그램(…-Windows-Setup.exe) → 없으면 zip / macOS: …-macOS.zip
    """
    platform = platform or sys.platform
    names = {a.get("name", ""): a.get("browser_download_url", "") for a in assets or []}
    if platform == "win32":
        order = ("-Windows-Setup.exe", "-Windows.zip")
    elif platform == "darwin":
        order = ("-macOS.zip",)
    else:
        return None
    for suffix in order:
        for name, url in names.items():
            if name.endswith(suffix) and url:
                return name, url
    return None


def release_summary(body, limit=12):
    """Release 본문에서 '내려받기' 표 앞까지, 앞쪽 몇 줄만 (알림 창에 보여 줄 변경 내용)"""
    text = str(body or "").split("## 내려받기")[0].strip()
    lines = [re.sub(r"[*`]", "", line).rstrip() for line in text.splitlines() if line.strip()]
    out = lines[:limit]
    if len(lines) > limit:
        out.append("…")
    return "\n".join(out)


def fetch_latest(url=API_URL):
    """→ {tag, name, page, asset: (이름, 주소) | None, notes}. 실패하면 None"""
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"DabbaView/{__version__}",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=_ssl_context()) as response:
        data = json.loads(response.read().decode("utf-8"))
    tag = data.get("tag_name")
    if not tag:
        return None
    return {"tag": tag, "name": data.get("name") or tag, "page": data.get("html_url") or RELEASES_PAGE,
            "asset": pick_asset(data.get("assets")), "notes": release_summary(data.get("body"))}


class UpdateChecker(QObject):
    """백그라운드로 최신 버전 확인

    found   : 새 버전이 있을 때만 (알림 창을 띄우는 쪽)
    checked : 확인이 끝날 때마다 (성공 여부, 최신 정보) — '최신입니다'와 '확인 못 했습니다'를
              가려서 말하려면 이게 필요하다. 네트워크가 끊겨도 최신이라고 말하면 안 되므로.
    """

    found = pyqtSignal(object)        # fetch_latest()의 dict (새 버전일 때만)
    checked = pyqtSignal(bool, object)  # (확인 성공, fetch_latest()의 dict 또는 None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None

    def check(self, skip_version=None):
        """확인을 시작했으면 True, 이미 확인 중이면 False"""
        if self._thread is not None:
            return False

        class Worker(QThread):
            result = pyqtSignal(bool, object)

            def run(self):
                try:
                    self.result.emit(True, fetch_latest())
                except Exception:  # noqa: BLE001 - 네트워크가 없으면 '확인 못 함'으로
                    self.result.emit(False, None)

        worker = Worker(self)
        self._thread = worker

        def done(ok, found):
            self._thread = None
            worker.deleteLater()
            self.checked.emit(bool(ok) and found is not None, found)
            if not found:
                return
            tag = found.get("tag")
            if is_newer(tag) and (skip_version or "").strip() != str(tag).strip():
                self.found.emit(found)
        worker.result.connect(done)
        worker.start()
        return True
