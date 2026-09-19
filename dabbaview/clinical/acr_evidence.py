# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
ACR 증빙 영상 44장 - 콘솔에서 수동으로 저장하는 화면 캡처 세트와 1:1

수동 세트(콘솔 Screen Save 44장)와 같은 순서·같은 내용: 검사마다 W/L 조절 전·후, ROI 배치와 평균,
측정선과 길이. 각 장에 번호·검사 항목·W/L, 아래 띠에 검사일·병원·호기·장비·검사자·생성 시각 표시.
44장 뒤에 분해능 확대 2장(추가)을 붙인다. 없는 시퀀스(사이트 시퀀스 등)는 '해당 없음' 장으로 자리만 채운다.
"""
import datetime
import math
import os

import numpy as np

from . import acr

W_IMG = 620          # 증빙 한 장의 영상 폭 (px)
TITLE_H, FOOT_H = 80, 40
SITE_NAMES = ("사이트 시퀀스 1", "사이트 시퀀스 2")

# (번호, 검사, 내용) - 콘솔 수동 캡처 순서와 같음
SLOTS = [
    (1, "Localizer", "T1 처방 확인"),
    (2, "Localizer", "T2 처방 확인"),
    (3, "기하 정확도", "Localizer 상하 길이"),
    (4, "기하 정확도", "T1 slice 5 수직 · 수평 직경"),
    (5, "기하 정확도", "T1 slice 5 대각선 직경"),
    (6, "절편 두께", "T1 slice 1 원본 (기본 W/L)"),
    (7, "절편 두께", "T1 slice 1 경사판 보기 (W/L 좁힘)"),
    (8, "절편 두께", "T2 slice 1 원본 (기본 W/L)"),
    (9, "절편 두께", "T2 slice 1 경사판 보기 (W/L 좁힘)"),
    (10, "절편 두께 (사이트)", f"{SITE_NAMES[0]} slice 1 경사판 ROI · 평균"),
    (11, "절편 두께 (사이트)", f"{SITE_NAMES[0]} slice 1 기준 레벨 W 1 · 경사판 길이"),
    (12, "절편 두께 (사이트)", f"{SITE_NAMES[1]} slice 1 경사판 ROI · 평균"),
    (13, "절편 두께 (사이트)", f"{SITE_NAMES[1]} slice 1 기준 레벨 W 1 · 경사판 길이"),
    (14, "절편 두께", "T1 경사판 ROI · 평균"),
    (15, "절편 두께", "T1 기준 레벨 W 1 · 경사판 길이"),
    (16, "절편 두께", "T2 경사판 ROI · 평균"),
    (17, "절편 두께", "T2 기준 레벨 W 1 · 경사판 길이"),
    (18, "절편 위치", "T1 slice 11 쐐기 길이"),
    (19, "절편 위치", "T2 slice 1 쐐기 길이"),
    (20, "절편 위치", "T2 slice 11 쐐기 길이"),
    (21, "영상 균일도", "T1 slice 7 큰 ROI 200 cm² (기본 W/L)"),
    (22, "영상 균일도", "T1 W 1 · 가장 어두운 곳 1 cm² ROI (Low)"),
    (23, "영상 균일도", "T1 W 1 · 가장 밝은 곳 1 cm² ROI (High)"),
    (24, "영상 균일도", "T2 W 1 · 가장 어두운 곳 1 cm² ROI (Low)"),
    (25, "영상 균일도", "T2 W 1 · 가장 밝은 곳 1 cm² ROI (High)"),
    (26, "고스팅", "T1 큰 ROI · 위 · 아래 배경 ROI"),
    (27, "고스팅", "T1 큰 ROI · 왼쪽 · 오른쪽 배경 ROI"),
] + [(28 + i, "저대조 검출", f"T1 slice {8 + i}") for i in range(4)] \
  + [(32 + i, "저대조 검출", f"T2 slice {8 + i}") for i in range(4)] \
  + [(36 + i, "저대조 검출 (사이트)", f"{SITE_NAMES[0]} slice {8 + i}") for i in range(4)] \
  + [(40 + i, "저대조 검출 (사이트)", f"{SITE_NAMES[1]} slice {8 + i}") for i in range(4)] \
  + [(44, "절편 위치", "T1 slice 1 쐐기 길이")]
EXTRA_SLOTS = [("A1", "고대조 분해능", "T1 slice 1 구멍 배열 확대"),
               ("A2", "고대조 분해능", "T2 slice 1 구멍 배열 확대")]


def _font(size):
    from PIL import ImageFont
    from ..library_export import korean_font_path
    path = korean_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _gray(a, window):
    a = np.asarray(a, dtype=float)
    if window is None:
        lo, hi = np.percentile(a, [1, 99.5])
    else:
        level, win = window
        if win <= 1:   # 콘솔 W 1: L보다 크면 흰색
            return np.where(a > level, 255, 0).astype(np.uint8)
        lo, hi = level - win / 2, level + win / 2
    return np.clip((a - lo) / max(1e-6, hi - lo) * 255, 0, 255).astype(np.uint8)


def render(a, anns=(), markers=(), window=None, crop=None, labels=(), width=W_IMG):
    """영상 + 주석 + 표시 + 글 → PIL 영상"""
    from PIL import Image, ImageDraw
    g = _gray(a, window)
    x0, y0, x1, y1 = crop or (0, 0, g.shape[1], g.shape[0])
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(g.shape[1], int(math.ceil(x1))), min(g.shape[0], int(math.ceil(y1)))
    s = width / (x1 - x0)
    im = Image.fromarray(g[y0:y1, x0:x1]).convert("RGB").resize(
        (width, max(1, int(round((y1 - y0) * s)))), Image.LANCZOS if s < 1 else Image.NEAREST)
    d = ImageDraw.Draw(im)

    def P(x, y):
        return ((x - x0) * s, (y - y0) * s)
    lw = max(2, int(round(s)))
    for ann in anns:
        color = ann.get("acr_color") or ann.get("color") or "#ffff00"   # 판정 색이 아닌 원래 색
        pts = [P(x - 0.5, y - 0.5) for x, y in ann["pts"]]
        if ann["type"] == "distance":
            d.line(pts[:2], fill=color, width=lw)
            for q in pts[:2]:   # 끝점 표시
                d.ellipse([q[0] - lw - 1, q[1] - lw - 1, q[0] + lw + 1, q[1] + lw + 1], outline=color, width=1)
        elif ann["type"] in ("ellipse", "rect"):
            (ax, ay), (bx, by) = pts[:2]
            box = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
            (d.ellipse if ann["type"] == "ellipse" else d.rectangle)(box, outline=color, width=lw)
    for m in markers:
        if m[0] == "disk":
            _k, x, y, r, color = m
            (cx, cy), rr = P(x + 0.5, y + 0.5), r * s
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=color, width=max(1, lw - 1))
        elif m[0] == "box":
            _k, bx0, by0, bx1, by1, color, _label = m
            (ax, ay), (bx, by) = P(bx0, by0), P(bx1, by1)
            d.rectangle([ax, ay, bx, by], outline=color, width=max(1, lw - 1))
    font = _font(17)
    for x, y, text, color in labels:
        px, py = P(x, y)
        tw = d.textlength(text, font=font)
        px = min(max(3, px - tw / 2), im.width - tw - 3)
        py = min(max(3, py), im.height - 24)
        d.rectangle([px - 4, py - 2, px + tw + 4, py + 22], fill=(0, 0, 0))
        d.text((px, py), text, fill=color, font=font)
    return im


def frame(im, number, test, desc, note, watermark):
    """위: 증빙 번호 · 검사 · 내용 · W/L, 아래: 워터마크 띠 (검사일 · 장비 · 검사자 · 생성 시각)"""
    from PIL import Image, ImageDraw
    w = im.width
    out = Image.new("RGB", (w, TITLE_H + im.height + FOOT_H), (255, 255, 255))
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, w, TITLE_H], fill=(31, 58, 95))
    head = f"증빙 {number}" if isinstance(number, str) else f"증빙 {number:02d}/44"
    d.text((10, 5), f"{head} · {test}", font=_font(20), fill=(255, 255, 255))
    d.text((10, 32), desc, font=_font(15), fill=(210, 225, 245))
    small = _font(14)
    while note and d.textlength(note, font=small) > w - 20:   # 넘치면 글자를 줄이지 않고 뒤를 자름
        note = note[:-2]
    d.text((10, 55), note or "", font=small, fill=(255, 214, 120))
    out.paste(im, (0, TITLE_H))
    y = TITLE_H + im.height
    d.rectangle([0, y, w, y + FOOT_H], fill=(236, 239, 244))
    for i, line in enumerate(watermark):
        d.text((10, y + 3 + 18 * i), line, font=_font(13), fill=(70, 80, 95))
    # 영상 위 옅은 표식 (잘라 붙여도 출처가 남게)
    mark = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((w - 238, TITLE_H + im.height - 24), "DabbaView ACR 자동 분석",
                              font=_font(14), fill=(255, 255, 255, 110))
    return Image.alpha_composite(out.convert("RGBA"), mark).convert("RGB")


def placeholder(reason):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (W_IMG, W_IMG), (40, 40, 44))
    d = ImageDraw.Draw(im)
    d.text((30, W_IMG / 2 - 30), "해당 없음", font=_font(30), fill=(220, 220, 220))
    d.text((30, W_IMG / 2 + 14), reason[:44], font=_font(16), fill=(180, 180, 180))
    return im


def _centre(ann):
    (x0, y0), (x1, y1) = ann["pts"][:2]
    return (x0 + x1) / 2, (y0 + y1) / 2


class Evidence:
    """ACRTool 분석 결과 → 증빙 영상 44장 (+ 추가 2장)"""

    def __init__(self, tool, info):
        self.tool = tool
        self.info = info
        self.roles = tool._roles_now()
        self.values = tool.values
        self.sets = tool.sets or {}
        self.sites = list(getattr(tool, "site_sets", []) or [])
        from ..annotations import image_key
        self._key = image_key
        store = tool.ctx.main._annotation_store
        self._store = store
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        place = " ".join(x for x in (info.get("hospital", ""), info.get("unit", ""), info.get("scanner", ""),
                                     info.get("field", "")) if x)
        self.watermark = [f"검사일 {info.get('date', '-')}  ·  {place or '장비 정보 없음'}  ·  "
                          f"검사자 {info.get('tester', '') or '-'}",
                          f"DabbaView ACR Phantom QC 자동 분석  ·  생성 {now}"]

    # ─── 공통 ───
    def img(self, ref):
        return self.tool.img(*ref)

    def role(self, name):
        return self.roles.get(name)

    def anns_of(self, ref):
        return list(self._store.items(self._key(*ref)))

    def ref(self, seq, n):
        refs = self.sets.get(seq)
        return refs[n] if refs and n < len(refs) else None

    # ─── 장별 ───
    def localizer(self, seq):
        loc = self.sets.get("LOC")
        if loc is None:
            return placeholder("localizer를 찾지 못함"), ""
        img = self.img(loc)
        lines = self._prescription(loc, self.sets.get(seq) or [])
        note = f"{seq} 11장 위치" if lines else "처방선 정보 없음 (화면 캡처는 위치 태그가 없음)"
        return render(img.a, lines), note

    def _prescription(self, loc, refs):
        """DICOM 위치 정보로 축상 슬라이스가 localizer와 만나는 선"""
        try:
            lds = loc[0].slices[loc[1]]
            o = np.array(lds.ImagePositionPatient, float)
            iop = np.array(lds.ImageOrientationPatient, float)
            ps = [float(v) for v in lds.PixelSpacing]
        except (AttributeError, TypeError, ValueError, IndexError):
            return []
        row, col = iop[:3], iop[3:]
        n_loc = np.cross(row, col)
        out = []
        for i, (s, k) in enumerate(refs):
            try:
                ds = s.slices[k]
                p = np.array(ds.ImagePositionPatient, float)
                aiop = np.array(ds.ImageOrientationPatient, float)
                aps = [float(v) for v in ds.PixelSpacing]
            except (AttributeError, TypeError, ValueError):
                return []
            n_ax = np.cross(aiop[:3], aiop[3:])
            c = p + aiop[:3] * aps[1] * int(ds.Columns) / 2 + aiop[3:] * aps[0] * int(ds.Rows) / 2
            c = c - np.dot(c - o, n_loc) * n_loc
            direction = np.cross(n_ax, n_loc)
            if np.linalg.norm(direction) < 1e-6:
                return []
            direction /= np.linalg.norm(direction)
            pts = []
            for t in (-110.0, 110.0):
                q = c + direction * t - o
                pts.append((float(np.dot(q, row) / ps[1]) + 0.5, float(np.dot(q, col) / ps[0]) + 0.5))
            out.append(acr.ann_line(f"S{i + 1}", *pts, "prescription",
                                    "#ffd200" if i in (0, 10) else "#7fd4ff"))
            out[-1]["pts"] = pts
        return out

    def geometry(self, names):
        items = [self.role(f"T1|ACR Geo {n}") for n in names]
        if not all(items):
            return placeholder("기하 측정선이 없음"), ""
        img = items[0][0]
        anns = [a for _i, a in items]
        labels = []
        for n, a in zip(names, anns):
            (x0, y0), (x1, y1) = a["pts"][:2]
            mm = acr.line_mm(img, a)
            t = 0.28 if n.endswith(("V", "D2")) else 0.62
            labels.append((x0 + (x1 - x0) * t + (14 if n.endswith("V") else 0),
                           y0 + (y1 - y0) * t - (26 if n.endswith("H") else 0), f"{n} {mm:.1f} mm", "#ffff00"))
        return render(img.a, anns, labels=labels), "기준 " + ("148" if names == ["LOC"] else "190") + " ± 2 mm"

    def thickness_raw(self, seq, narrow):
        ref = self.ref(seq, 0)
        v = self.values.get(("thickness", seq))
        if ref is None:
            return placeholder(f"{seq} 영상 없음"), ""
        img = self.img(ref)
        if not narrow or not v or "roi_top" not in v:
            return render(img.a), "기본 W/L"
        mean2 = (v["roi_top"] + v["roi_bottom"]) / 2
        win = (0.73 * mean2, 0.44 * mean2)   # 콘솔: 경사판이 보이게 좁힘 (W 188 / L 315 ≈ 평균 430)
        return render(img.a, window=win), f"W {win[1]:.0f} / L {win[0]:.0f}"

    def _thickness_views(self, img, rt, rb, lt, lb, m_t, m_b):
        """(ROI · 평균 장, W 1 · 길이 장) - 막대 부분 확대"""
        fit = img.fit()
        ys = [p[1] for a in (rt, rb, lt, lb) for p in a["pts"]]
        crop = (fit["cx"] - 0.85 * fit["r"], min(ys) - 0.42 * fit["r"],
                fit["cx"] + 0.85 * fit["r"], max(ys) + 0.42 * fit["r"])
        above, below = min(ys) - 0.3 * fit["r"], max(ys) + 0.12 * fit["r"]
        mean2 = (m_t + m_b) / 2
        win = (0.73 * mean2, 0.44 * mean2)
        level = (m_t + m_b) / 4
        (tx, _ty), (bx, _by) = _centre(rt), _centre(rb)
        a1 = render(img.a, [rt, rb], window=win, crop=crop,
                    labels=[(tx, above, f"위 ROI {acr.roi_area_mm2(img, rt):.1f} mm² 평균 {m_t:.2f}", "#ff5ad2"),
                            (bx, below, f"아래 ROI {acr.roi_area_mm2(img, rb):.1f} mm² 평균 {m_b:.2f}", "#ff5ad2")])
        t, b = acr.line_mm(img, lt), acr.line_mm(img, lb)
        a2 = render(img.a, [lt, lb], window=(level, 1), crop=crop,
                    labels=[(_centre(lt)[0], above, f"위 {t:.1f} mm", "#00e5ff"),
                            (_centre(lb)[0], below, f"아래 {b:.1f} mm", "#00e5ff")])
        thk = 0.2 * t * b / (t + b) if t + b else float("nan")
        return (a1, f"W {win[1]:.0f} / L {win[0]:.0f} · 확대"), \
               (a2, f"W 1 / L {level:.2f} = ({m_t:.2f} + {m_b:.2f}) / 4 → 두께 {thk:.2f} mm")

    def thickness(self, seq, which):
        names = [f"{seq}|ACR ST {n}" for n in ("ROI top", "ROI bottom", "top", "bottom")]
        items = [self.role(n) for n in names]
        if not all(items):
            return placeholder(f"{seq} 두께 측정 없음"), ""
        img = items[0][0]
        rt, rb, lt, lb = (a for _i, a in items)
        views = self._thickness_views(img, rt, rb, lt, lb, acr.roi_mean(img, rt), acr.roi_mean(img, rb))
        return views[which]

    def site_thickness(self, site, which):
        if site >= len(self.sites):
            return placeholder("사이트 시퀀스를 찾지 못함"), ""
        img = self.img(self.sites[site][0])
        try:
            anns = acr.place_thickness(img)
        except ValueError as e:
            return placeholder(str(e)), ""
        by = {a["name"]: a for a in anns}
        rt, rb = by["ACR ST ROI top"], by["ACR ST ROI bottom"]
        views = self._thickness_views(img, rt, rb, by["ACR ST top"], by["ACR ST bottom"],
                                      acr.roi_mean(img, rt), acr.roi_mean(img, rb))
        return views[which][0], views[which][1] + " (참고)"

    def position(self, seq, s):
        l_, r_ = self.role(f"{seq}|ACR SP {s} L"), self.role(f"{seq}|ACR SP {s} R")
        if not (l_ and r_):
            return placeholder(f"{seq} {s} 쐐기 측정 없음"), ""
        img = l_[0]
        ll, rl = acr.line_mm(img, l_[1]), acr.line_mm(img, r_[1])
        fit = img.fit()
        ys = [p[1] for a in (l_[1], r_[1]) for p in a["pts"]]
        crop = (fit["cx"] - 0.45 * fit["r"], fit["cy"] - 1.08 * fit["r"],
                fit["cx"] + 0.45 * fit["r"], max(ys) + 0.35 * fit["r"])
        (lx, _), (rx, _) = _centre(l_[1]), _centre(r_[1])
        return (render(img.a, [l_[1], r_[1]], crop=crop,
                       labels=[(lx - 0.22 * fit["r"], max(ys) + 0.08 * fit["r"], f"왼쪽 {ll:.1f} mm", "#ff9f40"),
                               (rx + 0.22 * fit["r"], max(ys) + 0.18 * fit["r"], f"오른쪽 {rl:.1f} mm", "#ff9f40")]),
                f"차이 = 오른쪽 - 왼쪽 = {rl - ll:+.1f} mm (기준 |차이| ≤ 5 mm) · 확대")

    def piu(self, seq, which):
        items = [self.role(f"{seq}|ACR PIU {n}") for n in ("large", "min", "max")]
        v = self.values.get(("uniformity", seq))
        if not all(items) or not v:
            return placeholder(f"{seq} 균일도 측정 없음"), ""
        img = items[0][0]
        big, lo, hi = (a for _i, a in items)
        m_big, m_lo, m_hi = v["large"], v["min"], v["max"]
        fit = img.fit()
        if which == "large":
            return (render(img.a, [big], labels=[(*_centre(big), f"큰 ROI {acr.roi_area_mm2(img, big) / 100:.0f} cm² "
                                                                  f"평균 {m_big:.2f}", "#50ff78")]),
                    "기본 W/L")
        if which == "low":
            level = m_lo + 0.3 * (m_big - m_lo)
            (x, y) = _centre(lo)
            return (render(img.a, [big, lo], window=(level, 1),
                           labels=[(x, y + 0.09 * fit["r"], f"Low {m_lo:.2f} (1 cm²)", "#5aa0ff")]),
                    f"W 1 / L {level:.2f} - 가장 어두운 곳만 검게 남김")
        level = m_hi - 0.3 * (m_hi - m_big)
        (x, y) = _centre(hi)
        piu = v["piu"]
        return (render(img.a, [big, hi], window=(level, 1),
                       labels=[(x, y + 0.09 * fit["r"], f"High {m_hi:.2f} (1 cm²)", "#ff5050")]),
                f"W 1 / L {level:.2f} → PIU = 100×(1-(H-L)/(H+L)) = {piu:.1f} %")

    def ghost(self, pair):
        seq = "T1"
        v = self.values.get(("ghosting", seq))
        big = self.role(f"{seq}|ACR Ghost large") or self.role(f"{seq}|ACR PIU large")
        sides = [self.role(f"{seq}|ACR Ghost {s}") for s in pair]
        if not v or not big or not all(sides):
            return placeholder("고스팅 측정 없음"), ""
        img = big[0]
        win = (0.025 * v["large"], 0.05 * v["large"])
        kor = {"top": "위", "bottom": "아래", "left": "왼쪽", "right": "오른쪽"}
        labels = [(*_centre(big[1]), f"큰 ROI 평균 {v['large']:.2f}", "#50ff78")]
        for s, (_i, a) in zip(pair, sides):
            x, y = _centre(a)
            labels.append((x, y + (-40 if s == "bottom" else 26 if s == "top" else 0),
                           f"{kor[s]} {v[s]:.2f}", "#ffd200"))
        return (render(img.a, [big[1]] + [a for _i, a in sides], window=win, labels=labels),
                f"W {win[1]:.1f} / L {win[0]:.2f} · 고스팅 {v['ratio']:.2f} %")

    def low_contrast(self, seq, i):
        ref = self.ref(seq, 7 + i)
        if ref is None:
            return placeholder(f"{seq} 영상 없음"), ""
        img = self.img(ref)
        marks = self.tool.markers.get(self._key(*ref), [])
        det = self.tool.lc_detail.get(seq)
        spokes = det[i]["spokes"] if det else None
        return self._lc_view(img, marks, spokes)

    def _lc_view(self, img, marks, spokes):
        cx, cy, r = acr.lc_center(img)
        crop = (cx - 1.12 * r, cy - 1.12 * r, cx + 1.12 * r, cy + 1.12 * r)
        yy, xx = np.indices(img.shape)
        inside = np.hypot(xx - cx, yy - cy) < 0.9 * r
        lo, hi = np.percentile(img.a[inside], [2, 99.5])
        win = ((lo + hi) / 2, max(1.0, hi - lo))
        return (render(img.a, markers=marks, window=win, crop=crop),
                f"W {win[1]:.0f} / L {win[0]:.0f} · 보이는 spoke {spokes if spokes is not None else '-'}개 "
                "(초록 = 보임, 빨강 = 안 보임)")

    def site_lc(self, site, i):
        if site >= len(self.sites):
            return placeholder("사이트 시퀀스를 찾지 못함"), ""
        if not hasattr(self, "_site_lc"):
            self._site_lc = {}
        if site not in self._site_lc:
            refs = self.sites[site][7:11]
            try:
                self._site_lc[site] = acr.low_contrast_set([self.img(r) for r in refs])
            except (ValueError, IndexError) as e:
                self._site_lc[site] = str(e)
        out = self._site_lc[site]
        if isinstance(out, str):
            return placeholder(out), ""
        img = self.img(self.sites[site][7 + i])
        marks = [("disk", d["x"], d["y"], d["r"], "#2e9e4f" if d["visible"] else "#d64545") for d in out[i]["disks"]]
        im, note = self._lc_view(img, marks, out[i]["spokes"])
        return im, note.replace("(초록 = 보임, 빨강 = 안 보임)", "· 참고 (육안 확인)")

    def resolution(self, seq):
        ref = self.ref(seq, 0)
        det = self.tool.res_detail.get(seq)
        if ref is None or not det:
            return placeholder(f"{seq} 분해능 결과 없음"), ""
        img = self.img(ref)
        marks = self.tool.markers.get(self._key(*ref), [])
        bx0, by0, bx1, by1 = det["box"]
        pad = 0.25 * (bx1 - bx0)
        crop = (bx0 - pad, by0 - pad, bx1 + pad, by1 + pad)
        ul, lr = det.get("ul"), det.get("lr")
        fmt = lambda x: f"{x:.1f} mm" if x else "-"   # noqa: E731
        return (render(img.a, markers=marks, crop=crop),
                f"UL {fmt(ul)} / LR {fmt(lr)} (초록 = 분해됨) · 확대")

    # ─── 전체 ───
    def builders(self):
        b = {1: lambda: self.localizer("T1"), 2: lambda: self.localizer("T2"),
             3: lambda: self.geometry(["LOC"]), 4: lambda: self.geometry(["S5 V", "S5 H"]),
             5: lambda: self.geometry(["S5 D1", "S5 D2"]),
             6: lambda: self.thickness_raw("T1", False), 7: lambda: self.thickness_raw("T1", True),
             8: lambda: self.thickness_raw("T2", False), 9: lambda: self.thickness_raw("T2", True),
             10: lambda: self.site_thickness(0, 0), 11: lambda: self.site_thickness(0, 1),
             12: lambda: self.site_thickness(1, 0), 13: lambda: self.site_thickness(1, 1),
             14: lambda: self.thickness("T1", 0), 15: lambda: self.thickness("T1", 1),
             16: lambda: self.thickness("T2", 0), 17: lambda: self.thickness("T2", 1),
             18: lambda: self.position("T1", "S11"), 19: lambda: self.position("T2", "S1"),
             20: lambda: self.position("T2", "S11"),
             21: lambda: self.piu("T1", "large"), 22: lambda: self.piu("T1", "low"),
             23: lambda: self.piu("T1", "high"), 24: lambda: self.piu("T2", "low"),
             25: lambda: self.piu("T2", "high"),
             26: lambda: self.ghost(("top", "bottom")), 27: lambda: self.ghost(("left", "right")),
             44: lambda: self.position("T1", "S1"),
             "A1": lambda: self.resolution("T1"), "A2": lambda: self.resolution("T2")}
        for i in range(4):
            b[28 + i] = (lambda i=i: self.low_contrast("T1", i))
            b[32 + i] = (lambda i=i: self.low_contrast("T2", i))
            b[36 + i] = (lambda i=i: self.site_lc(0, i))
            b[40 + i] = (lambda i=i: self.site_lc(1, i))
        return b

    def build(self, folder, progress=None):
        """→ [(번호, 검사, 내용, 비고, PNG 경로)] 44장 + 추가 2장"""
        os.makedirs(folder, exist_ok=True)
        b = self.builders()
        out = []
        slots = SLOTS + EXTRA_SLOTS
        for n, (number, test, desc) in enumerate(slots):
            try:
                im, note = b[number]()
            except Exception as e:  # noqa: BLE001 - 한 장이 실패해도 나머지는 만듦
                im, note = placeholder(f"만들지 못함: {e}"), ""
            name = f"E{number:02d}" if isinstance(number, int) else f"E{number}"   # 추가 장은 44장 뒤로 정렬
            path = os.path.join(folder, f"{name}.jpg")
            frame(im, number, test, desc, note, self.watermark).save(path, quality=88)
            out.append((number, test, desc, note, path))
            if progress:
                progress(n + 1, len(slots))
        return out
