# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
GitHub 소셜 미리보기 이미지(1280 × 640) 만들기

사용: python create_social_preview.py
결과: docs/images/social_preview.png
      → GitHub 저장소 Settings ▸ General ▸ Social preview 에 올립니다.
"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 640
BG_TOP = (11, 22, 43)
BG_BOTTOM = (23, 42, 77)
ACCENT = (61, 139, 253)
WHITE = (255, 255, 255)
GREY = (168, 186, 214)

FONTS = "/System/Library/Fonts/Supplemental"
KOREAN = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


def font(name, size, korean=False):
    for path in ([KOREAN] if korean else []) + [os.path.join(FONTS, name)]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def background():
    """위에서 아래로 어두운 남색 그라데이션 + 옅은 격자"""
    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(
            int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    for x in range(0, W, 40):
        g.line([(x, 0), (x, H)], fill=(255, 255, 255, 8))
    for y in range(0, H, 40):
        g.line([(0, y), (W, y)], fill=(255, 255, 255, 8))
    return Image.alpha_composite(img.convert("RGBA"), grid).convert("RGB")


def rounded(image, radius, border=ACCENT, width=3):
    """모서리를 둥글게 자르고 테두리를 그림"""
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, image.size[0] - 1, image.size[1] - 1],
                                           radius=radius, fill=255)
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.paste(image.convert("RGB"), (0, 0), mask)
    ImageDraw.Draw(out).rounded_rectangle([0, 0, image.size[0] - 1, image.size[1] - 1],
                                          radius=radius, outline=border + (255,), width=width)
    return out


def shadow(size, radius, blur=18, alpha=150):
    layer = Image.new("RGBA", (size[0] + blur * 3, size[1] + blur * 3), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [blur * 1.5, blur * 1.5, blur * 1.5 + size[0], blur * 1.5 + size[1]],
        radius=radius, fill=(0, 0, 0, alpha))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def build(out_path="docs/images/social_preview.png"):
    here = os.path.dirname(os.path.abspath(__file__))
    img = background()
    draw = ImageDraw.Draw(img)

    # ─── 오른쪽: 앱 화면 ───
    shot_path = os.path.join(here, "docs/images/m08_multiview.jpg")
    if os.path.exists(shot_path):
        shot = Image.open(shot_path)
        target_w = 660
        shot = shot.resize((target_w, round(shot.height * target_w / shot.width)),
                           Image.LANCZOS)
        shot = shot.crop((0, 0, target_w, min(shot.height, 430)))
        pos = (W - target_w - 44, (H - shot.height) // 2 + 10)
        sh = shadow(shot.size, 14)
        img.paste(sh, (pos[0] - 27, pos[1] - 18), sh)
        card = rounded(shot, 14)
        img.paste(card, pos, card)

    # ─── 왼쪽: 로고 · 제목 · 소개 ───
    x = 60
    icon_path = os.path.join(here, "resources/icon_1024.png")
    y = 58
    if os.path.exists(icon_path):
        icon = Image.open(icon_path).convert("RGBA").resize((96, 96), Image.LANCZOS)
        img.paste(icon, (x, y), icon)
    draw.text((x + 116, y + 6), "DabbaView", font=font("Arial Bold.ttf", 60), fill=WHITE)
    draw.text((x + 120, y + 74), "DICOM VIEWER  ·  IMAGE ANALYSIS",
              font=font("Arial Bold.ttf", 17), fill=ACCENT)

    y += 140
    draw.text((x, y), "Free, open-source DICOM viewer and",
              font=font("Arial.ttf", 27), fill=WHITE)
    draw.text((x, y + 38), "medical image analysis platform",
              font=font("Arial.ttf", 27), fill=WHITE)
    draw.text((x, y + 84), "무료 오픈소스 DICOM 뷰어 · 의료영상 분석 플랫폼",
              font=font("", 21, korean=True), fill=GREY)

    y += 140
    features = ["Cardiac MRI (EF · T1/T2 map · Flow · LGE)",
                "ACR phantom QC  ·  AI segmentation",
                "MPR  ·  3D volume rendering  ·  PACS"]
    small = font("Arial.ttf", 20)
    for line in features:
        draw.ellipse([x + 2, y + 8, x + 11, y + 17], fill=ACCENT)
        draw.text((x + 24, y), line, font=small, fill=(219, 229, 245))
        y += 34

    # ─── 아래 띠 ───
    draw.rectangle([0, H - 52, W, H], fill=(8, 16, 32))
    draw.text((x, H - 36), "macOS  ·  Windows  ·  Web      GPL-3.0",
              font=font("Arial Bold.ttf", 18), fill=GREY)
    draw.text((W - 430, H - 36), "github.com/Dabbabbu/DabbaView",
              font=font("Arial Bold.ttf", 18), fill=ACCENT)

    out = os.path.join(here, out_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    img.save(out, "PNG", optimize=True)
    print(out, img.size, os.path.getsize(out), "bytes")
    return out


if __name__ == "__main__":
    build()
