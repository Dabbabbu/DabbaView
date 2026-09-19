# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
영상 정합 (Image Registration) - SimpleITK

fixed(기준) 시리즈에 moving 시리즈를 맞춤: Rigid(회전+이동) 또는 Affine.
Mattes Mutual Information → 다른 모달리티(CT/MR/PET)끼리도 가능.
결과: 변환(.tfm 저장/불러오기) + fixed 격자로 다시 샘플링한 moving 볼륨.
"""
import numpy as np

import SimpleITK as sitk

METHODS = {"rigid": "Rigid (회전+이동, 6 DOF)", "affine": "Affine (12 DOF)"}
METRICS = {"mi": "Mutual Information (다른 모달리티: CT↔MR/PET)",
           "ms": "Mean Squares (같은 모달리티, 같은 장비)",
           "corr": "Correlation (같은 모달리티, 밝기 차이 허용)"}

sitk.ProcessObject.SetGlobalWarningDisplay(False)  # ITK 경고가 콘솔에 쏟아지지 않도록


def to_sitk(volume):
    """ai.volume.Volume → sitk.Image (LPS 물리 좌표 유지)"""
    a = volume.affine_lps
    spacing = [float(np.linalg.norm(a[:3, i])) or 1.0 for i in range(3)]
    direction = a[:3, :3] / np.array(spacing)
    image = sitk.GetImageFromArray(np.ascontiguousarray(volume.array, dtype=np.float32))
    image.SetSpacing(spacing)
    image.SetOrigin([float(v) for v in a[:3, 3]])
    image.SetDirection([float(v) for v in direction.flatten()])
    return image


def register(fixed, moving, method="rigid", iterations=300, sampling=0.2, progress=None,
             cancelled=None, metric="mi"):
    """fixed/moving: Volume → (sitk.Transform, 최종 metric 값)

    Regular Step Gradient Descent + 다해상도(4→2→1). 반환한 변환은 fixed 좌표 → moving 좌표.
    """
    f = to_sitk(fixed)
    m = to_sitk(moving)
    if method == "rigid":
        base = sitk.Euler3DTransform()
    elif method == "affine":
        base = sitk.AffineTransform(3)
    else:
        raise ValueError(f"알 수 없는 정합 방법: {method}")
    initial = sitk.CenteredTransformInitializer(
        f, m, base, sitk.CenteredTransformInitializerFilter.GEOMETRY)

    reg = sitk.ImageRegistrationMethod()
    if metric == "ms":
        reg.SetMetricAsMeanSquares()
    elif metric == "corr":
        reg.SetMetricAsCorrelation()
    else:
        reg.SetMetricAsMattesMutualInformation(numberOfHistogramBins=50)
    reg.SetMetricSamplingStrategy(reg.RANDOM)
    reg.SetMetricSamplingPercentage(float(sampling), seed=42)
    reg.SetInterpolator(sitk.sitkLinear)
    # 일반 Gradient Descent는 발산하기 쉬워 Regular Step 사용 (합성 검증: 오차 0.1~0.4 mm)
    reg.SetOptimizerAsRegularStepGradientDescent(
        learningRate=2.0, minStep=1e-4, numberOfIterations=int(iterations),
        relaxationFactor=0.5, gradientMagnitudeTolerance=1e-8)
    reg.SetOptimizerScalesFromPhysicalShift()
    reg.SetShrinkFactorsPerLevel([4, 2, 1])
    reg.SetSmoothingSigmasPerLevel([2, 1, 0])
    reg.SmoothingSigmasAreSpecifiedInPhysicalUnitsOn()
    reg.SetInitialTransform(initial, inPlace=False)

    total = int(iterations) * 3
    state = {"n": 0}

    def on_iteration():
        state["n"] += 1
        if cancelled and cancelled():
            reg.StopRegistration() if hasattr(reg, "StopRegistration") else None
        if progress and state["n"] % 5 == 0:
            progress(f"정합 중... 반복 {state['n']} · metric {reg.GetMetricValue():.4f}",
                     min(0.95, state["n"] / total))
    reg.AddCommand(sitk.sitkIterationEvent, on_iteration)
    transform = reg.Execute(sitk.Cast(f, sitk.sitkFloat32), sitk.Cast(m, sitk.sitkFloat32))
    return transform, reg.GetMetricValue()


def resample_to(fixed, moving, transform, default_value=None):
    """moving을 transform으로 옮겨 fixed 격자에 다시 샘플링 → (d, h, w) float32"""
    f = to_sitk(fixed)
    m = to_sitk(moving)
    if default_value is None:
        default_value = float(np.min(moving.array))
    out = sitk.Resample(m, f, transform, sitk.sitkLinear, default_value, sitk.sitkFloat32)
    return sitk.GetArrayFromImage(out)


def save_transform(path, transform):
    sitk.WriteTransform(transform, path)


def load_transform(path):
    return sitk.ReadTransform(path)


def describe(transform):
    """변환 요약 (이동 mm, 회전 도)"""
    t = transform
    while isinstance(t, sitk.CompositeTransform) and t.GetNumberOfTransforms():
        t = t.GetNthTransform(t.GetNumberOfTransforms() - 1)
    try:
        t = t.Downcast()
    except AttributeError:
        pass
    params = list(t.GetParameters())
    if isinstance(t, sitk.Euler3DTransform):
        rx, ry, rz, tx, ty, tz = params
        return (f"회전 ({np.degrees(rx):.2f}°, {np.degrees(ry):.2f}°, {np.degrees(rz):.2f}°) · "
                f"이동 ({tx:.2f}, {ty:.2f}, {tz:.2f}) mm")
    if isinstance(t, sitk.AffineTransform):
        tx, ty, tz = params[9:12]
        return f"Affine 행렬 + 이동 ({tx:.2f}, {ty:.2f}, {tz:.2f}) mm"
    return t.GetName()
