# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
매크로 - 자주 쓰는 분석을 Python 스크립트로 저장해 원클릭 실행 (ImageJ 매크로 대신 Python)

앱 데이터 폴더/macros/*.py. 처음 실행하면 예제 매크로를 만들어 둔다.
스크립트는 Python 콘솔과 같은 네임스페이스(app, np, ndi, plt, skimage)에서 실행된다.
"""
import os
import re

from PyQt5.QtCore import QObject, QStandardPaths, pyqtSignal

EXAMPLES = {
    "Otsu 임계값 → 입자 분석 (현재 슬라이스)": '''\
# 현재 슬라이스를 Otsu 임계값으로 이진화하고 객체(입자)를 측정
from skimage.filters import threshold_otsu
from dabbaview.analysis.measure import analyze_particles
img = app.current_image
t = threshold_otsu(img)
parts = analyze_particles(img > t, spacing=app.spacing[1:], min_area_px=10)
print(f"Otsu 임계값 {t:.1f} → 객체 {len(parts)}개")
for p in parts[:20]:
    print(f"#{p['id']:3d}  area {p['area_mm2']:8.1f} mm²  circularity {p['circularity']:.3f}")
plt.imshow(img > t, cmap="gray"); plt.title(f"Otsu > {t:.1f}")
''',
    "Gaussian 1.0 → 새 시리즈": '''\
# 3D Gaussian 흐림 (sigma 1 복셀) 결과를 새 시리즈로 (원본 보존)
result = ndi.gaussian_filter(app.current_array, sigma=1.0)
app.add_series(result, "Gaussian σ=1")
''',
    "볼륨 통계 + 히스토그램": '''\
# 전체 볼륨 통계와 히스토그램
vol = app.current_array
print(f"shape {vol.shape}, spacing {app.spacing} mm")
print(f"mean {vol.mean():.2f}  std {vol.std():.2f}  min {vol.min():.1f}  max {vol.max():.1f}")
plt.hist(vol.ravel(), bins=200, log=True)
plt.xlabel("value"); plt.title("volume histogram (log)")
''',
    "AI 라벨별 부피 (mL)": '''\
# AI 세그멘테이션 마스크의 라벨별 부피
import numpy as np
m = app.mask
if m is None:
    print("마스크가 없습니다 (AI 패널에서 칠하거나 SEG/라벨맵을 여세요)")
else:
    voxel_ml = float(np.prod(app.spacing)) / 1000
    for label, count in zip(*np.unique(m[m > 0], return_counts=True)):
        print(f"label {label}: {count * voxel_ml:.2f} mL ({count} voxels)")
''',
    "최대 강도 투영 (MIP)": '''\
# 슬라이스 방향 MIP를 그래프로
mip = app.current_array.max(axis=0)
plt.imshow(mip, cmap="gray"); plt.title("MIP (axial)"); plt.axis("off")
''',
}


def macros_dir():
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    path = os.path.join(base or os.path.expanduser("~/.dabbaview"), "macros")
    os.makedirs(path, exist_ok=True)
    return path


def _filename(name):
    safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "macro"
    return safe + ".py"


class MacroStore(QObject):
    changed = pyqtSignal()

    def __init__(self, folder=None, parent=None):
        super().__init__(parent)
        self.folder = folder or macros_dir()
        marker = os.path.join(self.folder, ".examples_installed")
        if not os.path.exists(marker):
            for name, code in EXAMPLES.items():
                path = os.path.join(self.folder, _filename(name))
                if not os.path.exists(path):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(code)
            with open(marker, "w") as f:
                f.write("1")

    def names(self):
        return sorted(os.path.splitext(f)[0] for f in os.listdir(self.folder)
                      if f.endswith(".py"))

    def path(self, name):
        return os.path.join(self.folder, _filename(name))

    def load(self, name):
        with open(self.path(name), encoding="utf-8") as f:
            return f.read()

    def save(self, name, code):
        with open(self.path(name), "w", encoding="utf-8") as f:
            f.write(code)
        self.changed.emit()

    def delete(self, name):
        try:
            os.remove(self.path(name))
        except OSError:
            pass
        self.changed.emit()
