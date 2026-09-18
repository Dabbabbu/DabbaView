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
    'iconfile': 'resources/RadiantView.icns',
    'plist': {
        'CFBundleName': 'RadiantView',
        'CFBundleDisplayName': 'RadiantView DICOM Viewer',
        'CFBundleIdentifier': 'com.radiantview.dicomviewer',
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
    'packages': ['radiantview', 'pydicom', 'pynetdicom', 'numpy', 'PIL', 'scipy', 'cv2',
                 'pypdfium2', 'pypdfium2_raw', 'striprtf'],
    'includes': ['PyQt5', 'PyQt5.QtWidgets', 'PyQt5.QtCore', 'PyQt5.QtGui',
                 'PyQt5.QtPrintSupport'],
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
    pattern = os.path.join('dist', 'RadiantView.app', 'Contents', 'Resources',
                           'lib', 'python3*', 'pydicom', 'data')
    for data_dir in glob.glob(pattern):
        for name in ('test_files', 'charset_files'):
            path = os.path.join(data_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
                print(f"removed sample DICOM: {path}")


if 'py2app' in sys.argv:
    strip_sample_dicom()
