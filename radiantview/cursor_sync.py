"""
크로스 레퍼런스 (Sync Cursor) 컨트롤러

한 뷰에서 지정한 환자 좌표(mm)를 같은 Frame of Reference를 공유하는
같은 환자의 다른 뷰로 전파.

참여 뷰 인터페이스:
  reference_point_selected(object) 시그널, series 속성, sync_geometry(),
  set_reference_point(point), clear_reference_point(),
  set_sync_cursor_enabled(bool)
"""
from PyQt5.QtCore import QObject, pyqtSignal


class CursorSyncController(QObject):

    # 전파 결과 (연동된 뷰 수, 좌표계가 달라 제외된 뷰 수) - 상태바 표시용
    synced = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._views = []
        self._enabled = False
        self._broadcasting = False

    def add_view(self, view):
        self._views.append(view)
        view.reference_point_selected.connect(
            lambda point, src=view: self._broadcast(src, point))
        view.set_sync_cursor_enabled(self._enabled)

    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        self._enabled = enabled
        for view in self._views:
            view.set_sync_cursor_enabled(enabled)

    @staticmethod
    def is_linked(source, target):
        """같은 환자 + 같은 Frame of Reference 인지"""
        src_geom, dst_geom = source.sync_geometry(), target.sync_geometry()
        if src_geom is None or dst_geom is None:
            return False
        if not src_geom.is_linkable_with(dst_geom):
            return False
        return source.series.patient_id == target.series.patient_id

    def _broadcast(self, source, point):
        if not self._enabled or self._broadcasting:
            return
        self._broadcasting = True  # 수신 뷰가 다시 시그널을 보내도 무시
        linked = skipped = 0
        try:
            for view in self._views:
                if view is source or view.series is None:
                    continue
                if self.is_linked(source, view):
                    view.set_reference_point(point)
                    linked += 1
                else:
                    view.clear_reference_point()
                    skipped += 1
        finally:
            self._broadcasting = False
        self.synced.emit(linked, skipped)
