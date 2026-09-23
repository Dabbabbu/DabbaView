# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Multi View 레이아웃 (INFINITT PACS의 Image set layout)

- 레이아웃 이름은 "행x열" (예: 2x3 = 2줄 3칸). 화면에 보이는 대로 세로 먼저.
- 기본(Default) 목록과 사용자가 고른 목록(User define)을 따로 둔다 —
  드롭다운에는 사용자가 고른 것만 나오고, Config 창에서 넣고 빼고 순서를 바꾼다.
- Auto: 열린 시리즈 수에 맞는 레이아웃을 스스로 고름
"""
import re

MAX_ROWS = 6
MAX_COLS = 6
MAX_CELLS = 24                 # 칸 개수 상한 (뷰포트를 미리 만들어 두는 수)

# 드롭다운 · Config 창의 '기본' 목록 (INFINITT과 같은 구성)
DEFAULT_LAYOUTS = ["1x1", "1x2", "1x3", "1x4", "2x1", "2x2", "2x3", "2x4",
                   "3x1", "3x2", "3x3", "3x4", "4x1", "4x2", "4x3", "4x4", "4x5"]

# 설정이 없을 때 드롭다운에 나오는 목록
DEFAULT_ACTIVE = ["1x1", "1x2", "2x1", "2x2", "2x3", "3x3", "4x4"]

AUTO = "auto"                  # 시리즈 수에 맞춰 스스로
_ID_RE = re.compile(r"^(\d+)\s*[xX×*]\s*(\d+)$")


def parse(layout_id):
    """'2x3' · '2X3' · '2×3' → (2, 3). 범위를 벗어나거나 형식이 다르면 None"""
    if not isinstance(layout_id, str):
        return None
    m = _ID_RE.match(layout_id.strip())
    if not m:
        return None
    rows, cols = int(m.group(1)), int(m.group(2))
    if not (1 <= rows <= MAX_ROWS and 1 <= cols <= MAX_COLS) or rows * cols > MAX_CELLS:
        return None
    return rows, cols


def name(rows, cols):
    return f"{int(rows)}x{int(cols)}"


def clean(layout_id):
    """올바른 레이아웃 이름으로 (아니면 None). 'auto'는 그대로"""
    if isinstance(layout_id, str) and layout_id.strip().lower() == AUTO:
        return AUTO
    size = parse(layout_id)
    return name(*size) if size else None


def cells(layout_id):
    size = parse(layout_id)
    return size[0] * size[1] if size else 0


def label(layout_id):
    """드롭다운에 보이는 글자 (INFINITT처럼 대문자 X)"""
    if layout_id == AUTO:
        return "Auto (시리즈 수에 맞춤)"
    size = parse(layout_id)
    return f"{size[0]}X{size[1]}" if size else str(layout_id)


def clean_list(values, fallback=None):
    """저장된 목록을 검사 (중복·잘못된 값 제거). 비면 fallback"""
    out = []
    for value in values or []:
        clean_id = clean(value)
        if clean_id and clean_id != AUTO and clean_id not in out:
            out.append(clean_id)
    return out or list(fallback if fallback is not None else DEFAULT_ACTIVE)


# 칸 수에 맞는 레이아웃을 고를 때 쓰는 순서 (Auto · 시리즈를 펼칠 때)
AUTO_ORDER = ["1x1", "1x2", "2x2", "2x3", "3x3", "3x4", "4x4", "4x5"]


def for_count(n, choices=None):
    """n개가 들어가는 가장 작은 레이아웃 (Auto)"""
    n = max(1, int(n))
    for layout_id in (choices or AUTO_ORDER):
        if cells(layout_id) >= n:
            return layout_id
    return (choices or AUTO_ORDER)[-1]


def icon(layout_id):
    """버튼·메뉴용 간단한 그림 문자 (없으면 이름 그대로)"""
    return {"1x1": "▣", "1x2": "◫", "2x1": "⊟", "2x2": "⊞"}.get(layout_id, label(layout_id))
