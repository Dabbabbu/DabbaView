# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
토큰·비밀값 저장소 - 앱 설정 파일(기본) 또는 OS 키체인(선택)

키체인은 앱 서명이 바뀔 때마다(새 버전으로 업데이트할 때마다) macOS가
"키체인 접근을 허용하시겠습니까?"라며 로그인 암호를 묻는다. 거부해도 설정 파일로 넘어가
그냥 열리기 때문에 물음 자체가 번거롭기만 해서, **기본값은 앱 설정 파일**로 둔다.
키체인에 두고 싶으면 Settings ▸ Cloud에서 켤 수 있다.

앱 번들(py2app)에서는 keyring의 자동 백엔드 탐색(entry point)이 동작하지 않을 수 있어
플랫폼별 백엔드를 직접 지정한다.
"""
import sys

from PyQt5.QtCore import QSettings

SERVICE = "DabbaView"
KEYCHAIN_SETTING = "cloud/use_keychain"     # 기본값: 사용 안 함(False)

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


def use_keychain(settings):
    """키체인에 저장할지 (기본: 아니요 — 업데이트마다 암호를 묻지 않도록)"""
    return bool(settings.value(KEYCHAIN_SETTING, False, type=bool))


def set_use_keychain(settings, on):
    settings.setValue(KEYCHAIN_SETTING, bool(on))
    settings.sync()


class SecureStore:
    def __init__(self, settings=None, service=SERVICE):
        self._settings = settings or QSettings("DabbaView", "DabbaView")
        self.service = service

    def _kr(self):
        """켜져 있을 때만 키체인 사용 (꺼져 있으면 암호를 묻지 않음)"""
        return _keyring() if use_keychain(self._settings) else None

    def get(self, key):
        kr = self._kr()
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
        kr = self._kr()
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
        kr = self._kr()
        if kr is not None:
            try:
                kr.delete_password(self.service, key)
            except Exception:  # noqa: BLE001 - 없는 항목
                pass
        self._settings.remove(f"secure/{key}")
