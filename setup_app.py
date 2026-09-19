# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
py2app 설정 파일
macOS .app 번들 생성용
"""
import datetime
import glob
import os
import re
import shutil
import subprocess
import sys

from setuptools import setup


def read_version():
    """dabbaview/__init__.py의 __version__ (버전의 단일 소스)"""
    with open(os.path.join('dabbaview', '__init__.py'), encoding='utf-8') as f:
        return re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M).group(1)


def write_build_info():
    """빌드 날짜·커밋을 dabbaview/_build_info.py로 (About 대화상자 표시용, git에는 안 올림)"""
    try:
        commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True,
                                text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = ''
    with open(os.path.join('dabbaview', '_build_info.py'), 'w', encoding='utf-8') as f:
        f.write('# setup_app.py가 빌드할 때 생성 - 직접 고치지 마세요\n')
        f.write(f'BUILD_DATE = "{datetime.datetime.now():%Y-%m-%d %H:%M}"\n')
        f.write(f'GIT_COMMIT = "{commit}"\n')


def sync_readme_version(version):
    """README의 버전 표기를 __version__에 맞춤"""
    path = 'README.md'
    with open(path, encoding='utf-8') as f:
        text = f.read()
    new = re.sub(r'(<!-- version -->\*\*Version\*\* )[0-9A-Za-z.\-+]+', rf'\g<1>{version}', text)
    if new != text:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new)


VERSION = read_version()
if 'py2app' in sys.argv:
    write_build_info()
    sync_readme_version(VERSION)

APP = ['run.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'resources/DabbaView.icns',
    'plist': {
        'CFBundleName': 'DabbaView',
        'CFBundleDisplayName': 'DabbaView - DICOM Viewer',
        'CFBundleIdentifier': 'com.dabbaview.dicomviewer',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'NSHumanReadableCopyright': 'Copyright (c) 2026 Park Seongho (Dabbabbu) · GPL-3.0',
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
                 'vtkmodules',
                 # AI Research: ONNX 모델 로컬 추론
                 'onnxruntime',
                 # 다중 포맷 불러오기/변환 (NIfTI, NRRD, MetaImage) + HTTPS 인증서
                 'nibabel', 'nrrd', 'SimpleITK', 'certifi',
                 # 압축 DICOM 디코더 (JPEG, JPEG-LS, JPEG 2000, GDCM)
                 'pylibjpeg', 'libjpeg', 'openjpeg', '_gdcm',
                 # 메모리 사용량 표시
                 'psutil',
                 # 분석: Python 콘솔·히스토그램 그래프, 입자 분석·Canny
                 'matplotlib', 'skimage',
                 # 클라우드: Google Drive (정적 API 문서·CA 파일 포함) / OneDrive / 키체인
                 # (google.*, jaraco.*는 네임스페이스 패키지 → 아래 includes로 모듈 지정)
                 'googleapiclient', 'google_auth_oauthlib', 'httplib2',
                 'oauthlib', 'requests_oauthlib', 'uritemplate', 'msal', 'jwt',
                 'requests', 'urllib3', 'keyring'],
    'includes': ['PyQt5', 'PyQt5.QtWidgets', 'PyQt5.QtCore', 'PyQt5.QtGui',
                 'PyQt5.QtPrintSupport', 'vtk', 'google_auth_httplib2',
                 'google.auth', 'google.auth.transport.requests', 'google.oauth2.credentials',
                 'google.api_core.client_options', 'google.api_core.exceptions',
                 'google.protobuf', 'jaraco.context', 'jaraco.functools',
                 'jaraco.classes.properties', 'keyring.backends.macOS', 'gdcm'],
    'excludes': ['tkinter', 'PySide2', 'PySide6', 'PyQt6',
                 'onnx'],   # 개발용(테스트 모델 생성)일 뿐, onnxruntime만 있으면 됨
}

setup(
    name='DabbaView',
    version=VERSION,
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
