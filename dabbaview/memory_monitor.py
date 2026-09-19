# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""메모리 사용량 (불러오는 동안 상태바 'RAM: X MB', 시스템 메모리 80% 경고)"""
import sys

try:
    import resource   # macOS/Linux (Windows에는 없음)
except ImportError:
    resource = None

SYSTEM_WARN_PERCENT = 80.0

try:
    import psutil
except ImportError:  # 없으면 최대 사용량(maxrss)만 표시
    psutil = None


def process_mb():
    """이 앱이 지금 쓰는 물리 메모리 (MB)"""
    if psutil is not None:
        return psutil.Process().memory_info().rss / 1e6
    if resource is None:
        return 0.0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3   # macOS는 바이트, Linux는 KB


def system_percent():
    """시스템 전체 메모리 사용률 (%). 알 수 없으면 None"""
    if psutil is None:
        return None
    return psutil.virtual_memory().percent


def status():
    """(표시 문자열, 경고 여부)"""
    mb = process_mb()
    pct = system_percent()
    text = f"RAM: {mb:,.0f} MB" + (f" (시스템 {pct:.0f}%)" if pct is not None else "")
    return text, pct is not None and pct >= SYSTEM_WARN_PERCENT
