# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""압축 DICOM 디코더 등록 (JPEG Baseline/Extended/Lossless, JPEG-LS, JPEG 2000, HTJ2K, RLE)

pydicom은 pylibjpeg 플러그인(pylibjpeg-libjpeg, pylibjpeg-openjpeg)을 패키지 entry point로
찾는데, 앱 번들(py2app)에서는 .dist-info가 없어 목록이 비는 경우가 있음.
그러면 압축 영상이 모두 '디코딩 불가'가 되므로 모듈에서 직접 등록함. GDCM은 import만 되면 사용됨.
"""

_LIBJPEG_UIDS = ("1.2.840.10008.1.2.4.50", "1.2.840.10008.1.2.4.51", "1.2.840.10008.1.2.4.57",
                 "1.2.840.10008.1.2.4.70", "1.2.840.10008.1.2.4.80", "1.2.840.10008.1.2.4.81")
_OPENJPEG_UIDS = ("1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91", "1.2.840.10008.1.2.4.201",
                  "1.2.840.10008.1.2.4.202", "1.2.840.10008.1.2.4.203")

_done = False


def ensure_decoders():
    """pydicom의 pylibjpeg 디코더 표에 빠진 항목을 채움 (여러 번 불러도 한 번만)"""
    global _done
    if _done:
        return
    _done = True
    try:
        from pydicom import uid
        from pydicom.pixels.decoders import pylibjpeg as plugin
    except ImportError:
        return
    table = getattr(plugin, "_DECODERS", None)
    if table is None:
        return
    for module_name, uids in (("libjpeg", _LIBJPEG_UIDS), ("openjpeg", _OPENJPEG_UIDS)):
        try:
            module = __import__(module_name)
            func = module.decode_pixel_data
        except (ImportError, AttributeError):
            continue
        for value in uids:
            table.setdefault(uid.UID(value), {}).setdefault(module_name, func)


def available():
    """지원 현황 {이름: 사용 가능 여부} (About·진단용)"""
    ensure_decoders()
    from pydicom import uid
    from pydicom.pixels.decoders.base import get_decoder
    names = {"JPEG Baseline": uid.JPEGBaseline8Bit, "JPEG Extended (12-bit)": uid.JPEGExtended12Bit,
             "JPEG Lossless": uid.JPEGLosslessSV1, "JPEG-LS": uid.JPEGLSLossless,
             "JPEG 2000": uid.JPEG2000, "HTJ2K": uid.HTJ2K, "RLE": uid.RLELossless}
    out = {}
    for name, ts in names.items():
        try:
            out[name] = get_decoder(ts).is_available
        except Exception:  # noqa: BLE001
            out[name] = False
    return out
