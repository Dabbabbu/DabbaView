# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
압축파일 열기 - 캐시 폴더(archives/)에 풀어서 안의 DICOM · 영상을 그대로 연다

- ZIP · TAR(.tar/.tgz/.tar.gz/.tar.bz2/.tar.xz) · .gz(파일 하나): 파이썬 내장 기능
- 7z · RAR · ISO 등: 운영체제에 들어 있는 tar(libarchive, macOS · Windows 10 이상)로 풂

- 같은 ZIP(경로 · 크기 · 수정 시각이 같음)을 다시 열면 풀어 둔 것을 그대로 씀
- 안전: 폴더 밖으로 나가는 이름(../, 절대 경로)은 건너뜀, 풀린 크기가 너무 크면 멈춤
- 윈도우에서 만든 ZIP의 한글 파일 이름(CP949)도 제대로 풂
- 암호가 걸린 압축파일은 풀 수 없다고 알림
- 압축 안의 압축은 한 단계까지 함께 풂
- 풀어 둔 것은 캐시 한도 · '캐시 지우기'로 함께 정리됨
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile

ZIP_EXT = (".zip",)
TAR_EXT = (".tar", ".tgz", ".tar.gz", ".tbz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")
GZ_EXT = (".gz",)                    # 파일 하나를 gzip으로 누른 것 (예: image.dcm.gz)
SYSTEM_EXT = (".7z", ".rar", ".iso", ".cab", ".xar", ".lha", ".lzh", ".cpio")
ARCHIVE_EXTENSIONS = ZIP_EXT + TAR_EXT + GZ_EXT + SYSTEM_EXT
NOT_ARCHIVES = (".nii.gz", ".nrrd.gz", ".mha.gz", ".npy.gz")   # 영상 형식 자체가 gz — 그대로 읽음
MAX_UNCOMPRESSED = 50 * 1024 ** 3    # 이보다 크게 풀리는 ZIP은 멈춤 (압축 폭탄 방지)
SKIP_PREFIXES = ("__MACOSX/",)


class ArchiveError(Exception):
    pass


def is_archive_name(name):
    lower = name.lower()
    return lower.endswith(ARCHIVE_EXTENSIONS) and not lower.endswith(NOT_ARCHIVES) \
        and not os.path.basename(lower).startswith(".")


def is_archive(path):
    return is_archive_name(path) and os.path.isfile(path)


def _kind(path):
    lower = path.lower()
    if lower.endswith(ZIP_EXT):
        return "zip"
    if lower.endswith(TAR_EXT):
        return "tar"
    if lower.endswith(GZ_EXT):
        return "tar" if tarfile.is_tarfile(path) else "gz"
    return "system"


def _member_name(info):
    """ZIP 안 파일 이름 — UTF-8 표시가 없으면 CP949(윈도우 한글)로 다시 읽어 봄"""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("cp949")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def _safe_target(root, name):
    """root 밖으로 나가지 않는 경로만 (../ · 절대 경로 · 드라이브 문자 거부)"""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        return None
    target = os.path.normpath(os.path.join(root, *parts))
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(target)]) != os.path.abspath(root):
        return None
    return target


def extraction_dir(path):
    """이 ZIP을 풀어 둘 폴더 (캐시/archives/<이름>_<키>)"""
    from . import cache
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]
    stem = os.path.splitext(os.path.basename(path))[0][:40] or "archive"
    return os.path.join(cache.category_dir("archives"), f"{stem}_{key}")


def extract(path, progress=None, cancelled=None):
    """압축파일을 풀고 풀린 폴더 경로를 돌려줌. progress(done, total, name) · cancelled() → True면 멈춤"""
    from . import cache
    dest = extraction_dir(path)
    if os.path.exists(os.path.join(dest, ".complete")):
        cache.touch(dest)                # 이미 풀어 둠
        return dest
    part = dest + ".part"
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(part, exist_ok=True)
    try:
        _extract_into(path, part, progress, cancelled)
        # 압축 안의 압축도 한 단계 풂 (그 자리에 폴더로)
        for root, _dirs, files in os.walk(part):
            for fn in files:
                inner = os.path.join(root, fn)
                if is_archive_name(fn):
                    inner_dir = _strip_ext(inner)
                    try:
                        _extract_into(inner, inner_dir, None, cancelled)
                        os.remove(inner)
                    except ArchiveError:
                        pass             # 안쪽 것을 못 풀면 그대로 둠
        open(os.path.join(part, ".complete"), "w").close()
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(part, dest)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return dest


def _strip_ext(path):
    lower = path.lower()
    for ext in sorted(ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(ext):
            return path[:-len(ext)]
    return os.path.splitext(path)[0]


def _check_space(dest, total_size):
    from . import cache
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(dest)) or ".").free
    if total_size > MAX_UNCOMPRESSED or total_size > free * 0.9:
        raise ArchiveError(f"풀면 {cache.human_size(total_size)}가 필요해 공간이 부족합니다 "
                           f"(남은 공간 {cache.human_size(free)}).")


