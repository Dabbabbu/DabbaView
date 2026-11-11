# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
그림 · 동영상으로 내보낼 때 '무엇을 함께 담을지' 고르는 드롭다운

캡처와 동영상 내보내기가 같은 목록을 쓴다 —
측정 · ROI · 측정 숫자 · 화살표 · 텍스트 · 판서 · 라인 프로파일 · 랜드마크 ·
환자 정보 글자(데모그래픽)를 하나씩 체크해서 넣고 뺀다.

고른 값은 설정에 남아 다음에 열 때 그대로 나온다.
"""
from contextlib import contextmanager

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
                             QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout)

from .viewport import DicomViewport

# (키, 메뉴에 보일 글자)
OVERLAY_PARTS = (
    ("measurements", "📐 측정 · ROI (선과 도형)"),
    ("measure_labels", "🔢 측정 숫자 (Area · Mean …)"),
    ("notes", "↗ 화살표 · 텍스트"),
    ("scribble", "✍ 판서 (필기)"),
    ("profile", "📈 라인 프로파일"),
    ("landmarks", "📍 랜드마크"),
    ("demographics", "🆔 환자 정보 글자 (모서리 글자)"),
)
ALL_KEYS = tuple(k for k, _ in OVERLAY_PARTS)
DEFAULT_KEYS = ALL_KEYS          # 처음에는 보이는 그대로 모두 담는다


def load(settings, key):
    """설정에서 고른 항목 불러오기 (없으면 모두)"""
    saved = settings.value(key, None)
    if saved is None:
        return set(DEFAULT_KEYS)
    chosen = {k.strip() for k in str(saved).split(",") if k.strip()}
    return {k for k in ALL_KEYS if k in chosen}


def save(settings, key, picked):
    settings.setValue(key, ",".join(k for k in ALL_KEYS if k in picked))


@contextmanager
def applied(viewport, picked):
    """고른 항목만 보이게 잠깐 바꿨다가 그대로 되돌린다

    DicomViewport의 표시 상태는 클래스 전체가 함께 쓰므로,
    내보내는 동안만 바꾸고 끝나면 반드시 되돌린다.
    """
    V = DicomViewport
    attrs = ("annotations_visible", "show_measurements", "show_measure_labels",
             "show_notes", "show_scribble", "show_profile", "landmark_display")
    saved = {a: getattr(V, a) for a in attrs}
    overlay_saved = getattr(viewport, "_show_overlay", None) if viewport is not None else None
    try:
        V.annotations_visible = True
        V.show_measurements = "measurements" in picked
        V.show_measure_labels = "measure_labels" in picked
        V.show_notes = "notes" in picked
        V.show_scribble = "scribble" in picked
        V.show_profile = "profile" in picked
        if "landmarks" not in picked:
            V.landmark_display = "hidden"
        elif saved["landmark_display"] == "hidden":
            V.landmark_display = "subtle"     # 켜 달라고 골랐으면 보이게
        if viewport is not None:
            viewport._show_overlay = "demographics" in picked
        yield
    finally:
        for a, v in saved.items():
            setattr(V, a, v)
        if viewport is not None and overlay_saved is not None:
            viewport._show_overlay = overlay_saved


class OverlayPicker(QToolButton):
    """'함께 담을 것 ▾' — 누르면 펼쳐지고, 체크한 것만 들어간다"""

    def __init__(self, settings=None, key="export/overlay_parts", parent=None):
        super().__init__(parent)
        self._settings = settings
        self._key = key
        self.picked = (load(settings, key) if settings is not None
                       else set(DEFAULT_KEYS))
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setMinimumWidth(220)
        self.setToolTip("체크한 것만 그림 · 동영상에 함께 담깁니다\n"
                        "(영상 자체는 늘 담깁니다)")
        self._menu = QMenu(self)
        self._menu.setToolTipsVisible(True)
        self._actions = {}
        for k, label in OVERLAY_PARTS:
            action = self._menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(k in self.picked)
            action.toggled.connect(lambda on, kk=k: self._toggled(kk, on))
            self._actions[k] = action
        self._menu.addSeparator()
        self._menu.addAction("모두 체크", lambda: self.set_picked(ALL_KEYS))
        self._menu.addAction("모두 해제", lambda: self.set_picked(()))
        self.setMenu(self._menu)
        self._refresh_text()

    def _toggled(self, key, on):
        if on:
            self.picked.add(key)
        else:
            self.picked.discard(key)
        self._store()

    def set_picked(self, keys):
        self.picked = {k for k in ALL_KEYS if k in set(keys)}
        for k, action in self._actions.items():
            action.blockSignals(True)
            action.setChecked(k in self.picked)
            action.blockSignals(False)
        self._store()

    def _store(self):
        if self._settings is not None:
            save(self._settings, self._key, self.picked)
        self._refresh_text()

    def _refresh_text(self):
        n = len(self.picked)
        if n == len(ALL_KEYS):
            text = "모두 함께 담기 (영상에 보이는 그대로)"
        elif n == 0:
            text = "영상만 (그린 것 없이)"
        else:
            text = f"고른 {n}가지만 함께 담기"
        self.setText(f"  {text}  ▾")


class CaptureOptionsDialog(QDialog):
    """📷 캡처 — 바탕과 '함께 담을 것'을 고르고 저장

    체크한 항목만 그림에 들어간다. 고른 값은 다음에 열 때도 그대로 남는다.
    """

    BACKGROUNDS = (
        ("image", "영상 그대로"),
        ("transparent", "투명 배경 — 그린 것만 (PNG)"),
    )

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📷 캡처 — 무엇을 담을까요?")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._background = QComboBox()
        for _key, label in self.BACKGROUNDS:
            self._background.addItem(label)
        self._background.setToolTip("투명 배경은 PNG로 저장해야 투명이 남습니다 "
                                    "(발표 자료에 겹쳐 쓰기 좋음)")
        form.addRow("바탕:", self._background)

        self.picker = OverlayPicker(settings, "capture/overlay_parts", self)
        form.addRow("함께 담을 것:", self.picker)
        layout.addLayout(form)

        hint = QLabel("누르면 목록이 펼쳐집니다 — 체크한 것만 그림에 들어갑니다.")
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.addStretch()
        save = QPushButton("저장…")          # 확인을 왼쪽에 (앱 전체 규칙)
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(save)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        layout.addLayout(row)

    @property
    def picked(self):
        return set(self.picker.picked)

    def kind(self):
        """DicomViewport.capture()에 넘길 종류"""
        if self.BACKGROUNDS[self._background.currentIndex()][0] == "transparent":
            return "marks"
        return "full" if self.picker.picked else "image"
