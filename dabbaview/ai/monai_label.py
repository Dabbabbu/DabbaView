# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
MONAI Label 서버 클라이언트 (REST API, 표준 라이브러리만 사용)

  GET  /info/                              모델·라벨 목록
  POST /infer/{model}?output=image         영상(NIfTI) 업로드 → 라벨 NIfTI
  PUT  /datastore/?image={id}              영상을 서버 데이터스토어에 등록
  PUT  /datastore/label?image={id}&tag=final   수정한 라벨 제출 (active learning)
  POST /train/{model}                      학습 시작

서버로는 픽셀 볼륨(NIfTI)만 보낸다 - 환자 이름·ID 등 DICOM 태그는 전송하지 않음.
"""
import hashlib
import json
import tempfile
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

import numpy as np

from . import nifti

TIMEOUT = 600  # 추론은 오래 걸릴 수 있음


class MonaiLabelError(Exception):
    pass


def image_id_for(series_uid):
    """서버 데이터스토어용 영상 ID (SeriesInstanceUID를 그대로 보내지 않음)"""
    return "dv_" + hashlib.sha1(series_uid.encode()).hexdigest()[:16]


def _multipart(fields, files):
    """multipart/form-data 본문 → (body, content_type)"""
    boundary = uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{name}"'.encode(), b"",
                  value.encode() if isinstance(value, str) else value]
    for name, (filename, data) in files.items():
        lines += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
                  b"Content-Type: application/octet-stream", b"", data]
    lines += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


class MonaiLabelClient:
    def __init__(self, url, token=""):
        self.url = url.rstrip("/")
        self.token = token

    def _request(self, method, path, query=None, body=None, content_type=None,
                 timeout=TIMEOUT):
        if not self.url:
            raise MonaiLabelError("MONAI Label 서버 주소가 설정되지 않았습니다 (Settings → AI).")
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise MonaiLabelError(f"서버 오류 {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise MonaiLabelError(f"서버에 연결할 수 없습니다: {e}") from e

    # ─── API ───

    def info(self):
        data, _ = self._request("GET", "/info/", timeout=15)
        try:
            return json.loads(data)
        except ValueError as e:
            raise MonaiLabelError("서버 응답이 MONAI Label 형식이 아닙니다.") from e

    @staticmethod
    def models(info):
        """[(이름, 종류, {라벨이름: 번호})]"""
        result = []
        for name, model in (info.get("models") or {}).items():
            labels = model.get("labels") or {}
            if isinstance(labels, list):
                labels = {n: i + 1 for i, n in enumerate(labels)}
            result.append((name, model.get("type", ""), dict(labels)))
        return result

    def infer(self, model, volume, params=None):
        """볼륨 → 라벨 배열 (k, row, col) uint8"""
        body, ctype = _multipart(
            {"params": json.dumps(params or {})},
            {"file": ("image.nii.gz", _nifti_gz(volume.array.astype(np.float32),
                                                volume.affine_ras))})
        data, content_type = self._request("POST", f"/infer/{urllib.parse.quote(model)}",
                                           {"output": "image"}, body, ctype)
        if "json" in content_type:
            raise MonaiLabelError("서버가 라벨 영상 대신 JSON을 보냈습니다: "
                                  + data[:200].decode("utf-8", "replace"))
        if data[:2] == b"--" or b"Content-Disposition" in data[:300]:
            data = _first_multipart_file(data, content_type)
        array, _affine = nifti.from_bytes(data)
        if array.shape != volume.shape:
            # 일부 서버는 (col, row, k) 순서를 그대로 돌려줌
            if array.shape[::-1] == volume.shape:
                array = array.transpose(2, 1, 0)
            else:
                raise MonaiLabelError(f"결과 크기 {array.shape}가 영상 {volume.shape}와 다릅니다.")
        return np.clip(np.rint(array), 0, 255).astype(np.uint8)

    def upload_image(self, image_id, volume):
        body, ctype = _multipart(
            {"params": json.dumps({"client_id": "DabbaView"})},
            {"file": ("image.nii.gz", _nifti_gz(volume.array.astype(np.float32),
                                                volume.affine_ras))})
        self._request("PUT", "/datastore/", {"image": image_id}, body, ctype)

    def submit_label(self, image_id, mask, volume, label_info):
        """수정한 라벨을 최종(final) 라벨로 제출. label_info: [{name, idx}]"""
        body, ctype = _multipart(
            {"params": json.dumps({"label_info": label_info, "client_id": "DabbaView"})},
            {"label": ("label.nii.gz", _nifti_gz(mask.astype(np.uint8), volume.affine_ras))})
        self._request("PUT", "/datastore/label", {"image": image_id, "tag": "final"},
                      body, ctype)

    def train(self, model):
        data, _ = self._request("POST", f"/train/{urllib.parse.quote(model)}",
                                {"run_sync": "false"}, b"{}", "application/json", timeout=30)
        return data.decode("utf-8", "replace")


def _nifti_gz(array, affine):
    path = os.path.join(tempfile.mkdtemp(prefix="dv_monai_"), "x.nii.gz")
    try:
        nifti.save(path, array, affine)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass


def _first_multipart_file(data, content_type):
    """multipart 응답에서 첫 번째 파일 부분 추출 (라벨 + JSON을 함께 보내는 서버)"""
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"').encode()
    if boundary is None:
        boundary = data.split(b"\r\n", 1)[0][2:]
    for chunk in data.split(b"--" + boundary):
        head, _, body = chunk.partition(b"\r\n\r\n")
        if b"filename=" in head:
            return body.rstrip(b"\r\n")
    raise MonaiLabelError("응답에서 라벨 파일을 찾지 못했습니다.")