def _extract_into(path, dest, progress=None, cancelled=None):
    kind = _kind(path)
    os.makedirs(dest, exist_ok=True)
    if kind == "zip":
        _extract_zip(path, dest, progress, cancelled)
    elif kind == "tar":
        _extract_tar(path, dest, progress, cancelled)
    elif kind == "gz":
        import gzip
        target = os.path.join(dest, os.path.basename(_strip_ext(path)) or "data")
        try:
            with gzip.open(path, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        except OSError as exc:
            raise ArchiveError(f"gz 파일을 풀 수 없습니다 ({os.path.basename(path)}): {exc}") from exc
    else:
        _extract_system(path, dest, cancelled)


def _extract_zip(path, dest, progress, cancelled):
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"ZIP 파일을 열 수 없습니다 ({os.path.basename(path)}): {exc}") from exc
    with archive:
        members = [i for i in archive.infolist()
                   if not i.is_dir() and not i.filename.startswith(SKIP_PREFIXES)
                   and not os.path.basename(_member_name(i)).startswith("._")]
        if any(i.flag_bits & 0x1 for i in members):
            raise ArchiveError(f"암호가 걸린 압축파일이라 풀 수 없습니다: {os.path.basename(path)}\n"
                               "압축 프로그램으로 먼저 풀어서 폴더를 여세요.")
        _check_space(dest, sum(i.file_size for i in members))
        for n, info in enumerate(members):
            if cancelled and cancelled():
                raise ArchiveError("취소했습니다.")
            target = _safe_target(dest, _member_name(info))
            if target is None:
                continue                 # 폴더 밖을 가리키는 이름은 건너뜀
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if progress:
                progress(n + 1, len(members), os.path.basename(target))


def _extract_tar(path, dest, progress, cancelled):
    try:
        archive = tarfile.open(path)
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"TAR 파일을 열 수 없습니다 ({os.path.basename(path)}): {exc}") from exc
    with archive:
        members = [m for m in archive.getmembers() if m.isfile()]   # 링크 · 장치 파일은 건너뜀
        _check_space(dest, sum(m.size for m in members))
        for n, member in enumerate(members):
            if cancelled and cancelled():
                raise ArchiveError("취소했습니다.")
            name = member.name.replace("\\", "/")
            if os.path.basename(name).startswith("._") or name.startswith(SKIP_PREFIXES):
                continue
            target = _safe_target(dest, name)
            if target is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = archive.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if progress:
                progress(n + 1, len(members), os.path.basename(target))


def _system_tar():
    """운영체제의 tar (libarchive: 7z · RAR · ISO도 읽음)"""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = os.path.join(root, "System32", "tar.exe")
        return candidate if os.path.exists(candidate) else shutil.which("tar")
    return "/usr/bin/tar" if os.path.exists("/usr/bin/tar") else shutil.which("bsdtar") or shutil.which("tar")


def _extract_system(path, dest, cancelled):
    """7z · RAR 등: 운영체제 tar로 풂 (폴더 밖 경로 · 절대 경로는 tar가 스스로 막음)"""
    import time
    tool = _system_tar()
    name = os.path.basename(path)
    if not tool:
        raise ArchiveError(f"이 컴퓨터에서는 {name}을(를) 풀 수 없습니다 (tar 없음). 압축 프로그램으로 먼저 푸세요.")
    try:
        _check_space(dest, os.path.getsize(path) * 3)     # 대략: 압축 전 크기를 알 수 없어 어림
    except OSError:
        pass
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    proc = subprocess.Popen([tool, "-xf", path, "-C", dest], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=flags)
    while proc.poll() is None:
        if cancelled and cancelled():
            proc.kill()
            raise ArchiveError("취소했습니다.")
        time.sleep(0.1)
    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        low = err.lower()
        if "passphrase" in low or "encrypt" in low or "password" in low:
            raise ArchiveError(f"암호가 걸린 압축파일이라 풀 수 없습니다: {name}\n압축 프로그램으로 먼저 풀어서 폴더를 여세요.")
        raise ArchiveError(f"{name}을(를) 풀 수 없습니다: {err.splitlines()[-1] if err else proc.returncode}\n"
                           "압축 프로그램으로 먼저 풀어서 폴더를 여세요.")
    for root, _dirs, files in os.walk(dest):               # macOS가 만든 ._ 파일 정리
        for fn in files:
            if fn.startswith("._"):
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass


def find_archives(dirpath, recursive=True):
    """폴더 안의 ZIP 파일들"""
    found = []
    if recursive:
        for root, _dirs, files in os.walk(dirpath):
            found.extend(os.path.join(root, f) for f in files if is_archive_name(f))
    else:
        found = [os.path.join(dirpath, f) for f in os.listdir(dirpath)
                 if is_archive_name(f) and os.path.isfile(os.path.join(dirpath, f))]
    return found
