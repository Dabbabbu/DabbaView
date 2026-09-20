# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
진행 상황 문구 - 오래 걸리는 작업은 모두 같은 형식으로 보여 준다

    12 / 40 (30%)  ·  경과 1분 5초  ·  2.1개/초  ·  남은 시간 약 2분 13초

`Eta`를 만들어 두고 한 단계 끝날 때마다 `text(done, total)`을 부르면 된다.
"""
import time


def human_time(seconds):
    """12 → '12초', 90 → '1분 30초', 3700 → '1시간 1분'"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분 {seconds % 60}초"
    return f"{seconds // 3600}시간 {(seconds % 3600) // 60}분"


def human_size(num_bytes):
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class Eta:
    """경과 시간으로 남은 시간을 어림잡는다 (단위: 개수 또는 바이트)"""

    def __init__(self, unit="개"):
        self.start = time.monotonic()
        self.unit = unit

    def reset(self):
        self.start = time.monotonic()

    @property
    def elapsed(self):
        return time.monotonic() - self.start

    def text(self, done, total, extra=None):
        """'12 / 40 (30%) · 경과 … · 2.1개/초 · 남은 시간 약 …'"""
        done, total = max(0, int(done)), max(0, int(total))
        percent = done * 100 // total if total else 0
        parts = [f"{done:,} / {total:,} {self.unit} ({percent}%)" if total
                 else f"{done:,} {self.unit}",
                 f"경과 {human_time(self.elapsed)}"]
        elapsed = self.elapsed
        if done >= 2 and elapsed > 1:
            speed = done / elapsed
            parts.append(f"{speed:.1f}{self.unit}/초" if speed >= 0.1
                         else f"{elapsed / done:.1f}초/{self.unit}")
            if total and done < total and speed > 0:
                parts.append(f"남은 시간 약 {human_time((total - done) / speed)}")
        if extra:
            parts.append(str(extra))
        return "  ·  ".join(parts)

    def bytes_text(self, got, total_bytes, extra=None):
        """용량 기준: '33.7 MB / 6.3 GB · 4.2 MB/s · 남은 시간 약 …'"""
        elapsed = max(0.001, self.elapsed)
        speed = got / elapsed
        parts = [f"{human_size(got)} / {human_size(total_bytes)}" if total_bytes
                 else human_size(got)]
        if speed > 0:
            parts.append(f"{human_size(speed)}/s")
            left = max(0, total_bytes - got)
            if left and got:
                parts.append(f"남은 시간 약 {human_time(left / speed)}")
        if extra:
            parts.append(str(extra))
        return "  ·  ".join(parts)
