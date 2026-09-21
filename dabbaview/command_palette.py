# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
기능 찾기 (⌘F / Ctrl+F) - 메뉴·도구 막대의 모든 기능을 이름으로 찾아 바로 실행

- 영어 메뉴 이름뿐 아니라 한글·비슷한 말로도 찾는다
  예) '내보내기', 'export', '저장', '동영상', '동영상으로 저장' → Export as Video…, Batch Export Videos…
- 초성(ㄷㅇㅅ → 동영상)과 띄어쓰기 없는 입력도 받는다
- 메뉴에 새 기능이 생기면 자동으로 검색 대상에 들어간다 (따로 등록할 필요 없음)
"""
import re

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QToolBar, QVBoxLayout)

# 비슷한 말: 사용자가 칠 만한 말 → 메뉴 이름에 들어 있는 말
SYNONYMS = {
    "내보내기": ["export", "save", "capture", "video", "내보내기", "저장"],
    "export": ["export", "save", "video", "capture"],
    "저장": ["save", "export", "capture", "저장"],
    "save": ["save", "export"],
    "출력": ["print", "export", "capture"],
    "동영상": ["video", "movie", "cine", "동영상"],
    "비디오": ["video"],
    "영상저장": ["video", "export", "capture"],
    "mp4": ["video"], "gif": ["video"], "avi": ["video"], "movie": ["video"],
    "캡처": ["capture", "screenshot", "캡처"], "스크린샷": ["capture"], "화면저장": ["capture"],
    "열기": ["open", "load", "열기"], "불러오기": ["open", "load", "import"], "open": ["open"],
    "폴더": ["folder", "open", "폴더"],
    "구글": ["google"], "드라이브": ["drive", "google"], "클라우드": ["google", "onedrive", "cloud"],
    "원드라이브": ["onedrive"],
    "설정": ["settings", "preferences", "설정", "환경설정"], "환경설정": ["settings", "preferences"],
    "옵션": ["settings", "preferences"],
    "측정": ["measure", "dist", "angle", "roi", "area"], "거리": ["dist", "distance"],
    "각도": ["angle", "cobb"], "면적": ["area", "roi"], "관심영역": ["roi"],
    "밝기": ["w/l", "window", "level"], "대조": ["w/l", "window"], "윈도우": ["w/l", "window"],
    "확대": ["zoom", "magnify"], "돋보기": ["magnify", "zoom", "찾기"], "축소": ["zoom"],
    "이동": ["pan"], "회전": ["rot", "rotate", "회전"], "뒤집기": ["flip"], "반전": ["flip", "inverse"],
    "재생": ["play", "cine"], "시네": ["cine", "play"],
    "익명": ["anonymize", "익명"], "개인정보": ["anonymize"],
    "전송": ["send", "dicom send"], "보내기": ["send"], "pacs": ["send", "dicom"],
    "인쇄": ["print"], "프린트": ["print"],
    "태그": ["tag", "dicom tags"], "정보": ["info", "image", "tag"],
    "비교": ["compare"], "라이브러리": ["library"], "보관함": ["library"],
    "다중": ["multi"], "여러화면": ["multi view", "tile"], "분할": ["tile", "multi"],
    "3차원": ["3d", "volume"], "입체": ["3d", "volume"], "재구성": ["mpr"],
    "십자": ["crosslink", "cursor"], "추가": ["add folder", "더하기"], "더하기": ["add folder"],
    "같이": ["add folder", "sync"], "함께": ["add folder", "sync"], "연동": ["crosslink", "sync", "연동"],
    "스캔플래닝": ["crosslink", "ref lines"], "플래닝": ["crosslink", "ref lines"], "위치선": ["crosslink", "ref lines"], "동기": ["sync"], "기준선": ["ref lines", "reference"],
    "도움말": ["help", "manual"], "매뉴얼": ["manual", "help"], "업데이트": ["update", "새 버전"],
    "버그": ["bug", "버그"], "문의": ["question", "질문", "bug"],
    "크기": ["화면 크기", "zoom", "크게", "작게"], "글자": ["화면 크기", "크게"],
    "인공지능": ["ai"], "분할(ai)": ["segment"], "세그": ["segment"],
    "변환": ["convert", "export as"], "nifti": ["convert", "nifti"],
    "acr": ["acr"], "팬텀": ["acr", "phantom"], "정도관리": ["acr", "qc"],
    "심장": ["cardiac"], "주석": ["annotation", "text", "arrow"], "화살표": ["arrow"],
    "글씨": ["text"], "메모": ["text", "note"],
    "캐시": ["캐시", "cache"], "cache": ["캐시"], "임시파일": ["캐시", "임시 파일"], "임시": ["캐시"],
    "정리": ["캐시", "정리", "clear"], "비우기": ["캐시 지우기", "clear"], "용량": ["캐시", "용량"],
    "디스크": ["캐시", "용량"], "공간": ["캐시", "공간"],
}

CHOSEONG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def choseong(text):
    """'동영상' → 'ㄷㅇㅅ' (한글 음절만 초성으로, 나머지는 그대로)"""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        out.append(CHOSEONG[code // 588] if 0 <= code < 11172 else ch)
    return "".join(out)


def matched_keys(q):
    """정규화된 입력 q 안에 들어 있는 비슷한 말 목록 (초성만 쳐도 됨: ㄷㅇㅅ → 동영상)"""
    if q and all(ch in CHOSEONG for ch in q):
        return [k for k in SYNONYMS if len(q) >= 2 and choseong(normalize(k)) == q]
    keys = [k for k in SYNONYMS if normalize(k) and normalize(k) in q]
    if not keys and len(q) >= 2:               # 치는 중: '동영' → 동영상
        keys = [k for k in SYNONYMS if normalize(k).startswith(q)]
    # 더 긴 말에 들어 있는 짧은 말은 뺌 ('영상저장' 안의 '저장')
    return [k for k in keys
            if not any(k != o and normalize(k) in normalize(o) for o in keys)]


def normalize(text):
    return re.sub(r"[\s·…\.\-_/()]+", "", str(text or "").lower())


def collect_actions(window):
    """메뉴(하위 메뉴 포함)·도구 막대의 기능 → [(QAction, '메뉴 ▸ 이름')] (중복 없이)"""
    found, seen = [], set()

    def add(action, path):
        if action is None or action.isSeparator() or id(action) in seen:
            return
        if action.menu() is not None:
            walk(action.menu(), path + [clean(action.text())])
            return
        name = clean(action.text())
        if not name:
            return
        seen.add(id(action))
        found.append((action, " ▸ ".join(path + [name])))

    def walk(menu, path):
        for action in menu.actions():
            add(action, path)

    for top in window.menuBar().actions():
        if top.menu() is not None:
            walk(top.menu(), [clean(top.text())])
    for bar in window.findChildren(QToolBar):
        for action in bar.actions():
            add(action, ["도구 막대"])
    return found


def clean(text):
    return str(text or "").replace("&", "").strip()


def score(query, action, path):
    """높을수록 잘 맞음. 0이면 결과에서 뺌"""
    q = normalize(query)
    if not q:
        return 1
    name = normalize(clean(action.text()))
    hay = normalize(" ".join([path, action.toolTip(), action.statusTip()]))
    best = 0
    if name.startswith(q):
        best = max(best, 100)
    if q in name:
        best = max(best, 80)
    if q in hay:
        best = max(best, 60)
    if all(ch in CHOSEONG for ch in q) and q in choseong(clean(action.text()) + " " + path + " " + action.toolTip()):
        best = max(best, 55)          # 초성 검색 (ㄷㅇㅅ → 동영상)
    # 비슷한 말 — 입력 안에 든 말(동영상·저장 …)마다 메뉴 이름과 맞춰 보고, 많이 맞을수록 위로
    #   '동영상으로 저장' → 동영상(video) + 저장(export) 둘 다 맞는 Export as Video가 맨 위
    keys = matched_keys(q)
    in_name = in_hay = 0
    for key in keys:
        targets = [normalize(t) for t in SYNONYMS[key]]
        if any(t and t in name for t in targets):
            in_name += 1
        elif any(t and t in hay for t in targets):
            in_hay += 1
    if in_name:
        exact = len(keys) == 1 and normalize(keys[0]) == q
        best = max(best, 85 if exact else 50 + 10 * in_name + 3 * in_hay)
    elif in_hay:
        best = max(best, 35 + 5 * in_hay)
    # 여러 낱말이면 낱말이 이름에 들어 있을 때마다 조금 더 ('화면 크기 크게' → 크게가 위로)
    if best:
        best += sum(2 for w in str(query).split() if normalize(w) and normalize(w) in name)
    # 흩어진 글자 (ex: 'bvid' → Batch Export Videos)
    if best == 0 and len(q) >= 3:
        it = iter(name)
        if all(ch in it for ch in q):
            best = 20
    if best and not action.isEnabled():
        best -= 15                     # 지금은 못 쓰는 기능은 뒤로
    return best


class CommandPalette(QDialog):
    """⌘F 기능 찾기 창"""

    ran = pyqtSignal(str)

    def __init__(self, window):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("기능 찾기")
        self.setMinimumWidth(620)
        self.setMinimumHeight(420)
        layout = QVBoxLayout(self)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("🔍 찾을 기능을 입력하세요 — 예: 내보내기, 동영상, export, 설정, ㄷㅇㅅ")
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self.refresh)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit)
        self.list = QListWidget()
        self.list.itemActivated.connect(self._run_item)
        self.list.itemDoubleClicked.connect(self._run_item)
        layout.addWidget(self.list, 1)
        self.hint = QLabel("↑↓ 고르기 · Enter 실행 · Esc 닫기")
        self.hint.setStyleSheet("color:#8a9;font-size:11px;")
        layout.addWidget(self.hint)
        self._actions = []

    def open_palette(self):
        self._actions = collect_actions(self.window_)
        self.edit.clear()
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()

    def refresh(self, *_args):
        query = self.edit.text()
        ranked = []
        for action, path in self._actions:
            s = score(query, action, path)
            if s > 0:
                ranked.append((s, path, action))
        ranked.sort(key=lambda r: (-r[0], r[1]))
        self.list.clear()
        for s, path, action in ranked[:60]:
            shortcut = action.shortcut().toString(QKeySequence.NativeText)
            text = path + (f"      {shortcut}" if shortcut else "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, action)
            tip = action.toolTip() or action.statusTip()
            if tip and tip != clean(action.text()):
                item.setToolTip(tip)
            if not action.isEnabled():
                item.setForeground(Qt.gray)
                item.setToolTip("지금은 쓸 수 없습니다 (영상을 먼저 열어야 하는 기능 등)")
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.hint.setText(f"{self.list.count()}개 · ↑↓ 고르기 · Enter 실행 · Esc 닫기"
                          if query else "↑↓ 고르기 · Enter 실행 · Esc 닫기")

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() == event.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                item = self.list.currentItem()
                if item is not None:
                    self._run_item(item)
                return True
        return super().eventFilter(obj, event)

    def _run_item(self, item):
        action = item.data(Qt.UserRole)
        if action is None or not action.isEnabled():
            return
        self.hide()
        name = clean(action.text())
        action.trigger()
        self.ran.emit(name)
