# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Google Drive - google-api-python-client + google-auth-oauthlib

로그인: 데스크톱 OAuth (브라우저 → http://localhost 리디렉션). 권한: drive.readonly
사용자가 Google Cloud Console에서 만든 'Desktop app' OAuth 클라이언트의
Client ID / Client Secret을 Settings → Cloud에 입력한다.
"""
import json
import os

from . import CloudError, CloudItem, NotConfigured, safe_name

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
TOKEN_KEY = "google_drive_token"
SECRET_KEY = "google_client_secret"
FIELDS = ("nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum, "
          "shortcutDetails)")

SETUP_HELP = (
    "Google Drive를 쓰려면 본인의 OAuth 클라이언트가 필요합니다 (앱에 내장된 키 없음).\n\n"
    "1. console.cloud.google.com → 프로젝트 만들기\n"
    "2. API 및 서비스 → 라이브러리 → 'Google Drive API' 사용 설정\n"
    "3. OAuth 동의 화면 설정 (테스트 사용자에 본인 계정 추가)\n"
    "4. 사용자 인증 정보 → OAuth 클라이언트 ID 만들기 → 유형 '데스크톱 앱'\n"
    "5. 발급된 Client ID와 Client Secret을 Settings → Cloud에 입력\n"
    "   (데스크톱 앱의 Client Secret은 Google 방식상 필요한 값입니다)")


class GoogleDriveProvider:
    setup_help = SETUP_HELP
    name = "Google Drive"
    key = "google"

    def __init__(self, app_settings, store):
        self.settings = app_settings
        self.store = store
        self._creds = None
        self._account = ""

    # ─── 설정 / 로그인 ───

    def client_config(self):
        client_id = self.settings.google_client_id()
        secret = self.store.get(SECRET_KEY) or ""
        if not client_id or not secret:
            raise NotConfigured(SETUP_HELP)
        return {"installed": {
            "client_id": client_id, "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"]}}

    def is_configured(self):
        try:
            self.client_config()
            return True
        except NotConfigured:
            return False

    def _load_saved(self):
        from google.oauth2.credentials import Credentials
        raw = self.store.get(TOKEN_KEY)
        if not raw:
            return None
        try:
            return Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
        except (ValueError, KeyError):
            return None

    def _save(self, creds):
        self.store.set(TOKEN_KEY, creds.to_json())

    def sign_in(self, interactive=True):
        """저장된 토큰 사용(필요하면 갱신), 없으면 브라우저 로그인. 블로킹 - 작업 스레드에서"""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        config = self.client_config()
        creds = self._load_saved()
        if creds is not None and creds.client_id != config["installed"]["client_id"]:
            creds = None   # 다른 클라이언트로 바꾼 경우
        if creds is not None and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._save(creds)
                except RefreshError:
                    creds = None
            else:
                creds = None
        if creds is None:
            if not interactive:
                raise CloudError("로그인이 필요합니다.")
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_config(config, SCOPES)
            creds = flow.run_local_server(
                port=0, open_browser=True, timeout_seconds=300,
                authorization_prompt_message="",
                success_message="DabbaView: Google 로그인 완료. 이 창을 닫고 앱으로 돌아가세요.")
            if creds is None:
                raise CloudError("로그인이 취소되었거나 시간이 초과되었습니다.")
            self._save(creds)
        self._creds = creds
        try:
            about = self._service().about().get(fields="user(emailAddress, displayName)").execute()
            user = about.get("user", {})
            self._account = user.get("emailAddress") or user.get("displayName", "")
        except Exception:  # noqa: BLE001 - 계정 표시는 부가 정보
            self._account = ""
        return self._account

    def sign_out(self):
        self.store.delete(TOKEN_KEY)
        self._creds = None
        self._account = ""

    @property
    def account(self):
        return self._account

    def _service(self):
        """스레드마다 새 서비스 (httplib2는 스레드 간 공유 불가)"""
        from googleapiclient.discovery import build
        if self._creds is None:
            raise CloudError("로그인이 필요합니다.")
        api_key = self.settings.google_api_key() or None
        return build("drive", "v3", credentials=self._creds, developerKey=api_key,
                     cache_discovery=False, static_discovery=True)

    # ─── 목록 ───

    def roots(self):
        items = [CloudItem("root", "내 드라이브", True, extra={"root": "my"}),
                 CloudItem("__shared__", "공유 문서함", True, extra={"root": "shared"})]
        try:
            drives = self._service().drives().list(pageSize=100).execute().get("drives", [])
            for d in drives:
                items.append(CloudItem(d["id"], f"공유 드라이브: {d['name']}", True,
                                       extra={"root": "drive", "drive_id": d["id"]}))
        except Exception:  # noqa: BLE001 - 공유 드라이브 권한이 없을 수 있음
            pass
        return items

    def list_children(self, folder):
        if folder.extra.get("root") == "shared":
            query = "sharedWithMe and trashed = false"
        else:
            query = f"'{folder.id}' in parents and trashed = false"
        params = {"q": query, "fields": FIELDS, "pageSize": 1000,
                  "orderBy": "folder,name_natural", "supportsAllDrives": True,
                  "includeItemsFromAllDrives": True}
        drive_id = folder.extra.get("drive_id")
        if drive_id:
            params.update(corpora="drive", driveId=drive_id)
        service = self._service()
        items, token = [], None
        while True:
            if token:
                params["pageToken"] = token
            result = service.files().list(**params).execute()
            for f in result.get("files", []):
                items.append(self._item(f, drive_id))
            token = result.get("nextPageToken")
            if not token:
                return items

    @staticmethod
    def _item(f, drive_id=None):
        mime = f.get("mimeType", "")
        file_id = f["id"]
        if mime == SHORTCUT:   # 바로가기 → 원본
            details = f.get("shortcutDetails", {})
            file_id = details.get("targetId", file_id)
            mime = details.get("targetMimeType", mime)
        is_folder = mime == FOLDER
        native = mime.startswith("application/vnd.google-apps.") and not is_folder
        return CloudItem(file_id, f.get("name", ""), is_folder, int(f.get("size", 0) or 0),
                         (f.get("modifiedTime") or "")[:16].replace("T", " "),
                         downloadable=not native,
                         extra={"mime": mime, "drive_id": drive_id,
                                # 캐시 버전: 수정 시각 + 내용 해시 (바뀌면 다시 받음)
                                "version": f"{f.get('modifiedTime', '')}|"
                                           f"{f.get('md5Checksum') or f.get('size', '')}"})

    # ─── 다운로드 ───

    def download(self, item, dest_dir, progress=None, cancelled=None):
        """파일 또는 폴더(하위 전체)를 dest_dir에 → 로컬 경로"""
        if item.is_folder:
            target = os.path.join(dest_dir, safe_name(item.name))
            os.makedirs(target, exist_ok=True)
            for child in self.list_children(item):
                if cancelled and cancelled():
                    raise CloudError("취소했습니다.")
                if child.is_folder or child.downloadable:
                    self.download(child, target, progress, cancelled)
            return target
        return self.download_file(item, os.path.join(dest_dir, safe_name(item.name)),
                                  progress, cancelled)

    def download_file(self, item, path, progress=None, cancelled=None):
        """파일 하나를 path에 저장"""
        if not item.downloadable:
            raise CloudError(f"'{item.name}'은(는) Google 문서 형식이라 내려받을 수 없습니다.")
        from googleapiclient.http import MediaIoBaseDownload
        request = self._service().files().get_media(fileId=item.id, supportsAllDrives=True)
        with open(path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                if cancelled and cancelled():
                    raise CloudError("취소했습니다.")
                status, done = downloader.next_chunk()
                if progress:
                    progress(item.name, int(status.resumable_progress) if status else item.size)
        return path
