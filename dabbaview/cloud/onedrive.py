# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
OneDrive - msal (로그인) + requests (Microsoft Graph)

로그인: 시스템 브라우저 → http://localhost 리디렉션 (MSAL interactive).
사용자가 Azure Portal에서 등록한 앱의 Application (client) ID를 Settings → Cloud에 입력.
권한: Files.Read.All (위임), 개인 계정·회사/학교 계정 모두 (authority = common)
"""
import os

from . import CloudError, CloudItem, NotConfigured, safe_name

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Files.Read.All"]
CACHE_KEY = "onedrive_token_cache"
SELECT = ("id,name,size,folder,file,package,lastModifiedDateTime,remoteItem,parentReference,"
          "cTag,eTag")

SETUP_HELP = (
    "OneDrive를 쓰려면 본인이 등록한 Azure 앱이 필요합니다 (앱에 내장된 키 없음).\n\n"
    "1. portal.azure.com → Microsoft Entra ID → 앱 등록 → 새 등록\n"
    "2. 지원되는 계정 유형: '모든 조직 디렉터리의 계정 및 개인 Microsoft 계정'\n"
    "3. 인증 → 플랫폼 추가 → '모바일 및 데스크톱 애플리케이션' → 리디렉션 URI: http://localhost\n"
    "4. API 사용 권한 → Microsoft Graph → 위임된 권한 → Files.Read.All 추가\n"
    "5. 개요의 '애플리케이션(클라이언트) ID'를 Settings → Cloud에 입력")


class OneDriveProvider:
    setup_help = SETUP_HELP
    name = "OneDrive"
    key = "onedrive"

    def __init__(self, app_settings, store, graph=GRAPH, authority=AUTHORITY):
        self.settings = app_settings
        self.store = store
        self.graph = graph.rstrip("/")
        self.authority = authority
        self._token = None
        self._account = ""
        self._app = None
        self._cache = None

    # ─── 설정 / 로그인 ───

    def client_id(self):
        cid = self.settings.onedrive_client_id()
        if not cid:
            raise NotConfigured(SETUP_HELP)
        return cid

    def is_configured(self):
        return bool(self.settings.onedrive_client_id())

    def _msal_app(self):
        import msal
        cid = self.client_id()
        if self._app is None or self._app.client_id != cid:
            self._cache = msal.SerializableTokenCache()
            saved = self.store.get(CACHE_KEY)
            if saved:
                try:
                    self._cache.deserialize(saved)
                except ValueError:
                    pass
            self._app = msal.PublicClientApplication(cid, authority=self.authority,
                                                     token_cache=self._cache)
        return self._app

    def _save_cache(self):
        if self._cache is not None and self._cache.has_state_changed:
            self.store.set(CACHE_KEY, self._cache.serialize())

    def sign_in(self, interactive=True):
        app = self._msal_app()
        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            if not interactive:
                raise CloudError("로그인이 필요합니다.")
            result = app.acquire_token_interactive(
                SCOPES, prompt="select_account", timeout=300,
                success_template="<h3>DabbaView: Microsoft 로그인 완료. 이 창을 닫고 앱으로 돌아가세요.</h3>",
                error_template="<h3>DabbaView: 로그인 실패 - {error}: {error_description}</h3>")
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description") or (result or {}).get("error") or ""
            raise CloudError(f"Microsoft 로그인에 실패했습니다.\n{detail}".strip())
        self._save_cache()
        self._token = result["access_token"]
        claims = result.get("id_token_claims") or {}
        self._account = claims.get("preferred_username") or claims.get("name") or (
            accounts[0].get("username", "") if accounts else "")
        return self._account

    def sign_out(self):
        # 토큰 캐시(계정·리프레시 토큰)를 통째로 지움 - 네트워크 없이
        self.store.delete(CACHE_KEY)
        self._cache = None
        self._token = None
        self._account = ""
        self._app = None

    @property
    def account(self):
        return self._account

    # ─── Graph ───

    def _get(self, url, stream=False, headers=None):
        import requests
        if not self._token:
            raise CloudError("로그인이 필요합니다.")
        if not url.startswith("http"):
            url = self.graph + url

        def head():
            return dict({"Authorization": f"Bearer {self._token}"}, **(headers or {}))
        resp = requests.get(url, headers=head(), stream=stream, timeout=60)
        if resp.status_code == 401:
            self.sign_in(interactive=False)   # 만료 → 조용히 갱신 후 한 번 더
            resp = requests.get(url, headers=head(), stream=stream, timeout=60)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", {}).get("message", resp.text[:200])
            except ValueError:
                message = resp.text[:200]
            raise CloudError(f"OneDrive {resp.status_code}: {message}")
        return resp

    def roots(self):
        return [CloudItem("root", "내 OneDrive", True, extra={"root": "my"}),
                CloudItem("__shared__", "공유 항목", True, extra={"root": "shared"})]

    def _children_url(self, folder):
        if folder.extra.get("root") == "my":
            return f"/me/drive/root/children?$top=999&$select={SELECT}"
        if folder.extra.get("root") == "shared":
            return "/me/drive/sharedWithMe"
        drive = folder.extra.get("drive_id")
        base = f"/drives/{drive}/items/{folder.id}" if drive else f"/me/drive/items/{folder.id}"
        return f"{base}/children?$top=999&$select={SELECT}"

    def search(self, text, folders_only=True, limit=200):
        """이름으로 OneDrive 전체 검색"""
        from urllib.parse import quote
        url = f"{GRAPH}/me/drive/root/search(q='{quote(str(text or ''))}')?$top={min(200, limit)}"
        items = []
        while url and len(items) < limit:
            data = self._get(url).json()
            for entry in data.get("value", []):
                item = self._item(entry)
                if not folders_only or item.is_folder:
                    items.append(item)
            url = data.get("@odata.nextLink")
        items.sort(key=lambda i: (not i.is_folder, i.name.lower()))
        return items

    def list_children(self, folder):
        url = self._children_url(folder)
        items = []
        while url:
            data = self._get(url).json()
            for entry in data.get("value", []):
                items.append(self._item(entry))
            url = data.get("@odata.nextLink")
        items.sort(key=lambda i: (not i.is_folder, i.name.lower()))
        return items

    @staticmethod
    def _item(entry):
        remote = entry.get("remoteItem")
        source = remote or entry
        drive_id = (source.get("parentReference") or {}).get("driveId")
        is_folder = "folder" in source or ("package" in source and "file" not in source)
        return CloudItem(source.get("id", entry.get("id")), entry.get("name", ""), is_folder,
                         int(source.get("size", 0) or 0),
                         (source.get("lastModifiedDateTime") or "")[:16].replace("T", " "),
                         extra={"drive_id": drive_id,
                                # 캐시 버전: 내용 태그(cTag) → 없으면 eTag/수정 시각+크기
                                "version": source.get("cTag") or source.get("eTag") or
                                f"{source.get('lastModifiedDateTime', '')}|{source.get('size', '')}"})

    def download(self, item, dest_dir, progress=None, cancelled=None):
        if item.is_folder:
            target = os.path.join(dest_dir, safe_name(item.name))
            os.makedirs(target, exist_ok=True)
            for child in self.list_children(item):
                if cancelled and cancelled():
                    raise CloudError("취소했습니다.")
                self.download(child, target, progress, cancelled)
            return target
        return self.download_file(item, os.path.join(dest_dir, safe_name(item.name)),
                                  progress, cancelled)

    def download_head(self, item, path, length):
        """파일 앞부분 length 바이트만 받아 저장 → 실제로 받은 바이트 수"""
        drive = item.extra.get("drive_id")
        url = (f"/drives/{drive}/items/{item.id}/content" if drive
               else f"/me/drive/items/{item.id}/content")
        resp = self._get(url, stream=True,
                         headers={"Range": f"bytes=0-{max(0, int(length) - 1)}"})
        data = resp.content
        with open(path, "wb") as fh:
            fh.write(data)
        return len(data)

    def download_file(self, item, path, progress=None, cancelled=None):
        """파일 하나를 path에 저장"""
        drive = item.extra.get("drive_id")
        url = (f"/drives/{drive}/items/{item.id}/content" if drive
               else f"/me/drive/items/{item.id}/content")
        resp = self._get(url, stream=True)
        done = 0
        with open(path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if cancelled and cancelled():
                    raise CloudError("취소했습니다.")
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(item.name, done)
        return path
