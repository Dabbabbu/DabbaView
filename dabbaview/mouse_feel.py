# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
마우스 조작감 — 상용 PACS(RadiAnt · GE AW · INFINITT)처럼 묵직하고 정밀하게

- 비선형 감도: 천천히 움직이면 느리게(정밀), 빨리 끌면 가속
- W/L 한 픽셀의 크기는 지금 Window Width에 비례 (좁은 창일수록 섬세하게, 넓은 창은 크게)
- 보간(무게감): 끈 만큼을 바로 적용하지 않고 짧은 시간(기본 55ms)에 걸쳐 따라가 부드럽게 멈춤
- Pan 관성: 빠르게 던지듯 놓으면 잠깐 미끄러지다 감속해서 멈춤 (W/L · Zoom에는 관성 없음 — 손을 떼면 그 자리)
- 휠: 천천히 굴리면 한 장씩, 빠르게 연달아 굴리면 가속 (트랙패드는 움직인 양만큼)

값은 Settings ▸ Mouse ▸ 조작감에서 바꿈 (MouseBindings의 feel_* 키).
"""
import math

# 조작감 기본값 (설정 키 → 값). 퍼센트 값은 100 = 보통
FEEL_DEFAULTS = {
    "feel_wl": 100,          # W/L 감도 %
    "feel_pan": 100,         # Pan 감도 %
    "feel_zoom": 100,        # Zoom 감도 %
    "feel_scroll": 100,      # 드래그 넘기기 감도 %
    "feel_accel": 100,       # 빠르게 끌 때 가속 세기 % (0 = 가속 없음, 일정한 속도)
    "feel_smooth_ms": 55,    # 무게감(보간 시간) ms — 0이면 즉시 반영
    "feel_inertia": True,    # Pan 관성 (던지면 미끄러지다 멈춤)
    "feel_wheel_accel": True,   # 휠을 빠르게 연달아 굴리면 여러 장씩
}

# 설정 창의 한 번에 고르는 조작감
FEEL_PRESETS = {
    "light": ("가볍게 (빠르고 즉각적)", {"feel_wl": 130, "feel_pan": 100, "feel_zoom": 120,
                                  "feel_scroll": 120, "feel_accel": 140, "feel_smooth_ms": 20,
                                  "feel_inertia": False, "feel_wheel_accel": True}),
    "standard": ("보통 — 상용 PACS 느낌 (기본)", dict(FEEL_DEFAULTS)),
    "heavy": ("묵직하게 (정밀 조작)", {"feel_wl": 75, "feel_pan": 90, "feel_zoom": 80,
                                 "feel_scroll": 80, "feel_accel": 70, "feel_smooth_ms": 95,
                                 "feel_inertia": True, "feel_wheel_accel": True}),
    "raw": ("즉시 반영 (보간 · 가속 없음)", {"feel_wl": 100, "feel_pan": 100, "feel_zoom": 100,
                                     "feel_scroll": 100, "feel_accel": 0, "feel_smooth_ms": 0,
                                     "feel_inertia": False, "feel_wheel_accel": False}),
}

FEEL_RANGES = {"feel_wl": (25, 400), "feel_pan": (25, 400), "feel_zoom": (25, 400),
               "feel_scroll": (25, 400), "feel_accel": (0, 300), "feel_smooth_ms": (0, 200)}


def clean_feel(key, value):
    """설정 값 검사 → 올바른 값 (범위 밖이면 가장자리로). 모르는 키 · 잘못된 값은 None"""
    if key not in FEEL_DEFAULTS:
        return None
    if isinstance(FEEL_DEFAULTS[key], bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    low, high = FEEL_RANGES[key]
    return max(low, min(high, number))


# ─── 속도 → 배율 (비선형 감도 커브) ───
SLOW_GAIN = 0.4        # 아주 천천히(정밀) 움직일 때 배율
SLOW_SPEED = 0.05      # px/ms — 이보다 느리면 SLOW_GAIN
NORMAL_SPEED = 0.45    # px/ms — 보통 드래그 = 배율 1
FAST_SPEED = 2.0       # px/ms — 이보다 빠르면 최대 가속
MAX_EXTRA = 1.5        # 가속 100%일 때 최대 추가 배율 (1 + 1.5 = 2.5배)


def speed_gain(speed, accel=100):
    """드래그 속도(px/ms) → 감도 배율

    천천히(≤0.05) 0.4배 → 보통(0.45) 1배 → 빠르게(≥2.0) 최대 1+1.5×(가속%/100)배.
    구간마다 smoothstep으로 이어 붙여 속도가 바뀌어도 튀지 않음. 가속 0%면 늘 1배.
    """
    a = max(0.0, accel) / 100.0
    if a <= 0:
        return 1.0
    speed = max(0.0, float(speed))
    if speed <= NORMAL_SPEED:
        t = _smoothstep((speed - SLOW_SPEED) / (NORMAL_SPEED - SLOW_SPEED))
        slow = 1.0 - (1.0 - SLOW_GAIN) * min(1.0, a)   # 가속을 줄이면 느린 구간도 덜 느리게
        return slow + (1.0 - slow) * t
    t = _smoothstep((speed - NORMAL_SPEED) / (FAST_SPEED - NORMAL_SPEED))
    return 1.0 + MAX_EXTRA * a * t


def _smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def wl_unit(width, value_range=None):
    """W/L 한 픽셀이 바꾸는 값 — 드래그를 시작할 때 Width의 1/100 (좁은 창은 섬세하게)

    드래그 중에는 같은 값을 써야 Width가 커질수록 더 빨리 커지는(복리) 일이 없다.

    Width 400이면 4 (예전 고정값과 같음). 영상 값 범위를 알면 그 0.02%~2% 사이로 제한해
    너무 좁은 창에서 꿈쩍 않거나 넓은 창에서 튀지 않게 한다.
    """
    unit = max(float(width), 1e-6) / 100.0
    if value_range:
        span = max(float(value_range), 1e-6)
        unit = max(span * 0.0002, min(span * 0.02, unit))
    return unit


class Smoother:
    """남은 양을 보간 시간(tau) 동안 지수적으로 따라감 — 부드럽게 멈추는 무게감

    add(키, 양)으로 쌓고, step(dt_ms)가 이번 프레임에 적용할 양 {키: 값}을 돌려줌.
    tau가 0이면 쌓는 즉시 전부 내줌.
    """

    EPS = {"wc": 1e-4, "ww": 1e-4, "px": 0.05, "py": 0.05, "zoom": 1e-4}

    def __init__(self):
        self.pending = {}

    def add(self, key, amount):
        self.pending[key] = self.pending.get(key, 0.0) + amount

    def active(self):
        return any(abs(v) > self.EPS.get(k, 1e-4) for k, v in self.pending.items())

    def step(self, dt_ms, tau_ms):
        if tau_ms <= 0:
            out, self.pending = dict(self.pending), {}
            return out
        f = 1.0 - math.exp(-max(0.0, dt_ms) / tau_ms)
        out = {}
        for key, value in list(self.pending.items()):
            if abs(value) <= self.EPS.get(key, 1e-4):
                out[key] = value              # 아주 조금 남은 것은 한 번에 (끝없이 따라가지 않게)
                del self.pending[key]
                continue
            part = value * f
            out[key] = part
            self.pending[key] = value - part
        return out

    def flush(self):
        out, self.pending = dict(self.pending), {}
        return out

    def clear(self):
        self.pending = {}


class Fling:
    """Pan 관성: 놓을 때의 속도(px/ms)로 미끄러지다 마찰로 감속"""

    FRICTION_MS = 140.0     # 속도가 1/e로 줄어드는 시간 (2px/ms로 던지면 약 280px 미끄러짐)
    MIN_START = 0.35        # px/ms — 이보다 느리게 놓으면 관성 없음 (천천히 맞춘 위치는 그대로)
    STOP = 0.02             # px/ms — 이보다 느려지면 멈춤
    MAX_SPEED = 4.0         # px/ms

    def __init__(self):
        self.vx = self.vy = 0.0

    def start(self, vx, vy):
        speed = math.hypot(vx, vy)
        if speed < self.MIN_START:
            self.stop()
            return False
        if speed > self.MAX_SPEED:
            vx, vy = vx * self.MAX_SPEED / speed, vy * self.MAX_SPEED / speed
        self.vx, self.vy = vx, vy
        return True

    def active(self):
        return math.hypot(self.vx, self.vy) > self.STOP

    def step(self, dt_ms):
        """이번 프레임에 움직일 (dx, dy)"""
        if not self.active():
            self.stop()
            return 0.0, 0.0
        decay = math.exp(-dt_ms / self.FRICTION_MS)
        # 이 구간 동안의 이동량 = v × τ × (1 - e^(-dt/τ)) — 프레임 간격이 달라도 같은 거리
        k = self.FRICTION_MS * (1.0 - decay)
        dx, dy = self.vx * k, self.vy * k
        self.vx *= decay
        self.vy *= decay
        return dx, dy

    def stop(self):
        self.vx = self.vy = 0.0


class VelocityTracker:
    """최근 움직임의 속도(px/ms)를 부드럽게 추정 (놓을 때 관성 · 드래그 가속용)"""

    WINDOW_MS = 60.0

    def __init__(self):
        self.samples = []   # (t_ms, dx, dy)

    def reset(self):
        self.samples = []

    def add(self, t_ms, dx, dy):
        self.samples.append((t_ms, dx, dy))
        cutoff = t_ms - self.WINDOW_MS * 2
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def velocity(self, now_ms):
        recent = [(t, dx, dy) for t, dx, dy in self.samples if now_ms - t <= self.WINDOW_MS]
        if len(recent) < 2:
            return 0.0, 0.0
        span = max(8.0, now_ms - recent[0][0])
        return (sum(s[1] for s in recent[1:]) / span, sum(s[2] for s in recent[1:]) / span)


class WheelAccel:
    """휠 넘기기: 천천히 한 칸씩은 한 장, 빠르게 연달아 굴리면 가속

    트랙패드처럼 잘게 오는 값은 120(한 칸)이 모일 때마다 한 장.
    """

    FAST_MS = 70.0      # 칸 사이가 이보다 짧으면 '빠르게 굴리는 중'
    STREAK_START = 3    # 빠른 칸이 이만큼 이어져야 가속 시작 (한두 번 빠른 건 한 장씩)
    MAX_STEP = 6        # 한 칸에 최대 장 수

    def __init__(self):
        self.accum = 0.0
        self.last_ms = None
        self.streak = 0

    def feed(self, delta, now_ms, accelerate=True):
        """휠 값(angleDelta) → 이번에 넘길 장 수 (부호 포함, 위로 굴리면 +)"""
        notch = delta / 120.0
        if self.last_ms is not None and now_ms - self.last_ms < self.FAST_MS and abs(notch) >= 0.5:
            self.streak += 1
        elif self.last_ms is None or now_ms - self.last_ms > self.FAST_MS * 2:
            self.streak = 0
        self.last_ms = now_ms
        if (self.accum > 0) != (notch > 0) and self.accum:
            self.accum = 0.0                      # 방향을 바꾸면 모인 양을 버림
            self.streak = 0
        gain = 1.0
        if accelerate and self.streak >= self.STREAK_START and abs(notch) >= 0.5:   # 트랙패드는 가속 없음
            gain = min(self.MAX_STEP, 1.0 + (self.streak - self.STREAK_START + 1) * 0.75)
        self.accum += notch * gain
        steps = int(self.accum)
        self.accum -= steps
        return steps
