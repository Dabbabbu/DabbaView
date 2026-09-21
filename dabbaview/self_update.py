# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
앱 안에서 업데이트 - 새 버전을 받아 두고, DabbaView를 닫으면 설치한 뒤 다시 켠다

- Windows: 설치 프로그램(…-Windows-Setup.exe)을 받아 조용히 실행 → 같은 자리에 덮어쓰고 다시 켬
  (설치 프로그램으로 설치한 경우만. zip으로 쓰던 경우는 Setup.exe로 한 번 설치해야 함)
- macOS: …-macOS.zip을 받아 풀고, 앱이 꺼진 뒤 지금 DabbaView.app 자리에 바꿔 넣고 다시 켬
  (옛 앱은 휴지통으로)
- 소스로 실행 중이면(개발용) 하지 않음
"""
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.request

from PyQt5.QtCore import QThread, pyqtSignal

from . import __version__
from .update_check import _ssl_context

CHUNK = 256 * 1024


def frozen():
    """설치된 앱으로 실행 중인지 (py2app · PyInstaller)"""
    return bool(getattr(sys, "frozen", False)) or ".app/Contents/" in (sys.executable or "")


def app_bundle_path():
    """macOS: 지금 실행 중인 DabbaView.app 경로 (없으면 None)"""
    exe = os.path.abspath(sys.executable or "")
    marker = ".app/Contents/"
    if marker not in exe:
        return None
    return exe[:exe.index(marker) + len(".app")]


def installed_with_setup():
    """Windows: 설치 프로그램으로 설치됐는지 (제거 정보 unins000.exe가 옆에 있음)"""
    folder = os.path.dirname(os.path.abspath(sys.executable or ""))
    return os.path.exists(os.path.join(folder, "unins000.exe"))


def can_self_update(asset_name, platform=None):
    """이 설치 파일로 앱 안 업데이트를 할 수 있는지 → (가능?, 안 되는 이유)"""
    platform = platform or sys.platform
    if not frozen():
        return False, "소스로 실행 중이라 앱 안 업데이트를 하지 않습니다."
    if platform == "win32":
        if not asset_name.endswith("-Setup.exe"):
            return False, "이 버전에는 Windows 설치 프로그램이 없습니다."
        if not installed_with_setup():
            return False, ("zip으로 쓰고 있어 자동 업데이트가 안 됩니다 — 설치 파일(Setup.exe)을 받아 "
                           "한 번 설치하면 다음부터는 앱 안에서 업데이트됩니다.")
        return True, ""
    if platform == "darwin":
        if not asset_name.endswith("-macOS.zip"):
            return False, "이 버전에는 macOS 앱이 없습니다."
        bundle = app_bundle_path()
        if not bundle or not os.access(os.path.dirname(bundle), os.W_OK):
            return False, "앱이 있는 폴더에 쓸 수 없어 자동으로 바꿀 수 없습니다."
        return True, ""
    return False, "이 운영체제는 앱 안 업데이트를 지원하지 않습니다."


class Downloader(QThread):
    """설치 파일을 임시 폴더에 받음 — progress(받은 바이트, 전체), finished_ok(경로) / failed(메시지)"""

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, url, name, parent=None):
        super().__init__(parent)
        self.url, self.name = url, name
        self.cancelled = False

    def run(self):
        folder = tempfile.mkdtemp(prefix="dabbaview-update-")
        path = os.path.join(folder, self.name)
        try:
            request = urllib.request.Request(self.url, headers={"User-Agent": f"DabbaView/{__version__}"})
            with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response, \
                    open(path, "wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    if self.cancelled:
                        raise InterruptedError("취소했습니다.")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    self.progress.emit(got, total)
            if total and got < total:
                raise IOError(f"덜 받았습니다 ({got:,} / {total:,} 바이트)")
            self.finished_ok.emit(path)
        except Exception as exc:  # noqa: BLE001 - 네트워크 · 디스크 오류는 알림으로
            self.failed.emit(str(exc) or type(exc).__name__)


def apply_windows(setup_path):
    """앱이 꺼진 뒤: 설치 프로그램을 조용히 실행 → 같은 자리에 덮어쓰고 DabbaView를 다시 켬"""
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([setup_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=flags, close_fds=True)


def macos_script(new_zip, bundle, pid, relaunch=True):
    """앱이 꺼진 뒤 실행할 셸 스크립트 (테스트할 수 있게 문자열로)"""
    q = shlex.quote
    work = os.path.join(os.path.dirname(new_zip), "unpacked")
    lines = [
        "#!/bin/sh",
        f"while kill -0 {int(pid)} 2>/dev/null; do sleep 0.5; done",
        f"rm -rf {q(work)} && mkdir -p {q(work)}",
        f"ditto -x -k {q(new_zip)} {q(work)} || exit 1",
        f"NEW=$(find {q(work)} -maxdepth 2 -name '*.app' -type d | head -1)",
        '[ -n "$NEW" ] || exit 1',
        f'TRASH="$HOME/.Trash/DabbaView-{__version__}-$(date +%s).app"',
        f'mv {q(bundle)} "$TRASH" 2>/dev/null || rm -rf {q(bundle)}',
        f'ditto "$NEW" {q(bundle)} || {{ mv "$TRASH" {q(bundle)}; exit 1; }}',
        f"xattr -dr com.apple.quarantine {q(bundle)} 2>/dev/null",
        f"rm -rf {q(work)} {q(new_zip)}",
    ]
    if relaunch:
        lines.append(f"open {q(bundle)}")
    return "\n".join(lines) + "\n"


def apply_macos(zip_path):
    """앱이 꺼진 뒤: 새 DabbaView.app으로 바꾸고 다시 켬 (옛 앱은 휴지통)"""
    bundle = app_bundle_path()
    script = os.path.join(os.path.dirname(zip_path), "apply_update.sh")
    with open(script, "w") as f:
        f.write(macos_script(zip_path, bundle, os.getpid()))
    os.chmod(script, 0o755)
    subprocess.Popen(["/bin/sh", script], start_new_session=True, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply(path):
    if sys.platform == "win32":
        apply_windows(path)
    elif sys.platform == "darwin":
        apply_macos(path)
