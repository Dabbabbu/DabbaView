# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""오픈소스 세그멘테이션 모델 실행 (TotalSegmentator, nnU-Net, REST API)

TotalSegmentator·nnU-Net은 PyTorch(수 GB)가 필요해서 앱 안에 넣지 않고,
DabbaView 전용 Python 가상환경(앱 데이터 폴더/ai/model-env)에 pip로 설치한 뒤
명령줄 프로그램으로 실행한다. 입력은 현재 시리즈를 NIfTI(.nii.gz)로 저장해 넘기고,
결과 라벨 NIfTI를 다시 읽어 세그멘테이션 오버레이로 쓴다.

- Settings → AI → '모델 실행 Python'을 지정하면 그 환경(예: conda)을 대신 사용
- 취소하면 실행 중인 프로세스를 종료
- 서버로 보내는 것은 픽셀 볼륨(NIfTI)뿐, 환자 이름·ID 등 DICOM 정보는 보내지 않음
"""
import base64
import colorsys
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

import numpy as np

from . import data_dir, nifti
from .export import Cancelled

PIP_MODELS = {
    "totalseg": {"name": "TotalSegmentator", "package": "TotalSegmentator",
                 "module": "totalsegmentator", "cli": "TotalSegmentator",
                 "size": "PyTorch 포함 약 2–3 GB + 처음 실행 시 모델 가중치 약 1–2 GB"},
    "nnunet": {"name": "nnU-Net v2", "package": "nnunetv2", "module": "nnunetv2",
               "cli": "nnUNetv2_predict_from_modelfolder",
               "size": "PyTorch 포함 약 2–3 GB (사전학습 모델은 따로 준비)"},
}

# TotalSegmentator 과제 (버전에 따라 구조 수가 다를 수 있어 이름은 설치된 패키지에서 읽음)
TOTALSEG_TASKS = [
    ("total", "CT: 전신 해부학 구조 (total)"),
    ("total_mr", "MR: 전신 해부학 구조 (total_mr)"),
]
DEVICES = [("auto", "자동"), ("cpu", "CPU"), ("mps", "Apple GPU (MPS)"), ("gpu", "NVIDIA GPU")]


class ModelError(Exception):
    pass


def distinct_colors(n, seed=0.13):
    """구조마다 구별되는 색 (황금각 색상환)"""
    colors = []
    for i in range(n):
        h = (seed + i * 0.61803398875) % 1.0
        s = 0.55 + 0.35 * ((i * 7) % 3) / 2
        v = 0.95 - 0.25 * ((i * 5) % 2)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        colors.append((int(r * 255), int(g * 255), int(b * 255)))
    return colors


# ─── Python 환경 ───

# 앱 번들(py2app)이 자기 Python용으로 설정한 변수 - 외부 Python에 넘어가면 그 Python이 깨짐
_BUNDLE_VARS = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE",
                "PYTHONDONTWRITEBYTECODE", "RESOURCEPATH", "ARGVZERO", "EXECUTABLEPATH",
                "__PYVENV_LAUNCHER__")


def clean_env(extra=None):
    """외부 Python·모델 프로그램에 줄 환경 변수 (번들 Python 설정 제거)"""
    env = {k: v for k, v in os.environ.items() if k not in _BUNDLE_VARS}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    for extra_dir in ("/opt/homebrew/bin", "/usr/local/bin"):
        if extra_dir not in env["PATH"].split(os.pathsep):
            env["PATH"] += os.pathsep + extra_dir
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def _python_version(path):
    try:
        out = subprocess.run([path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=20, env=clean_env())
        major, minor = (int(v) for v in out.stdout.strip().split("."))
        return major, minor
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_base_python():
    """가상환경을 만들 시스템 Python (3.9 이상, 앱 번들 안의 Python은 제외)"""
    candidates = [os.environ.get("DABBAVIEW_BASE_PYTHON", "")]
    for name in ("python3.12", "python3.11", "python3.10", "python3"):
        candidates.append(shutil.which(name) or "")
    candidates += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                   "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
                   "/usr/bin/python3"]
    if sys.platform.startswith("win"):
        candidates += [shutil.which("py") or ""]
    seen = set()
    for path in candidates:
        if not path or path in seen or ".app/Contents" in path or not os.path.exists(path):
            continue
        seen.add(path)
        version = _python_version(path)
        if version and (3, 9) <= version < (3, 14):
            return path
    return None


class ModelRuntime:
    """모델 실행 환경: 지정한 Python 또는 DabbaView 전용 가상환경"""

    def __init__(self, python_override="", env_dir=None):
        self.python_override = (python_override or "").strip()
        self.env_dir = env_dir or os.path.join(data_dir(), "model-env")

    def _env_python(self):
        sub = "Scripts" if sys.platform.startswith("win") else "bin"
        exe = "python.exe" if sys.platform.startswith("win") else "python"
        return os.path.join(self.env_dir, sub, exe)

    def python(self):
        if self.python_override and os.path.exists(self.python_override):
            return self.python_override
        path = self._env_python()
        return path if os.path.exists(path) else None

    def describe(self):
        py = self.python()
        if py is None:
            return "모델 환경 없음 — 설치하면 자동으로 만듭니다"
        version = _python_version(py)
        v = f" (Python {version[0]}.{version[1]})" if version else ""
        return ("지정한 Python: " if py == self.python_override else "전용 환경: ") + py + v

    def bin(self, name):
        py = self.python()
        if py is None:
            return None
        folder = os.path.dirname(py)
        for candidate in (name, name + ".exe"):
            path = os.path.join(folder, candidate)
            if os.path.exists(path):
                return path
        return shutil.which(name, path=folder)

    def has_module(self, module):
        py = self.python()
        if py is None:
            return False
        try:
            out = subprocess.run(
                [py, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"],
                capture_output=True, timeout=30, env=clean_env())
            return out.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def package_version(self, package):
        py = self.python()
        if py is None:
            return None
        try:
            out = subprocess.run([py, "-c", f"import importlib.metadata as m; print(m.version({package!r}))"],
                                 capture_output=True, text=True, timeout=30, env=clean_env())
            return out.stdout.strip() or None if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def ensure_env(self, log, cancelled):
        if self.python() is not None:
            return self.python()
        base = find_base_python()
        if base is None:
            raise ModelError("Python 3.9 이상이 필요합니다. python.org에서 설치하거나 "
                             "Settings → AI → '모델 실행 Python'에 사용할 Python을 지정하세요.")
        log(f"가상환경 만들기: {self.env_dir} (기반 {base})")
        run_process([base, "-m", "venv", self.env_dir], log, cancelled)
        py = self._env_python()
        run_process([py, "-m", "pip", "install", "-U", "pip"], log, cancelled)
        return py

    def install(self, package, log, cancelled):
        py = self.ensure_env(log, cancelled)
        log(f"pip install -U {package}")
        run_process([py, "-m", "pip", "install", "-U", package], log, cancelled,
                    progress=None)


# ─── 프로세스 실행 (출력 한 줄씩, 취소 시 종료) ───

_PERCENT = re.compile(r"(\d{1,3})%")


def run_process(cmd, log, cancelled, env=None, cwd=None, progress=None):
    """명령 실행. 출력은 log(line)으로, 'NN%'가 보이면 progress(fraction). 실패하면 ModelError"""
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env if env is not None else clean_env(), cwd=cwd)
    except OSError as e:
        raise ModelError(f"실행하지 못했습니다: {cmd[0]} ({e})") from e
    lines = queue.Queue()

    def reader():
        buf = b""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)   # tqdm 진행 표시는 \r 로 갱신
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    lines.put(part.decode("utf-8", "replace"))
        if buf.strip():
            lines.put(buf.decode("utf-8", "replace"))
        lines.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    tail = []
    while True:
        if cancelled():
            proc.kill()
            proc.wait(5)
            raise Cancelled()
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            break
        tail = (tail + [line])[-15:]
        log(line)
        if progress is not None:
            m = _PERCENT.search(line)
            if m:
                progress(min(100, int(m.group(1))) / 100.0)
    rc = proc.wait()
    if rc != 0:
        raise ModelError(f"{os.path.basename(str(cmd[0]))} 실패 (종료 코드 {rc}):\n" + "\n".join(tail[-6:]))
    return rc


# ─── 결과 읽기 ───

def _read_label_volume(path, shape):
    array, _affine = nifti.load(path)
    if array.shape != tuple(shape):
        if array.shape[::-1] == tuple(shape):
            array = array.transpose(2, 1, 0)
        else:
            raise ModelError(f"결과 크기 {array.shape}가 영상 {tuple(shape)}와 다릅니다.")
    return np.clip(np.rint(array), 0, 255).astype(np.uint8)


def _save_input(path, volume):
    data = volume.array
    if np.allclose(data, np.round(data)) and data.min() >= -32768 and data.max() <= 32767:
        data = data.astype(np.int16)   # CT HU는 정수: 파일 크기 절반
    else:
        data = data.astype(np.float32)
    nifti.save(path, data, volume.affine_ras, "DabbaView")


# ─── TotalSegmentator ───

def totalseg_class_map(runtime, task):
    py = runtime.python()
    code = ("import json\nfrom totalsegmentator.map_to_binary import class_map\n"
            f"print(json.dumps({{int(k): v for k, v in class_map[{task!r}].items()}}))")
    try:
        out = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=120,
                             env=clean_env())
        if out.returncode == 0:
            return {int(k): v for k, v in json.loads(out.stdout.strip().splitlines()[-1]).items()}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return {}


def run_totalsegmentator(runtime, volume, task="total", fast=False, device="auto",
                         log=print, progress=None, cancelled=lambda: False):
    """→ (라벨 배열 (k,row,col) uint8, {번호: 구조 이름})"""
    cli = runtime.bin(PIP_MODELS["totalseg"]["cli"])
    if cli is None:
        raise ModelError("TotalSegmentator가 설치되어 있지 않습니다 (Models 탭에서 설치).")
    with tempfile.TemporaryDirectory(prefix="dabbaview-totalseg-") as tmp:
        inp = os.path.join(tmp, "image.nii.gz")
        out = os.path.join(tmp, "segmentation.nii.gz")
        if progress:
            progress(0.02, "입력 NIfTI 저장")
        _save_input(inp, volume)
        cmd = [cli, "-i", inp, "-o", out, "--ml", "-ta", task]
        if fast:
            cmd.append("--fast")
        if device and device != "auto":
            cmd += ["-d", device]
        run_process(cmd, log, cancelled,
                    progress=(lambda f: progress(0.05 + 0.9 * f, "TotalSegmentator 실행 중"))
                    if progress else None)
        if not os.path.exists(out):
            raise ModelError("TotalSegmentator가 결과 파일을 만들지 않았습니다.")
        mask = _read_label_volume(out, volume.shape)
    names = totalseg_class_map(runtime, task)
    return mask, names


# ─── nnU-Net ───

def nnunet_labels(model_folder):
    """모델 폴더(또는 상위)의 dataset.json → {번호: 이름}"""
    for folder in (model_folder, os.path.dirname(model_folder.rstrip(os.sep))):
        path = os.path.join(folder, "dataset.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    labels = json.load(f).get("labels", {})
            except (OSError, ValueError):
                return {}
            out = {}
            for name, value in labels.items():
                if isinstance(value, list):
                    value = value[-1]
                if isinstance(value, str) and value.isdigit():   # 옛 형식 {"1": "liver"}
                    name, value = value, int(name) if name.isdigit() else value
                try:
                    if int(value) > 0:
                        out[int(value)] = str(name)
                except (TypeError, ValueError):
                    continue
            return out
    return {}


def run_nnunet(runtime, volume, model_folder, folds="0", device="auto", log=print,
               progress=None, cancelled=lambda: False):
    cli = runtime.bin(PIP_MODELS["nnunet"]["cli"])
    if cli is None:
        raise ModelError("nnU-Net v2가 설치되어 있지 않습니다 (Models 탭에서 설치).")
    if not model_folder or not os.path.isdir(model_folder):
        raise ModelError("nnU-Net 사전학습 모델 폴더를 Settings → AI에서 지정하세요 "
                         "(예: .../Dataset001_X/nnUNetTrainer__nnUNetPlans__3d_fullres).")
    with tempfile.TemporaryDirectory(prefix="dabbaview-nnunet-") as tmp:
        in_dir, out_dir = os.path.join(tmp, "in"), os.path.join(tmp, "out")
        os.makedirs(in_dir)
        os.makedirs(out_dir)
        _save_input(os.path.join(in_dir, "case_0000.nii.gz"), volume)
        fold_args = folds.replace(",", " ").split() if folds and folds.strip() != "all" else ["all"]
        cmd = [cli, "-i", in_dir, "-o", out_dir, "-m", model_folder, "-f", *fold_args]
        if device and device != "auto":
            cmd += ["-device", {"gpu": "cuda"}.get(device, device)]
        env = clean_env()
        env.setdefault("nnUNet_raw", tmp)
        env.setdefault("nnUNet_preprocessed", tmp)
        env.setdefault("nnUNet_results", os.path.dirname(os.path.dirname(model_folder.rstrip(os.sep))))
        run_process(cmd, log, cancelled, env=env,
                    progress=(lambda f: progress(0.05 + 0.9 * f, "nnU-Net 실행 중")) if progress else None)
        out = os.path.join(out_dir, "case.nii.gz")
        if not os.path.exists(out):
            found = [f for f in os.listdir(out_dir) if f.endswith(".nii.gz")]
            if not found:
                raise ModelError("nnU-Net이 결과 파일을 만들지 않았습니다.")
            out = os.path.join(out_dir, found[0])
        mask = _read_label_volume(out, volume.shape)
    return mask, nnunet_labels(model_folder)


# ─── REST API (원격 추론) ───

REST_PROTOCOL = """요청: POST <URL>  (multipart/form-data)
  image  = 볼륨 NIfTI (.nii.gz, RAS affine, 픽셀 값은 CT HU 등 원래 값)
  params = JSON {"shape": [k, row, col], "spacing": [Δk, Δrow, Δcol], "modality": "CT"}
  헤더 Authorization: Bearer <토큰> (지정한 경우)
