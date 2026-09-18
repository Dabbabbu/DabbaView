"""
DICOM 파일 로딩 및 시리즈 분류 엔진

- 폴더 로딩 시 메타데이터만 병렬로 읽고(stop_before_pixels=True)
- 픽셀 데이터는 뷰포트가 요청할 때 파일에서 lazy load
"""
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pydicom

from .geometry import build_series_geometry

_UNSET = object()


# DICOM일 수 있는 확장자 (확장자 없음 포함)
DICOM_EXTENSIONS = {'', '.dcm', '.dicom'}

# 픽셀 캐시에 보관할 최대 슬라이스 수
PIXEL_CACHE_SIZE = 100


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
        """픽셀 데이터를 포함한 전체 Dataset을 파일에서 다시 읽어 반환"""
        self.sort_slices()
        if index < 0 or index >= len(self.slices):
            return None
        return pydicom.dcmread(self.slices[index].filename, force=True)

    def _load_pixels(self, index):
        """디스크에서 픽셀을 읽어 Rescale 적용 (캐시 미사용)"""
        try:
            ds = self.get_full_dataset(index)
            if ds is None:
                return None
            arr = ds.pixel_array.astype(np.float64)

            # Rescale Slope/Intercept 적용 (CT 등)
            slope = float(getattr(ds, 'RescaleSlope', 1))
            intercept = float(getattr(ds, 'RescaleIntercept', 0))
            return arr * slope + intercept
        except Exception:
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

    def get_default_window(self):
        """기본 윈도우 센터/너비 반환"""
        if not self.slices:
            return 400, 2000  # 기본값

        ds = self.slices[0]
        wc = float(getattr(ds, 'WindowCenter', 400))
        ww = float(getattr(ds, 'WindowWidth', 2000))

        # WindowCenter/Width가 리스트인 경우 첫 번째 값 사용
        if hasattr(wc, '__iter__'):
            wc = float(wc) if not hasattr(wc, '__len__') else float(list(wc)[0])
        if hasattr(ww, '__iter__'):
            ww = float(ww) if not hasattr(ww, '__len__') else float(list(ww)[0])

        return wc, ww

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

    def _add_dataset(self, ds):
        series_uid = str(getattr(ds, 'SeriesInstanceUID', 'unknown'))
        if series_uid not in self.series_dict:
            self.series_dict[series_uid] = DicomSeries(series_uid)
        self.series_dict[series_uid].add_slice(ds)

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
        """디렉토리에서 DICOM 후보 파일 경로 수집 (확장자 사전 필터링)"""
        files = []
        if recursive:
            for root, _, filenames in os.walk(dirpath):
                for fn in filenames:
                    if is_candidate_file(fn):
                        files.append(os.path.join(root, fn))
        else:
            for fn in os.listdir(dirpath):
                path = os.path.join(dirpath, fn)
                if is_candidate_file(fn) and os.path.isfile(path):
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
        self.series_dict.clear()
        self.load_errors.clear()

        files = []
        for path in paths:
            if os.path.isdir(path):
                files.extend(self.collect_files(path, recursive))
            elif os.path.isfile(path):
                files.append(path)
        total = len(files)
        if total == 0:
            return 0

        loaded = 0
        done = 0
        with ThreadPoolExecutor(
                max_workers=max_workers or default_worker_count()) as ex:
            futures = {ex.submit(_read_metadata, f): f for f in files}
            # 결과 취합은 이 스레드에서만 하므로 series_dict에 락 불필요
            for future in as_completed(futures):
                done += 1
                try:
                    ds = future.result()
                    if ds is not None:
                        self._add_dataset(ds)
                        loaded += 1
                except Exception as e:
                    self.load_errors.append((futures[future], str(e)))

                if progress_callback:
                    progress_callback(done, total)

                if cancel_event is not None and cancel_event.is_set():
                    for f in futures:
                        f.cancel()
                    break

        # 모든 시리즈 정렬
        for series in self.series_dict.values():
            series.sort_slices()

        return loaded

    def get_series_list(self):
        """로드된 시리즈 목록 반환"""
        series_list = list(self.series_dict.values())
        # 시리즈 번호 또는 설명 순으로 정렬
        series_list.sort(key=lambda s: (s.study_date, s.description))
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
        return uids

    def get_series_by_uid(self, uid):
        """UID로 시리즈 조회"""
        return self.series_dict.get(uid)

    def clear(self):
        """모든 데이터 초기화"""
        self.series_dict.clear()
        self.load_errors.clear()
