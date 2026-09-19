# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""Help → About DabbaView"""
import os
import platform

from PyQt5.QtCore import Qt, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import APP_NAME, COPYRIGHT, GITHUB_URL, LICENSE_NAME, __version__, build_info

LOGO_PATH = os.path.join(os.path.dirname(__file__), "resources", "logo.png")


def library_versions():
    """번들된 주요 라이브러리 버전 (없는 것은 생략)"""
    import importlib
    rows = [("Python", platform.python_version()), ("Qt", QT_VERSION_STR),
            ("PyQt5", PYQT_VERSION_STR)]
    for label, module, attr in (("pydicom", "pydicom", "__version__"),
                                ("NumPy", "numpy", "__version__"),
                                ("SciPy", "scipy", "__version__"),
                                ("VTK", "vtkmodules", "__version__"),
                                ("nibabel", "nibabel", "__version__"),
                                ("pynrrd", "nrrd", "__version__"),
                                ("SimpleITK", "SimpleITK", "__version__")):
        try:
            rows.append((label, str(getattr(importlib.import_module(module), attr))))
        except Exception:  # noqa: BLE001 - 설치 안 됐거나 버전 표기가 없으면 생략
            continue
    # onnxruntime은 import하면 원격 수집이 시작되므로 버전만 따로 확인
    import sys
    try:
        if "onnxruntime" in sys.modules:
            rows.append(("onnxruntime", sys.modules["onnxruntime"].__version__))
        else:
            from importlib import metadata, util
            if util.find_spec("onnxruntime") is not None:
                try:
                    rows.append(("onnxruntime", metadata.version("onnxruntime")))
                except metadata.PackageNotFoundError:
                    rows.append(("onnxruntime", "포함됨 (모델 사용 시 로드)"))
    except Exception:  # noqa: BLE001
        pass
    return rows


def version_text():
    date, commit = build_info()
    build = f"빌드 {date}" + (f" ({commit})" if commit else "") if date else "소스 실행 (개발 버전)"
    return f"v{__version__}", build


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        logo = QLabel()
        pix = QPixmap(LOGO_PATH)
        if not pix.isNull():
            logo.setPixmap(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        top.addWidget(logo, 0, Qt.AlignTop)
        version, build = version_text()
        info = QLabel(
            f"<h2 style='margin-bottom:2px'>{APP_NAME}</h2>"
            f"<div style='font-size:15px'>DICOM Viewer · <b>{version}</b></div>"
            f"<div style='color:#999'>{build}</div><br>"
            f"<div>{COPYRIGHT}</div>"
            f"<div>License: {LICENSE_NAME}</div>"
            f"<div><a href='{GITHUB_URL}'>{GITHUB_URL}</a></div>")
        info.setTextFormat(Qt.RichText)
        info.setOpenExternalLinks(True)
        info.setTextInteractionFlags(Qt.TextBrowserInteraction)
        top.addWidget(info, 1)
        layout.addLayout(top)

        notice = QLabel(
            "이 프로그램은 자유 소프트웨어입니다. GPL-3.0 조건에 따라 재배포·수정할 수 있으며, "
            "어떠한 보증도 제공하지 않습니다. 진단용 의료기기가 아닙니다.")
        notice.setWordWrap(True)
        notice.setStyleSheet("color: #999; margin-top: 6px;")
        layout.addWidget(notice)

        libs = QLabel("  ·  ".join(f"{name} {ver}" for name, ver in library_versions()))
        libs.setWordWrap(True)
        libs.setStyleSheet("color: #888; font-size: 11px; margin-top: 6px;")
        libs.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(libs)

        close = QPushButton("닫기")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignRight)