응답 (둘 중 하나)
  ① 라벨 NIfTI 바이트 (Content-Type: application/gzip 또는 octet-stream),
     구조 이름은 선택 헤더 X-Labels: {"1": "liver", ...}
  ② JSON {"mask": "<base64 .nii.gz>", "labels": {"1": "liver", ...}}"""


def run_rest(url, token, volume, modality="", log=print, timeout=600):
    from .monai_label import _multipart
    if not url:
        raise ModelError("REST API 주소를 Settings → AI에서 지정하세요.")
    data = volume.array
    buf = nifti.to_bytes(data.astype(np.float32), volume.affine_ras, "DabbaView")
    import gzip
    body, ctype = _multipart(
        {"params": json.dumps({"shape": list(volume.shape), "spacing": list(volume.spacing),
                               "modality": modality})},
        {"image": ("image.nii.gz", gzip.compress(buf, compresslevel=3))})
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": ctype})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    log(f"POST {url} ({len(body) / 1e6:.1f} MB)")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            header_labels = resp.headers.get("X-Labels")
    except urllib.error.HTTPError as e:
        raise ModelError(f"서버 오류 {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ModelError(f"서버에 연결하지 못했습니다: {e}") from e
    labels = {}
    if "json" in content_type:
        try:
            doc = json.loads(payload)
            payload = base64.b64decode(doc["mask"])
            labels = doc.get("labels") or {}
        except (ValueError, KeyError) as e:
            raise ModelError("JSON 응답에 'mask'(base64 NIfTI)가 없습니다.") from e
    elif header_labels:
        try:
            labels = json.loads(header_labels)
        except ValueError:
            labels = {}
    with tempfile.NamedTemporaryFile(suffix=".nii.gz", delete=False) as f:
        f.write(payload)
        path = f.name
    try:
        mask = _read_label_volume(path, volume.shape)
    finally:
        os.remove(path)
    log(f"응답: 라벨 {len(np.unique(mask)) - 1}개")
    return mask, {int(k): str(v) for k, v in labels.items() if str(k).isdigit()}
