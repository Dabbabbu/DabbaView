# 변경 이력

버전 규칙 ([Semantic Versioning](https://semver.org/lang/ko/)):
버그 수정만 → patch (2.1.0 → 2.1.1), 기능 추가·개선 → minor (2.1.x → 2.2.0), 대규모 변경 → major (2.x → 3.0.0).
버전은 `dabbaview/__init__.py`의 `__version__` 하나로 관리합니다 (앱 번들·타이틀 바·About·시작 화면·README).

## 2.2.0 — 2026-09-19

### 추가 · 개선
- View ▸ 패널: 닫은 탭 · 패널 다시 열기 (★ Library Ctrl+Shift+L, Image Ctrl+I, AI Ctrl+Shift+A, Python Console F3).
- View ▸ 오버레이 항목: Phase Encoding 방향 · 방향 문자(A/P · R/L · S/I) · 스캔 커버리지 선을 각각 켜고 끔. 기본값은 Settings ▸ Display, T 키는 전체.

## 2.1.0 — 2026-09-19

### 추가 · 개선
- **ACR Phantom QC**
  - 보고서에 측정 과정: 콘솔 수동 절차와 같은 순서 (경사판 ROI 평균 → 기준 레벨 (m1+m2)/4 → W 1 → 경사판 길이 → 두께, 200 cm² ROI → W 1로 가장 어두운·밝은 1 cm² → PIU, 고스팅 배경 ROI → 계산).
  - 증빙 영상 44장: 콘솔 Screen Save 세트와 같은 순서·내용, 검사일·장비·검사자 워터마크. PDF·Word에 포함, JPG로도 저장. 사이트 시퀀스도 자동으로 찾음.
  - 뷰어: 슬라이스마다 쓰이는 검사 배지(값·판정), ROI 적합 초록 / 부적합 빨강, 7개 검사 요약표(클릭하면 해당 영상), 증빙 영상 보기 창.
- **Reference Line**: 다른 시리즈의 전체 스캔 범위를 점선으로(Multi View 칸마다 다른 색), 현재 슬라이스는 노란 실선.
- 영상 가장자리 방향 문자 (L/R · A/P · S/I, 비스듬하면 두 글자)와 위상 인코딩 방향 표시 (0018,1312).
- 시네 버튼: 재생 중 ■ Stop (빨강), 멈춤 ▶ Play (초록).

### 버그 수정
- 클라우드(OneDrive 등) 폴더 경고창의 버튼 · ESC가 눌리지 않고 앱을 끌 수 없던 문제 (진행창이 입력을 막음). 경고창이 떠 있어도 ⌘Q로 종료, 불러오는 중 종료 시 로더를 정리.

## 2.0.0

- 이전 버전 (Library Export, ACR Phantom QC, Image Quality Assessment, 매뉴얼 · 분석 가이드 등).
