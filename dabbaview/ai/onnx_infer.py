# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
ONNX 세그멘테이션 모델 로컬 추론 (onnxruntime)

입력 모양으로 모드 자동 판별:
  (N, C, H, W)     → 2D: 슬라이스마다 추론
  (N, C, D, H, W)  → 3D: 볼륨 한 번에 추론
고정 크기 입력이면 리사이즈해서 넣고 결과를 원래 크기로 되돌림 (최근접).
출력 채널이 여러 개면 argmax (채널 c → 라벨 c), 하나면 sigmoid > 0.5 → 지정 라벨.
"""
import numpy as np
from scipy import ndimage

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
    try:   # 내장 원격 수집(telemetry) 스레드가 앱 종료 중에 충돌(abort)하는 일이 있어 끔
        ort.disable_telemetry_events()
    except Exception:  # noqa: BLE001 - 버전에 따라 없음
        pass
except ImportError:  # pragma: no cover - 설치 안 된 환경
    ort = None
    ONNX_AVAILABLE = False


class OnnxModelError(Exception):
    pass


class OnnxSegmenter:
    def __init__(self, path):
        if not ONNX_AVAILABLE:
            raise OnnxModelError("onnxruntime이 설치되어 있지 않습니다 (pip install onnxruntime).")
        try:
            self.session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001 - onnxruntime 예외 종류가 다양
            raise OnnxModelError(f"모델을 불러오지 못했습니다: {e}") from e
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.input_shape = list(inp.shape)
        self.input_type = inp.type
        if len(self.input_shape) not in (4, 5):
            raise OnnxModelError(f"입력 모양 {self.input_shape}: (N,C,H,W) 또는 "
                                 "(N,C,D,H,W) 모델만 지원합니다.")
        self.is_3d = len(self.input_shape) == 5
        self.channels = self._dim(1) or 1
        out = self.session.get_outputs()[0]
        self.output_shape = list(out.shape)

    def _dim(self, i):
        v = self.input_shape[i]
        return v if isinstance(v, int) and v > 0 else None

    def describe(self):
        mode = "3D" if self.is_3d else "2D (슬라이스별)"
        spatial = self.input_shape[2:]
        size = "x".join(str(v) if isinstance(v, int) and v > 0 else "?" for v in spatial)
        return f"{mode}, 입력 {self.input_name} [{size}], 채널 {self.channels}, " \
               f"출력 {self.output_shape}"

    def _fixed_spatial(self):
        dims = [self._dim(i) for i in range(2, len(self.input_shape))]
        return dims if all(dims) else None

    def _run(self, block):
        """block: (*spatial) 정규화된 float32 → 라벨 (*spatial) uint8"""
        target = self._fixed_spatial()
        original = block.shape
        if target and list(original) != list(target):
            block = ndimage.zoom(block, np.array(target) / np.array(original), order=1)
        x = np.repeat(block[None, None].astype(np.float32), self.channels, axis=1)
        if "float16" in self.input_type:
            x = x.astype(np.float16)
        out = self.session.run(None, {self.input_name: x})[0]
        out = np.asarray(out, dtype=np.float32)[0]   # (C_out, *spatial)
        if out.ndim == block.ndim:                    # 채널 축 없는 출력
            out = out[None]
        if out.shape[0] > 1:
            labels = np.argmax(out, axis=0).astype(np.uint8)
        else:
            prob = out[0]
            if prob.min() < 0 or prob.max() > 1:
                prob = 1 / (1 + np.exp(-prob))
            labels = (prob > 0.5).astype(np.uint8)
        if labels.shape != original:
            labels = ndimage.zoom(labels, np.array(original) / np.array(labels.shape), order=0)
            labels = _fit(labels, original)
        return labels

    def predict(self, volume, window, single_label=1, progress=None, cancelled=None):
        """volume (d, h, w) → 라벨 마스크 (d, h, w). window=(center, width)로 0~1 정규화"""
        center, width = window
        low = center - width / 2
        norm = np.clip((volume - low) / max(width, 1e-6), 0, 1).astype(np.float32)
        if self.is_3d:
            if progress:
                progress(0, 1)
            labels = self._run(norm)
            if progress:
                progress(1, 1)
        else:
            labels = np.zeros(volume.shape, dtype=np.uint8)
            for k in range(volume.shape[0]):
                if cancelled and cancelled():
                    return None
                labels[k] = self._run(norm[k])
                if progress:
                    progress(k + 1, volume.shape[0])
        if labels.max() <= 1 and single_label != 1:
            labels = labels * np.uint8(single_label)
        return labels


def _fit(array, shape):
    """zoom 반올림으로 1픽셀 어긋난 크기를 잘라내거나 채워서 맞춤"""
    out = np.zeros(shape, dtype=array.dtype)
    region = tuple(slice(0, min(a, b)) for a, b in zip(array.shape, shape))
    out[region] = array[region]
    return out
