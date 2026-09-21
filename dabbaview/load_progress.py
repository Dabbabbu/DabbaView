# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
불러오기 진행 창 - 얼마나 남았는지 보이고, 기다리다 그만둘 수 있게

- 단계 이름 · 파일 수 · 퍼센트 · 경과 시간 · 속도 · 남은 시간 예상
- 응답이 없는 파일이 있으면 파일 이름과 몇 초째인지 표시
- [취소] 지금까지 읽은 영상만 열기   [강제 중단] 기다리지 않고 바로 닫기
"""
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QVBoxLayout)


def human_time(seconds):
    """12 → '12초', 90 → '1분 30초', 3700 → '1시간 1분'"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분 {seconds % 60}초"
    return f"{seconds // 3600}시간 {(seconds % 3600) // 60}분"


class LoadProgressDialog(QDialog):
    """QProgressDialog 대신 쓰는 진행 창 (같은 이름의 메서드를 제공해 그대로 바꿔 낄 수 있음)"""

    canceled = pyqtSignal()        # 취소: 지금까지 읽은 것으로 진행
    force_stopped = pyqtSignal()   # 강제 중단: 기다리지 않고 바로 끝냄

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("불러오는 중")
        self.setMinimumWidth(620)
        self.setSizeGripEnabled(False)
        # 대화상자가 아니라 '보통 창'으로 → 메인 창 위에 늘 붙어 있지 않고, 뒤로 보낼 수 있음
        self.setWindowFlags(Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint
                            | Qt.WindowMinimizeButtonHint)
        self._start = time.monotonic()
        self._maximum = 100
        self._force_shown = False
        self._last_update = time.monotonic()
        self._spin = 0
        self._last_stats = ""
        # 새 소식이 없어도 화면이 멈춘 것처럼 보이지 않게 0.5초마다 갱신
        self._ticker = QTimer(self)
        self._ticker.setInterval(500)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start()

        layout = QVBoxLayout(self)
        self.label = QLabel("Loading DICOM files...")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)

        stats_row = QHBoxLayout()
        self.spinner = QLabel("")            # 폭 고정 — 글자가 밀리지 않게
        self.spinner.setFixedWidth(18)
        self.spinner.setAlignment(Qt.AlignCenter)
        self.spinner.setStyleSheet("color:#9ab;font-size:12px")
        self.stats = QLabel("")
        self.stats.setStyleSheet("color:#9ab;font-size:12px")
        stats_row.addWidget(self.spinner)
        stats_row.addWidget(self.stats, 1)
        layout.addLayout(stats_row)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#ffb84d;font-size:12px")
        self.detail.setVisible(False)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        row.addStretch(1)
        self.force_button = QPushButton("강제 중단")
        self.force_button.setToolTip("기다리지 않고 바로 닫습니다 (읽던 파일은 버립니다)")
        self.force_button.clicked.connect(self._on_force)
        self.force_button.setVisible(False)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setToolTip("여기까지 읽은 영상만 열립니다")
        self.cancel_button.clicked.connect(self._on_cancel)
        row.addWidget(self.force_button)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        from PyQt5.QtWidgets import QSizePolicy
        for label in (self.label, self.stats, self.detail):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    # ─── QProgressDialog와 같은 이름의 메서드들 ───
    def setLabelText(self, text):                      # noqa: N802 - Qt 이름 그대로
        first, _, rest = str(text).partition("\n")
        self.label.setText(first)
        self.detail.setText(rest.strip())
        self.detail.setVisible(bool(rest.strip()))

    def setValue(self, value):                         # noqa: N802
        self.bar.setValue(int(value))

    def setRange(self, low, high):                     # noqa: N802
        self._maximum = high
        self.bar.setRange(low, high)

    def maximum(self):
        return self._maximum

    def setMinimumDuration(self, _ms):                 # noqa: N802 - 호환용(바로 띄움)
        pass

    def setAutoClose(self, _on):                       # noqa: N802 - 호환용
        pass

    # ─── 진행 상황 ───
    def update_load(self, phase, current, total):
        """단계 이름 · 개수 · 퍼센트 · 속도 · 남은 시간"""
        self.label.setText(f"{phase or '불러오는 중'}…")
        if total <= 0:
            self.bar.setRange(0, 0)
            self._last_stats = "파일을 찾는 중입니다…  ·  경과 " + human_time(
                time.monotonic() - self._start)
            self._last_update = time.monotonic()
            self.spinner.setText(self.SPINNER[self._spin])
            self.stats.setText(self._last_stats)
            return
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
            self._maximum = 100
        percent = current * 100 // total
        self.bar.setValue(percent)
        elapsed = time.monotonic() - self._start
        parts = [f"{current:,} / {total:,} 파일 ({percent}%)", f"경과 {human_time(elapsed)}"]
        if current >= 5 and elapsed > 1:
            speed = current / elapsed
            parts.append(f"{speed:.1f}개/초")
            if speed > 0 and current < total:
                parts.append(f"남은 시간 약 {human_time((total - current) / speed)}")
        self._last_stats = "  ·  ".join(parts)
        self._last_numbers = (current, total)
        self._last_update = time.monotonic()
        self.spinner.setText(self.SPINNER[self._spin])
        self.stats.setText(self._last_stats)
        # 5초 넘게 걸리는 작업이면 강제 중단 버튼을 보여 줌
        if not self._force_shown and elapsed > 5:
            self._force_shown = True
            self.force_button.setVisible(True)

    SPINNER = "◐◓◑◒"

    def _tick(self):
        """0.5초마다: 회전 표시와 경과 시간을 갱신해 '진행 중'임을 보여 준다"""
        if not self.isVisible():
            return
        self._spin = (self._spin + 1) % len(self.SPINNER)
        elapsed = time.monotonic() - self._start
        quiet = time.monotonic() - self._last_update
        mark = self.SPINNER[self._spin]
        self.spinner.setText(mark)
        numbers = getattr(self, "_last_numbers", None)
        if numbers:                      # 경과 시간은 지금 기준으로 다시 계산
            done, total = numbers
            percent = (done * 100.0 / total) if total else 0.0
            text = f"{done:,} / {total:,} 파일 ({percent:.1f}%)  ·  경과 {human_time(elapsed)}"
        else:
            text = self._last_stats or f"준비 중…  ·  경과 {human_time(elapsed)}"
        if quiet > 3:
            text += f"   (마지막 응답 {human_time(quiet)} 전 — 큰 파일이면 시간이 걸립니다)"
        if getattr(self, "_cancel_at", None) and time.monotonic() - self._cancel_at > 3:
            text += "   ·  정리가 오래 걸리면 '강제 중단'을 누르세요"
        self.stats.setText(text)

    def show_force_button(self):
        """멈춤이 감지되면 바로 보여 줌"""
        self._force_shown = True
        self.force_button.setVisible(True)

    def set_warning(self, text):
        self.detail.setText(text or "")
        self.detail.setVisible(bool(text))

    # ─── 버튼 ───
    def _on_cancel(self):
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("정리하는 중…")
        self.show_force_button()
        self.force_button.setEnabled(True)      # 취소가 안 먹힐 때를 위해 항상 누를 수 있게
        self.force_button.setDefault(True)
        self._cancel_at = time.monotonic()
        self.canceled.emit()

    def _on_force(self):
        self.force_stopped.emit()

    def reject(self):
        """ESC = 취소 (창은 닫지 않음 — 정리 후 닫힘)"""
        self._on_cancel()

    def wasCanceled(self):                             # noqa: N802 - 호환용
        return not self.cancel_button.isEnabled()
