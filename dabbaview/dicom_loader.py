# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
DICOM 파일 로딩 및 시리즈 분류 엔진

- 폴더 로딩 시 메타데이터만 병렬로 읽고(stop_before_pixels=True)
- 픽셀 데이터는 뷰포트가 요청할 때 파일에서 lazy load
- DICOM이 아닌 의료영상(NIfTI, NRRD, MetaImage, NumPy, 이미지 시퀀스)은
  formats.readers가 메모리 시리즈(VolumeSeries)로 만들어 함께 목록에 넣음
- DICOM SEG는 시리즈가 아니라 세그멘테이션 오버레이, STL은 3D 메시로 따로 모음
"""
import gc
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pydicom

from .dicom_codecs import ensure_decoders
from .geometry import build_series_geometry

ensure_decoders()

_UNSET = object()


# DICOM일 수 있는 확장자 (확장자 없음 포함)
DICOM_EXTENSIONS = {'', '.dcm', '.dicom'}

# 픽셀 캐시에 보관할 최대 슬라이스 수
PIXEL_CACHE_SIZE = 100

# 파일 하나를 읽거나 디코딩하는 데 이보다 오래 걸리면 건너뜀 (네트워크 드라이브 멈춤, 손상 파일)
# 설정(불러오기)에서 바꿀 수 있음 → configure_limits
FILE_TIMEOUT_S = 10.0
# 네트워크 드라이브(SMB · NFS · WebDAV) · 클라우드 가상 드라이브(Google Drive G: 등)에서는
# 파일을 받는 데 시간이 걸리므로 한도를 늘리고 동시에 읽는 수를 줄임
# (32개를 한꺼번에 요청하면 모두 줄을 서다 10초 한도에 걸려 대부분 건너뛰던 문제)
NETWORK_TIMEOUT_S = 30.0
NETWORK_WORKERS = 6
# 다시 시도할 때는 한도를 이만큼 늘리고, 더 적게 동시에 읽음
RETRY_TIMEOUT_FACTOR = 2.0
RETRY_WORKERS = 3

# 클라우드(OneDrive·iCloud·Google Drive 등) 동기화 폴더에서 아직 이 컴퓨터에 받지 않은 파일.
# 읽는 순간 OS가 다운로드를 시작하는데, 동기화 앱이 멈춰 있으면 read()가 끝나지 않음.
CLOUD_WORKERS = 4                 # 동시에 받을 파일 수 (동기화 앱에 요청이 몰리지 않게)
CLOUD_TIMEOUT_S = 30.0            # 파일 하나 다운로드 대기 한도
# DICOM과 같은 폴더에 이미지(JPG·PNG)가 이만큼 넘게 섞여 있으면 이미지는 건너뜀
#  (논문 그림·캡처 자료가 섞인 폴더를 열면 수만 장을 읽다가 멈춘 것처럼 보임)
MIXED_IMAGE_LIMIT = 300
# 영상과 같은 폴더에 함께 있는 텍스트 메모 (Reading 기록으로 연결)
TEXT_REPORT_EXTENSIONS = (".txt",)
CLOUD_MAX_CONSECUTIVE_FAILS = 20  # 연속으로 이만큼 실패하면 나머지는 시도하지 않고 건너뜀 (재시도 가능)
# 시스템 메모리 사용률이 이만큼(%)을 넘으면 불러오기를 잠시 멈춤 (None: 끔)
MEMORY_PAUSE_PERCENT = 90.0
MEMORY_RESUME_MARGIN = 5.0        # 이만큼 내려가면 저절로 다시 시작
_SF_DATALESS = 0x40000000         # macOS: 내용이 로컬에 없는 파일 (File Provider)
_WIN_CLOUD_ATTRS = 0x00400000 | 0x00040000 | 0x00001000   # RECALL_ON_DATA_ACCESS/OPEN, OFFLINE


_CLOUD_STORAGE_RE = re.compile(r"^/Users/[^/]+/Library/CloudStorage/([^/]+)")
_CLOUD_NAMES = (("onedrive", "OneDrive"), ("googledrive", "Google Drive"), ("dropbox", "Dropbox"),
                ("box", "Box"), ("icloud", "iCloud Drive"))


def cloud_provider(path):
    """클라우드 동기화 폴더면 서비스 이름 (OneDrive, iCloud Drive, Dropbox, Google Drive, Box...)

    - macOS File Provider: /Users/*/Library/CloudStorage/<서비스-계정>/...
    - iCloud Drive: ~/Library/Mobile Documents/...
    - 예전 방식·Windows: 경로 구성요소가 OneDrive·Dropbox 등으로 시작
    """
    try:
        full = os.path.realpath(os.path.abspath(path))
    except (OSError, ValueError):
        return None
    m = _CLOUD_STORAGE_RE.match(full)
    if m:
        name = m.group(1).lower().replace(" ", "")
        for key, label in _CLOUD_NAMES:
            if name.startswith(key):
                return label
        return m.group(1).split("-")[0] or "클라우드"
    if "/Library/Mobile Documents/" in full:
        return "iCloud Drive"
    parts = [p.lower().replace(" ", "") for p in re.split(r"[\\/]", full) if p]
    for part in parts:
        for key, label in _CLOUD_NAMES[:3]:
            if part.startswith(key):
                return label
        if part in _GDRIVE_PARTS:
            return "Google Drive"
    return None


# Google Drive 가상 드라이브 (Windows G: 아래 "내 드라이브" · "My Drive" · "공유 드라이브")
_GDRIVE_PARTS = ("mydrive", "내드라이브", "shareddrives", "공유드라이브", "googledrive")
_NETWORK_FS = ("smbfs", "afpfs", "nfs", "webdav", "cifs", "ftp", "macfuse", "osxfuse",
               "fusefs", "fuse", "sshfs", "davfs", "9p")
_mount_cache = {"at": 0.0, "mounts": []}


def _mounts():
    """(마운트 위치, 파일시스템 종류) 목록 — 긴 위치부터 (macOS · Linux). 30초 동안 재사용"""
    now = time.monotonic()
    if now - _mount_cache["at"] < 30 and _mount_cache["at"]:
        return _mount_cache["mounts"]
    mounts = []
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():   # "//u@srv/share on /Volumes/share (smbfs, nodev, …)"
                m = re.match(r"^.+? on (.+) \(([^,)]+)", line)
                if m:
                    mounts.append((m.group(1), m.group(2).strip().lower()))
        elif os.path.exists("/proc/mounts"):
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mounts.append((parts[1].replace("\\040", " "), parts[2].lower()))
    except (OSError, subprocess.SubprocessError):
        pass
    mounts.sort(key=lambda m: -len(m[0]))
    _mount_cache.update(at=now, mounts=mounts)
    return mounts


def is_network_path(path):
    """네트워크 드라이브 · 원격 마운트인지 (파일을 읽을 때 네트워크를 거침)"""
    try:
        full = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    if sys.platform == "win32":
        if full.startswith("\\\\"):                       # \\서버\공유
            return True
        try:
            import ctypes
            root = os.path.splitdrive(full)[0] + "\\"
            return ctypes.windll.kernel32.GetDriveTypeW(root) == 4   # DRIVE_REMOTE
        except (AttributeError, OSError, ValueError):
            return False
    for mount_point, fstype in _mounts():
        if mount_point != "/" and (full == mount_point or full.startswith(mount_point.rstrip("/") + "/")):
            return any(fstype.startswith(kind) for kind in _NETWORK_FS)
    return False


def configure_limits(file_timeout=None, network_timeout=None, cloud_timeout=None,
                     cloud_max_fails=None, memory_pause=_UNSET):
    """설정 창의 값으로 한도를 바꿈 (None은 그대로). memory_pause=None이면 메모리 일시정지 끔"""
    global FILE_TIMEOUT_S, NETWORK_TIMEOUT_S, CLOUD_TIMEOUT_S, CLOUD_MAX_CONSECUTIVE_FAILS
    global MEMORY_PAUSE_PERCENT
    if file_timeout:
        FILE_TIMEOUT_S = float(file_timeout)
    if network_timeout:
        NETWORK_TIMEOUT_S = float(network_timeout)
    if cloud_timeout:
        CLOUD_TIMEOUT_S = float(cloud_timeout)
    if cloud_max_fails is not None:
        CLOUD_MAX_CONSECUTIVE_FAILS = int(cloud_max_fails)
    if memory_pause is not _UNSET:
        MEMORY_PAUSE_PERCENT = float(memory_pause) if memory_pause else None


RETRYABLE_PREFIXES = ("시간 초과", "클라우드", "읽기 오류")


def is_retryable(reason):
    """다시 시도하면 읽힐 수 있는 실패인지 (시간 초과 · 클라우드 · 디스크/네트워크 오류)
    — DICOM이 아닌 파일 · 손상된 파일은 다시 해도 같으므로 제외"""
    return str(reason).startswith(RETRYABLE_PREFIXES)


_CAT = shutil.which("cat")
_children = set()                 # 실행 중인 읽기 프로세스 (취소·중단·종료 시 kill)
_children_lock = threading.Lock()


def kill_fetch_processes():
    """진행 중인 클라우드 읽기 프로세스를 모두 종료 (취소, 연속 실패 중단, 앱 종료)"""
    with _children_lock:
        procs = list(_children)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


import atexit  # noqa: E402 - 위 함수를 등록하기 위해 여기서
atexit.register(kill_fetch_processes)


class CloudFetchError(OSError):
    """클라우드 동기화 앱이 파일을 내주지 못함 (실패 목록 단계: 클라우드 미다운로드)"""


def fetch_in_process(src, dest=None, timeout=None):
    """src 내용을 별도 프로세스(cat)로 끝까지 읽음 → 클라우드 파일이 이 컴퓨터로 다운로드됨

    dest가 있으면 그 파일로 복사. 시간 초과면 자식 프로세스를 kill하고 TimeoutError:
    파이썬 스레드는 OS의 read()에서 멈추면 끊을 수 없지만 프로세스는 강제로 끝낼 수 있음.
    """
    timeout = CLOUD_TIMEOUT_S if timeout is None else timeout
    if dest:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    if _CAT is None:   # cat이 없는 환경(Windows): 스레드에서 직접 (멈추면 스레드를 버림)
        if dest:
            shutil.copyfile(src, dest)
        else:
            with open(src, "rb") as f:
                while f.read(1 << 20):
                    pass
        return
    out = open(dest + ".part", "wb") if dest else subprocess.DEVNULL
    try:
        proc = subprocess.Popen([_CAT, src], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.PIPE)
        with _children_lock:
            _children.add(proc)
        try:
            _out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                pass   # 커널에서 못 빠져나온 프로세스는 두고 감 (우리 스레드는 계속 진행)
            raise TimeoutError(f"{timeout:.0f}초 안에 받지 못해 읽기 프로세스를 종료하고 건너뜀")
        finally:
            with _children_lock:
                _children.discard(proc)
    except BaseException:
        if dest:
            out.close()
            _silent_remove(dest + ".part")
        raise
    else:
        if dest:
            out.close()
    if proc.returncode != 0:
        if dest:
            _silent_remove(dest + ".part")
        message = err.decode("utf-8", "replace").strip().rsplit(": ", 1)[-1]
        message = message or f"읽기 실패 (종료 코드 {proc.returncode})"
        if is_cloud_placeholder(src):
            raise CloudFetchError(f"동기화 앱이 파일을 내주지 않음 — {message}")
        raise OSError(message)
    if dest:
        os.replace(dest + ".part", dest)


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def is_cloud_placeholder(path):
    """다운로드되지 않은 클라우드 파일인지 (stat만 하므로 다운로드를 일으키지 않음)"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if getattr(st, "st_flags", 0) & _SF_DATALESS:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _WIN_CLOUD_ATTRS)


def default_worker_count():
    """CPU 코어 기반 워커 수 (파일 I/O 대기가 섞이므로 코어의 2배, 최대 32)"""
    return min(32, (os.cpu_count() or 4) * 2)


def is_candidate_file(filename):
    """확장자로 DICOM 후보 파일인지 사전 판별"""
    if filename.startswith('.'):  # .DS_Store 등 숨김 파일
        return False
    if filename.upper() == 'DICOMDIR':  # 인덱스 파일, 영상 없음
        return False
    ext = os.path.splitext(filename)[1].lower()
    if ext in DICOM_EXTENSIONS:
        return True
    # UID 형태 파일명(예: 1.2.840.113619.2.55.3.1234)은 마지막 조각이 숫자
    return ext[1:].isdigit()


# 이보다 큰 태그 값(제조사 private 헤더, 큰 텍스트·바이너리)은 메모리에 두지 않고
# 실제로 쓸 때 파일에서 읽음 (pydicom defer_size). 수천 장 폴더의 메모리·캐시 크기를 줄임
METADATA_DEFER_SIZE = 2048


def _read_metadata(filepath):
    """픽셀 데이터 이전까지만 읽기. 영상이 없는 파일이면 None 반환"""
    ds = pydicom.dcmread(filepath, stop_before_pixels=True, force=True,
                         defer_size=METADATA_DEFER_SIZE)
    # stop_before_pixels라 PixelData 존재는 확인 불가 → 영상 필수 태그로 판별
    if 'Rows' not in ds or 'Columns' not in ds:
        return None
    return ds


def frame_index(ds):
    """멀티프레임 파일에서 펼친 프레임이면 프레임 번호, 아니면 None"""
    return getattr(ds, "_dv_frame", None)


def _functional_value(groups, sequence, keyword):
    for group in groups:
        seq = group.get(sequence) if group is not None else None
        if seq and keyword in seq[0]:
            return seq[0][keyword].value
    return None


def expand_frames(ds):
    """멀티프레임(Enhanced CT/MR, 초음파·XA 시네 등) Dataset → 프레임마다 Dataset

    Enhanced 객체는 프레임별 위치·방향·간격·Rescale·Window가 Functional Group에 있으므로
    일반 슬라이스처럼 최상위 태그로 옮김. 픽셀은 프레임 번호로 한 장씩 디코딩.
    """
    try:
        n = int(getattr(ds, "NumberOfFrames", 1) or 1)
    except (TypeError, ValueError):
        n = 1
    if n <= 1:
        return [ds]
    shared = ds.get("SharedFunctionalGroupsSequence")
    shared = shared[0] if shared else None
    per_frame = ds.get("PerFrameFunctionalGroupsSequence")
    frames = []
    for i in range(n):
        f = pydicom.Dataset(dict(ds._dict))   # 얕은 복사: 원본 요소는 공유, 태그 교체는 독립
        f.file_meta = getattr(ds, "file_meta", pydicom.dataset.FileMetaDataset())
        f.filename = getattr(ds, "filename", None)
        groups = [per_frame[i] if per_frame is not None and i < len(per_frame) else None, shared]
        values = {
            "ImagePositionPatient": _functional_value(groups, "PlanePositionSequence",
                                                      "ImagePositionPatient"),
            "ImageOrientationPatient": _functional_value(groups, "PlaneOrientationSequence",
                                                         "ImageOrientationPatient"),
            "PixelSpacing": _functional_value(groups, "PixelMeasuresSequence", "PixelSpacing"),
            "SliceThickness": _functional_value(groups, "PixelMeasuresSequence", "SliceThickness"),
            "RescaleSlope": _functional_value(groups, "PixelValueTransformationSequence",
                                              "RescaleSlope"),
            "RescaleIntercept": _functional_value(groups, "PixelValueTransformationSequence",
                                                  "RescaleIntercept"),
            "WindowCenter": _functional_value(groups, "FrameVOILUTSequence", "WindowCenter"),
            "WindowWidth": _functional_value(groups, "FrameVOILUTSequence", "WindowWidth"),
            "InstanceNumber": i + 1,
        }
        for keyword in ("PerFrameFunctionalGroupsSequence", "NumberOfFrames"):
            if keyword in f:
                del f[keyword]
        for keyword, value in values.items():
            if value is None:
                continue
            if keyword in f:
                del f[keyword]   # 원본과 공유하는 요소를 바꾸지 않도록 지우고 새로 만듦
            setattr(f, keyword, value)
        offsets = ds.get("GridFrameOffsetVector")   # RT Dose 등: 프레임 위치 = 기준 + 오프셋 × 법선
        if (values["ImagePositionPatient"] is None and offsets is not None and i < len(offsets)
                and "ImagePositionPatient" in ds and "ImageOrientationPatient" in ds):
            iop = [float(v) for v in ds.ImageOrientationPatient]
            normal = np.cross(iop[:3], iop[3:])
            ipp = np.array([float(v) for v in ds.ImagePositionPatient]) + float(offsets[i]) * normal
            del f["ImagePositionPatient"]
            f.ImagePositionPatient = [round(float(v), 4) for v in ipp]
        f._dv_frame = i
        f._dv_frames = n
        frames.append(f)
    return frames


def _decode_pixels(ds):
    """메타데이터 Dataset이 가리키는 파일에서 해당 프레임 픽셀만 디코딩"""
    from pydicom.pixels import pixel_array
    frame = frame_index(ds)
    path = ds.filename
    try:
        return pixel_array(path, index=frame)
    except Exception:  # noqa: BLE001 - 파일 메타 없는 옛 파일 등은 전체를 읽어 재시도
        full = pydicom.dcmread(path, force=True)
        if "TransferSyntaxUID" not in getattr(full, "file_meta", {}):
            full.file_meta = getattr(full, "file_meta", pydicom.dataset.FileMetaDataset())
            full.file_meta.TransferSyntaxUID = (pydicom.uid.ImplicitVRLittleEndian
                                                if full.original_encoding[0]
                                                else pydicom.uid.ExplicitVRLittleEndian)
        arr = full.pixel_array
        if frame is not None and arr.ndim >= 3 and arr.shape[0] == getattr(ds, "_dv_frames", 0):
            arr = arr[frame]
        return arr


def _short_error(exc):
    if isinstance(exc, CloudFetchError):
        return f"클라우드: {exc}"
    if isinstance(exc, TimeoutError):
        return f"시간 초과: {exc}"
    if isinstance(exc, OSError) and not isinstance(exc, (FileNotFoundError, IsADirectoryError)):
        # 네트워크 끊김 · 입출력 오류 등 — 다시 시도하면 읽힐 수 있음
        return f"읽기 오류: {(exc.strerror or str(exc) or type(exc).__name__)[:160]}"
    text = str(exc).strip().splitlines()
    text = text[0] if text else type(exc).__name__
    if "all available plugins" in text or "missing dependenc" in text or "plugins are missing" in text:
        return "압축 형식을 디코딩할 수 없음 (" + str(exc).strip().splitlines()[-1].strip()[:120] + ")"
    return f"{type(exc).__name__}: {text[:160]}"


def run_with_timeout(fn, arg, timeout):
    """fn(arg)를 데몬 스레드에서 실행해 timeout초까지만 기다림 (멈춘 스레드는 버림)"""
    box = {}

    def target():
        try:
            box["value"] = fn(arg)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
    thread = threading.Thread(target=target, daemon=True, name="dabbaview-decode")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{timeout:.0f}초 안에 끝나지 않아 건너뜀")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# 디코딩 실패 알림 (메인 창이 연결: fn(filename, frame, message), 아무 스레드에서나 호출됨)
decode_error_listeners = []


def _time_text(value):
    """DICOM TM 'HHMMSS.ffffff' → 비교할 수 있게 자릿수를 맞춘 글자 (없으면 '')"""
    value = str(value or "").strip().replace(":", "")
    if not value:
        return ""
    whole, _, frac = value.partition(".")
    return whole.ljust(6, "0")[:6] + "." + frac.ljust(6, "0")[:6]


def _number(ds, keyword):
    try:
        value = getattr(ds, keyword, None)
        return None if value is None or str(value).strip() == "" else float(value)
    except (TypeError, ValueError):
        return None


def _projections(slices):
    """슬라이스 위치를 첫 슬라이스 법선에 투영한 값(mm) — 위치 정보가 없거나 방향이 섞이면 None"""
    try:
        iop = [float(v) for v in slices[0].ImageOrientationPatient]
        normal = np.cross(iop[:3], iop[3:])
        if np.linalg.norm(normal) == 0:
            return None
        out = []
        for ds in slices:
            other = [float(v) for v in ds.ImageOrientationPatient]
            if abs(float(np.dot(np.cross(other[:3], other[3:]), normal))) < 0.99:
                return None                   # 방향이 다른 영상이 섞임 (3면 로컬라이저 등)
            out.append(float(np.dot([float(v) for v in ds.ImagePositionPatient], normal)))
        return out
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


def _time_of(ds):
    """시간 순서 값: TriggerTime(ms) → TemporalPositionIdentifier → AcquisitionTime → ContentTime"""
    for keyword in ("TriggerTime", "TemporalPositionIdentifier"):
        value = _number(ds, keyword)
        if value is not None:
            return (0, value, "")
    for keyword in ("AcquisitionTime", "ContentTime"):
        text = _time_text(getattr(ds, keyword, ""))
        if text:
            return (1, 0.0, text)
    return (2, 0.0, "")


def slice_order_keys(slices):
    """시리즈 안 영상 정렬 키 — 불러올 때마다 늘 같은 순서가 되게

    1. 시간 시리즈(cine · perfusion: 같은 위치가 여러 번 + 시간 태그가 다름)
       → 위치(찍은 순서) → 그 위치 안에서 TriggerTime · TemporalPositionIdentifier · AcquisitionTime
    2. InstanceNumber가 모두 있으면 → InstanceNumber (찍은 순서)
    3. SliceLocation이 모두 있으면 → SliceLocation
    4. 위치 정보(IPP)가 있으면 → 슬라이스 법선 방향 위치 (z만 쓰면 sagittal · coronal이 안 됨)
    5. 그 밖에는 시간
    어느 경우든 같은 값이면 InstanceNumber → SOPInstanceUID → 파일 이름으로 마무리 (병렬로 읽은 순서와 무관)
    """
    n = len(slices)
    inst = [_number(ds, "InstanceNumber") for ds in slices]
    tail = [((inst[i] is None, inst[i] or 0.0), str(getattr(ds, "SOPInstanceUID", "") or ""),
             str(getattr(ds, "filename", "") or ""), float(getattr(ds, "_dv_frame", 0) or 0))
            for i, ds in enumerate(slices)]
    times = [_time_of(ds) for ds in slices]
    proj = _projections(slices)
    if proj is not None:
        where = [round(p, 2) for p in proj]
        repeated = len(set(where)) < n
        if repeated:
            by_pos = {}
            for i, w in enumerate(where):
                by_pos.setdefault(w, []).append(i)
            timed = any(len({times[i] for i in idx}) > 1 for idx in by_pos.values())
            if timed:
                # 위치는 찍은 순서(그 위치의 가장 작은 InstanceNumber, 없으면 위치 값)대로
                def first_seen(idx):
                    nums = [inst[i] for i in idx if inst[i] is not None]
                    return min(nums) if nums else min(where[i] for i in idx)
                rank = {w: r for r, w in enumerate(sorted(by_pos, key=lambda w: (first_seen(by_pos[w]), w)))}
                return [(0, rank[where[i]], times[i], tail[i]) for i in range(n)]
    if all(v is not None for v in inst):
        return [(1, inst[i], times[i], tail[i]) for i in range(n)]
    locations = [_number(ds, "SliceLocation") for ds in slices]
    if all(v is not None for v in locations):
        return [(2, locations[i], times[i], tail[i]) for i in range(n)]
    if proj is not None:
        return [(3, round(proj[i], 3), times[i], tail[i]) for i in range(n)]
    return [(4, 0.0, times[i], tail[i]) for i in range(n)]


def series_order_key(series):
    """시리즈 순서: 검사 날짜 · 시각 → 시리즈를 찍은 때 → 시리즈 번호 → 설명 → UID (늘 같은 순서)"""
    number = series.series_number
    return (series.study_date, _time_text(series.study_time), series.study_uid or "",
            series.series_datetime, number is None, number if number is not None else 0,
            series.description, series.series_uid)


def _slice_key(ds):
    """같은 영상인지 가리는 키 (파일 · 프레임, 없으면 SOPInstanceUID)"""
    return (str(getattr(ds, "filename", "") or ""), getattr(ds, "_dv_frame", None),
            str(getattr(ds, "SOPInstanceUID", "") or ""))


class DicomSeries:
    """하나의 DICOM 시리즈를 나타내는 클래스

    slices에는 픽셀이 없는 메타데이터 Dataset이 들어 있음.
    픽셀이 포함된 전체 Dataset이 필요하면 get_full_dataset() 사용.
    """

    def __init__(self, series_uid, description="", modality=""):
        self.series_uid = series_uid
        self.description = description
        self.modality = modality
        self.slices = []  # list of pydicom Dataset (메타데이터만)
        self._sorted = False
        self._pixel_array_cache = OrderedDict()  # index -> ndarray (LRU)
        self._cache_lock = threading.Lock()
        self._geometry = _UNSET
        # 디코딩 실패는 (파일, 프레임) 단위로 기억 → 다시 그릴 때마다 디스크를 읽지 않음
        self.decode_errors = {}

    def add_slice(self, ds):
        self.slices.append(ds)
        self._sorted = False
        self._geometry = _UNSET
        with self._cache_lock:
            self._pixel_array_cache.clear()
        if not self.description and hasattr(ds, 'SeriesDescription'):
            self.description = str(ds.SeriesDescription)
        if not self.modality and hasattr(ds, 'Modality'):
            self.modality = str(ds.Modality)

    def sort_slices(self):
        """슬라이스를 늘 같은 순서로 (찍은 순서 기준, slice_order_keys 참고)"""
        if self._sorted:
            return
        try:
            keys = slice_order_keys(self.slices)
            order = sorted(range(len(self.slices)), key=lambda i: keys[i])
            self.slices[:] = [self.slices[i] for i in order]
        except Exception:                 # noqa: BLE001 - 태그가 이상해도 불러오기는 계속
            pass
        # 정렬로 인덱스가 바뀌므로 캐시 무효화
        with self._cache_lock:
            self._pixel_array_cache.clear()
        self._geometry = _UNSET
        self._sorted = True

    def spatial_order(self):
        """공간 순서(슬라이스 법선 방향)의 인덱스 — MPR · 3D 볼륨을 쌓을 때 (보기 순서와 다를 수 있음)"""
        self.sort_slices()
        proj = _projections(self.slices)
        if proj is None:
            return list(range(len(self.slices)))
        return sorted(range(len(self.slices)), key=lambda i: (round(proj[i], 3), i))

    @property
    def series_datetime(self):
        """시리즈를 찍은 때 'YYYYMMDDHHMMSS.ffffff' (SeriesDate/Time → AcquisitionDate/Time → ContentDate/Time)"""
        if not self.slices:
            return ""
        first = self.slices[0]
        for date_key, time_key in (("SeriesDate", "SeriesTime"), ("AcquisitionDate", "AcquisitionTime"),
                                   ("ContentDate", "ContentTime")):
            date = str(getattr(first, date_key, "") or "").strip()
            time_ = str(getattr(first, time_key, "") or "").strip()
            if date or time_:
                return f"{date or self.study_date}{_time_text(time_)}"
        return ""

    @property
    def geometry(self):
        """SeriesGeometry (공간 정보가 없으면 None). 크로스 레퍼런스용"""
        self.sort_slices()
        if self._geometry is _UNSET:
            self._geometry = build_series_geometry(self.slices)
        return self._geometry

    @property
    def num_slices(self):
        return len(self.slices)

    @property
    def patient_name(self):
        if self.slices and hasattr(self.slices[0], 'PatientName'):
            return str(self.slices[0].PatientName)
        return "Unknown"

    @property
    def study_description(self):
        if self.slices and hasattr(self.slices[0], 'StudyDescription'):
            return str(self.slices[0].StudyDescription)
        return ""

    @property
    def study_date(self):
        if self.slices and hasattr(self.slices[0], 'StudyDate'):
            return str(self.slices[0].StudyDate)
        return ""

    def _first_str(self, keyword):
        if self.slices:
            value = getattr(self.slices[0], keyword, None)
            if value is not None:
                return str(value).strip()
        return ""

    @property
    def patient_id(self):
        return self._first_str('PatientID')

    @property
    def study_uid(self):
        return self._first_str('StudyInstanceUID')

    @property
    def study_time(self):
        return self._first_str('StudyTime')

    @property
    def series_number(self):
        """SeriesNumber (없거나 숫자가 아니면 None)"""
        try:
            return int(float(self._first_str('SeriesNumber')))
        except ValueError:
            return None

    def get_full_dataset(self, index):
        """픽셀 데이터를 포함한 전체 Dataset을 파일에서 다시 읽어 반환

        멀티프레임 파일의 프레임이면 파일 전체(모든 프레임)가 들어 있음.
        """
        self.sort_slices()
        if index < 0 or index >= len(self.slices):
            return None
        return pydicom.dcmread(self.slices[index].filename, force=True)

    @staticmethod
    def _error_key(ds):
        return (str(getattr(ds, "filename", "")), frame_index(ds))

    def decode_error(self, index):
        """이 슬라이스를 표시할 수 없는 이유 (정상이면 None)"""
        if 0 <= index < len(self.slices):
            return self.decode_errors.get(self._error_key(self.slices[index]))
        return None

    def _load_pixels(self, index):
        """디스크에서 픽셀을 읽어 Rescale 적용 (캐시 미사용). 실패하면 이유를 기억하고 None"""
        self.sort_slices()
        if index < 0 or index >= len(self.slices):
            return None
        ds = self.slices[index]
        key = self._error_key(ds)
        if key in self.decode_errors:
            return None
        try:
            arr = run_with_timeout(_decode_pixels, ds, FILE_TIMEOUT_S)
            arr = np.asarray(arr).astype(np.float64)
            # Rescale Slope/Intercept 적용 (CT 등, 멀티프레임은 프레임별 값)
            slope = float(getattr(ds, 'RescaleSlope', 1) or 1)
            intercept = float(getattr(ds, 'RescaleIntercept', 0) or 0)
            return arr * slope + intercept
        except Exception as e:  # noqa: BLE001 - 해당 영상만 건너뛰고 계속
            message = _short_error(e)
            if message.startswith("압축 형식"):
                ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
                name = getattr(ts, "name", "") if ts is not None else ""
                message = f"압축 형식({name or '알 수 없음'})을 디코딩할 수 없거나 데이터가 손상됨"
            self.decode_errors[key] = message
            for listener in list(decode_error_listeners):
                try:
                    listener(key[0], key[1], message)
                except Exception:  # noqa: BLE001
                    pass
            return None

    def get_pixel_array(self, index):
        """특정 슬라이스의 픽셀 데이터를 numpy 배열로 반환 (lazy load + LRU 캐시)"""
        self.sort_slices()
        if index < 0 or index >= len(self.slices):
            return None

        with self._cache_lock:
            if index in self._pixel_array_cache:
                self._pixel_array_cache.move_to_end(index)
                return self._pixel_array_cache[index]

        arr = self._load_pixels(index)
        if arr is None:
            return None

        with self._cache_lock:
            self._pixel_array_cache[index] = arr
            while len(self._pixel_array_cache) > PIXEL_CACHE_SIZE:
                self._pixel_array_cache.popitem(last=False)
        return arr

    def get_all_pixel_arrays(self, max_workers=None):
        """모든 슬라이스 픽셀을 병렬로 읽어 리스트로 반환 (MPR/3D 볼륨 구성용)

        캐시를 거치지 않으므로 시리즈가 커도 캐시 내용이 밀려나지 않음.
        """
        self.sort_slices()
        n = len(self.slices)
        if n == 0:
            return []
        with ThreadPoolExecutor(
                max_workers=max_workers or default_worker_count()) as ex:
            return list(ex.map(self._load_pixels, range(n)))

    def get_volume_array(self):
        """(Z, H, W) 볼륨. 디코딩에 실패한 슬라이스는 0으로 채움 (크기가 다르면 ValueError)"""
        arrays = self.get_all_pixel_arrays()
        good = [a for a in arrays if a is not None]
        if not good:
            raise ValueError("픽셀을 읽을 수 있는 슬라이스가 없습니다.")
        shape = good[0].shape
        if any(a.shape != shape for a in good):
            raise ValueError("슬라이스 크기가 서로 다릅니다.")
        return np.stack([a if a is not None else np.zeros(shape) for a in arrays])

    def get_default_window(self):
        """기본 윈도우 센터/너비 반환"""
        if not self.slices:
            return 400, 2000  # 기본값

        ds = self.slices[0]

        def first_float(value):
            # WindowCenter/Width는 다중값(MultiValue)인 경우가 흔함 → 첫 값 사용
            if value is None:
                return None
            if not isinstance(value, (str, bytes)) and hasattr(value, '__len__'):
                value = value[0] if len(value) else None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        wc = first_float(getattr(ds, 'WindowCenter', None))
        ww = first_float(getattr(ds, 'WindowWidth', None))
        if wc is not None and ww is not None and ww > 0:
            return wc, ww

        # 윈도 태그가 없으면 (MR 등) 중간 슬라이스 분포로 자동 설정
        arr = self.get_pixel_array(len(self.slices) // 2)
        if arr is not None and arr.size:
            low, high = np.percentile(arr, [1, 99])
            if high > low:
                return float((low + high) / 2), float(high - low)
        return 400, 2000

    def get_dicom_tags(self, index=0):
        """DICOM 태그 정보를 딕셔너리로 반환"""
        self.sort_slices()
        if index < 0 or index >= len(self.slices):
            return {}

        ds = self.slices[index]
        tags = {}
        for elem in ds:
            if elem.tag.group == 0x7FE0:  # 픽셀 데이터 제외
                continue
            tag_name = elem.keyword if elem.keyword else f"({elem.tag.group:04X},{elem.tag.element:04X})"
            try:
                tags[tag_name] = str(elem.value)[:200]  # 길이 제한
            except Exception:
                tags[tag_name] = "<binary>"
        return tags

    def get_pixel_spacing(self, index=0):
        """픽셀 간격 (mm) 반환"""
        if index < 0 or index >= len(self.slices):
            return 1.0, 1.0
        ds = self.slices[index]
        if hasattr(ds, 'PixelSpacing'):
            return float(ds.PixelSpacing[0]), float(ds.PixelSpacing[1])
        return 1.0, 1.0

    def __repr__(self):
        return (f"DicomSeries('{self.description}', "
                f"modality='{self.modality}', slices={self.num_slices})")


class DicomLoader:
    """DICOM 파일/폴더를 로드하고 시리즈로 분류"""

    def __init__(self):
        self.series_dict = {}  # series_uid -> DicomSeries
        self.load_errors = []
        self._running = {}           # 워커 이름 → (파일, 시작 시각): 멈춘 파일 표시용
        self._running_lock = threading.Lock()
        self.phase = ""              # 진행 단계 설명 (파일 목록 확인 / 메타데이터 읽기)
        self.cache_thread = None     # 메타데이터 캐시를 뒤에서 저장하는 스레드 (테스트에서 join)
        self.cached_count = 0  # 캐시에서 바로 읽은 파일 수 (상태 표시용)
        self.retry = False     # 실패한 파일 다시 읽기: 한도를 늘리고 천천히, 연속 실패로 멈추지 않음
        self.remote = False    # 네트워크 · 클라우드 폴더였는지 (설명용)
        # 일시정지: 메모리가 한도를 넘거나(자동) 사용자가 누르면 새 파일을 읽지 않고 기다림
        self._pause = threading.Event()
        self.pause_reason = ""       # "memory" | "user" | ""
        self._memory_ignored = False  # '그래도 계속'을 누른 뒤에는 메모리로 다시 멈추지 않음
        self._reset_extras()

    # ─── 일시정지 ───
    @property
    def paused(self):
        return self._pause.is_set()

    def pause(self, reason="user"):
        self.pause_reason = reason
        self._pause.set()

    def resume(self, ignore_memory=False):
        """다시 시작. ignore_memory=True면 이번 불러오기에서는 메모리로 다시 멈추지 않음"""
        if ignore_memory or self.pause_reason == "memory":
            self._memory_ignored = self._memory_ignored or ignore_memory
        self.pause_reason = ""
        self._pause.clear()

    def _check_memory(self):
        """메모리 한도를 넘으면 멈추고, 충분히 내려가면 저절로 다시 시작 (메모리로 멈춘 경우만)"""
        limit = MEMORY_PAUSE_PERCENT
        if not limit or self._memory_ignored:
            return
        from .memory_monitor import system_percent
        percent = system_percent()
        if percent is None:
            return
        if not self.paused and percent >= limit:
            self.memory_percent = percent
            gc.collect()           # 읽는 동안 꺼 둔 순환 GC를 한 번 돌려 되찾을 수 있는 만큼 되찾음
            self.pause("memory")
        elif self.paused and self.pause_reason == "memory" and percent < limit - MEMORY_RESUME_MARGIN:
            self.resume()
        if self.paused and self.pause_reason == "memory":
            self.memory_percent = percent

    def _reset_extras(self):
        self.volume_masks = {}       # series_uid -> 함께 읽은 마스크 (NumPy _mask 등)
        self.label_candidates = []   # 라벨맵으로 보이는 VolumeSeries (다른 시리즈 오버레이 후보)
        self.segmentations = []      # DICOM SEG 파일 경로
        self.meshes = []             # STL 경로
        self.skipped_images = 0      # DICOM과 섞여 있어 건너뛴 이미지 장수
        self.text_files = []         # 같은 폴더에서 발견한 텍스트 파일

    @property
    def extra_count(self):
        return len(self.segmentations) + len(self.meshes)

    def _add_dataset(self, ds):
        from .formats.seg_reader import is_segmentation
        if is_segmentation(ds):
            self.segmentations.append(str(getattr(ds, "filename", "")))
            return
        series_uid = str(getattr(ds, 'SeriesInstanceUID', 'unknown'))
        if series_uid not in self.series_dict:
            self.series_dict[series_uid] = DicomSeries(series_uid)
        for frame in expand_frames(ds):
            self.series_dict[series_uid].add_slice(frame)

    def load_file(self, filepath):
        """단일 DICOM 파일 로드 (메타데이터만)"""
        try:
            ds = _read_metadata(filepath)
            if ds is None:
                return False
            self._add_dataset(ds)
            return True
        except Exception as e:
            self.load_errors.append((filepath, str(e)))
            return False

    def _expand_archives(self, paths, recursive, progress_callback=None, cancel_event=None):
        """압축파일(ZIP · 7z · RAR · TAR …)은 풀어서 풀린 폴더를 대신 연다. 폴더 안의 압축파일도 함께"""
        from . import archives
        out = []
        self.extracted_archives = []
        for path in paths:
            if os.path.isfile(path) and archives.is_archive(path):
                found = [path]
            else:
                out.append(path)
                found = archives.find_archives(path, recursive) if os.path.isdir(path) else []
            for archive in found:
                name = os.path.basename(archive)
                self.phase = f"압축 푸는 중: {name}"

                def step(done, total, _file, cb=progress_callback):
                    if cb:
                        cb(done, total)
                try:
                    dest = archives.extract(
                        archive, step,
                        cancelled=lambda: cancel_event is not None and cancel_event.is_set())
                except archives.ArchiveError as exc:
                    self.load_errors.append((archive, str(exc)))
                    continue
                except OSError as exc:
                    self.load_errors.append((archive, f"압축을 풀 수 없습니다: {exc}"))
                    continue
                out.append(dest)
                self.extracted_archives.append((archive, dest))
                if progress_callback:
                    progress_callback(0, 0)
        return out

    def collect_files(self, dirpath, recursive=True):
        """디렉토리에서 불러올 파일 경로 수집 (DICOM 후보 + 지원하는 다른 형식)

        텍스트 파일은 영상이 아니므로 따로 모아 둔다(self.text_files).
        """
        from .formats.readers import file_kind
        files = []

        def wanted(fn):
            return is_candidate_file(fn) or (
                not fn.startswith('.') and file_kind(fn) != "dicom")

        def take(path, fn):
            if fn.lower().endswith(TEXT_REPORT_EXTENSIONS) and not fn.startswith('.'):
                self.text_files.append(path)
            elif wanted(fn):
                files.append(path)
        if recursive:
            for root, _, filenames in os.walk(dirpath):
                for fn in filenames:
                    take(os.path.join(root, fn), fn)
        else:
            for fn in os.listdir(dirpath):
                path = os.path.join(dirpath, fn)
                if os.path.isfile(path):
                    take(path, fn)
        return files

    def load_directory(self, dirpath, recursive=True, progress_callback=None,
                       cancel_event=None, max_workers=None):
        """디렉토리에서 모든 DICOM 파일을 병렬로 로드

        progress_callback(current, total)은 호출한 스레드에서 실행됨.
        cancel_event(threading.Event)가 set되면 남은 작업을 취소하고 반환.
        """
        return self.load_paths([dirpath], recursive, progress_callback,
                               cancel_event, max_workers)

    def load_paths(self, paths, recursive=True, progress_callback=None,
                   cancel_event=None, max_workers=None, placeholder_policy=None):
        """placeholder_policy(info) → "download" | "skip" | "cancel" | ("copy", 대상 폴더)

        info = {"provider": 클라우드 이름 또는 None, "placeholders": 받지 않은 파일 수,
                "total": 읽을 DICOM 수, "bytes": 전체 크기, "roots": 연 폴더들}
        클라우드 동기화 폴더이거나 받지 않은 파일이 있을 때만 호출됨.
        """
        self.opened_paths = list(paths)   # 로컬 복사본으로 열면 복사본 경로로 바뀜 (최근 목록용)
        self.copied_to = None
        """폴더/파일 경로 목록을 병렬로 로드 (드래그 앤 드롭 다중 선택 등)

        직접 지정한 파일은 확장자와 관계없이 시도하고,
        폴더 안의 파일은 확장자 사전 필터링 적용.
        """
        from .formats.readers import file_kind
        self.series_dict.clear()
        self.load_errors.clear()
        self._reset_extras()
        self.phase = "파일 목록 확인 중"
        if progress_callback:
            progress_callback(0, 0)   # 전체 개수를 모르는 단계 (폴더 탐색)
        paths = self._expand_archives(paths, recursive, progress_callback, cancel_event)
        self.phase = "파일 목록 확인 중"

        from . import cache
        files = []
        cached = 0
        pending = {}   # 캐시에 없거나 바뀐 폴더 → (서명, DICOM 후보 파일)
        for path in paths:
            if os.path.isdir(path):
                dir_files = self.collect_files(path, recursive)
                dicom_files = [f for f in dir_files if file_kind(f) == "dicom"]
                signature = cache.folder_signature(dicom_files)
                data = cache.load_metadata(path, recursive, signature) if dicom_files else None
                if data is not None and data.get("pydicom") == pydicom.__version__:
                    # 같은 폴더를 다시 열었고 파일이 그대로 → 메타데이터 파싱 생략
                    for ds in data["datasets"]:
                        self._add_dataset(ds)
                    self.load_errors.extend(data.get("errors", []))
                    cached += len(data["datasets"])
                    files.extend(f for f in dir_files if file_kind(f) != "dicom")
                    continue
                pending[path] = (signature, dicom_files)
                files.extend(dir_files)
            elif os.path.isfile(path):
                if path.lower().endswith(TEXT_REPORT_EXTENSIONS):
                    self.text_files.append(path)      # 텍스트는 영상으로 읽지 않음 (⚠ 오류 방지)
                else:
                    files.append(path)
        self.cached_count = cached
        # 형식별 분류: DICOM은 병렬 메타데이터 읽기, 나머지는 형식별 reader
        others = [f for f in files if file_kind(f) != "dicom"]
        files = [f for f in files if file_kind(f) == "dicom"]
        # DICOM과 이미지가 한 폴더에 섞여 있으면(논문 그림·캡처 자료) 이미지는 건너뜀 —
        # 수만 장을 읽느라 멈춘 것처럼 보이던 문제. 이미지만 보려면 그 폴더만 열면 된다.
        pictures = [f for f in others if file_kind(f) == "image"]
        self.skipped_images = 0
        if files and len(pictures) > MIXED_IMAGE_LIMIT:
            picture_set = set(pictures)
            others = [f for f in others if f not in picture_set]
            self.skipped_images = len(pictures)
            self.load_errors.append((
                os.path.dirname(pictures[0]),
                f"이미지 {len(pictures):,}장을 건너뜀 — DICOM {len(files):,}개와 같은 폴더에 있습니다"
                f" (이미지만 보려면 그 폴더만 따로 여세요)"))
        total = len(files) + len(others)
        if total == 0:
            for series in self.series_dict.values():
                series.sort_slices()
            return cached

        parsed, failed = {}, {}   # 폴더 캐시 저장용
        # 클라우드에만 있는 파일은 따로: 받을지 물어보고, 받더라도 천천히·실패가 이어지면 중단
        # ★ 이미지·NIfTI 등도 읽으면 다운로드가 일어나므로 '읽기 전에' 다 함께 물어본다.
        cloud = [f for f in files if is_cloud_placeholder(f)]
        cloud_set = set(cloud)
        local = [f for f in files if f not in cloud_set] if cloud else files
        cloud_others = [f for f in others if is_cloud_placeholder(f)]
        self.cloud_placeholders = len(cloud) + len(cloud_others)
        provider = next((cloud_provider(p) for p in paths if cloud_provider(p)), None)
        self.cloud_provider = provider
        policy = "download"
        if (cloud or cloud_others or provider) and placeholder_policy is not None:
            size = 0
            for f in files + others:
                try:
                    size += os.lstat(f).st_size   # lstat은 다운로드를 일으키지 않음
                except OSError:
                    pass
            policy = placeholder_policy({"provider": provider,
                                         "placeholders": len(cloud) + len(cloud_others),
                                         "total": total, "bytes": size,
                                         "roots": list(paths)})
            if policy == "cancel":
                if cancel_event is not None:
                    cancel_event.set()
                return cached
        if policy == "skip" and cloud_others:
            others = [f for f in others if f not in set(cloud_others)]
            total = len(files) + len(others)
        loaded = self._load_other_formats(others, progress_callback, total, cancel_event) + cached
        done = len(others)
        if cancel_event is not None and cancel_event.is_set():
            return loaded
        cloud_options = dict(max_workers=CLOUD_WORKERS, timeout=CLOUD_TIMEOUT_S + 10,
                             max_consecutive_failures=CLOUD_MAX_CONSECUTIVE_FAILS)
        # 네트워크 드라이브 · 클라우드 폴더(이미 받은 파일 포함): 한도를 늘리고 동시에 적게 읽음
        self.remote = bool(provider) or any(is_network_path(p) for p in paths)
        local_options = dict(max_workers=max_workers)
        if self.remote:
            local_options = dict(max_workers=min(max_workers or NETWORK_WORKERS, NETWORK_WORKERS),
                                 timeout=max(NETWORK_TIMEOUT_S, FILE_TIMEOUT_S))
        if self.retry:
            # 다시 시도: 한도를 늘리고 천천히 — 연속 실패로 나머지를 포기하지 않음
            base = local_options.get("timeout", FILE_TIMEOUT_S)
            local_options = dict(max_workers=RETRY_WORKERS, timeout=base * RETRY_TIMEOUT_FACTOR)
            cloud_options = dict(max_workers=RETRY_WORKERS,
                                 timeout=(CLOUD_TIMEOUT_S + 10) * RETRY_TIMEOUT_FACTOR,
                                 max_consecutive_failures=None)
        if isinstance(policy, tuple) and policy[0] == "copy":
            # 로컬로 복사한 뒤 복사본을 읽음 (받지 않은 파일은 프로세스로 받아 복사)
            mapping = self._copy_targets(files, paths, policy[1])
            self.copied_to = policy[1]
            self.opened_paths = sorted({mapping[f][1] for f in files})
            pending.clear()   # 원래 폴더 캐시에 복사본 경로를 저장하지 않음

            def copy_reader(path):
                dest = mapping[path][0]
                try:
                    same = os.path.getsize(dest) == os.lstat(path).st_size
                except OSError:
                    same = False
                if not same:   # 이전에 복사해 둔 같은 크기 파일은 다시 받지 않음
                    fetch_in_process(path, dest)
                return _read_metadata(dest)
            groups = [(local, dict(local_options, reader=copy_reader,
                                   phase="로컬로 복사하며 읽는 중"))]
            if cloud:
                groups.append((cloud, dict(cloud_options, reader=copy_reader,
                                           phase="클라우드에서 받아 로컬로 복사하는 중")))
            cloud = []
        else:
            groups = [(local, dict(local_options, phase=("다시 읽는 중" if self.retry else
                                                         "네트워크 폴더에서 읽는 중" if self.remote
                                                         else "메타데이터 읽는 중")))]
        if cloud and policy == "download":
            fetch_timeout = CLOUD_TIMEOUT_S * (RETRY_TIMEOUT_FACTOR if self.retry else 1)

            def download_reader(path):
                fetch_in_process(path, timeout=fetch_timeout)   # 별도 프로세스가 받는 동안 멈추면 kill
                return _read_metadata(path)
            groups.append((cloud, dict(cloud_options, reader=download_reader,
                                       phase="클라우드에서 다운로드하며 읽는 중")))
        elif cloud:
            for path in cloud:
                done += 1
                failed[path] = "클라우드: 이 컴퓨터에 다운로드되지 않은 파일이라 건너뜀"
                self.load_errors.append((path, failed[path]))
            if progress_callback:
                progress_callback(done, total)
        cancelled = False
        results = (item for group, options in groups
                   for item in self._read_group(group, cancel_event, options))
        # 읽는 동안 순환 GC를 멈춤: Dataset이 수천 개 쌓일수록 전체 수집이 점점 오래 걸려
        # 후반이 느려짐. 끝나면 한 번만 수집 (실측 7200개: GC 0.54초 → 0.27초)
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            for path, ds, error in results:
                done += 1
                if error is None:
                    parsed[path] = ds
                    if ds is not None:
                        try:
                            self._add_dataset(ds)
                            loaded += 1
                        except Exception as e:  # noqa: BLE001 - 이상한 태그 값 등: 이 파일만 건너뜀
                            error = _short_error(e)
                if error is not None:
                    failed[path] = error
                    self.load_errors.append((path, error))
                if progress_callback:
                    progress_callback(done, total)
        finally:
            if gc_was_enabled:
                gc.enable()
            gc.collect()
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True

        # 다 읽은 폴더는 메타데이터를 캐시에 저장 (다음에 열 때 파싱 생략).
        # 수천 장이면 압축·기록에 몇 초 걸리므로 불러오기 완료를 기다리게 하지 않고 뒤에서 저장
        to_save = []
        if not cancelled:
            for folder, (signature, dicom_files) in pending.items():
                if not dicom_files:
                    continue
                datasets = [parsed[f] for f in dicom_files if parsed.get(f) is not None]
                errors = [(f, failed[f]) for f in dicom_files if f in failed]
                if any(is_retryable(e) for _f, e in errors):
                    continue   # 일시적인 멈춤일 수 있으니 다음에 다시 읽도록 캐시하지 않음
                if datasets:
                    to_save.append((folder, recursive, signature, datasets, errors))
        if to_save:
            def save():
                for folder, rec, signature, datasets, errors in to_save:
                    cache.save_metadata(folder, rec, signature, datasets, errors,
                                        extra={"pydicom": pydicom.__version__})
            self.cache_thread = threading.Thread(target=save, daemon=True,
                                                 name="dabbaview-metadata-cache")
            self.cache_thread.start()

        # 모든 시리즈 정렬
        for series in self.series_dict.values():
            series.sort_slices()

        return loaded

    @staticmethod
    def _copy_targets(files, roots, dest_dir):
        """원본 파일 → (복사할 경로, 복사본 최상위 폴더). 연 폴더 이름 아래 같은 구조로"""
        mapping = {}
        abs_roots = [os.path.abspath(r) for r in roots]
        for f in files:
            full = os.path.abspath(f)
            root = next((r for r in abs_roots if os.path.isdir(r)
                         and (full + os.sep).startswith(r.rstrip(os.sep) + os.sep)), None)
            if root is None:   # 파일을 직접 연 경우
                root = os.path.dirname(full)
            top = os.path.join(dest_dir, os.path.basename(root.rstrip(os.sep)) or "DICOM")
            mapping[f] = (os.path.join(top, os.path.relpath(full, root)), top)
        return mapping

    def _forget_running(self, path):
        with self._running_lock:
            for name, (running_path, _t0) in list(self._running.items()):
                if running_path == path:
                    del self._running[name]

    def _mark_slow(self, path, key="image"):
        """지금 읽고 있는 이미지 한 장을 기록 — 멈춤 감지(slow_files)에 쓰임"""
        with self._running_lock:
            self._running[key] = (path, time.monotonic())

    def _clear_slow(self, key="image"):
        with self._running_lock:
            self._running.pop(key, None)

    def slow_files(self, min_seconds=0.0):
        """지금 읽는 중인 파일 중 min_seconds 이상 걸리는 것 [(경로, 경과 초)]"""
        now = time.monotonic()
        with self._running_lock:
            items = list(self._running.values())
        return sorted(((f, now - t0) for f, t0 in items if now - t0 >= min_seconds),
                      key=lambda x: -x[1])

    def _read_group(self, files, cancel_event, options):
        options = dict(options)
        self.phase = options.pop("phase", "메타데이터 읽는 중")
        return self._read_all(files, cancel_event=cancel_event, **options)

    def _read_all(self, files, max_workers=None, cancel_event=None, timeout=None,
                  reader=None, max_consecutive_failures=None):
        """파일 메타데이터를 데몬 스레드들로 읽으며 (경로, ds, 오류) 를 차례로 넘김

        - 한 파일이 timeout초를 넘기면 오류(시간 초과)로 넘기고 다음으로 진행
          (그 스레드는 버리고 새 워커를 띄움 — 네트워크 드라이브·손상 파일 멈춤 대비)
        - cancel_event가 set되면 0.2초 안에 반환 (멈춘 읽기를 기다리지 않음)
        - max_consecutive_failures: 연속 실패(오류·시간 초과)가 이만큼이면 남은 파일은 읽지 않고
          오류로 넘김 (클라우드 동기화 앱이 응답하지 않을 때 수천 개를 하나씩 기다리지 않도록)
        """
        timeout = FILE_TIMEOUT_S if timeout is None else timeout
        reader = reader or _read_metadata
        tasks = queue.Queue()
        for f in files:
            tasks.put(f)
        results = queue.Queue()
        stop = threading.Event()
        counter = [0]

        def work():
            name = threading.current_thread().name
            while not stop.is_set():
                while self._pause.is_set() and not stop.is_set():
                    time.sleep(0.2)   # 일시정지: 새 파일을 읽지 않고 기다림 (읽던 것은 마저 읽음)
                if stop.is_set():
                    return
                try:
                    path = tasks.get_nowait()
                except queue.Empty:
                    return
                with self._running_lock:
                    self._running[name] = (path, time.monotonic())
                try:
                    results.put((path, reader(path), None))
                except Exception as e:  # noqa: BLE001 - 손상 파일: 이 파일만 건너뜀
                    results.put((path, None, _short_error(e)))
                finally:
                    with self._running_lock:
                        self._running.pop(name, None)

        def spawn():
            counter[0] += 1
            threading.Thread(target=work, daemon=True,
                             name=f"dabbaview-read-{counter[0]}").start()

        for _ in range(min(len(files), max_workers or default_worker_count())):
            spawn()
        remaining = set(files)
        skipped = set()
        fails = 0
        next_memory_check = 0.0
        try:
            while remaining:
                if cancel_event is not None and cancel_event.is_set():
                    kill_fetch_processes()
                    return
                now = time.monotonic()
                if now >= next_memory_check:
                    next_memory_check = now + 1.0
                    self._check_memory()
                # 도착한 결과를 한 번에 최대 100개씩 모아 처리 (파일마다 깨어나지 않도록)
                batch = []
                try:
                    batch.append(results.get(timeout=0.2))
                    while len(batch) < 100:
                        batch.append(results.get_nowait())
                except queue.Empty:
                    pass
                finished = []
                for path, ds, error in batch:
                    if path not in skipped:
                        remaining.discard(path)
                        finished.append((path, ds, error))
                for slow_path, elapsed in self.slow_files(timeout):
                    if slow_path in remaining:
                        skipped.add(slow_path)
                        remaining.discard(slow_path)
                        self._forget_running(slow_path)   # 멈춘 스레드는 버림 (데몬)
                        spawn()   # 멈춘 워커 대신
                        finished.append((slow_path, None,
                                         f"시간 초과: {elapsed:.0f}초 넘게 응답이 없어 건너뜀"))
                for item in finished:
                    fails = fails + 1 if item[2] is not None else 0
                    yield item
                if max_consecutive_failures and fails >= max_consecutive_failures and remaining:
                    stop.set()
                    kill_fetch_processes()   # 이미 받는 중인 것도 끝냄
                    reason = (f"클라우드: 연속 {fails}개 파일을 받지 못해 나머지는 시도하지 않고 건너뜀 "
                              "(동기화 앱 상태를 확인하거나 Finder에서 '다운로드' 후 다시 열기)")
                    for path in sorted(remaining):
                        yield path, None, reason
                    remaining.clear()
        finally:
            stop.set()

    def _load_other_formats(self, paths, progress_callback=None, total=0, cancel_event=None):
        """NIfTI/NRRD/MetaImage/NumPy/이미지/STL. 불러온 항목 수 반환

        이미지는 폴더별로 한 시리즈가 되는데 장수가 많으면 오래 걸리므로
        한 장 읽을 때마다 진행률을 알리고, 취소하면 그 자리에서 멈춘다.
        """
        from .formats.readers import (file_kind, read_volume_file, read_image_sequence,
                                      is_mask_image_folder)
        cancelled = lambda: cancel_event is not None and cancel_event.is_set()   # noqa: E731
        loaded = 0
        images = {}
        self.phase = "파일 형식 확인 중"
        for i, path in enumerate(paths):
            if cancelled():
                return loaded
            kind = file_kind(path)
            if kind == "mesh":
                self.meshes.append(path)
                loaded += 1
            elif kind == "image":
                images.setdefault(os.path.dirname(path), []).append(path)
            else:
                self.phase = "볼륨 파일 읽는 중"
                try:
                    loaded += self._add_volumes(read_volume_file(path))
                except Exception as e:  # noqa: BLE001 - 형식 오류는 목록에만 기록
                    self.load_errors.append((path, str(e)))
            if progress_callback and total:
                progress_callback(i + 1, total)
        done = len(paths)
        shot = sum(len(v) for v in images.values())
        for folder, files in images.items():
            if cancelled():
                return loaded
            if is_mask_image_folder(folder):
                continue  # images/ 시리즈의 마스크로 함께 읽음
            self.phase = f"이미지 읽는 중 ({os.path.basename(folder) or folder})"
            step = [0]

            def tick(_path, _n=len(files)):     # 이미지 한 장을 읽을 때마다
                step[0] += 1
                self._mark_slow(_path)
                if progress_callback and total and shot:
                    progress_callback(min(total, done + step[0] * done // max(1, shot)), total)
                return not cancelled()
            try:
                loaded += self._add_volumes(read_image_sequence(files, progress=tick))
            except Exception as e:  # noqa: BLE001
                self.load_errors.append((folder, str(e)))
            self._clear_slow()
        self.phase = ""
        return loaded

    def _add_volumes(self, volumes):
        for lv in volumes:
            self.series_dict[lv.series.series_uid] = lv.series
            if lv.mask is not None:
                self.volume_masks[lv.series.series_uid] = lv.mask
            if lv.label_candidate:
                self.label_candidates.append(lv.series)
        return len(volumes)

    def get_series_list(self):
        """로드된 시리즈 목록 반환"""
        series_list = list(self.series_dict.values())
        # 시리즈 번호 또는 설명 순으로 정렬
        # 날짜·설명이 같아도 순서가 항상 같도록 시리즈 번호·UID로 마무리 (캐시/파싱 무관)
        series_list.sort(key=series_order_key)
        return series_list

    def merge(self, other):
        """다른 로더의 시리즈를 추가 (이미 있는 시리즈는 기존 객체 유지)

        반환: other의 시리즈 UID 목록 (get_series_list 순서)
        """
        uids = []
        for series in other.get_series_list():
            mine = self.series_dict.setdefault(series.series_uid, series)
            if mine is not series:
                # 같은 시리즈의 나머지 영상 (실패한 파일을 다시 읽은 경우) → 빠진 것만 더함
                have = {_slice_key(ds) for ds in mine.slices}
                added = [ds for ds in series.slices if _slice_key(ds) not in have]
                for ds in added:
                    mine.add_slice(ds)
                if added:
                    mine.sort_slices()
            uids.append(series.series_uid)
        self.load_errors.extend(other.load_errors)
        self.volume_masks.update(other.volume_masks)
        self.label_candidates.extend(other.label_candidates)
        self.segmentations.extend(other.segmentations)
        self.meshes.extend(other.meshes)
        return uids

    def get_series_by_uid(self, uid):
        """UID로 시리즈 조회"""
        return self.series_dict.get(uid)

    def clear(self):
        """모든 데이터 초기화"""
        self.series_dict.clear()
        self.load_errors.clear()
        self._reset_extras()
