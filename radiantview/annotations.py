"""
주석/측정 + Key Image 저장소

영상 키 = SOPInstanceUID (없으면 'SeriesInstanceUID#index').
뷰포트들이 하나의 저장소를 공유하므로 시리즈를 바꿨다 돌아와도 주석이 남고,
JSON 파일로 저장/불러오기 할 수 있다.
"""
import json

from PyQt5.QtCore import QObject, pyqtSignal


FILE_FORMAT = "radiantview-annotations"
FILE_VERSION = 1


def image_key(series, index):
    """영상을 식별하는 키 (SOPInstanceUID 우선)"""
    if series is None or not (0 <= index < series.num_slices):
        return None
    uid = str(getattr(series.slices[index], "SOPInstanceUID", "") or "")
    return uid or f"{series.series_uid}#{index}"


def _to_jsonable(value):
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "tolist"):  # numpy
        return value.tolist()
    return value


def _from_json_annotation(ann):
    ann = dict(ann)
    ann["pts"] = [tuple(p) for p in ann.get("pts", [])]
    return ann


class AnnotationStore(QObject):
    """영상별 주석 목록 + Key Image 집합"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = {}        # key → [annotation dict]
        self._key_images = {}   # key → {"series_uid", "index", "description"}

    # 주석
    def items(self, key):
        return self._items.get(key, []) if key else []

    def add(self, key, ann):
        if key:
            self._items.setdefault(key, []).append(ann)
            self.changed.emit()

    def remove_last(self, key):
        items = self._items.get(key)
        if items:
            items.pop()
            if not items:
                del self._items[key]
            self.changed.emit()
            return True
        return False

    def clear_image(self, key):
        if self._items.pop(key, None) is not None:
            self.changed.emit()

    def clear(self):
        self._items.clear()
        self.changed.emit()

    def count(self):
        return sum(len(v) for v in self._items.values())

    # Key Image
    def is_key_image(self, key):
        return key in self._key_images

    def toggle_key_image(self, series, index):
        """토글 후 새 상태(True=마킹됨) 반환"""
        key = image_key(series, index)
        if key is None:
            return False
        if key in self._key_images:
            del self._key_images[key]
            marked = False
        else:
            self._key_images[key] = {"series_uid": series.series_uid, "index": index,
                                     "description": series.description}
            marked = True
        self.changed.emit()
        return marked

    def key_images(self):
        """[(key, info)] 마킹한 순서대로"""
        return list(self._key_images.items())

    def clear_key_images(self):
        self._key_images.clear()
        self.changed.emit()

    # JSON
    def to_dict(self):
        return {
            "format": FILE_FORMAT,
            "version": FILE_VERSION,
            "annotations": {k: _to_jsonable(v) for k, v in self._items.items()},
            "key_images": _to_jsonable(self._key_images),
        }

    def save_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def load_json(self, path, merge=False):
        """반환: 불러온 주석 수. 형식이 다르면 ValueError"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") != FILE_FORMAT:
            raise ValueError("RadiantView 주석 파일이 아닙니다.")
        if not merge:
            self._items.clear()
            self._key_images.clear()
        count = 0
        for key, anns in data.get("annotations", {}).items():
            for ann in anns:
                self._items.setdefault(key, []).append(_from_json_annotation(ann))
                count += 1
        for key, info in data.get("key_images", {}).items():
            self._key_images[key] = info
        self.changed.emit()
        return count
