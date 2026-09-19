# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""MedSAM / Segment Anything (ONNX) - 클릭 한 번으로 관심 영역 세그멘테이션

인코더(.onnx) + 디코더(.onnx) 두 파일 (segment-anything의 ONNX 내보내기 형식):
  인코더 입력: 영상 1×3×1024×1024 (또는 1024×1024×3) → 임베딩 1×256×64×64
  디코더 입력: image_embeddings, point_coords, point_labels, mask_input, has_mask_input,
              orig_im_size  (또는 MedSAM-lite처럼 image_embeddings + boxes)
전처리
  "medsam": 1024×1024로 바로 늘림, 0–1 정규화 (MedSAM 학습 방식)
  "sam":    긴 변을 1024로, 오른쪽·아래 채움, ImageNet 평균/표준편차 (원래 SAM)
프롬프트: 클릭 점 (label 1) 또는 클릭을 중심으로 한 박스 (MedSAM은 박스로 학습됨)
같은 슬라이스·W/L의 임베딩은 캐시 → 두 번째 클릭부터는 디코더만 실행
"""
from collections import OrderedDict

import numpy as np

try:
    import onnxruntime as ort
    try:
        ort.disable_telemetry_events()
    except Exception:  # noqa: BLE001
        pass
except ImportError:  # pragma: no cover
    ort = None

SIZE = 1024
_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
_STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)


class SamError(Exception):
    pass


def _resize(img, shape, linear=True):
    import cv2
    h, w = shape
    return cv2.resize(img, (int(w), int(h)),
                      interpolation=cv2.INTER_LINEAR if linear else cv2.INTER_NEAREST)


class SamSegmenter:
    def __init__(self, encoder_path, decoder_path, mode="medsam"):
        if ort is None:
            raise SamError("onnxruntime이 없습니다.")
        try:
            self.encoder = ort.InferenceSession(encoder_path, providers=["CPUExecutionProvider"])
            self.decoder = ort.InferenceSession(decoder_path, providers=["CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001
            raise SamError(f"ONNX 모델을 불러오지 못했습니다: {e}") from e
        self.mode = mode
        self.paths = (encoder_path, decoder_path)
        self.enc_input = self.encoder.get_inputs()[0]
        self.dec_inputs = {i.name: i for i in self.decoder.get_inputs()}
        if not any(n in self.dec_inputs for n in ("image_embeddings", "embeddings")):
            raise SamError("디코더에 image_embeddings 입력이 없습니다 (SAM ONNX 형식이 아님).")
        self._cache = OrderedDict()

    def describe(self):
        return (f"인코더 입력 {self.enc_input.name} {self.enc_input.shape} · "
                f"디코더 입력 {', '.join(self.dec_inputs)} · 전처리 {self.mode}")

    # ─── 인코더 ───
    def _prepare(self, image, window):
        """(H, W) 원래 값 → 인코더 입력, 좌표 변환용 (sx, sy)"""
        center, width = window
        low = center - width / 2
        img = np.clip((np.asarray(image, dtype=np.float32) - low) / max(width, 1e-6), 0, 1)
        h, w = img.shape
        if self.mode == "sam":
            scale = SIZE / max(h, w)
            nh, nw = int(round(h * scale)), int(round(w * scale))
            small = _resize(img * 255.0, (nh, nw))
            canvas = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
            canvas[:nh, :nw] = (np.repeat(small[..., None], 3, axis=2) - _MEAN) / _STD
            data, sx, sy = canvas, scale, scale
        else:
            big = _resize(img, (SIZE, SIZE))
            lo, hi = float(big.min()), float(big.max())
            big = (big - lo) / max(hi - lo, 1e-8)
            data, sx, sy = np.repeat(big[..., None], 3, axis=2), SIZE / w, SIZE / h
        shape = self.enc_input.shape
        if len(shape) == 4 and shape[1] == 3:
            data = data.transpose(2, 0, 1)[None]
        elif len(shape) == 4:
            data = data[None]
        elif len(shape) == 3 and shape[-1] != 3:
            data = data.transpose(2, 0, 1)
        return data.astype(np.float32), (sx, sy)

    def embedding(self, key, image, window):
        cache_key = (key, float(window[0]), float(window[1]))
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        data, scale = self._prepare(image, window)
        emb = self.encoder.run(None, {self.enc_input.name: data})[0]
        self._cache[cache_key] = (emb, scale)
        while len(self._cache) > 6:
            self._cache.popitem(last=False)
        return emb, scale

    # ─── 디코더 ───
    def segment(self, key, image, window, point=None, box=None):
        """point=(x, y) 픽셀 인덱스 / box=(x0, y0, x1, y1) → bool 마스크 (H, W)"""
        h, w = np.asarray(image).shape
        emb, (sx, sy) = self.embedding(key, image, window)
        feed = {}
        name = "image_embeddings" if "image_embeddings" in self.dec_inputs else "embeddings"
        feed[name] = emb.astype(np.float32)
        if "point_coords" in self.dec_inputs:
            coords, labels = [], []
            if box is not None:
                x0, y0, x1, y1 = box
                coords += [[x0 * sx, y0 * sy], [x1 * sx, y1 * sy]]
                labels += [2, 3]
            if point is not None:
                coords.append([point[0] * sx, point[1] * sy])
                labels.append(1)
            if box is None:
                coords.append([0.0, 0.0])     # SAM 규약: 박스가 없으면 패딩 점
                labels.append(-1)
            feed["point_coords"] = np.array([coords], dtype=np.float32)
            feed["point_labels"] = np.array([labels], dtype=np.float32)
        if "boxes" in self.dec_inputs:
            if box is None:
                raise SamError("이 디코더는 박스 프롬프트가 필요합니다 (프롬프트: 박스).")
            x0, y0, x1, y1 = box
            feed["boxes"] = np.array([[x0 * sx, y0 * sy, x1 * sx, y1 * sy]], dtype=np.float32)
        if "mask_input" in self.dec_inputs:
            feed["mask_input"] = np.zeros((1, 1, 256, 256), dtype=np.float32)
        if "has_mask_input" in self.dec_inputs:
            feed["has_mask_input"] = np.zeros(1, dtype=np.float32)
        if "orig_im_size" in self.dec_inputs:
            feed["orig_im_size"] = (np.array([h, w], dtype=np.float32) if self.mode == "sam"
                                    else np.array([SIZE, SIZE], dtype=np.float32))
        missing = [n for n in self.dec_inputs if n not in feed]
        if missing:
            raise SamError(f"디코더 입력을 채우지 못했습니다: {missing}")
        outputs = self.decoder.run(None, feed)
        names = [o.name for o in self.decoder.get_outputs()]
        masks = next((o for o in outputs if np.ndim(o) == 4), None)
        if masks is None:
            raise SamError("디코더 출력에서 마스크(4차원)를 찾지 못했습니다.")
        index = 0
        if masks.shape[1] > 1:
            iou = next((o for n, o in zip(names, outputs) if "iou" in n.lower()), None)
            index = int(np.argmax(iou[0])) if iou is not None else 0
        logits = masks[0, index].astype(np.float32)
        return self._to_image(logits, (h, w), (sx, sy)) > 0

    def _to_image(self, logits, shape, scale):
        h, w = shape
        if logits.shape == (h, w):
            return logits
        if self.mode == "sam":
            full = _resize(logits, (SIZE, SIZE)) if logits.shape != (SIZE, SIZE) else logits
            nh, nw = int(round(h * scale[1])), int(round(w * scale[0]))
            return _resize(full[:nh, :nw], (h, w))
        return _resize(logits, (h, w))


def click_box(pos_index, size_mm, spacing, shape):
    """클릭(픽셀 인덱스)을 중심으로 size_mm 정사각형 박스 (영상 안으로 자름)"""
    row_sp, col_sp = spacing
    hx, hy = size_mm / 2 / col_sp, size_mm / 2 / row_sp
    x, y = pos_index
    h, w = shape
    return (max(0.0, x - hx), max(0.0, y - hy), min(w - 1.0, x + hx), min(h - 1.0, y + hy))
