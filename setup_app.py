# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
py2app 설정 파일
macOS .app 번들 생성용
"""
import glob
import os
import shutil
import sys

from setuptools import setup

APP = ['run.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'resources/DabbaView.icns',
    'plist': {
        'CFBundleName': 'DabbaView',
        'CFBundleDisplayName': 'DabbaView - DICOM Viewer',
        'CFBundleIdentifier': 'com.dabbaview.dicomviewer',
        'CFBundleVersion': '0.2.0',
        'CFBundleShortVersionString': '0.2.0',
        'NSHighResolutionCapable': True,
        'CFBundleDocumentTypes': [
            {
                'CFBundleTypeName': 'DICOM File',
                'CFBundleTypeExtensions': ['dcm', 'DCM', 'dicom'],
                'CFBundleTypeRole': 'Viewer',
            }
        ],
    },
    'packages': ['dabbaview', 'pydicom', 'pynetdicom', 'numpy', 'PIL', 'scipy', 'cv2',
                 'pypdfium2', 'pypdfium2_raw', 'striprtf',
                 # 3D Volume Rendering (import vtk → vtkmodules.*, .dylibs 포함)
                 'vtkmodules'],
    'includes': ['PyQt5', 'PyQt5.QtWidgets', 'PyQt5.QtCore', 'PyQt5.QtGui',
                 'PyQt5.QtPrintSupport', 'vtk'],
    # vtk wheel이 끌어오는 matplotlib 등은 앱에서 쓰지 않음
    'excludes': ['matplotlib', 'tkinter', 'PySide2', 'PySide6', 'PyQt6'],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)


def strip_sample_dicom():
    """pydicom에 포함된 테스트용 샘플 DICOM(test_files, charset_files) 제거

    앱에서 쓰지 않는 파일이며, 번들에 샘플 영상이 들어가지 않도록 정리.
    런타임에 필요한 palettes 등은 유지.
    """
    pattern = os.path.join('dist', 'DabbaView.app', 'Contents', 'Resources',
                           'lib', 'python3*', 'pydicom', 'data')
    for data_dir in glob.glob(pattern):
        for name in ('test_files', 'charset_files'):
            path = os.path.join(data_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
                print(f"removed sample DICOM: {path}")


if 'py2app' in sys.argv:
    strip_sample_dicom()
