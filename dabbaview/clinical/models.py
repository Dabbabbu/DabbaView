# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
정량 모델 피팅 (numpy 벡터화 + scipy.optimize)

확산: ADC(단일 지수), IVIM(분할 피팅: D, f, D*)
이완: T2(단일 지수), T1 MOLLI(3-파라미터 + Look-Locker 보정, 극성 복원)
관류: 감마바리에이트, DCE 확장 전 Tofts(선형화, Murase 2004), DSC(sSVD 디컨볼루션)
유량: Phase Contrast (속도 × 면적)
"""
import numpy as np
from scipy import optimize

EPS = 1e-6


# ═══ 확산 ═══

def fit_adc(signals, b_values, mask=None):
    """signals (B, ...) → (ADC mm²/s, S0). 로그 선형 최소제곱 (가중: 신호²)"""
    s = np.asarray(signals, dtype=np.float64)
    b = np.asarray(b_values, dtype=np.float64)
    shape = s.shape[1:]
    flat = s.reshape(len(b), -1)
    valid = np.all(flat > 0, axis=0)
    if mask is not None:
        valid &= np.asarray(mask).ravel().astype(bool)
    y = np.log(np.maximum(flat, EPS))
    w = np.maximum(flat, EPS) ** 2            # 저신호 점의 잡음 증폭 억제
    sw = w.sum(0)
    bm = (w * b[:, None]).sum(0) / sw
    ym = (w * y).sum(0) / sw
    slope = (w * (b[:, None] - bm) * (y - ym)).sum(0) / np.maximum(
        (w * (b[:, None] - bm) ** 2).sum(0), EPS)
    adc = np.where(valid, -slope, 0.0)
    s0 = np.where(valid, np.exp(ym - slope * bm), 0.0)
    return np.clip(adc, 0, None).reshape(shape), s0.reshape(shape)


def ivim_segmented(signals, b_values, b_threshold=200.0, mask=None):
    """분할 IVIM: 고 b(≥threshold)로 D, 절편으로 f, 저 b 잔차로 D* (복셀별)

    반환 dict: D, f, Dstar (mm²/s), S0
    """
    s = np.asarray(signals, dtype=np.float64)
    b = np.asarray(b_values, dtype=np.float64)
    high = b >= b_threshold
    if high.sum() < 2 or (~high).sum() < 1 or 0 not in set(b.tolist()) and b.min() > 0:
        raise ValueError("IVIM에는 b=0, 낮은 b(<200) 1개 이상, 높은 b(≥200) 2개 이상이 필요합니다.")
    shape = s.shape[1:]
    flat = s.reshape(len(b), -1)
    s0 = flat[np.argmin(b)]
    D, intercept = fit_adc(flat[high], b[high])
    f = np.clip(1 - intercept / np.maximum(s0, EPS), 0, 1)
    # D*: 저 b 신호에서 확산 성분을 뺀 관류 성분의 기울기
    low = ~high
    perf = flat[low] - intercept[None] * np.exp(-np.outer(b[low], D))
    perf0 = np.maximum(s0 - intercept, EPS)
    ratio = np.clip(perf / perf0[None], EPS, 1)
    bl = b[low]
    nonzero = bl > 0
    if nonzero.any():
        # 관류 성분이 거의 사라진 점(비율≈0)은 잡음이 크므로 남은 비율²로 가중
        y = -np.log(ratio[nonzero])                      # (L, V) ≈ b·D*
        w = ratio[nonzero] ** 2
        bb = bl[nonzero][:, None]
        dstar = (w * bb * y).sum(0) / np.maximum((w * bb ** 2).sum(0), EPS)
    else:
        dstar = np.zeros_like(D)
    valid = np.all(flat > 0, axis=0)
    if mask is not None:
        valid &= np.asarray(mask).ravel().astype(bool)
    out = {"D": D, "f": f, "Dstar": np.clip(dstar, 0, 1), "S0": s0}
    return {k: np.where(valid, v, 0).reshape(shape) for k, v in out.items()}


def ivim_curve_fit(signal, b_values):
    """ROI 평균 신호 하나를 비선형 bi-exponential로 정밀 피팅 → (D, f, D*, S0)"""
    b = np.asarray(b_values, dtype=float)
    y = np.asarray(signal, dtype=float)
    seg = ivim_segmented(y[:, None], b)
    p0 = [seg["S0"][0], seg["f"][0], max(seg["D"][0], 1e-4), max(seg["Dstar"][0], 5e-3)]

    def model(bv, s0, f, d, ds):
        return s0 * (f * np.exp(-bv * ds) + (1 - f) * np.exp(-bv * d))
    try:
        popt, _ = optimize.curve_fit(model, b, y, p0=p0,
                                     bounds=([0, 0, 0, 0], [np.inf, 1, 5e-3, 0.5]), maxfev=20000)
        s0, f, d, ds = popt
    except (RuntimeError, ValueError):
        s0, f, d, ds = p0
    return {"D": d, "f": f, "Dstar": ds, "S0": s0}


# ═══ 이완 (T1/T2 매핑) ═══

def fit_t2(signals, te_ms, mask=None):
    """다중 에코 → T2 (ms). 로그 선형 (첫 에코 제외 옵션은 호출 측에서)"""
    adc, s0 = fit_adc(signals, te_ms, mask)   # 같은 형태: S = S0·exp(-TE/T2)
    t2 = np.where(adc > 0, 1.0 / np.maximum(adc, EPS), 0)
    return np.clip(t2, 0, 5000), s0


def fit_t1_molli(signals, ti_ms, mask=None, t1_range=(50, 3000), steps=160, chunk=20000):
    """MOLLI 크기 영상 → T1 (ms). |A − B·exp(−TI/T1*)| 격자 탐색 + 극성 복원 + Look-Locker 보정

    각 T1* 후보와 '앞에서 몇 개 점의 부호를 뒤집을지'마다 A, B를 선형 최소제곱으로 풀고
    잔차가 가장 작은 조합을 고른다. T1 = T1*·(B/A − 1)
    """
    s = np.asarray(signals, dtype=np.float64)
    order = np.argsort(ti_ms)
    ti = np.asarray(ti_ms, dtype=np.float64)[order]
    s = s[order]
    shape = s.shape[1:]
    flat = s.reshape(len(ti), -1)
    n_vox = flat.shape[1]
    valid = flat.max(0) > 0
    if mask is not None:
        valid &= np.asarray(mask).ravel().astype(bool)
    idx = np.nonzero(valid)[0]
    t1 = np.zeros(n_vox)
    rsq = np.zeros(n_vox)
    grid = np.geomspace(t1_range[0], t1_range[1], steps)
    n = len(ti)
    # 후보마다 설계행렬 [1, -e] 의 유사역행렬 미리 계산
    designs = []
    for t1s in grid:
        e = np.exp(-ti / t1s)
        X = np.stack([np.ones(n), -e], axis=1)
        designs.append((X, np.linalg.pinv(X)))
    for start in range(0, len(idx), chunk):
        sel = idx[start:start + chunk]
        y = flat[:, sel]                                   # (N, V)
        best_err = np.full(len(sel), np.inf)
        best = np.zeros((3, len(sel)))                     # A, B, T1*
        for flip in range(n + 1):
            sign = np.ones((n, 1))
            sign[:flip] = -1
            ys = y * sign
            for (X, Xp), t1s in zip(designs, grid):
                coef = Xp @ ys                             # (2, V)
                err = ((X @ coef - ys) ** 2).sum(0)
                better = err < best_err
                if better.any():
                    best_err[better] = err[better]
                    best[0, better] = coef[0, better]
                    best[1, better] = coef[1, better]
                    best[2, better] = t1s
        a, b, t1s = best
        with np.errstate(divide="ignore", invalid="ignore"):
            t1v = t1s * (b / a - 1)
        t1[sel] = np.where((a > 0) & (b > a), t1v, 0)
        ss = ((y - y.mean(0)) ** 2).sum(0)
        rsq[sel] = 1 - best_err / np.maximum(ss, EPS)
    return np.clip(t1, 0, 5000).reshape(shape), rsq.reshape(shape)


# ═══ 관류 ═══

def gamma_variate(t, t0, a, alpha, beta):
    tt = np.clip(np.asarray(t, dtype=float) - t0, 0, None)
    return a * tt ** alpha * np.exp(-tt / max(beta, EPS))


def fit_gamma(t, curve):
    """첫 통과 곡선 → (파라미터, 피팅 곡선, 지표 dict: 도달시간, TTP, 최고치, 면적, MTT)"""
    t = np.asarray(t, dtype=float)
    y = np.asarray(curve, dtype=float)
    peak = int(np.argmax(y))
    t0_guess = t[max(0, peak - 3)]
    p0 = [t0_guess, max(y[peak], EPS), 3.0, max((t[peak] - t0_guess) / 3.0, 0.3)]
    try:
        popt, _ = optimize.curve_fit(gamma_variate, t, y, p0=p0, maxfev=20000,
                                     bounds=([t[0] - 5, 0, 0.1, 0.05],
                                             [t[peak], np.inf, 20, 50]))
    except (RuntimeError, ValueError):
        popt = p0
    t0, a, alpha, beta = popt
    fine = np.linspace(t[0], t[-1], 400)
    fitted = gamma_variate(fine, *popt)
    area = a * beta ** (alpha + 1) * _gamma_fn(alpha + 1)
    metrics = {"arrival (s)": t0, "TTP (s)": t0 + alpha * beta,
               "peak": float(gamma_variate(t0 + alpha * beta, *popt)),
               "AUC": area, "MTT (s)": (alpha + 1) * beta}
    return popt, (fine, fitted), metrics


def _gamma_fn(x):
    from scipy.special import gamma
    return float(gamma(x))


def upslope(t, curve, window=3):
    """최대 기울기 (평활 후)"""
    c = np.asarray(curve, dtype=np.float64)
    half = window // 2
    padded = np.pad(c, (half, window - 1 - half), mode="edge")   # 0으로 채우면 끝에서 가짜 기울기
    y = np.convolve(padded, np.ones(window) / window, mode="valid")
    return float(np.max(np.gradient(y, np.asarray(t, dtype=np.float64))))


def signal_to_delta_r2(signals, te_ms, baseline_frames):
    """DSC: ΔR2*(t) = −ln(S(t)/S0)/TE (1/s)"""
    s = np.asarray(signals, dtype=np.float64)
    s0 = s[:baseline_frames].mean(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = -np.log(np.maximum(s, EPS) / np.maximum(s0, EPS)[None]) / (te_ms / 1000.0)
    r[:baseline_frames] = 0
    return np.nan_to_num(r)


def dsc_maps(delta_r2, t, aif, threshold=0.2, mask=None):
    """sSVD 디컨볼루션 → CBF(상대), CBV(상대), MTT(s)

    CBV = ∫C / ∫AIF,  R(t) = SVD⁻¹(AIF) · C,  CBF = max R,  MTT = CBV / CBF
    """
    c = np.asarray(delta_r2, dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    n = len(t)
    dt = float(np.mean(np.diff(t)))
    A = np.zeros((n, n))
    for i in range(n):
        A[i, :i + 1] = aif[i::-1]
    A *= dt
    U, S, Vt = np.linalg.svd(A)
    S_inv = np.where(S > threshold * S.max(), 1.0 / S, 0.0)
    A_inv = Vt.T @ np.diag(S_inv) @ U.T
    shape = c.shape[1:]
    flat = c.reshape(n, -1)
    residue = A_inv @ flat
    cbf = residue.max(0)
    cbv = np.trapezoid(flat, t, axis=0) / max(np.trapezoid(aif, t), EPS)
    mtt = np.where(cbf > EPS, cbv / np.maximum(cbf, EPS), 0)
    maps = {"CBF": cbf, "CBV": cbv, "MTT": mtt}
    if mask is not None:
        m = np.asarray(mask).ravel().astype(bool)
        maps = {k: np.where(m, v, 0) for k, v in maps.items()}
    return {k: np.clip(v, 0, None).reshape(shape) for k, v in maps.items()}


def tofts_linear(ct, cp, t, mask=None):
    """확장 전 표준 Tofts, 선형화(Murase): Ct(t) = Ktrans∫Cp − kep∫Ct

    ct: (T, ...) 조직 농도, cp: (T,) 혈장 농도(AIF), t: 분(min)
    반환 Ktrans(1/min), kep(1/min), ve
    """
    c = np.asarray(ct, dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    shape = c.shape[1:]
    flat = c.reshape(len(t), -1)
    int_cp = _cumtrapz(np.asarray(cp, dtype=np.float64), t)          # (T,)
    int_ct = _cumtrapz(flat, t)                                      # (T, V)
    # 복셀마다 [∫Cp, −∫Ct] · [Ktrans, kep]ᵀ = Ct  →  2×2 정규방정식을 벡터화로 풀기
    a11 = (int_cp ** 2).sum()
    a12 = -(int_cp[:, None] * int_ct).sum(0)
    a22 = (int_ct ** 2).sum(0)
    b1 = (int_cp[:, None] * flat).sum(0)
    b2 = -(int_ct * flat).sum(0)
    det = a11 * a22 - a12 ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        ktrans = (a22 * b1 - a12 * b2) / det
        kep = (a11 * b2 - a12 * b1) / det
        ve = ktrans / kep
    ok = (det > EPS) & (ktrans > 0) & (kep > 0)
    if mask is not None:
        ok &= np.asarray(mask).ravel().astype(bool)
    out = {"Ktrans": np.where(ok, ktrans, 0), "kep": np.where(ok, kep, 0),
           "ve": np.where(ok, np.clip(ve, 0, 1), 0)}
    return {k: v.reshape(shape) for k, v in out.items()}


def _cumtrapz(y, t):
    dt = np.diff(t)
    if y.ndim == 1:
        return np.concatenate([[0], np.cumsum((y[1:] + y[:-1]) / 2 * dt)])
    return np.concatenate([np.zeros((1,) + y.shape[1:]),
                           np.cumsum((y[1:] + y[:-1]) / 2 * dt[:, None], axis=0)])


def parker_aif(t_min, hematocrit=0.42):
    """Parker 2006 집단 AIF (혈장 농도, mM) - t: 분, 조영 도달 0.17분 가정"""
    t = np.asarray(t_min, dtype=float)
    A1, A2, T1, T2, s1, s2 = 0.809, 0.330, 0.17046, 0.365, 0.0563, 0.132
    alpha, beta, s, tau = 1.050, 0.1685, 38.078, 0.483
    cb = (A1 / (s1 * np.sqrt(2 * np.pi)) * np.exp(-(t - T1) ** 2 / (2 * s1 ** 2)) +
          A2 / (s2 * np.sqrt(2 * np.pi)) * np.exp(-(t - T2) ** 2 / (2 * s2 ** 2)) +
          alpha * np.exp(-beta * t) / (1 + np.exp(-s * (t - tau))))
    return cb / (1 - hematocrit)


def relative_enhancement(signals, baseline_frames):
    """DCE 신호 → 상대 조영 증강 (S−S0)/S0 (농도에 비례한다고 근사)"""
    s = np.asarray(signals, dtype=np.float64)
    s0 = s[:baseline_frames].mean(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.nan_to_num((s - s0[None]) / np.maximum(s0, EPS)[None])


def auto_aif(curves, t, n=10, mask=None):
    """AIF 자동 선택: 최고치가 높고 도달이 빠르며 폭이 좁은 곡선 n개 평균

    curves: (T, V). 반환 (평균 곡선, 선택한 인덱스)
    """
    c = np.asarray(curves, dtype=np.float64)
    peak = c.max(0)
    ttp = np.argmax(c, axis=0)
    candidates = np.nonzero(peak > np.percentile(peak, 95))[0]
    if mask is not None:
        m = np.asarray(mask).ravel().astype(bool)
        candidates = candidates[m[candidates]] if m[candidates].any() else candidates
    if len(candidates) == 0:
        candidates = np.argsort(peak)[-n:]
    width = (c[:, candidates] > 0.5 * peak[candidates]).sum(0)
    score = peak[candidates] / (1 + ttp[candidates]) / (1 + width)
    chosen = candidates[np.argsort(score)[-n:]]
    return c[:, chosen].mean(1), chosen


# ═══ 유량 (Phase Contrast) ═══

def pc_flow(velocity_frames, roi_mask, pixel_area_mm2, times_ms):
    """속도 영상(cm/s) 시계열 + ROI → 유량 곡선과 지표

    유량(mL/s) = 평균 속도(cm/s) × 면적(cm²)
    """
    v = np.asarray(velocity_frames, dtype=np.float64)
    m = np.asarray(roi_mask, dtype=bool)
    area_cm2 = m.sum() * pixel_area_mm2 / 100.0
    mean_v = np.array([frame[m].mean() if m.any() else 0 for frame in v])
    peak_v = np.array([np.abs(frame[m]).max() if m.any() else 0 for frame in v])
    flow = mean_v * area_cm2
    t = np.asarray(times_ms, dtype=float) / 1000.0
    rr = t[-1] - t[0] + (t[1] - t[0] if len(t) > 1 else 0)
    dt = rr / len(t)
    forward = float(np.sum(np.clip(flow, 0, None)) * dt)
    backward = float(-np.sum(np.clip(flow, None, 0)) * dt)
    return {"flow": flow, "t": t, "area_cm2": area_cm2,
            "forward_ml": forward, "backward_ml": backward, "net_ml": forward - backward,
            "regurgitant_fraction": backward / forward * 100 if forward > 0 else 0.0,
            "peak_velocity": float(peak_v.max()), "rr_s": rr,
            "cardiac_output_l_min": (forward - backward) * 60 / rr / 1000 if rr > 0 else 0.0}


def phase_to_velocity(phase_values, venc, value_range=None):
    """위상 영상 값 → 속도(cm/s). value_range=(최소, 최대)가 ±VENC에 대응 (Siemens: −4096~4096)"""
    p = np.asarray(phase_values, dtype=np.float64)
    if value_range is None:
        return p
    lo, hi = value_range
    return (p - (lo + hi) / 2) / ((hi - lo) / 2) * venc
