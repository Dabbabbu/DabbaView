# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DabbaView 앱 아이콘 생성 스크립트
PIL로 아이콘 이미지를 만들고 macOS .icns로 변환
"""
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont


def create_icon():
    """DabbaView 아이콘 생성"""
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    os.makedirs("resources", exist_ok=True)

    # 1024x1024 기본 아이콘 생성
    size = 1024
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 배경: 둥근 사각형 (그라데이션 효과)
    margin = int(size * 0.05)
    radius = int(size * 0.2)
    # 진한 남색 배경
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=(20, 30, 60, 255)
    )

    # 안쪽 테두리
    inner_margin = int(size * 0.08)
    draw.rounded_rectangle(
        [inner_margin, inner_margin,
         size - inner_margin, size - inner_margin],
        radius=radius - 10,
        outline=(0, 122, 204, 200),
        width=4
    )

    # 중앙: 의료 영상 느낌의 원형
    cx, cy = size // 2, size // 2
    r = int(size * 0.28)

    # 바깥 원 (파란색 글로우)
    for i in range(20, 0, -1):
        alpha = int(255 * (1 - i / 20) * 0.3)
        draw.ellipse(
            [cx - r - i * 2, cy - r - i * 2,
             cx + r + i * 2, cy + r + i * 2],
            outline=(0, 122, 204, alpha),
            width=1
        )

    # 메인 원
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        outline=(0, 180, 255, 255),
        width=6
    )

    # 십자선 (크로스헤어)
    line_color = (0, 180, 255, 180)
    # 수평선
    draw.line(
        [cx - r + 20, cy, cx + r - 20, cy],
        fill=line_color, width=2
    )
    # 수직선
    draw.line(
        [cx, cy - r + 20, cx, cy + r - 20],
        fill=line_color, width=2
    )

    # 작은 중앙 점
    dot_r = 8
    draw.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=(255, 255, 255, 255)
    )

    # "RV" 텍스트 (하단) - macOS / Windows / Linux 순으로 폰트 탐색
    font = None
    for font_path in ("/System/Library/Fonts/Helvetica.ttc",
                      "/System/Library/Fonts/SFNSMono.ttf",
                      "arial.ttf",  # Windows (C:\Windows\Fonts에서 자동 탐색)
                      "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(font_path, 120)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default(size=120)

    text = "DV"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw // 2
    ty = cy + r + 30
    draw.text((tx, ty), text, fill=(255, 255, 255, 230), font=font)

    # PNG 저장
    img.save("resources/icon_1024.png")
    # 앱 시작 화면용 로고 (dabbaview 패키지에 포함되어 번들에 들어감)
    os.makedirs("dabbaview/resources", exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save("dabbaview/resources/logo.png")

    # .iconset 디렉토리 생성
    iconset_dir = "resources/DabbaView.iconset"
    os.makedirs(iconset_dir, exist_ok=True)

    icon_configs = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]

    for filename, s in icon_configs:
        resized = img.resize((s, s), Image.LANCZOS)
        resized.save(os.path.join(iconset_dir, filename))

    # Windows용 .ico (여러 해상도를 한 파일에)
    img.save("resources/dabbaview.ico",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])
    print("✅ resources/dabbaview.ico 생성 완료!")

    # iconutil로 .icns 생성 (macOS 전용)
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir,
             "-o", "resources/DabbaView.icns"],
            check=True
        )
        print("✅ resources/DabbaView.icns 생성 완료!")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️ iconutil을 사용할 수 없습니다 (macOS에서 실행 필요).")
        print("   PNG 아이콘은 resources/icon_1024.png에 저장됨.")

    print("✅ 아이콘 이미지 생성 완료!")


if __name__ == "__main__":
    create_icon()
