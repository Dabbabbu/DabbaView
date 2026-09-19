# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Help → Deploy Web: DabbaView-Web(GitHub Pages)을 GitHub Actions로 다시 배포

1. (선택) 웹 package.json / package-lock.json 버전을 앱 __version__으로 맞춤
   - Git Data API로 두 파일을 한 커밋에 ([skip ci] → push 배포는 건너뜀)
2. workflow_dispatch로 deploy.yml 실행
3. 실행 상태를 폴링해 "Deploying..." → "Deploy complete!" + 사이트 주소

토큰: gh CLI에 로그인되어 있으면 `gh auth token`을 쓰고(저장 안 함),
아니면 한 번 입력받아 QSettings에 저장한다. 필요한 권한: Actions(쓰기),
Contents(쓰기, 버전 동기화 시), Pages(읽기).
"""
import base64
import datetime
import email.utils
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QVBoxLayout)

from . import __version__
from .net_ssl import ssl_context

API = "https://api.github.com"
POLL_SECONDS = 5
TIMEOUT_SECONDS = 30 * 60


class GitHubError(Exception):
    pass


# ─── 토큰 ───

def gh_cli_path():
    """앱 번들은 PATH가 짧으므로 흔한 설치 위치도 확인"""
    for candidate in (shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh",
                      "/usr/bin/gh"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def gh_cli_token():
    gh = gh_cli_path()
    if not gh:
        return None
    try:
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


# ─── GitHub REST API ───

class GitHub:
    def __init__(self, token, api=API):
        self.token = token
        self.api = api.rstrip("/")
        self.server_time = None   # 마지막 응답의 Date 헤더 (실행 찾기에 사용)

    def request(self, method, path, body=None, expect_json=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", f"DabbaView/{__version__}")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                date = resp.headers.get("Date")
                if date:
                    self.server_time = email.utils.parsedate_to_datetime(date)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except ValueError:
                pass
            hint = {401: "토큰이 올바르지 않거나 만료되었습니다.",
                    403: "권한이 없습니다 (토큰에 Actions·Contents 쓰기 권한 필요).",
                    404: "저장소·워크플로를 찾을 수 없거나 토큰에 접근 권한이 없습니다.",
                    422: "요청이 거부되었습니다 (브랜치 이름·workflow_dispatch 트리거 확인)."
                    }.get(e.code, "")
            raise GitHubError(f"GitHub {e.code}: {detail}\n{hint}".strip()) from e
        except (urllib.error.URLError, OSError) as e:
            raise GitHubError(f"GitHub에 연결할 수 없습니다: {e}") from e
        if not expect_json or not raw:
            return None
        return json.loads(raw)

    # 파일
    def get_file(self, repo, path, ref):
        info = self.request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        return base64.b64decode(info["content"]).decode("utf-8")

    def commit_files(self, repo, branch, files, message):
        """{경로: 내용}을 한 커밋으로 (Git Data API)"""
        ref = self.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        parent = ref["object"]["sha"]
        base_tree = self.request("GET", f"/repos/{repo}/git/commits/{parent}")["tree"]["sha"]
        entries = []
        for path, content in files.items():
            blob = self.request("POST", f"/repos/{repo}/git/blobs",
                                {"content": content, "encoding": "utf-8"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self.request("POST", f"/repos/{repo}/git/trees",
                            {"base_tree": base_tree, "tree": entries})
        commit = self.request("POST", f"/repos/{repo}/git/commits",
                              {"message": message, "tree": tree["sha"], "parents": [parent]})
        self.request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"]})
        return commit["sha"]

    # Actions
    def dispatch(self, repo, workflow, branch):
        self.request("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
                     {"ref": branch}, expect_json=False)

    def find_dispatched_run(self, repo, workflow, branch, since):
        runs = self.request("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs"
                                   f"?event=workflow_dispatch&branch={branch}&per_page=10")
        for run in runs.get("workflow_runs", []):
            created = datetime.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if created >= since:
                return run
        return None

    def run(self, repo, run_id):
        return self.request("GET", f"/repos/{repo}/actions/runs/{run_id}")

    def current_step(self, repo, run_id):
        jobs = self.request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
        for job in jobs.get("jobs", []):
            if job.get("status") != "completed":
                for step in job.get("steps", []):
                    if step.get("status") == "in_progress":
                        return f"{job['name']}: {step['name']}"
                return job["name"]
        return ""

    def pages_url(self, repo):
        try:
            return self.request("GET", f"/repos/{repo}/pages").get("html_url")
        except GitHubError:
            return None


# ─── 버전 동기화 ───

def set_package_version(package_json, version):
    """package.json 문자열의 최상위 "version"만 바꿈 (나머지 서식 유지)"""
    new = re.sub(r'("version"\s*:\s*")[^"]*(")', rf"\g<1>{version}\g<2>", package_json, count=1)
    _check_only_versions_changed(package_json, new, version, lock=False)
    return new


def set_lock_version(lock_json, version):
    """package-lock.json의 루트 version과 packages[""].version"""
    new = re.sub(r'("version"\s*:\s*")[^"]*(")', rf"\g<1>{version}\g<2>", lock_json, count=2)
    _check_only_versions_changed(lock_json, new, version, lock=True)
    return new


def _check_only_versions_changed(old, new, version, lock):
    a, b = json.loads(old), json.loads(new)
    a["version"] = version
    if lock and "" in a.get("packages", {}):
        a["packages"][""]["version"] = version
    if a != b:
        raise GitHubError("package 파일 형식이 예상과 달라 버전을 자동으로 바꾸지 않았습니다.")


# ─── 백그라운드 작업 ───

class DeployWorker(QThread):
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal(str, str)   # 사이트 주소, 실행 주소
    failed = pyqtSignal(str)

    def __init__(self, github, repo, workflow, branch, sync_version):
        super().__init__()
        self.gh = github
        self.repo, self.workflow, self.branch = repo, workflow, branch
        self.sync_version = sync_version
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self._deploy()
        except GitHubError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - 예상 못 한 오류도 대화상자에 표시
            self.failed.emit(f"{type(e).__name__}: {e}")

    def _deploy(self):
        gh, repo, branch = self.gh, self.repo, self.branch
        if self.sync_version:
            self.status.emit("버전 확인 중...")
            package = gh.get_file(repo, "package.json", branch)
            web_version = json.loads(package).get("version")
            if web_version != __version__:
                files = {"package.json": set_package_version(package, __version__)}
                try:
                    lock = gh.get_file(repo, "package-lock.json", branch)
                    files["package-lock.json"] = set_lock_version(lock, __version__)
                except GitHubError:
                    pass  # lock 파일이 없는 저장소
                sha = gh.commit_files(repo, branch, files,
                                      f"Sync version to v{__version__} (DabbaView) [skip ci]")
                self.log.emit(f"웹 버전 {web_version} → {__version__} 커밋 {sha[:7]}")
            else:
                self.log.emit(f"웹 버전이 이미 {__version__}입니다.")

        self.status.emit("Deploying... (워크플로 시작)")
        gh.dispatch(repo, self.workflow, branch)
        since = (gh.server_time or datetime.datetime.now(datetime.timezone.utc)) \
            - datetime.timedelta(seconds=15)
        self.log.emit(f"{self.workflow} 실행 요청 ({branch})")

        run = None
        started = time.monotonic()
        while run is None:
            if self._stop:
                return
            if time.monotonic() - started > 120:
                raise GitHubError("실행을 찾지 못했습니다 (2분). GitHub Actions 페이지를 확인하세요.")
            time.sleep(2)
            run = gh.find_dispatched_run(repo, self.workflow, branch, since)
        run_url = run.get("html_url", "")
        self.log.emit(f"실행 #{run.get('run_number', '')}: {run_url}")

        last = None
        while True:
            if self._stop:
                return
            if time.monotonic() - started > TIMEOUT_SECONDS:
                raise GitHubError(f"30분이 지나도 끝나지 않았습니다.\n{run_url}")
            run = gh.run(repo, run["id"])
            if run["status"] == "completed":
                break
            step = gh.current_step(repo, run["id"])
            text = f"Deploying... ({run['status']}{' · ' + step if step else ''})"
            if text != last:
                self.status.emit(text)
                last = text
            time.sleep(POLL_SECONDS)

        if run.get("conclusion") != "success":
            raise GitHubError(f"배포 실패: {run.get('conclusion')}\n{run_url}")
        site = gh.pages_url(repo) or ""
        self.finished_ok.emit(site, run_url)


# ─── 대화상자 ───

class DeployWebDialog(QDialog):
    def __init__(self, app_settings, parent=None, api=API):
        super().__init__(parent)
        self.settings = app_settings
        self.api = api
        self._worker = None
        self.setWindowTitle("Deploy Web")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._repo = QLineEdit(app_settings.deploy_repo())
        self._workflow = QLineEdit(app_settings.deploy_workflow())
        self._branch = QLineEdit(app_settings.deploy_branch())
        form.addRow("저장소:", self._repo)
        form.addRow("워크플로:", self._workflow)
        form.addRow("브랜치:", self._branch)
        token_row = QHBoxLayout()
        self._token_label = QLabel()
        token_btn = QPushButton("토큰 입력…")
        token_btn.clicked.connect(self._ask_token)
        forget_btn = QPushButton("저장된 토큰 삭제")
        forget_btn.clicked.connect(self._forget_token)
        token_row.addWidget(self._token_label, 1)
        token_row.addWidget(token_btn)
        token_row.addWidget(forget_btn)
        form.addRow("GitHub 토큰:", token_row)
        layout.addLayout(form)

        self._sync = QCheckBox(f"배포 전에 웹 버전(package.json)을 앱 버전 v{__version__}으로 맞추기")
        self._sync.setChecked(app_settings.deploy_sync_version())
        layout.addWidget(self._sync)

        self._status = QLabel("준비")
        font = self._status.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self._status.setFont(font)
        layout.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(200)
        self._log.setMinimumHeight(120)
        layout.addWidget(self._log)
        self._links = QLabel()
        self._links.setOpenExternalLinks(True)
        self._links.setTextFormat(Qt.RichText)
        self._links.setWordWrap(True)
        layout.addWidget(self._links)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._deploy_btn = QPushButton("🚀 Deploy")
        self._deploy_btn.setDefault(True)
        self._deploy_btn.clicked.connect(self._deploy)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        buttons.addWidget(self._deploy_btn)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._update_token_label()

    # 토큰
    def _token(self):
        return self.settings.github_token() or gh_cli_token()

    def _update_token_label(self):
        if self.settings.github_token():
            self._token_label.setText("저장된 토큰 사용 (이 컴퓨터 설정 파일에 평문 저장)")
        elif gh_cli_path() and gh_cli_token():
            self._token_label.setText("gh CLI 로그인 토큰 사용 (저장 안 함)")
        else:
            self._token_label.setText("없음 - 토큰을 입력하세요")

    def _ask_token(self):
        token, ok = QInputDialog.getText(
            self, "GitHub 토큰",
            "Personal access token (fine-grained: 이 저장소의 Actions·Contents 쓰기, Pages 읽기)\n"
            "한 번만 입력하면 이 컴퓨터 설정에 저장됩니다 (평문).",
            QLineEdit.Password)
        if ok and token.strip():
            self.settings.set_github_token(token.strip())
            self._update_token_label()

    def _forget_token(self):
        self.settings.set_github_token("")
        self._update_token_label()

    # 배포
    def _deploy(self):
        repo = self._repo.text().strip()
        workflow = self._workflow.text().strip()
        branch = self._branch.text().strip()
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not workflow or not branch:
            QMessageBox.warning(self, "Deploy Web", "저장소(소유자/이름)·워크플로·브랜치를 확인하세요.")
            return
        token = self._token()
        if not token:
            self._ask_token()
            token = self._token()
            if not token:
                return
        self.settings.set_deploy_target(repo, workflow, branch)
        self.settings.set_deploy_sync_version(self._sync.isChecked())
        worker = DeployWorker(GitHub(token, self.api), repo, workflow, branch,
                              self._sync.isChecked())
        worker.log.connect(self._log.appendPlainText)
        worker.status.connect(self._status.setText)
        worker.finished_ok.connect(self._done)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        self._deploy_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._links.setText("")
        self._status.setText("Deploying...")
        worker.start()

    def _done(self, site, run_url):
        self._status.setText("✅ Deploy complete!")
        self._log.appendPlainText("배포 완료")
        links = []
        if site:
            links.append(f"사이트: <a href='{site}'>{site}</a>")
        if run_url:
            links.append(f"실행 기록: <a href='{run_url}'>{run_url}</a>")
        self._links.setText("<br>".join(links))

    def _failed(self, message):
        self._status.setText("❌ 배포 실패")
        self._log.appendPlainText(message)
        QMessageBox.warning(self, "Deploy Web", message)

    def _worker_finished(self):
        self._worker = None
        self._deploy_btn.setEnabled(True)
        self._progress.setVisible(False)

    def closeEvent(self, event):
        if self._worker is not None:
            if QMessageBox.question(
                    self, "Deploy Web",
                    "배포 상태 확인을 멈출까요?\n(GitHub에서 이미 시작된 배포는 계속 진행됩니다)"
            ) != QMessageBox.Yes:
                event.ignore()
                return
            self._worker.stop()
            self._worker.wait(POLL_SECONDS * 1000 + 2000)
        super().closeEvent(event)
