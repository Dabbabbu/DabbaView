# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
HTTPS 인증서 검증용 SSL 컨텍스트

python.org 파이썬(과 이를 묶은 앱 번들)은 시스템 인증서를 쓰지 않아
기본 설정으로는 HTTPS 검증이 실패한다 → certifi의 CA 목록을 사용.
"""
import ssl

_context = None


def ssl_context():
    global _context
    if _context is None:
        try:
            import certifi
            _context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _context = ssl.create_default_context()
    return _context
