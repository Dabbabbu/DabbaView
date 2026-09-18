"""
DICOM 네트워크 (pynetdicom): C-ECHO, C-STORE (Send), Basic Grayscale Print

Qt에 의존하지 않는 순수 함수. 진행률 콜백과 취소 이벤트를 받는다.
node dict: {"name", "ae_title", "host", "port", "type": "storage" | "print"}
"""
from pydicom.dataset import Dataset
from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                         generate_uid)
from pynetdicom import AE
from pynetdicom.sop_class import (Verification, BasicGrayscalePrintManagementMeta,
                                  BasicFilmSession, BasicFilmBox,
                                  BasicGrayscaleImageBox)

FILM_SIZES = ["8INX10IN", "10INX12IN", "11INX14IN", "14INX14IN", "14INX17IN",
              "24CMX24CM", "24CMX30CM"]
FILM_FORMATS = ["1,1", "1,2", "2,1", "2,2", "2,3", "3,3", "3,4", "4,4", "4,5"]
MEDIUM_TYPES = ["BLUE FILM", "CLEAR FILM", "PAPER"]
MAGNIFICATION_TYPES = ["REPLICATE", "BILINEAR", "CUBIC", "NONE"]
ORIENTATIONS = ["PORTRAIT", "LANDSCAPE"]

TIMEOUT = 15


class DicomNetError(Exception):
    pass


class Cancelled(Exception):
    pass


def _make_ae(local_ae):
    ae = AE(ae_title=(local_ae or "RADIANTVIEW")[:16])
    ae.acse_timeout = TIMEOUT
    ae.dimse_timeout = TIMEOUT * 2
    ae.network_timeout = TIMEOUT
    return ae


def _associate(ae, node):
    try:
        port = int(node["port"])
    except (KeyError, TypeError, ValueError):
        raise DicomNetError("포트 번호가 올바르지 않습니다.")
    assoc = ae.associate(node["host"], port, ae_title=node["ae_title"][:16])
    if not assoc.is_established:
        raise DicomNetError(
            f"{node['ae_title']}@{node['host']}:{port} 연결 실패 "
            "(주소/포트/AE Title 또는 상대편 허용 목록을 확인하세요)")
    return assoc


def _status_ok(status):
    code = getattr(status, "Status", None)
    # 0x0000 성공, 0xB000/0xB007/0xB006 경고(저장됨)
    return code is not None and (code == 0x0000 or (code & 0xF000) == 0xB000)


def echo(node, local_ae):
    """C-ECHO → (성공 여부, 메시지)"""
    ae = _make_ae(local_ae)
    ae.add_requested_context(Verification)
    try:
        assoc = _associate(ae, node)
    except DicomNetError as e:
        return False, str(e)
    try:
        status = assoc.send_c_echo()
        ok = _status_ok(status)
        return ok, "C-ECHO 성공" if ok else f"C-ECHO 실패 (status {getattr(status, 'Status', '?')})"
    finally:
        assoc.release()


def send_datasets(node, local_ae, datasets, progress=None, cancel_event=None):
    """C-STORE로 전송 → (성공 수, 실패 수)

    datasets: 픽셀 포함 전체 Dataset 목록 (파일에서 읽은 것)
    """
    datasets = list(datasets)
    if not datasets:
        return 0, 0
    ae = _make_ae(local_ae)
    contexts = set()
    for ds in datasets:
        ts = str(ds.file_meta.TransferSyntaxUID) if hasattr(ds, "file_meta") else None
        contexts.add((str(ds.SOPClassUID), ts))
    for sop_class in sorted({c[0] for c in contexts}):
        syntaxes = [ts for sc, ts in contexts if sc == sop_class and ts]
        for ts in (ExplicitVRLittleEndian, ImplicitVRLittleEndian):
            if ts not in syntaxes:
                syntaxes.append(ts)
        ae.add_requested_context(sop_class, syntaxes)

    assoc = _associate(ae, node)
    sent = failed = 0
    try:
        for i, ds in enumerate(datasets):
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled()
            try:
                status = assoc.send_c_store(ds)
                if _status_ok(status):
                    sent += 1
                else:
                    failed += 1
            except (ValueError, RuntimeError):
                failed += 1  # 협상되지 않은 전송 구문 등
            if progress:
                progress(i + 1, len(datasets))
    finally:
        assoc.release()
    return sent, failed


