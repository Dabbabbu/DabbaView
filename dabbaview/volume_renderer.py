# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
3D Volume Rendering (VTK 기반)
- Volume Rendering (레이캐스팅)
- MIP (Maximum Intensity Projection)
- 전송함수 프리셋 (CT Bone, CT Skin, MRI 등)
"""
import numpy as np

try:
    import vtk
    from vtk.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
    VTK_AVAILABLE = True
except ImportError:
    VTK_AVAILABLE = False

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QComboBox,
                              QLabel, QPushButton, QSlider, QFrame,
                              QMessageBox)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QGuiApplication

# 실제 창이 없는 Qt 플랫폼에서는 VTK 렌더 창 생성 시 크래시 (CI 스모크 테스트 등)
_HEADLESS_PLATFORMS = ("offscreen", "minimal")


def vtk_usable():
    """VTK가 설치되어 있고 현재 Qt 플랫폼에서 렌더 창을 만들 수 있는지"""
    return VTK_AVAILABLE and QGuiApplication.platformName() not in _HEADLESS_PLATFORMS


# VTK 없을 때 대체 위젯
class VTKNotAvailableWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel(
            "3D Volume Rendering을 사용하려면 VTK가 필요합니다.\n\n"
            "설치 방법:\n"
            "  pip install vtk\n\n"
            "설치 후 프로그램을 다시 시작해주세요."
        )
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #aaa; font-size: 14px;")
        layout.addWidget(label)


class VolumePresets:
    """전송함수 프리셋"""

    @staticmethod
    def get_preset_names():
        return [
            "CT Bone",
            "CT Skin",
            "CT Soft Tissue",
            "CT Lung",
            "CT Angiography",
            "MRI Default",
            "MIP",
        ]

    @staticmethod
    def apply_preset(name, volume_property, color_func, opacity_func):
        """프리셋에 따라 전송함수 설정"""
        color_func.RemoveAllPoints()
        opacity_func.RemoveAllPoints()

        if name == "CT Bone":
            color_func.AddRGBPoint(-1000, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(100, 0.55, 0.25, 0.15)
            color_func.AddRGBPoint(300, 0.88, 0.82, 0.66)
            color_func.AddRGBPoint(1500, 1.0, 1.0, 0.9)
            opacity_func.AddPoint(-1000, 0.0)
            opacity_func.AddPoint(100, 0.0)
            opacity_func.AddPoint(300, 0.3)
            opacity_func.AddPoint(500, 0.7)
            opacity_func.AddPoint(1500, 1.0)

        elif name == "CT Skin":
            color_func.AddRGBPoint(-1000, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(-500, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(-100, 0.86, 0.69, 0.58)
            color_func.AddRGBPoint(200, 0.92, 0.78, 0.65)
            color_func.AddRGBPoint(1500, 1.0, 1.0, 1.0)
            opacity_func.AddPoint(-1000, 0.0)
            opacity_func.AddPoint(-500, 0.0)
            opacity_func.AddPoint(-100, 0.5)
            opacity_func.AddPoint(200, 0.7)
            opacity_func.AddPoint(1500, 0.0)

        elif name == "CT Soft Tissue":
            color_func.AddRGBPoint(-1000, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(-150, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(0, 0.8, 0.3, 0.2)
            color_func.AddRGBPoint(100, 0.9, 0.6, 0.4)
            color_func.AddRGBPoint(300, 1.0, 0.9, 0.7)
            opacity_func.AddPoint(-1000, 0.0)
            opacity_func.AddPoint(-150, 0.0)
            opacity_func.AddPoint(0, 0.15)
            opacity_func.AddPoint(100, 0.4)
            opacity_func.AddPoint(300, 0.7)

        elif name == "CT Lung":
            color_func.AddRGBPoint(-1000, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(-900, 0.15, 0.25, 0.45)
            color_func.AddRGBPoint(-500, 0.3, 0.5, 0.7)
            color_func.AddRGBPoint(0, 0.9, 0.8, 0.7)
            color_func.AddRGBPoint(500, 1.0, 1.0, 0.9)
            opacity_func.AddPoint(-1000, 0.0)
            opacity_func.AddPoint(-900, 0.02)
            opacity_func.AddPoint(-500, 0.05)
            opacity_func.AddPoint(0, 0.3)
            opacity_func.AddPoint(500, 0.8)

        elif name == "CT Angiography":
            color_func.AddRGBPoint(-1000, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(100, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(200, 0.9, 0.15, 0.1)
            color_func.AddRGBPoint(400, 1.0, 0.3, 0.2)
            color_func.AddRGBPoint(1500, 1.0, 0.9, 0.8)
            opacity_func.AddPoint(-1000, 0.0)
            opacity_func.AddPoint(100, 0.0)
            opacity_func.AddPoint(200, 0.5)
            opacity_func.AddPoint(400, 0.8)
            opacity_func.AddPoint(1500, 1.0)

        elif name == "MRI Default":
            color_func.AddRGBPoint(0, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(500, 0.6, 0.6, 0.6)
            color_func.AddRGBPoint(1000, 0.9, 0.9, 0.9)
            color_func.AddRGBPoint(2000, 1.0, 1.0, 1.0)
            opacity_func.AddPoint(0, 0.0)
            opacity_func.AddPoint(200, 0.0)
            opacity_func.AddPoint(500, 0.3)
            opacity_func.AddPoint(1000, 0.6)
            opacity_func.AddPoint(2000, 0.9)

        elif name == "MIP":
            # MIP는 별도 mapper 사용, 여기선 밝기만 설정
            color_func.AddRGBPoint(-1000, 0.0, 0.0, 0.0)
            color_func.AddRGBPoint(0, 0.3, 0.3, 0.3)
            color_func.AddRGBPoint(500, 0.8, 0.8, 0.8)
            color_func.AddRGBPoint(1500, 1.0, 1.0, 1.0)
            opacity_func.AddPoint(-1000, 0.0)
            opacity_func.AddPoint(-500, 0.0)
            opacity_func.AddPoint(0, 0.1)
            opacity_func.AddPoint(500, 0.5)
            opacity_func.AddPoint(1500, 1.0)


class VolumeRenderWidget(QWidget):
    """VTK 기반 3D 볼륨 렌더링 위젯"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._vtk_ok = vtk_usable()
        if not self._vtk_ok:
            layout = QVBoxLayout(self)
            layout.addWidget(VTKNotAvailableWidget())
            return

        self._volume_data = None
        self._init_ui()
        self._init_vtk()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 컨트롤 바
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Preset:"))
        self._preset_combo = QComboBox()
        self._preset_combo.addItems(VolumePresets.get_preset_names())
        self._preset_combo.currentTextChanged.connect(self._apply_preset)
        ctrl.addWidget(self._preset_combo)

        ctrl.addWidget(QLabel("  Quality:"))
        self._quality_slider = QSlider(Qt.Horizontal)
        self._quality_slider.setRange(1, 10)
        self._quality_slider.setValue(5)
        self._quality_slider.setFixedWidth(120)
        self._quality_slider.valueChanged.connect(self._on_quality_changed)
        ctrl.addWidget(self._quality_slider)

        self._reset_btn = QPushButton("Reset Camera")
        self._reset_btn.clicked.connect(self._reset_camera)
        ctrl.addWidget(self._reset_btn)

        self._mesh_label = QLabel("")
        ctrl.addWidget(self._mesh_label)
        self._clear_mesh_btn = QPushButton("메시 지우기")
        self._clear_mesh_btn.setToolTip("불러온 STL 메시를 3D 화면에서 제거")
        self._clear_mesh_btn.clicked.connect(self.clear_meshes)
        self._clear_mesh_btn.setVisible(False)
        ctrl.addWidget(self._clear_mesh_btn)

        ctrl.addStretch()
        layout.addLayout(ctrl)

        # VTK 렌더 위젯
        self._vtk_frame = QFrame()
        vtk_layout = QVBoxLayout(self._vtk_frame)
        vtk_layout.setContentsMargins(0, 0, 0, 0)
        self._vtk_widget = QVTKRenderWindowInteractor(self._vtk_frame)
        vtk_layout.addWidget(self._vtk_widget)
        layout.addWidget(self._vtk_frame)

    def _init_vtk(self):
        self._renderer = vtk.vtkRenderer()
        self._renderer.SetBackground(0.1, 0.1, 0.1)
        self._vtk_widget.GetRenderWindow().AddRenderer(self._renderer)
        self._interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
        style = vtk.vtkInteractorStyleTrackballCamera()
        self._interactor.SetInteractorStyle(style)

        # 전송함수
        self._color_func = vtk.vtkColorTransferFunction()
        self._opacity_func = vtk.vtkPiecewiseFunction()

        # 볼륨 속성
        self._volume_property = vtk.vtkVolumeProperty()
        self._volume_property.SetColor(self._color_func)
        self._volume_property.SetScalarOpacity(self._opacity_func)
        self._volume_property.ShadeOn()
        self._volume_property.SetInterpolationTypeToLinear()
        self._volume_property.SetAmbient(0.2)
        self._volume_property.SetDiffuse(0.7)
        self._volume_property.SetSpecular(0.3)

        self._volume_actor = None
        self._mesh_actors = []
        self._vtk_widget.Initialize()

    def set_series(self, series):
        """DicomSeries로부터 볼륨 데이터 구성"""
        if not self._vtk_ok or not series or series.num_slices < 2:
            return

        series.sort_slices()
        slices = []
        arrays = series.get_all_pixel_arrays()
        for arr in (arrays[i] for i in series.spatial_order()):   # 볼륨은 공간 순서로
            if arr is not None and len(arr.shape) == 2:
                slices.append(arr)

        if len(slices) < 2:
            return

        ref_shape = slices[0].shape
        uniform = [s for s in slices if s.shape == ref_shape]
        if len(uniform) < 2:
            return

        volume = np.clip(np.stack(uniform, axis=0), -32768, 32767).astype(np.int16)
        # 환자 좌표(LPS mm)에 놓아 실제 비율로 보이고 STL 메시와도 겹치도록
        from .ai.volume import series_spacing_affine
        _spacing, affine = series_spacing_affine(series)
        if len(uniform) != len(slices):
            affine = np.eye(4)
        self._set_volume_data(volume, affine)

    def _set_volume_data(self, volume_np, affine=None):
        """numpy 볼륨 (k, row, col)을 VTK에 전달. affine: (col, row, k) 인덱스 → LPS mm"""
        from vtkmodules.util.numpy_support import numpy_to_vtk
        self._volume_data = volume_np
        d, h, w = volume_np.shape

        # VTK 이미지 데이터 (인덱스 공간) - 위치·간격·방향은 액터 행렬로
        vtk_data = vtk.vtkImageData()
        vtk_data.SetDimensions(w, h, d)
        vtk_data.SetSpacing(1.0, 1.0, 1.0)
        vtk_data.SetOrigin(0, 0, 0)
        # C 순서 (k, row, col) = x(col)가 가장 빠름 → VTK 점 순서와 같음
        vtk_array = numpy_to_vtk(np.ascontiguousarray(volume_np).ravel(), deep=True,
                                 array_type=vtk.VTK_SHORT)
        vtk_data.GetPointData().SetScalars(vtk_array)

        # 볼륨 매퍼
        mapper = vtk.vtkSmartVolumeMapper()
        mapper.SetInputData(vtk_data)

        # 기존 액터 제거
        if self._volume_actor:
            self._renderer.RemoveVolume(self._volume_actor)

        self._volume_actor = vtk.vtkVolume()
        self._volume_actor.SetMapper(mapper)
        self._volume_actor.SetProperty(self._volume_property)
        if affine is not None:
            matrix = vtk.vtkMatrix4x4()
            for r in range(4):
                for c in range(4):
                    matrix.SetElement(r, c, float(affine[r, c]))
            self._volume_actor.SetUserMatrix(matrix)
        self._renderer.AddVolume(self._volume_actor)

        # 프리셋 적용
        self._apply_preset(self._preset_combo.currentText())
        self._reset_camera()

    def _apply_preset(self, name):
        if not self._vtk_ok:
            return
        VolumePresets.apply_preset(
            name, self._volume_property,
            self._color_func, self._opacity_func)

        if self._volume_actor:
            mapper = self._volume_actor.GetMapper()
            if name == "MIP":
                mapper.SetBlendModeToMaximumIntensity()
            else:
                mapper.SetBlendModeToComposite()

        self._render()

    def _on_quality_changed(self, value):
        if not self._vtk_ok or not self._volume_actor:
            return
        # 샘플 거리 조정 (작을수록 고품질)
        dist = 2.0 / value
        self._volume_actor.GetMapper().SetSampleDistance(dist)
        self._render()

    def _reset_camera(self):
        if not self._vtk_ok:
            return
        self._renderer.ResetCamera()
        self._render()

    def _render(self):
        if not self._vtk_ok:
            return
        self._vtk_widget.GetRenderWindow().Render()

    # ─── STL 메시 ───

    MESH_COLORS = [(0.95, 0.85, 0.7), (0.9, 0.4, 0.4), (0.4, 0.6, 0.95),
                   (0.5, 0.85, 0.5), (0.9, 0.8, 0.3)]

    def add_mesh(self, path):
        """STL 메시 추가 (좌표는 LPS mm로 가정 - 3D Slicer 기본 내보내기와 같음)"""
        if not self._vtk_ok:
            return False
        reader = vtk.vtkSTLReader()
        reader.SetFileName(path)
        reader.Update()
        polydata = reader.GetOutput()
        if polydata is None or polydata.GetNumberOfPoints() == 0:
            raise ValueError(f"STL을 읽지 못했습니다: {path}")
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(polydata)
        normals.Update()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(normals.GetOutput())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        color = self.MESH_COLORS[len(self._mesh_actors) % len(self.MESH_COLORS)]
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetSpecular(0.3)
        self._renderer.AddActor(actor)
        self._mesh_actors.append((path, actor))
        self._update_mesh_label()
        self._reset_camera()
        return polydata.GetNumberOfCells()

    def add_polydata(self, polydata, name, color=(230, 200, 170)):
        """표면 모델(vtkPolyData, LPS mm) 추가 - Surface Modeling 결과"""
        if not self._vtk_ok:
            return False
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        mapper.ScalarVisibilityOff()
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*[c / 255.0 for c in color])
        actor.GetProperty().SetSpecular(0.3)
        actor.GetProperty().SetSpecularPower(20)
        self._renderer.AddActor(actor)
        self._mesh_actors.append((name, actor))
        self._update_mesh_label()
        self._reset_camera()
        return True

    def set_landmarks(self, points):
        """랜드마크를 3D 구 + 이름으로 표시 (points: [{name, position}])"""
        if not self._vtk_ok:
            return
        for actor in getattr(self, "_landmark_actors", []):
            self._renderer.RemoveActor(actor)
        self._landmark_actors = []
        for p in points:
            sphere = vtk.vtkSphereSource()
            sphere.SetCenter(*p["position"])
            sphere.SetRadius(2.0)
            sphere.SetThetaResolution(16)
            sphere.SetPhiResolution(16)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0.47, 1.0, 0.47)
            text = vtk.vtkBillboardTextActor3D()
            text.SetInput(p["name"])
            text.SetPosition(*p["position"])
            text.GetTextProperty().SetFontSize(14)
            text.GetTextProperty().SetColor(0.47, 1.0, 0.47)
            text.SetDisplayOffset(8, 8)
            for a in (actor, text):
                self._renderer.AddActor(a)
                self._landmark_actors.append(a)
        self._render()

    def clear_meshes(self):
        if not self._vtk_ok:
            return
        for _path, actor in self._mesh_actors:
            self._renderer.RemoveActor(actor)
        self._mesh_actors = []
        self._update_mesh_label()
        self._render()

    def _update_mesh_label(self):
        import os
        names = [os.path.basename(p) for p, _ in self._mesh_actors]  # 경로 또는 표면 이름
        self._mesh_label.setText(("  메시: " + ", ".join(names)) if names else "")
        self._clear_mesh_btn.setVisible(bool(names))

    def cleanup(self):
        """위젯 정리"""
        if self._vtk_ok and hasattr(self, '_vtk_widget'):
            self._vtk_widget.Finalize()
