# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
표면 모델 (Surface Modeling) - 세그멘테이션 라벨 → 3D 메시 (VTK)

Marching Cubes(이산 Flying Edges) → 스무딩(Windowed Sinc) → 데시메이션 → 환자 좌표(LPS mm)
STL / OBJ 내보내기
"""
import numpy as np


def _vtk():
    import vtk
    from vtkmodules.util.numpy_support import numpy_to_vtk
    return vtk, numpy_to_vtk


def label_surface(mask, label_id, affine_lps, smoothing=20, decimation=0.5):
    """mask (k, row, col) 에서 label_id 영역의 표면 → vtkPolyData (LPS mm)

    smoothing: Windowed Sinc 반복 횟수 (0 = 끔), decimation: 줄일 삼각형 비율 (0~0.95)
    """
    vtk, numpy_to_vtk = _vtk()
    binary = (np.asarray(mask) == label_id).astype(np.uint8)
    if not binary.any():
        raise ValueError("이 라벨이 칠해진 곳이 없습니다.")
    # 가장자리에서도 닫힌 표면이 되도록 한 칸씩 여백
    binary = np.pad(binary, 1)
    d, h, w = binary.shape
    image = vtk.vtkImageData()
    image.SetDimensions(w, h, d)
    image.SetSpacing(1.0, 1.0, 1.0)
    image.SetOrigin(-1.0, -1.0, -1.0)   # 여백만큼 원점 이동 → 인덱스가 원래 복셀 번호
    image.GetPointData().SetScalars(numpy_to_vtk(binary.ravel(), deep=True,
                                                 array_type=vtk.VTK_UNSIGNED_CHAR))

    surface = vtk.vtkDiscreteFlyingEdges3D()
    surface.SetInputData(image)
    surface.SetValue(0, 1)
    surface.ComputeNormalsOff()
    surface.Update()
    poly = surface.GetOutput()
    if poly.GetNumberOfPoints() == 0:
        raise ValueError("표면을 만들지 못했습니다.")

    if smoothing and smoothing > 0:
        smoother = vtk.vtkWindowedSincPolyDataFilter()
        smoother.SetInputData(poly)
        smoother.SetNumberOfIterations(int(smoothing))
        smoother.SetPassBand(0.05)
        smoother.BoundarySmoothingOff()
        smoother.FeatureEdgeSmoothingOff()
        smoother.NonManifoldSmoothingOn()
        smoother.NormalizeCoordinatesOn()
        smoother.Update()
        poly = smoother.GetOutput()

    if decimation and decimation > 0:
        tri = vtk.vtkTriangleFilter()
        tri.SetInputData(poly)
        tri.Update()
        dec = vtk.vtkQuadricDecimation()
        dec.SetInputData(tri.GetOutput())
        dec.SetTargetReduction(float(min(decimation, 0.95)))
        dec.Update()
        poly = dec.GetOutput()

    # 인덱스 (col, row, k) → LPS mm
    matrix = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            matrix.SetElement(r, c, float(affine_lps[r, c]))
    transform = vtk.vtkTransform()
    transform.SetMatrix(matrix)
    tf = vtk.vtkTransformPolyDataFilter()
    tf.SetInputData(poly)
    tf.SetTransform(transform)
    tf.Update()
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(tf.GetOutput())
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.Update()
    out = vtk.vtkPolyData()
    out.DeepCopy(normals.GetOutput())
    return out


def mesh_stats(poly):
    """(삼각형 수, 표면적 mm², 부피 mL)"""
    vtk, _ = _vtk()
    tri = vtk.vtkTriangleFilter()
    tri.SetInputData(poly)
    tri.Update()
    mass = vtk.vtkMassProperties()
    mass.SetInputData(tri.GetOutput())
    mass.Update()
    return tri.GetOutput().GetNumberOfCells(), mass.GetSurfaceArea(), mass.GetVolume() / 1000.0


def save_mesh(path, poly):
    vtk, _ = _vtk()
    lower = path.lower()
    if lower.endswith(".obj"):
        writer = vtk.vtkOBJWriter()
    elif lower.endswith(".ply"):
        writer = vtk.vtkPLYWriter()
    else:
        writer = vtk.vtkSTLWriter()
        writer.SetFileTypeToBinary()
    writer.SetFileName(path)
    writer.SetInputData(poly)
    if not writer.Write():
        raise OSError(f"저장하지 못했습니다: {path}")
