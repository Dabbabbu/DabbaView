# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
MR 분광 (MRS) - DICOM MR Spectroscopy / Siemens .rda → 스펙트럼, 피크, 대사물질 비율

처리: 지수 선폭 가중(apodization) → 0 채움 → FFT → 0차 위상 자동 보정 → 선형 기저선 → 피크 적분
ppm 축은 물 4.7 ppm 기준.
"""
import re

import numpy as np

MRS_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.4.2"
WATER_PPM = 4.7

# (이름, 중심 ppm, 적분 반폭 ppm)
METABOLITES = [
    ("NAA", 2.01, 0.08), ("Cr", 3.03, 0.06), ("Cho", 3.21, 0.06),
    ("mI", 3.56, 0.07), ("Glx", 2.35, 0.10), ("Lac", 1.33, 0.08), ("Lip", 0.90, 0.10),
]


class Spectrum:
    def __init__(self, fid, spectral_width_hz, transmitter_mhz, source=""):
        self.fid = np.asarray(fid, dtype=np.complex128)
        self.sw = float(spectral_width_hz)
        self.tf = float(transmitter_mhz)
        self.source = source


def is_mrs_dataset(ds):
    return str(getattr(ds, "SOPClassUID", "")) == MRS_SOP_CLASS or "SpectroscopyData" in ds


def read_dicom(ds, voxel=0):
    """pydicom Dataset → Spectrum (여러 복셀이면 voxel번째, -1이면 평균)"""
    if "SpectroscopyData" not in ds:
        raise ValueError("SpectroscopyData가 없는 DICOM입니다.")
    n = int(ds.DataPointColumns)
    raw = np.frombuffer(ds.SpectroscopyData, dtype="<f4") if isinstance(
        ds.SpectroscopyData, bytes) else np.asarray(ds.SpectroscopyData, dtype=np.float32)
    data = raw[0::2] + 1j * raw[1::2]
    voxels = data.reshape(-1, n)
    fid = voxels.mean(0) if voxel < 0 else voxels[min(voxel, len(voxels) - 1)]
    if str(getattr(ds, "SignalDomainColumns", "TIME")).upper() == "FREQUENCY":
        fid = np.fft.ifft(np.fft.ifftshift(fid))
    return Spectrum(fid, float(ds.SpectralWidth), float(ds.TransmitterFrequency),
                    str(getattr(ds, "SeriesDescription", "")))


def read_rda(path):
    """Siemens .rda (텍스트 헤더 + complex128)"""
    with open(path, "rb") as f:
        data = f.read()
    end = data.find(b">>> End of header <<<")
    if end < 0:
        raise ValueError("RDA 헤더를 찾지 못했습니다.")
    header = data[:end].decode("latin-1")
    body_start = data.find(b"\n", end) + 1

    def val(key):
        m = re.search(rf"^{key}:\s*(.+)$", header, re.M)
        if not m:
            raise ValueError(f"RDA 헤더에 {key}가 없습니다.")
        return m.group(1).strip()
    n = int(val("VectorSize"))
    dwell_us = float(val("DwellTime"))
    tf = float(val("MRFrequency"))
    values = np.frombuffer(data[body_start:], dtype="<f8")
    fid = values[0:2 * n:2] + 1j * values[1:2 * n:2]
    return Spectrum(fid, 1e6 / dwell_us, tf, path)


def process(spec, line_broadening_hz=3.0, zero_fill=2, auto_phase=True, phase_deg=0.0):
    """→ (ppm 축, 실수 스펙트럼(기저선 보정), 사용한 위상 °)"""
    fid = spec.fid.copy()
    n = len(fid)
    t = np.arange(n) / spec.sw
    fid *= np.exp(-np.pi * line_broadening_hz * t)
    fid[0] *= 0.5                                       # 첫 점 보정
    size = int(2 ** np.ceil(np.log2(n * zero_fill)))
    spectrum = np.fft.fftshift(np.fft.fft(fid, size))
    freq = np.fft.fftshift(np.fft.fftfreq(size, d=1 / spec.sw))
    ppm = WATER_PPM + freq / spec.tf
    region = (ppm > 0.5) & (ppm < 4.2)
    if auto_phase:
        # 대사물질 영역 실수부 적분이 최대 + 음의 면적이 최소가 되는 0차 위상
        best, best_score = 0.0, -np.inf
        for deg in np.arange(0, 360, 1.0):
            real = (spectrum * np.exp(1j * np.radians(deg))).real[region]
            score = real.sum() - 5 * np.abs(real[real < 0]).sum()
            if score > best_score:
                best, best_score = deg, score
        phase_deg = best
    real = (spectrum * np.exp(1j * np.radians(phase_deg))).real
    # 선형 기저선: 피크가 없는 구간(0.2~0.6, 4.0~4.3 ppm)의 중앙값을 잇는 직선
    quiet = ((ppm > 0.2) & (ppm < 0.6)) | ((ppm > 4.0) & (ppm < 4.3))
    if quiet.sum() > 4:
        coef = np.polyfit(ppm[quiet], real[quiet], 1)
        real = real - np.polyval(coef, ppm)
    return ppm, real, phase_deg


def quantify(ppm, real, metabolites=METABOLITES):
    """피크마다 위치·높이·면적, 그리고 비율"""
    step = abs(ppm[1] - ppm[0]) if len(ppm) > 1 else 1.0
    peaks = {}
    noise_region = (ppm > 8.0) & (ppm < 9.5)
    noise = float(np.std(real[noise_region])) if noise_region.sum() > 10 else float(np.std(real) * 0.01)
    for name, center, half in metabolites:
        sel = (ppm > center - half) & (ppm < center + half)
        if not sel.any():
            continue
        seg = real[sel]
        idx = int(np.argmax(np.abs(seg)))
        height = float(seg[idx])
        area = float(seg.sum() * step)
        peaks[name] = {"ppm": float(ppm[sel][idx]), "height": height, "area": area,
                       "SNR": abs(height) / noise if noise > 0 else float("inf")}
    ratios = {}
    for num, den in (("Cho", "Cr"), ("NAA", "Cr"), ("Cho", "NAA"), ("mI", "Cr"), ("Lac", "Cr")):
        if num in peaks and den in peaks and abs(peaks[den]["area"]) > 1e-12:
            ratios[f"{num}/{den}"] = peaks[num]["area"] / peaks[den]["area"]
    return peaks, ratios


def synthetic_fid(n=1024, sw=2000.0, tf=123.2, peaks=None, noise=0.0, seed=0):
    """시험용 FID - peaks: [(ppm, 진폭, T2* 초)]"""
    peaks = peaks or [(2.01, 1.0, 0.1), (3.03, 0.6, 0.1), (3.21, 0.5, 0.1)]
    t = np.arange(n) / sw
    fid = np.zeros(n, dtype=np.complex128)
    for ppm, amp, t2 in peaks:
        f = (ppm - WATER_PPM) * tf
        fid += amp * np.exp(2j * np.pi * f * t - t / t2)
    if noise:
        rng = np.random.default_rng(seed)
        fid += noise * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return Spectrum(fid, sw, tf, "synthetic")
