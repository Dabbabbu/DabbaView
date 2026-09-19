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
import os
import queue
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
FILE_TIMEOUT_S = 10.0


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


def _read_metadata(filepath):
    """픽셀 데이터 이전까지만 읽기. 영상이 없는 파일이면 None 반환"""
    ds = pydicom.dcmread(filepath, stop_before_pixels=True, force=True)
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
    if isinstance(exc, TimeoutError):
        return f"시간 초과: {exc}"
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
        """슬라이스를 위치 순서로 정렬"""
        if self._sorted:
            return
        try:
            self.slices.sort(
                key=lambda s: float(s.InstanceNumber)
                if hasattr(s, 'InstanceNumber') else 0
            )
            # ImagePositionPatient가 있으면 슬라이스 법선 방향 위치로 정렬
            # (z만 쓰면 Sagittal/Coronal 시리즈는 z가 모두 같아 정렬되지 않음)
            if all(hasattr(s, 'ImagePositionPatient') for s in self.slices):
                normal = np.array([0.0, 0.0, 1.0])
                iop = getattr(self.slices[0], 'ImageOrientationPatient', None)
                if iop is not None and len(iop) == 6:
                    n = np.cross([float(v) for v in iop[:3]],
                                 [float(v) for v in iop[3:]])
                    if np.linalg.norm(n) > 0:
                        normal = n
                self.slices.sort(
                    key=lambda s: float(np.dot(
                        [float(v) for v in s.ImagePositionPatient], normal))
                )
        except (TypeError, ValueError, IndexError):
            pass
        # 정렬로 인덱스가 바뀌므로 캐시 무효화
        with self._cache_lock:
            self._pixel_array_cache.clear()
        self._geometry = _UNSET
        self._sorted = True

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
        self.cached_count = 0  # 캐시에서 바로 읽은 파일 수 (상태 표시용)
        self._reset_extras()

    def _reset_extras(self):
        self.volume_masks = {}       # series_uid -> 함께 읽은 마스크 (NumPy _mask 등)
        self.label_candidates = []   # 라벨맵으로 보이는 VolumeSeries (다른 시리즈 오버레이 후보)
        self.segmentations = []      # DICOM SEG 파일 경로
        self.meshes = []             # STL 경로

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

    @staticmethod
    def collect_files(dirpath, recursive=True):
        """디렉토리에서 불러올 파일 경로 수집 (DICOM 후보 + 지원하는 다른 형식)"""
        from .formats.readers import file_kind
        files = []

        def wanted(fn):
            return is_candidate_file(fn) or (
                not fn.startswith('.') and file_kind(fn) != "dicom")
        if recursive:
            for root, _, filenames in os.walk(dirpath):
                for fn in filenames:
                    if wanted(fn):
                        files.append(os.path.join(root, fn))
        else:
            for fn in os.listdir(dirpath):
                path = os.path.join(dirpath, fn)
                if wanted(fn) and os.path.isfile(path):
                    files.append(path)
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
                   cancel_event=None, max_workers=None):
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
                files.append(path)
        self.cached_count = cached
        # 형식별 분류: DICOM은 병렬 메타데이터 읽기, 나머지는 형식별 reader
        others = [f for f in files if file_kind(f) != "dicom"]
        files = [f for f in files if file_kind(f) == "dicom"]
        total = len(files) + len(others)
        if total == 0:
            for series in self.series_dict.values():
                series.sort_slices()
            return cached

        loaded = self._load_other_formats(others, progress_callback, total) + cached
        done = len(others)
        if cancel_event is not None and cancel_event.is_set():
            return loaded
        parsed, failed = {}, {}   # 폴더 캐시 저장용
        self.phase = "메타데이터 읽는 중"
        cancelled = False
        for path, ds, error in self._read_all(files, max_workers, cancel_event):
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
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True

        # 다 읽은 폴더는 메타데이터를 캐시에 저장 (다음에 열 때 파싱 생략)
        if not cancelled:
            for folder, (signature, dicom_files) in pending.items():
                if not dicom_files:
                    continue
                datasets = [parsed[f] for f in dicom_files if parsed.get(f) is not None]
                errors = [(f, failed[f]) for f in dicom_files if f in failed]
                if any(e.startswith("시간 초과") for _f, e in errors):
                    continue   # 일시적인 멈춤일 수 있으니 다음에 다시 읽도록 캐시하지 않음
                if datasets:
                    cache.save_metadata(folder, recursive, signature, datasets, errors,
                                        extra={"pydicom": pydicom.__version__})

        # 모든 시리즈 정렬
        for series in self.series_dict.values():
            series.sort_slices()

        return loaded

    def slow_files(self, min_seconds=0.0):
        """지금 읽는 중인 파일 중 min_seconds 이상 걸리는 것 [(경로, 경과 초)]"""
        now = time.monotonic()
        with self._running_lock:
            items = list(self._running.values())
        return sorted(((f, now - t0) for f, t0 in items if now - t0 >= min_seconds),
                      key=lambda x: -x[1])

    def _read_all(self, files, max_workers=None, cancel_event=None, timeout=None,
                  reader=None):
        """파일 메타데이터를 데몬 스레드들로 읽으며 (경로, ds, 오류) 를 차례로 넘김

        - 한 파일이 timeout초를 넘기면 오류(시간 초과)로 넘기고 다음으로 진행
          (그 스레드는 버리고 새 워커를 띄움 — 네트워크 드라이브·손상 파일 멈춤 대비)
        - cancel_event가 set되면 0.2초 안에 반환 (멈춘 읽기를 기다리지 않음)
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
        try:
            while remaining:
                if cancel_event is not None and cancel_event.is_set():
                    return
                try:
                    path, ds, error = results.get(timeout=0.2)
                except queue.Empty:
                    path = None
                if path is not None and path not in skipped:
                    remaining.discard(path)
                    yield path, ds, error
                for path, elapsed in self.slow_files(timeout):
                    if path in remaining:
                        skipped.add(path)
                        remaining.discard(path)
                        spawn()   # 멈춘 워커 대신
                        yield path, None, f"시간 초과: {elapsed:.0f}초 넘게 응답이 없어 건너뜀"
        finally:
            stop.set()

    def _load_other_formats(self, paths, progress_callback=None, total=0):
        """NIfTI/NRRD/MetaImage/NumPy/이미지/STL. 불러온 항목 수 반환"""
        from .formats.readers import (file_kind, read_volume_file, read_image_sequence,
                                      is_mask_image_folder)
        loaded = 0
        images = {}
        for i, path in enumerate(paths):
            kind = file_kind(path)
            if kind == "mesh":
                self.meshes.append(path)
                loaded += 1
            elif kind == "image":
                images.setdefault(os.path.dirname(path), []).append(path)
            else:
                try:
                    loaded += self._add_volumes(read_volume_file(path))
                except Exception as e:  # noqa: BLE001 - 형식 오류는 목록에만 기록
                    self.load_errors.append((path, str(e)))
            if progress_callback and total:
                progress_callback(i + 1, total)
        for folder, files in images.items():
            if is_mask_image_folder(folder):
                continue  # images/ 시리즈의 마스크로 함께 읽음
            try:
                loaded += self._add_volumes(read_image_sequence(files))
            except Exception as e:  # noqa: BLE001
                self.load_errors.append((folder, str(e)))
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
        series_list.sort(key=lambda s: (s.study_date, s.description,
                                        s.series_number if s.series_number is not None else -1,
                                        s.series_uid))
        return series_list

    def merge(self, other):
        """다른 로더의 시리즈를 추가 (이미 있는 시리즈는 기존 객체 유지)

        반환: other의 시리즈 UID 목록 (get_series_list 순서)
        """
        uids = []
        for series in other.get_series_list():
            self.series_dict.setdefault(series.series_uid, series)
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