def _image_box_dataset(position, image):
    """Basic Grayscale Image Box (N-SET) - 8비트 사전 렌더링 영상"""
    rows, cols = image.shape
    pixel = Dataset()
    pixel.SamplesPerPixel = 1
    pixel.PhotometricInterpretation = "MONOCHROME2"
    pixel.Rows = rows
    pixel.Columns = cols
    pixel.PixelAspectRatio = [1, 1]
    pixel.BitsAllocated = 8
    pixel.BitsStored = 8
    pixel.HighBit = 7
    pixel.PixelRepresentation = 0
    pixel.PixelData = image.tobytes()
    box = Dataset()
    box.ImageBoxPosition = position
    box.BasicGrayscaleImageSequence = [pixel]
    return box


def print_images(node, local_ae, images, options, progress=None, cancel_event=None):
    """Basic Grayscale Print Management로 인쇄 → 필름 매수

    images: uint8 2D 배열 목록
    options: film_size, film_format('2,2'), orientation, medium, magnification, copies
    """
    images = list(images)
    if not images:
        raise DicomNetError("인쇄할 영상이 없습니다.")
    cols, rows = (int(v) for v in options.get("film_format", "1,1").split(","))
    per_film = cols * rows

    ae = _make_ae(local_ae)
    ae.add_requested_context(BasicGrayscalePrintManagementMeta)
    assoc = _associate(ae, node)
    meta = BasicGrayscalePrintManagementMeta
    films = 0
    try:
        session = Dataset()
        session.NumberOfCopies = int(options.get("copies", 1))
        session.PrintPriority = "MED"
        session.MediumType = options.get("medium", "BLUE FILM")
        session.FilmDestination = "MAGAZINE"
        session_uid = generate_uid()
        status, _ = assoc.send_n_create(session, BasicFilmSession, session_uid,
                                        meta_uid=meta)
        if not _status_ok(status):
            raise DicomNetError(f"Film Session 생성 실패 (status {getattr(status, 'Status', '?')})")

        done = 0
        for start in range(0, len(images), per_film):
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled()
            film = Dataset()
            film.ImageDisplayFormat = f"STANDARD\\{cols},{rows}"
            film.FilmOrientation = options.get("orientation", "PORTRAIT")
            film.FilmSizeID = options.get("film_size", "14INX17IN")
            film.MagnificationType = options.get("magnification", "REPLICATE")
            ref = Dataset()
            ref.ReferencedSOPClassUID = BasicFilmSession
            ref.ReferencedSOPInstanceUID = session_uid
            film.ReferencedFilmSessionSequence = [ref]
            film_uid = generate_uid()
            status, attrs = assoc.send_n_create(film, BasicFilmBox, film_uid,
                                                meta_uid=meta)
            if not _status_ok(status) or attrs is None:
                raise DicomNetError(f"Film Box 생성 실패 (status {getattr(status, 'Status', '?')})")
            boxes = list(getattr(attrs, "ReferencedImageBoxSequence", []))
            if len(boxes) < min(per_film, len(images) - start):
                raise DicomNetError("프린터가 반환한 Image Box 수가 부족합니다.")

            for pos, image in enumerate(images[start:start + per_film]):
                box_uid = boxes[pos].ReferencedSOPInstanceUID
                status, _ = assoc.send_n_set(_image_box_dataset(pos + 1, image),
                                             BasicGrayscaleImageBox, box_uid,
                                             meta_uid=meta)
                if not _status_ok(status):
                    raise DicomNetError(f"영상 {start + pos + 1} 전송 실패")
                done += 1
                if progress:
                    progress(done, len(images))

            status, _ = assoc.send_n_action(None, 1, BasicFilmBox, film_uid,
                                            meta_uid=meta)
            if not _status_ok(status):
                raise DicomNetError(f"인쇄 명령 실패 (status {getattr(status, 'Status', '?')})")
            films += 1

        assoc.send_n_delete(BasicFilmSession, session_uid, meta_uid=meta)
    finally:
        assoc.release()
    return films
