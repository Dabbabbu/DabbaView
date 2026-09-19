# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Analysis 메뉴 (9개 카테고리) + 오른쪽 Analysis 도크 + 심장 윤곽 오버레이"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPen, QPolygonF

from ..annotations import instance_key
from . import tools_cardiac, tools_quant, tools_regional
from .cardiac import CONTOUR_TYPES, ContourStore
from .panel import AnalysisDock

CATEGORIES = [
    ("Cardiac", tools_cardiac.TOOLS),
    ("Neuro", tools_regional.NEURO + [("neuro_dsc", tools_quant.DSCTool)]),
    ("Oncology", tools_regional.ONCOLOGY),
    ("Lung", tools_regional.LUNG),
    ("MSK", tools_regional.MSK),
    ("Vascular", tools_regional.VASCULAR),
    ("Diffusion", tools_quant.DIFFUSION),
    ("Perfusion", tools_quant.PERFUSION),
    ("Spectroscopy", tools_quant.SPECTROSCOPY),
]


def install(main, menubar):
    """main에 ContourStore·AnalysisDock을 만들고 메뉴바에 &Analysis 메뉴를 넣음"""
    main._contours = ContourStore(main)
    dock = AnalysisDock(main)
    main._analysis_dock = dock
    main.addDockWidget(Qt.RightDockWidgetArea, dock)
    dock.hide()
    menu = menubar.addMenu("&Analysis")
    for category, tools in CATEGORIES:
        sub = menu.addMenu(category)
        for key, cls in tools:
            action = sub.addAction(cls.title)
            action.triggered.connect(
                lambda _=False, c=category, k=key, t=cls: dock.open_tool(c, k, t))
    menu.addSeparator()
    toggle = menu.addAction("Analysis 패널 보이기/숨기기")
    toggle.triggered.connect(lambda: dock.setVisible(not dock.isVisible()))
    from ..viewport import DicomViewport
    DicomViewport.add_overlay_painter(draw_contours(main))
    main._contours.changed.connect(lambda *_: [vp.update() for vp in main._all_viewports()])
    return menu


def draw_contours(main):
    """뷰포트 overlay painter: 현재 영상의 심장 윤곽"""
    def paint(vp, painter):
        store = getattr(main, "_contours", None)
        ds = vp.current_dataset()
        if store is None or ds is None:
            return
        items = store.for_image(instance_key(ds, str(vp._current_slice)))
        for item in items:
            name, rgb = CONTOUR_TYPES[item["kind"]]
            poly = QPolygonF([vp._image_to_screen_f(p) for p in item["pts"]])
            painter.setPen(QPen(QColor(*rgb), 1.8))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(poly)
            if len(poly):
                top = min(poly, key=lambda q: q.y())
                vp._draw_text_shadow(painter, int(top.x()), int(top.y() - 4), name)
    return paint
