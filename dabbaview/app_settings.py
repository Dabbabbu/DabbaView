# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
사용자 설정 (QSettings에 JSON으로 저장)

- 마우스 매핑 (PACS 표준 기본값)
- 윈도 프리셋 (추가/편집/삭제)
- Hanging Protocol
- DICOM 네트워크 노드 (Send / Print)
"""
import copy
import json


# ─── 마우스 매핑 ───

# 드래그 동작: 버튼(+수정키)을 누른 채 움직일 때
DRAG_ACTIONS = {
    "tool": "선택한 도구",
    "window": "Window/Level",
    "pan": "Pan (이동)",
    "zoom": "Zoom",
    "scroll": "슬라이스 스크롤",
    "roi_window": "ROI 자동 W/L (사각형)",
    "none": "없음",
}
# ROI 자동 W/L 계산 방식
ROI_WINDOW_METHODS = {
    "minmax": "Min–Max (Center=(min+max)/2, Width=max−min)",
    "mean2sd": "Mean ± 2SD (이상값에 덜 민감)",
}
# 휠 동작
WHEEL_ACTIONS = {
    "scroll": "슬라이스 이동 (1장)",
    "fast_scroll": "빠른 슬라이스 이동",
    "zoom": "Zoom",
    "none": "없음",
}
# 더블클릭 동작
DOUBLE_CLICK_ACTIONS = {
    "fit": "Fit to Window",
    "reset_window": "윈도잉 리셋 (DICOM 기본값)",
    "reset_view": "뷰 전체 리셋 (회전/반전 포함)",
    "none": "없음",
}

MOUSE_BINDING_LABELS = {
    "left_drag": ("좌클릭 드래그", DRAG_ACTIONS),
    "right_drag": ("우클릭 드래그", DRAG_ACTIONS),
    "middle_drag": ("가운데 버튼 드래그", DRAG_ACTIONS),
    "ctrl_left_drag": ("Ctrl(⌘) + 좌클릭 드래그", DRAG_ACTIONS),
    "alt_left_drag": ("Alt(⌥) + 좌클릭 드래그", DRAG_ACTIONS),
    "wheel": ("휠", WHEEL_ACTIONS),
    "ctrl_wheel": ("Ctrl(⌘) + 휠", WHEEL_ACTIONS),
    "shift_wheel": ("Shift + 휠", WHEEL_ACTIONS),
    "left_double": ("좌측 더블클릭", DOUBLE_CLICK_ACTIONS),
    "right_double": ("우측 더블클릭", DOUBLE_CLICK_ACTIONS),
}

DEFAULT_MOUSE_BINDINGS = {
    "left_drag": "tool",
    "right_drag": "window",
    "middle_drag": "pan",
    "ctrl_left_drag": "roi_window",
    "alt_left_drag": "pan",
    "wheel": "scroll",
    "ctrl_wheel": "zoom",
    "shift_wheel": "fast_scroll",
    "left_double": "fit",
    "right_double": "reset_window",
    "fast_scroll_step": 5,
    "roi_window_method": "mean2sd",  # Ctrl+드래그 ROI W/L: 평균±2SD (잡음·이상값에 강함)
}
MOUSE_BINDINGS_VERSION = 2  # 2: Ctrl+좌클릭 = Zoom → ROI 자동 W/L


class MouseBindings:
    """뷰포트들이 공유하는 마우스 매핑 (설정 변경 시 즉시 반영)"""

    def __init__(self, values=None):
        self._values = dict(DEFAULT_MOUSE_BINDINGS)
        if values:
            self.update(values)

    def update(self, values):
        for key, value in values.items():
            if key == "roi_window_method":
                if value in ROI_WINDOW_METHODS:
                    self._values[key] = value
            elif key == "fast_scroll_step":
                try:
                    self._values[key] = max(1, min(50, int(value)))
                except (TypeError, ValueError):
                    pass
            elif key in MOUSE_BINDING_LABELS:
                choices = MOUSE_BINDING_LABELS[key][1]
                if value in choices:
                    self._values[key] = value

    def get(self, key):
        return self._values.get(key, DEFAULT_MOUSE_BINDINGS.get(key))

    def to_dict(self):
        return dict(self._values)


# ─── 윈도 프리셋 ───

DEFAULT_WINDOW_PRESETS = [
    {"name": "Brain", "center": 40, "width": 80},
    {"name": "Subdural", "center": 75, "width": 215},
    {"name": "Stroke", "center": 40, "width": 40},
    {"name": "Bone", "center": 400, "width": 2000},
    {"name": "Lung", "center": -600, "width": 1600},
    {"name": "Abdomen", "center": 60, "width": 400},
    {"name": "Liver", "center": 80, "width": 150},
    {"name": "Soft Tissue", "center": 50, "width": 350},
    {"name": "Spine", "center": 50, "width": 250},
    {"name": "Mediastinum", "center": 50, "width": 350},
]


# ─── Hanging Protocol ───
# match: 모든 조건을 만족해야 적용 (빈 값은 무시)
#   modality: 정확히 일치, body: StudyDescription/BodyPartExamined/시리즈 설명에
#   포함될 키워드 중 하나 ('|'로 구분)
# slots: 칸 순서대로 SeriesDescription 키워드 (공백으로 나뉜 토큰이 모두 포함되면 일치)

DEFAULT_HANGING_PROTOCOLS = [
    {"name": "Brain MRI", "modality": "MR", "body": "BRAIN|HEAD|뇌",
     "layout": "2x2", "slots": ["T1", "T2", "FLAIR", "DWI"]},
    {"name": "Spine MRI", "modality": "MR",
     "body": "SPINE|L-SPINE|C-SPINE|T-SPINE|LSPINE|CSPINE|LUMBAR|CERVICAL|THORACIC|척추",
     "layout": "1x2", "slots": ["SAG T1", "SAG T2"]},
    {"name": "Chest CT", "modality": "CT", "body": "CHEST|LUNG|THORAX|흉부",
     "layout": "1x2", "slots": ["LUNG", "MEDIASTINUM"]},
]


# ─── DICOM 노드 ───

DEFAULT_LOCAL_AE = "DABBAVIEW"


class AppSettings:
    """QSettings 래퍼. 복합 값은 JSON 문자열로 저장 (플랫폼별 타입 차이 방지)"""

    def __init__(self, qsettings):
        self._qs = qsettings
        saved = self._load_json("mouse_bindings", {})
        # 이전 기본값(Ctrl+좌클릭=Zoom)으로 저장된 설정은 새 기본값으로 이전
        if saved.get("version", 1) < MOUSE_BINDINGS_VERSION and saved.get("ctrl_left_drag") == "zoom":
            saved["ctrl_left_drag"] = "roi_window"
        self.mouse = MouseBindings(saved)

    # 내부 헬퍼
    def _load_json(self, key, default):
        raw = self._qs.value(key, "", type=str)
        if not raw:
            return copy.deepcopy(default)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return copy.deepcopy(default)

    def _save_json(self, key, value):
        self._qs.setValue(key, json.dumps(value, ensure_ascii=False))
        self._qs.sync()

    # 마우스
    def save_mouse(self, values):
        self.mouse.update(values)
        self._save_json("mouse_bindings", dict(self.mouse.to_dict(),
                                               version=MOUSE_BINDINGS_VERSION))

    def reset_mouse(self):
        # 뷰포트들이 같은 객체를 참조하므로 새로 만들지 않고 값만 되돌림
        self.mouse.update(DEFAULT_MOUSE_BINDINGS)
        self._qs.remove("mouse_bindings")

    # 프리셋
    def window_presets(self):
        presets = self._load_json("window_presets", DEFAULT_WINDOW_PRESETS)
        clean = []
        for p in presets:
            try:
                clean.append({"name": str(p["name"]), "center": float(p["center"]),
                              "width": max(1.0, float(p["width"]))})
            except (KeyError, TypeError, ValueError):
                continue
        return clean

    def save_window_presets(self, presets):
        self._save_json("window_presets", presets)

    # Hanging Protocol
    def hanging_protocols(self):
        return self._load_json("hanging_protocols", DEFAULT_HANGING_PROTOCOLS)

    def save_hanging_protocols(self, protocols):
        self._save_json("hanging_protocols", protocols)

    def auto_hanging(self):
        return self._qs.value("auto_hanging", True, type=bool)

    def set_auto_hanging(self, enabled):
        self._qs.setValue("auto_hanging", bool(enabled))

    # Reading (기록)
    def report_folder(self):
        return self._qs.value("report_folder", "", type=str)

    def set_report_folder(self, folder):
        self._qs.setValue("report_folder", folder or "")

    def report_creator(self):
        return self._qs.value("report_creator", "", type=str)

    def set_report_creator(self, name):
        self._qs.setValue("report_creator", name.strip())

    # AI Research (MONAI Label 서버)
    def monai_url(self):
        return self._qs.value("monai_label_url", "", type=str)

    def set_monai_url(self, url):
        self._qs.setValue("monai_label_url", (url or "").strip())

    def monai_token(self):
        return self._qs.value("monai_label_token", "", type=str)

    def set_monai_token(self, token):
        self._qs.setValue("monai_label_token", (token or "").strip())

    # 오픈소스 모델 (TotalSegmentator, nnU-Net, MedSAM, ONNX, REST) - 키는 "models/..."
    MODEL_DEFAULTS = {"python": "", "device": "auto", "nnunet_folder": "", "nnunet_folds": "0",
                      "medsam_encoder": "", "medsam_decoder": "", "medsam_mode": "medsam",
                      "medsam_prompt": "box", "medsam_box_mm": "40", "onnx_paths": "",
                      "rest_url": ""}

    def model_value(self, key):
        return self._qs.value(f"models/{key}", self.MODEL_DEFAULTS.get(key, ""), type=str)

    def set_model_value(self, key, value):
        self._qs.setValue(f"models/{key}", "" if value is None else str(value).strip())

    def onnx_model_paths(self):
        return [p for p in self.model_value("onnx_paths").split(";") if p.strip()]

    # Deploy Web (DabbaView-Web GitHub Actions)
    def deploy_repo(self):
        return self._qs.value("deploy_repo", "Dabbabbu/DabbaView-Web", type=str) \
            or "Dabbabbu/DabbaView-Web"

    def deploy_workflow(self):
        return self._qs.value("deploy_workflow", "deploy.yml", type=str) or "deploy.yml"

    def deploy_branch(self):
        return self._qs.value("deploy_branch", "main", type=str) or "main"

    def set_deploy_target(self, repo, workflow, branch):
        self._qs.setValue("deploy_repo", (repo or "").strip())
        self._qs.setValue("deploy_workflow", (workflow or "").strip())
        self._qs.setValue("deploy_branch", (branch or "").strip())

    def github_token(self):
        return self._qs.value("github_token", "", type=str)

    def set_github_token(self, token):
        """주의: QSettings(plist/레지스트리)에 평문 저장"""
        if token:
            self._qs.setValue("github_token", token.strip())
        else:
            self._qs.remove("github_token")

    def deploy_sync_version(self):
        return self._qs.value("deploy_sync_version", True, type=bool)

    def set_deploy_sync_version(self, enabled):
        self._qs.setValue("deploy_sync_version", bool(enabled))

    # 클라우드 (Google Drive / OneDrive) - 비밀값·토큰은 cloud.secure_store(키체인)
    def google_client_id(self):
        return self._qs.value("google_client_id", "", type=str).strip()

    def google_api_key(self):
        return self._qs.value("google_api_key", "", type=str).strip()

    def onedrive_client_id(self):
        return self._qs.value("onedrive_client_id", "", type=str).strip()

    def set_cloud_ids(self, google_client_id, google_api_key, onedrive_client_id):
        self._qs.setValue("google_client_id", (google_client_id or "").strip())
        self._qs.setValue("google_api_key", (google_api_key or "").strip())
        self._qs.setValue("onedrive_client_id", (onedrive_client_id or "").strip())

    # DICOM 노드
    def dicom_nodes(self):
        return self._load_json("dicom_nodes", [])

    def save_dicom_nodes(self, nodes):
        self._save_json("dicom_nodes", nodes)

    def local_ae_title(self):
        return self._qs.value("local_ae_title", DEFAULT_LOCAL_AE, type=str) or DEFAULT_LOCAL_AE

    def set_local_ae_title(self, title):
        self._qs.setValue("local_ae_title", title.strip()[:16] or DEFAULT_LOCAL_AE)
