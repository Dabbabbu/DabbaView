# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""뷰포트 주석 편집 (DicomViewport가 상속)

- 선택: Select 도구로 ROI/측정을 누르면 선택 (Shift/⌘: 여러 개), 빈 곳을 누르면 해제
- 이동: 선택한 주석 몸통을 끌기 / 끝점·모서리 핸들 끌기로 수정 (모든 그리기 도구에서 핸들은 바로 잡힘)
- 잠금(locked) 주석은 움직이지 않음
- Shift: 직선을 0/45/90° 로 스냅
- 측정 도구 사용 중 커서 옆에 실시간 거리 (측정 전에는 마지막 점에서의 거리)
- 표시 설정: 글자 크기, 단위 (mm / cm / pixel) - 모든 뷰포트 공통
"""
import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainterPath, QPen, QPolygonF

from . import dicom_info, roi_tools
from .annotations import ROI_TYPES

LINE_TYPES = ("distance", "arrow", "path", "angle", "cobb")
EDITABLE_TYPES = LINE_TYPES + ROI_TYPES + ("area", "text")


class MeasureSettings:
    """측정값 표시 설정 (모든 뷰포트 공통, 메인 창이 QSettings에서 읽어 넣음)"""
    font_pt = 10
    unit = "mm"          # "mm" | "cm" | "px"


def fmt_length(mm, px=None):
    unit = MeasureSettings.unit
    if unit == "cm":
        return f"{mm / 10:.2f} cm"
    if unit == "px" and px is not None:
        return f"{px:.1f} px"
    return f"{mm:.1f} mm"


def fmt_area(mm2, px2=None):
    unit = MeasureSettings.unit
    if unit == "cm":
        return f"{mm2 / 100:.2f} cm²"
    if unit == "px" and px2 is not None:
        return f"{px2:.0f} px²"
    return f"{mm2:.1f} mm²"


def _seg_dist(p, a, b):
    """점 p와 선분 ab 사이 화면 거리"""
    ax, ay, bx, by = a.x(), a.y(), b.x(), b.y()
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p.x() - ax) * dx + (p.y() - ay) * dy) / length2))
    return math.hypot(p.x() - (ax + t * dx), p.y() - (ay + t * dy))


class AnnotationEditMixin:
    HANDLE_PX = 7

    def _edit_init(self):
        self._selected_ids = set()
        self._edit = None          # 끌기 중: {"id", "handle", "start", "orig"}
        self._anchor = None        # (영상 키, 이미지 좌표): 마지막으로 찍은 측정 점

    # ─── 공개 도움 (ROI Manager에서 사용) ───
    def current_array(self):
        return self._current_array()

    def pixel_spacing(self):
        return self._spacing()

    # ─── 선택 ───
    def selected_ids(self):
        here = {a.get("id") for a in self.annotations_here()}
        return [i for i in self._selected_ids if i in here]

    def select_ids(self, ids, emit=True):
        ids = set(ids)
        if ids != self._selected_ids:
            self._selected_ids = ids
            if emit:
                self.selection_changed.emit(sorted(ids))
            self.update()

    def selected_annotations(self):
        ids = set(self.selected_ids())
        return [a for a in self.annotations_here() if a.get("id") in ids]

    # ─── 값 계산 ───
    def _spacing_or_unit(self):
        return self._spacing() or (1.0, 1.0)

    def recompute_annotation(self, ann):
        """점이 바뀐 주석의 측정값을 현재 영상 기준으로 다시 계산 (복사본 반환)"""
        tmp = dict(ann)
        tmp["pts"] = [tuple(p) for p in ann["pts"]]
        label, factor = dicom_info.value_label(self.current_dataset())
        roi_tools.recompute(tmp, self._current_array(), self._spacing_or_unit(), factor, label)
        return tmp

    def _measured_fields(self, tmp):
        return {k: tmp[k] for k in ("pts", "mm", "deg", "area", "perimeter", "stats", "label")
                if k in tmp}

    # ─── 핸들 / 맞추기 ───
    @staticmethod
    def _handles(ann):
        kind, pts = ann["type"], ann["pts"]
        if kind in LINE_TYPES:
            return list(pts)
        if kind in ("ellipse", "rect"):
            (x0, y0), (x1, y1) = pts[:2]
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return []

    def _body_hit(self, ann, pos):
        kind = ann["type"]
        pts = [self._image_to_screen_f(p) for p in ann["pts"]]
        if not pts:
            return False
        tol = 6.0
        if kind in ("distance", "arrow", "path", "angle"):
            return any(_seg_dist(pos, a, b) <= tol for a, b in zip(pts, pts[1:]))
        if kind == "cobb":
            return any(_seg_dist(pos, pts[i], pts[i + 1]) <= tol for i in (0, 2)
                       if i + 1 < len(pts))
        if kind == "text":
            return math.hypot(pos.x() - pts[0].x(), pos.y() - pts[0].y()) <= 24
        if kind == "rect":
            return QRectF(pts[0], pts[1]).normalized().adjusted(-tol, -tol, tol, tol).contains(pos)
        if kind == "ellipse":
            rect = QRectF(pts[0], pts[1]).normalized().adjusted(-tol, -tol, tol, tol)
            if rect.width() <= 0 or rect.height() <= 0:
                return False
            c = rect.center()
            return (((pos.x() - c.x()) / (rect.width() / 2)) ** 2
                    + ((pos.y() - c.y()) / (rect.height() / 2)) ** 2) <= 1.0
        if kind in ("roi", "area"):
            poly = QPolygonF(pts)
            return (poly.containsPoint(pos, Qt.OddEvenFill)
                    or any(_seg_dist(pos, a, b) <= tol for a, b in zip(pts, pts[1:] + pts[:1])))
        return False

    def hit_annotation(self, pos, handles_only=False):
        """화면 좌표 → (주석, 핸들 번호 또는 None). 위에 그린 것부터"""
        for ann in reversed(self.annotations_here()):
            if not ann.get("visible", True) or ann["type"] not in EDITABLE_TYPES:
                continue
            if not ann.get("locked"):
                for i, h in enumerate(self._handles(ann)):
                    p = self._image_to_screen_f(h)
                    if math.hypot(pos.x() - p.x(), pos.y() - p.y()) <= self.HANDLE_PX:
                        return ann, i
            if not handles_only and self._body_hit(ann, pos):
                return ann, None
        return None, None

    def snap_point(self, anchor, point):
        """Shift: anchor→point 방향을 0/45/90° 로 맞춤 (mm 공간 기준, 길이 유지)"""
        sp = self._spacing_or_unit()
        dx, dy = (point[0] - anchor[0]) * sp[1], (point[1] - anchor[1]) * sp[0]
        length = math.hypot(dx, dy)
        if length == 0:
            return point
        angle = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
        return (anchor[0] + length * math.cos(angle) / sp[1],
                anchor[1] + length * math.sin(angle) / sp[0])

    # ─── 누르기 / 끌기 / 놓기 ───
    def edit_press(self, event, img_pos, select_mode):
        """주석 핸들·몸통을 잡으면 True (그리기 대신 편집). select_mode: Select 도구"""
        if img_pos is None or self._draft is not None:
            return False
        ann, handle = self.hit_annotation(event.pos(), handles_only=not select_mode)
        if ann is None:
            if select_mode and not event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier
                                                        | Qt.MetaModifier):
                self.select_ids(set())
            return False
        if select_mode:
            ids = set(self._selected_ids)
            if event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier | Qt.MetaModifier):
                ids ^= {ann["id"]}
            elif ann["id"] not in ids:
                ids = {ann["id"]}
            self.select_ids(ids)
        if ann.get("locked"):
            if select_mode:
                self.status_message.emit(f"'{ann.get('name') or roi_tools.type_name(ann)}'은(는) "
                                         "잠겨 있습니다 (ROI Manager에서 잠금 해제)")
            return select_mode
        self._edit = {"id": ann["id"], "handle": handle, "start": img_pos,
                      "orig": [tuple(p) for p in ann["pts"]], "moved": False}
        return True

    def edit_move(self, img_pos, shift=False):
        edit = self._edit
        if edit is None or img_pos is None:
            return
        _key, ann = self._store.find(edit["id"])
        if ann is None:
            self._edit = None
            return
        orig, handle = edit["orig"], edit["handle"]
        dx, dy = img_pos[0] - edit["start"][0], img_pos[1] - edit["start"][1]
        if abs(dx) + abs(dy) > 0:
            edit["moved"] = True
        kind = ann["type"]
        if handle is None:
            ann["pts"] = [(x + dx, y + dy) for x, y in orig]
        elif kind in ("ellipse", "rect"):
            (x0, y0), (x1, y1) = orig[:2]
            px, py = img_pos
            if handle == 0:
                x0, y0 = px, py
            elif handle == 1:
                x1, y0 = px, py
            elif handle == 2:
                x1, y1 = px, py
            else:
                x0, y1 = px, py
            ann["pts"] = [(x0, y0), (x1, y1)]
        else:
            pts = list(orig)
            point = img_pos
            if shift and kind in ("distance", "arrow") and len(pts) == 2:
                point = self.snap_point(pts[1 - handle], img_pos)
            pts[handle] = point
            ann["pts"] = pts
        self.update()

    def edit_release(self):
        edit, self._edit = self._edit, None
        if edit is None:
            return
        _key, ann = self._store.find(edit["id"])
        if ann is None:
            return
        new_pts = [tuple(p) for p in ann["pts"]]
        ann["pts"] = edit["orig"]    # 되돌리기 기록을 위해 원래 값으로 돌린 뒤 한 번에 변경
        if not edit["moved"] or new_pts == edit["orig"]:
            self.update()
            return
        tmp = self.recompute_annotation(dict(ann, pts=new_pts))
        self._store.update(edit["id"], **self._measured_fields(tmp))
        lines = self._annotation_text(tmp)
        if lines:
            self.status_message.emit(" | ".join(lines))

    def delete_selected(self):
        """선택한(잠기지 않은) 주석 삭제. 선택이 없으면 False"""
        ids = self.selected_ids()
        if not ids:
            return False
        key = self._image_key()
        with self._store.group():
            for ann in list(self.annotations_here()):
                if ann.get("id") in ids and not ann.get("locked"):
                    self._store.remove(key, ann["id"])
        self.select_ids(set())
        return True

    # ─── 그리기 도움 ───
    def label_font(self):
        font = QFont()
        font.setPointSize(int(MeasureSettings.font_pt))
        font.setBold(True)
        return font

    def draw_selection(self, painter, ann, color):
        if ann.get("id") not in self._selected_ids:
            return
        pen = QPen(QColor(255, 255, 255), 1, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        pts = [self._image_to_screen_f(p) for p in ann["pts"]]
        if pts:
            xs, ys = [p.x() for p in pts], [p.y() for p in pts]
            painter.drawRect(QRectF(min(xs) - 4, min(ys) - 4,
                                    max(xs) - min(xs) + 8, max(ys) - min(ys) + 8))
        if ann.get("locked"):
            return
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.setBrush(color)
        for h in self._handles(ann):
            p = self._image_to_screen_f(h)
            painter.drawRect(QRectF(p.x() - 3.5, p.y() - 3.5, 7, 7))
        painter.setBrush(Qt.NoBrush)

    def draw_live_readout(self, painter):
        """측정 도구 사용 중 커서 옆 실시간 거리"""
        pos = self._hover_pos
        if pos is None or self._current_tool not in (self.TOOL_MEASURE, self.TOOL_PATH,
                                                      self.TOOL_ANGLE):
            return
        cursor = self._screen_to_image(pos)
        if cursor is None:
            return
        sp = self._spacing_or_unit()
        draft = self._draft
        text = None
        if draft and draft["type"] == "distance":
            a, b = draft["pts"][:2]
            text = fmt_length(roi_tools.path_length_mm([a, b], sp),
                              roi_tools.path_length_mm([a, b], (1.0, 1.0)))
        elif draft and draft["type"] == "path":
            text = "Σ " + fmt_length(roi_tools.path_length_mm(draft["pts"], sp),
                                     roi_tools.path_length_mm(draft["pts"], (1.0, 1.0)))
        elif draft and draft["type"] == "angle" and len(draft["pts"]) == 3:
            value = roi_tools.angle_deg(*draft["pts"], sp)
            text = f"{value:.1f}°" if value is not None else None
        elif draft is None and self._anchor and self._anchor[0] == self._image_key():
            a = self._anchor[1]
            text = "↔ " + fmt_length(roi_tools.path_length_mm([a, cursor], sp),
                                     roi_tools.path_length_mm([a, cursor], (1.0, 1.0)))
        if not text:
            return
        font = self.label_font()
        font.setPointSize(max(8, int(MeasureSettings.font_pt) - 1))
        painter.setFont(font)
        fm = painter.fontMetrics()
        rect = QRectF(pos.x() + 14, pos.y() + 12, fm.horizontalAdvance(text) + 8, fm.height() + 4)
        path = QPainterPath()
        path.addRoundedRect(rect, 3, 3)
        painter.fillPath(path, QColor(0, 0, 0, 170))
        painter.setPen(QColor(255, 255, 120))
        painter.drawText(QPointF(rect.left() + 4, rect.top() + 2 + fm.ascent()), text)
