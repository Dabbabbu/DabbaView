# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
토큰·비밀값 저장소 - OS 키체인 (keyring), 안 되면 QSettings

앱 번들(py2app)에서는 keyring의 자동 백엔드 탐색(entry point)이 동작하지 않을 수 있어
플랫폼별 백엔드를 직접 지정한다.
"""
import sys

from PyQt5.QtCore import QSettings

SERVICE = "DabbaView"

_backend = None
_backend_checked = False


def _keyring():
    """사용할 keyring 모듈 (백엔드 지정 완료) 또는 None"""
    global _backend, _backend_checked
    if _backend_checked:
        return _backend
    _backend_checked = True
    try:
        import keyring
        if sys.platform == "darwin":
            from keyring.backends import macOS
            keyring.set_keyring(macOS.Keyring())
        elif sys.platform == "win32":
            from keyring.backends import Windows
            keyring.set_keyring(Windows.WinVaultKeyring())
        backend = keyring.get_keyring()
        if "fail" in type(backend).__module__ or "null" in type(backend).__module__:
            return None
        _backend = keyring
    except Exception:  # noqa: BLE001 - 키체인을 못 쓰면 QSettings로
        _backend = None
    return _backend


def backend_name():
    kr = _keyring()
    if kr is None:
        return "QSettings (키체인 사용 불가)"
    return "macOS 키체인" if sys.platform == "darwin" else \
        "Windows 자격 증명 관리자" if sys.platform == "win32" else "시스템 키링"


class SecureStore:
    def __init__(self, settings=None, service=SERVICE):
        self._settings = settings or QSettings("DabbaView", "DabbaView")
        self.service = service

    def get(self, key):
        kr = _keyring()
        if kr is not None:
            try:
                value = kr.get_password(self.service, key)
                if value is not None:
                    return value
            except Exception:  # noqa: BLE001
                pass
        value = self._settings.value(f"secure/{key}", "", type=str)
        return value or None

    def set(self, key, value):
        kr = _keyring()
        if kr is not None:
            try:
                kr.set_password(self.service, key, value)
                self._settings.remove(f"secure/{key}")   # 예전에 평문으로 둔 값 제거
                return True
            except Exception:  # noqa: BLE001
                pass
        self._settings.setValue(f"secure/{key}", value)
        return False

    def delete(self, key):
        kr = _keyring()
        if kr is not None:
            try:
                kr.delete_password(self.service, key)
            except Exception:  # noqa: BLE001 - 없는 항목
                pass
        self._settings.remove(f"secure/{key}")
