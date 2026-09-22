# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
불러오기 진행 창 - 얼마나 남았는지 보이고, 기다리다 그만둘 수 있게

- 단계 이름 · 파일 수 · 퍼센트 · 경과 시간 · 속도 · 남은 시간 예상
- 응답이 없는 파일이 있으면 파일 이름과 몇 초째인지 표시
- [취소] 지금까지 읽은 영상만 열기   [강제 중단] 기다리지 않고 바로 닫기
- [⏸ 일시정지] / [▶ 계속] — 메모리가 한도를 넘으면 저절로 멈추고 [그래도 계속]
- 끝난 뒤 건너뛴 파일이 있으면 [⟳ 실패 N개 재시도]
"""
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QProgressBar,
                             QPushButton, QVBoxLayout)

AUTO_CLOSE_KEY = "ui/auto_close_progress"


def auto_close_setting():
    """진행 창을 끝나면 자동으로 닫을지 (기본: 닫음) — 앱 전체에서 같은 값"""
    return QSettings("DabbaView", "DabbaView").value(AUTO_CLOSE_KEY, True, type=bool)


def set_auto_close_setting(on):
    QSettings("DabbaView", "DabbaView").setValue(AUTO_CLOSE_KEY, bool(on))


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
    pause_toggled = pyqtSignal(bool)   # True: 일시정지 / False: 계속 (메모리 한도도 무시)
    retry_requested = pyqtSignal()     # 끝난 뒤 '실패 N개 재시도'

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
        self.stats.setStyleSheet("color:#9ab;font-size:12px;"
                                 "font-family:'Menlo','Consolas','Courier New';")
        stats_row.addWidget(self.spinner)
        stats_row.addWidget(self.stats, 1)
        layout.addLayout(stats_row)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#ffb84d;font-size:12px")
        self.detail.setVisible(False)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.auto_close = QCheckBox("끝나면 자동으로 닫기")
        self.auto_close.setToolTip("끄면 다 끝난 뒤에도 창이 남아 결과를 보여 주고, '닫기'로 직접 닫습니다")
        self.auto_close.setChecked(auto_close_setting())
        self.auto_close.toggled.connect(set_auto_close_setting)
        row.addWidget(self.auto_close)
        row.addStretch(1)
        self.close_button = QPushButton("닫기")
        self.close_button.setEnabled(False)           # 끝나기 전에는 비활성
        self.close_button.setToolTip("작업이 끝나면 누를 수 있습니다")
        self.close_button.clicked.connect(self.close)
        self.force_button = QPushButton("강제 중단")
        self.force_button.setToolTip("기다리지 않고 바로 닫습니다 (읽던 파일은 버립니다)")
        self.force_button.clicked.connect(self._on_force)
        self.force_button.setVisible(False)
        self.pause_button = QPushButton("⏸ 일시정지")
        self.pause_button.setToolTip("새 파일을 읽지 않고 잠시 멈춥니다 (읽던 파일은 마저 읽음)")
        self.pause_button.clicked.connect(self._on_pause)
        self._paused = False
        self.retry_button = QPushButton("")
        self.retry_button.setVisible(False)
        self.retry_button.setToolTip("시간 초과 · 클라우드 · 읽기 오류로 건너뛴 파일을\n"
                                     "한도를 두 배로 늘려 천천히 다시 읽습니다")
        self.retry_button.clicked.connect(self._on_retry)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setToolTip("여기까지 읽은 영상만 열립니다")
        self.cancel_button.clicked.connect(self._on_cancel)
        row.addWidget(self.pause_button)
        row.addWidget(self.retry_button)
        row.addWidget(self.force_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self._finished = False
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
        width = len(f"{total:,}")          # 자릿수 고정 — 글자가 좌우로 밀리지 않게
        parts = [f"{current:,}".rjust(width) + f" / {total:,} 파일 ({current * 100.0 / total:5.1f}%)",
                 "경과 " + human_time(elapsed).rjust(8)]
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
        if getattr(self, "_ongoing", False):
            self.stats.setText(f"{self._last_stats}  ·  경과 {human_time(elapsed)}")
            return
        if numbers:                      # 경과 시간은 지금 기준으로 다시 계산
            done, total = numbers
            percent = (done * 100.0 / total) if total else 0.0
            width = len(f"{total:,}")
            text = (f"{done:,}".rjust(width) + f" / {total:,} 파일 ({percent:5.1f}%)"
                    + "  ·  경과 " + human_time(elapsed).rjust(8))
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

    # ─── 일시정지 ───
    def set_paused(self, paused, reason="", percent=None):
        """로더 상태를 창에 반영 (메모리로 멈추면 이유와 '그래도 계속')"""
        if paused == self._paused and not paused:
            return
        self._paused = paused
        if paused:
            self.pause_button.setText("▶ 그래도 계속" if reason == "memory" else "▶ 계속")
            self.pause_button.setToolTip("메모리 한도를 무시하고 이어서 읽습니다" if reason == "memory"
                                         else "이어서 읽습니다")
            if reason == "memory":
                pct = f" {percent:.0f}%" if percent is not None else ""
                self.set_warning(f"⏸ 시스템 메모리 사용{pct} — 한도를 넘어 불러오기를 잠시 멈췄습니다.\n"
                                 "다른 앱을 닫으면 저절로 다시 시작합니다. 지금까지 읽은 영상만 보려면 '취소'.")
            else:
                self.set_warning("⏸ 일시정지했습니다. '▶ 계속'을 누르면 이어서 읽습니다.")
            if not self.label.text().startswith("⏸"):
                self.label.setText("⏸ 일시정지 — " + self.label.text())
        else:
            self.label.setText(self.label.text().replace("⏸ 일시정지 — ", "", 1))
            self.pause_button.setText("⏸ 일시정지")
            self.pause_button.setToolTip("새 파일을 읽지 않고 잠시 멈춥니다 (읽던 파일은 마저 읽음)")
            self.set_warning("")

    def is_paused(self):
        return self._paused

    def _on_pause(self):
        self.pause_toggled.emit(not self._paused)

    def offer_retry(self, count):
        """끝났는데 다시 시도할 수 있는 실패가 있으면 버튼을 켜고 창을 닫지 않음"""
        self._retry_count = count
        self.retry_button.setText(f"⟳ 실패 {count:,}개 재시도")
        self.retry_button.setVisible(count > 0)
        if count > 0:
            self.retry_button.setDefault(True)
            self.retry_button.setFocus()

    def _on_retry(self):
        self.retry_button.setEnabled(False)
        self.close()
        self.retry_requested.emit()

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
        """ESC = 취소 (창은 닫지 않음 — 정리 후 닫힘). 끝난 뒤에는 닫기"""
        if self._finished or getattr(self, "_ongoing", False):
            self.close()
            return
        self._on_cancel()

    def closeEvent(self, event):
        """프로그램이 close()로 닫을 때는 반드시 닫힌다.

        ★ QDialog의 기본 closeEvent는 reject()를 부르고, 그 뒤에도 창이 보이면 닫기를
          '무시'한다. 위 reject()는 ESC를 '취소'로 쓰려고 창을 숨기지 않으므로,
          기본 동작을 쓰면 close()가 영원히 무시되어 진행 창이 남아 있었다.
          → 부모 closeEvent를 거치지 않고 직접 받아들인다.
        """
        self._ticker.stop()
        event.accept()
        self.hide()

    def show_ongoing(self, text, stats, done, total):
        """클라우드: 첫 묶음은 열었고 나머지 폴더는 받는 대로 더하는 중 — 누적 숫자로 계속 갱신"""
        self._ongoing = True
        self._last_numbers = None
        self.label.setText(text)
        self._last_stats = stats
        self._last_update = time.monotonic()
        self.stats.setText(stats)
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        self.detail.setVisible(False)
        self.force_button.setVisible(False)
        self.cancel_button.setVisible(False)
        self.pause_button.setVisible(False)
        self.close_button.setEnabled(True)          # 창만 닫음 — 받기·불러오기는 계속됨
        self.close_button.setToolTip("창만 닫습니다. 받기와 불러오기는 뒤에서 계속됩니다")
        self.setWindowTitle("불러오는 중 — 받는 대로 추가")
        if not self._ticker.isActive():
            self._ticker.start()

    def wasCanceled(self):                             # noqa: N802 - 호환용
        return not self.cancel_button.isEnabled()

    def finish(self, summary="", ok=True):
        """작업이 끝남: 결과를 보여 주고, 설정에 따라 자동으로 닫거나 '닫기'를 활성화"""
        if self._finished:
            return
        self._finished = True
        self._ongoing = False
        self._ticker.stop()
        elapsed = time.monotonic() - self._start
        self.bar.setRange(0, 100)
        self.bar.setValue(100 if ok else self.bar.value())
        self.spinner.setText("✓" if ok else "!")
        self.label.setText(("완료" if ok else "중단됨") + (f" — {summary}" if summary else ""))
        self.stats.setText(f"걸린 시간 {human_time(elapsed)}")
        self.detail.setVisible(False)
        self.force_button.setVisible(False)
        self.cancel_button.setVisible(False)
        self.pause_button.setVisible(False)
        self.close_button.setEnabled(True)
        self.close_button.setDefault(not self.retry_button.isVisible())
        self.close_button.setToolTip("")
        self.setWindowTitle("불러오기 완료" if ok else "불러오기 중단")
        if self.auto_close.isChecked() and not self.retry_button.isVisible():
            QTimer.singleShot(1200, self.close)   # '완료'를 잠깐 보여 주고 닫음 (재시도할 게 있으면 남김)
