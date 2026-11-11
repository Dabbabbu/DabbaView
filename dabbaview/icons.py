# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
도구 막대 아이콘 - PACS(RadiAnt · INFINITT)에서 쓰는 그림에 맞춘 선 · 면 아이콘

웹 DabbaView와 같은 그림을 쓴다 (24x24 격자의 SVG 경로).
 - 문자열    → 선으로만 그린다
 - (선, 면)  → 면은 꽉 채운다. 작게 봐도 뜻이 사는 아이콘이 된다

QtSvg가 없는 설치본에서도 앱이 멈추지 않도록, 못 그리면 빈 QIcon을 돌려준다
(그러면 도구 막대는 지금처럼 글자만 나온다).
"""
import functools

from PyQt5.QtCore import QByteArray, QSize, Qt
from PyQt5.QtGui import QIcon, QPainter, QPixmap

ICON_COLOR = "#cfd6df"       # 도구 막대 글자색과 같은 회백색
ICON_COLOR_ON = "#5aa9ff"    # 켜진 도구

# 선만 쓰는 아이콘은 문자열, 채운 면이 있는 아이콘은 (선, 면)
PATHS = {
    "select": "M5 3l10.5 6.5-4.3 1.3-1.4 4.2z",
    # W/L — 밝기 · 대비(해 모양)
    "wl": ("M12 2v2.6M12 19.4V22M2 12h2.6M19.4 12H22M4.9 4.9l1.9 1.9M17.2 17.2l1.9 1.9"
           "M19.1 4.9l-1.9 1.9M6.8 17.2l-1.9 1.9",
           "M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6z"),
    # 원본(DICOM 태그) W/L로 되돌리기 — 대비 원 + 되돌림 화살표
    "wlsrc": ("M12 8.4a3.6 3.6 0 1 0 0 7.2 3.6 3.6 0 0 0 0-7.2M20.5 12a8.5 8.5 0 1 1-2.6-6.1"
              "M20.5 3.8v4.3h-4.3",
              "M12 8.4a3.6 3.6 0 0 1 0 7.2z"),
    "pan": ("M12 3.5v17M3.5 12h17M12 3.5 9.7 5.9M12 3.5l2.3 2.4M12 20.5l-2.3-2.4M12 20.5l2.3-2.4"
            "M3.5 12l2.4-2.3M3.5 12l2.4 2.3M20.5 12l-2.4-2.3M20.5 12l-2.4 2.3"),
    "zoom": "M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM15.5 15.5 21 21M10.5 7.5v6M7.5 10.5h6",
    # 거리 — 두 점 사이
    "dist": ("M6.4 17.6 17.6 6.4",
             "M5 16.8a2.2 2.2 0 1 0 0 4.4 2.2 2.2 0 0 0 0-4.4zM19 2.8a2.2 2.2 0 1 0 0 4.4 2.2 2.2 0 0 0 0-4.4z"),
    # 다중 점 경로
    "path": ("M4 18l5-7 4 3 7-9",
             "M4 16.4a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6zM9 9.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6z"
             "M13 12.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6zM20 3.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6z"),
    "angle": "M5 19h15M5 19 17 5M11 19a6 6 0 0 0-1.8-4.3",
    # Cobb 각 — 두 선과 사잇각
    "cobb": "M3 7h8M13 17h8M8.5 7 15.5 17M6 10.5a5 5 0 0 0 1.2 2.4",
    # 3D 커서 — 정육면체 + 찍은 점
    "cursor3d": ("M12 2.8 20 7.2v9.6L12 21.2 4 16.8V7.2zM4 7.2l8 4.4M20 7.2l-8 4.4M12 11.6v9.6",
                 "M12 13.8a1.4 1.4 0 1 0 0 2.8 1.4 1.4 0 0 0 0-2.8z"),
    "magnify": "M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM15.5 15.5 21 21M7.8 10.5h5.4",
    # 자유곡선 ROI — 연필
    "roi": ("M4 20l1-4L16 5a2.1 2.1 0 0 1 3 3L8 19l-4 1zM14.5 6.5l3 3",
            "M4 20l1-4 .8-.8 3 3-.8.8z"),
    "ellipse": "M12 5.2c4.8 0 8.8 3 8.8 6.8s-4 6.8-8.8 6.8-8.8-3-8.8-6.8 4-6.8 8.8-6.8z",
    "rect": "M4 6.5h16v11H4z",
    # 면적 — 빗금 친 영역
    "area": "M4.5 7.5h15v9h-15zM6 16.5 15 7.5M10 16.5 19 7.5M4.5 13 10 7.5",
    # 화살표 주석
    "arrow": ("M20 4 7.5 16.5",
              "M20 4l-1.2 6.4-5.2-5.2z"),
    # 텍스트 메모
    "text": "M5 6.5V5h14v1.5M12 5v14M9 19h6",
    "landmark": ("M12 2.8c-3.4 0-6 2.6-6 5.9 0 4.3 6 12.5 6 12.5s6-8.2 6-12.5c0-3.3-2.6-5.9-6-5.9z",
                 "M12 6.4a2.4 2.4 0 1 0 0 4.8 2.4 2.4 0 0 0 0-4.8z"),
    "profile": "M3 18h18M5 15l4-6 4 4 5-8",
}

# 도구 막대 글자 → 아이콘 이름 (글자는 그대로 두고 그림만 앞에 붙인다)
TOOL_ICONS = {
    "⬚ Select": "select", "W/L": "wl", "Pan": "pan", "Zoom": "zoom",
    "Dist": "dist", "Path": "path", "Angle": "angle", "Cobb": "cobb",
    "3D Cursor": "cursor3d", "Magnify": "magnify",
    "ROI": "roi", "Ellipse": "ellipse", "Rect": "rect", "Area": "area",
    "Arrow": "arrow", "Text": "text", "📍 Landmark": "landmark", "Profile": "profile",
}


def _svg(name, color):
    """아이콘 하나의 SVG 원문 (선 + 채운 면)"""
    entry = PATHS.get(name)
    if entry is None:
        return None
    stroke, fill = entry if isinstance(entry, tuple) else (entry, "")
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">']
    if fill:
        parts.append(f'<path d="{fill}" fill="{color}" stroke="none"/>')
    if stroke:
        parts.append(f'<path d="{stroke}" fill="none" stroke="{color}" stroke-width="1.7"'
                     ' stroke-linecap="round" stroke-linejoin="round"/>')
    parts.append("</svg>")
    return "".join(parts)


@functools.lru_cache(maxsize=256)
def _pixmap(name, color, size, ratio):
    """SVG → QPixmap. QtSvg가 없으면 None"""
    text = _svg(name, color)
    if not text:
        return None
    try:
        from PyQt5.QtSvg import QSvgRenderer
    except Exception:      # noqa: BLE001 - QtSvg가 빠진 설치본에서는 글자만 쓴다
        return None
    renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
    if not renderer.isValid():
        return None
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


@functools.lru_cache(maxsize=256)
def icon(name, size=20, color=ICON_COLOR):
    """이름 → QIcon. 못 만들면 빈 QIcon (도구 막대는 글자만 나온다)"""
    result = QIcon()
    for ratio in (1.0, 2.0):     # 레티나에서도 또렷하게
        pixmap = _pixmap(name, color, size, ratio)
        if pixmap is not None:
            result.addPixmap(pixmap)
    return result


def has_icons():
    """이 설치본에서 아이콘을 그릴 수 있는가 (QtSvg 유무)"""
    return _pixmap("wl", ICON_COLOR, 20, 1.0) is not None


def icon_size():
    return QSize(20, 20)
