# RadiantView - Python DICOM Viewer

RadiAnt, GE AW, INFINITT PACS 워크스테이션의 작업 방식을 참고한 Python/PyQt5 기반 DICOM 뷰어입니다.
macOS(.app)와 Windows(.exe)로 빌드됩니다.

> ⚠️ 진단용으로 인증된 의료기기가 아닙니다. 학습·연구·참고용으로 사용하세요.

## 주요 기능

### 불러오기 / 시리즈 목록
- **파일·폴더 열기**: 메뉴, 드래그 앤 드롭, Recent Files(최근 10개). 앱 시작 시 자동으로 불러오지 않음
- **빠른 로딩**: 메타데이터만 병렬로 먼저 읽고 픽셀은 필요할 때 읽음 (확장자 사전 필터링)
- **시리즈 패널 (INFINITT 스타일)**: 중간 슬라이스 썸네일, `시리즈번호/총 장수`, 시퀀스 이름, 선택 시 노란 테두리, 검사별 묶음
  - **클릭 = 선택만**, **더블클릭 / Enter = 활성 칸에 표시**, **드래그 앤 드롭 = 원하는 칸에 표시** (트리도 동일)
- **환자/검사 트리**: Patient → Study → Series (☰ 버튼으로 전환)
- **시퀀스 툴팁**: TR/TE/TI, Flip Angle, ETL, NEX, Bandwidth, 두께/간격, Matrix, FoV, 시퀀스 종류 (SE/FSE/GRE/EPI/IR, FatSat)

### 화면 구성
- **2D View**: Stack 모드와 Tile 모드(2x2~6x6)
- **Multi View**: 1x1 ~ 4x4 (최대 16칸), 트리/패널이나 Finder에서 칸으로 드래그 앤 드롭
- **레이아웃 드롭다운**: `2D` / `1X1`~`3X3` / `Default`(Hanging Protocol) / `ALL`(검사의 모든 시리즈)
- **Hanging Protocol**: 폴더를 열면 모달리티·부위에 맞춰 자동 배치 (예: Brain MRI → 2x2 T1/T2/FLAIR/DWI, Spine MRI → 1x2 Sag T1/T2). 편집하거나 현재 배치를 프로토콜로 저장 가능
- **MPR**: Axial/Sagittal/Coronal 3평면 (mm 기준 실제 비율)
- **3D Volume Rendering**: VTK 설치 시 (선택사항)

### 비교 / 동기화
- **Crosslink (Sync Cursor)**: 한 뷰에서 찍은 위치(mm)를 같은 좌표계(Frame of Reference)의 다른 뷰·MPR에 십자선으로 표시하고 가장 가까운 슬라이스로 이동
- **Reference Line (Scout)**: 다른 칸의 현재 슬라이스 위치를 노란 선으로 표시
- **동기화 스크롤 / 동기화 W/L**: 같은 좌표계의 평행한 시리즈끼리 위치 기준으로 연동
- **Compare**: 같은 환자의 다른 날짜 검사를 나란히 비교 (슬라이스 간격을 유지하며 함께 스크롤)

### 측정 / 주석
- 거리, 각도, **Cobb 각**, **Freehand ROI**, **타원 ROI**(면적·Mean·SD·Min·Max), Freehand 면적, 2D 화살표(라벨), **텍스트 메모**(크기·색상)
- **3D Cursor**: 클릭 위치의 환자 좌표 (R/L, A/P, S/I). Crosslink 여부와 관계없이 같은 좌표계의 다른 칸·MPR에도 같은 위치를 표시 (각 시리즈의 픽셀 값, 슬라이스에서 벗어나면 Δ mm)
- **Pixel Probe**: 상태바에 좌표(x, y, z mm)와 픽셀 값 (CT: HU, MR: SI, PET: SUVbw)
- 측정·주석은 영상(SOPInstanceUID)별로 저장되며 **JSON 저장/불러오기** 가능
- **Key Image**: 표시 → 모아보기(Tile) → PNG + 목록(JSON) 내보내기

### 영상 조작 (GE AW 스타일)
- W/L 조절, 사용자 정의 **W/L 프리셋**(추가/편집/삭제), Pan, Zoom, 돋보기 렌즈(2x~4x)
- 상하/좌우 반전, 90° 회전, 흑백 반전 (측정·주석은 영상에 붙어서 함께 회전)
- **GE 스타일 오버레이**: 기관·환자·성별/나이/체중, 날짜·시리즈/영상 번호·위치·FoV·Matrix·두께, 시퀀스·장비·코일·TR/TE/TI·FA/ETL/NEX, 스케일 바
- **Image 정보 패널**: 현재 영상의 환자·검사·시리즈·획득 파라미터 상세

### 내보내기 / 네트워크
- 이미지 내보내기, **Capture**(오버레이·측정선 포함), **동영상**(MP4/AVI/GIF)
- **DICOM Send** (C-STORE), **DICOM Print** (Basic Grayscale Print), 연결 확인(C-ECHO) — pynetdicom 사용
- **DICOM 익명화**: 환자 정보·Private 태그 제거, UID 재생성

> DICOM Send는 원본 파일을 그대로 보내므로 **환자 정보가 포함됩니다.** 외부로 보낼 때는 먼저 익명화하세요.

## 마우스 조작 (PACS 표준, Settings에서 변경 가능)

| 조작 | 기능 |
|---|---|
| 좌클릭 드래그 | 선택한 도구 (기본: Selector = 선택만) |
| **우클릭 드래그** | **항상 W/L** (좌우 = Width, 상하 = Level) |
| **가운데 버튼 드래그** | **항상 Pan** |
| Ctrl(⌘) + 좌클릭 드래그 | Zoom |
| 휠 | 슬라이스 이동 (위 = 이전, 아래 = 다음) |
| Shift + 휠 | 5장씩 빠르게 이동 |
| Ctrl(⌘) + 휠 | Zoom |
| 좌측 더블클릭 | 화면에 맞춤 (Fit to Window) |
| 우측 더블클릭 | W/L을 DICOM 기본값으로 리셋 |

macOS에서는 표의 `Ctrl`이 **⌘ (Command)** 키입니다.

## 키보드 단축키

| 키 | 기능 | 키 | 기능 |
|---|---|---|---|
| S | Selector | 1 | W/L |
| 2 | Pan | 3 | Zoom |
| 4 | 거리 | 5 | 각도 |
| B | Cobb 각 | 6 | 3D Cursor |
| 7 | 돋보기 | 8 | Freehand ROI |
| E | 타원 ROI (Shift: 원) | 9 | 면적 |
| 0 | 화살표 | T | 텍스트 |
| V / H | 상하 / 좌우 반전 | [ / ] | 왼쪽 / 오른쪽 90° 회전 |
| I | 흑백 반전 | R | 회전·반전 초기화 + 화면 맞춤 |
| C | Crosslink | L | HU Lens (커서 옆 픽셀 값) |
| K | Key Image 표시/해제 | Shift+K | Key Image 모아보기 |
| Shift+T | Stack ↔ Tile | O | 오버레이 표시/숨김 |
| Space | 시네 재생/정지 | Delete | 현재 영상의 마지막 주석 삭제 |
| Esc | 그리던 측정 취소 / 3D Cursor 지우기 | Ctrl+T | DICOM 태그 |
| Ctrl+I | Image 정보 패널 | Ctrl+Shift+S | Capture |
| Ctrl+O | 파일 열기 | Ctrl+Shift+O | 폴더 열기 |
| Ctrl+S | 이미지 내보내기 | Ctrl+Shift+E | 동영상 내보내기 |
| ⌘, (macOS) | Settings (Windows는 File → Settings) | | |

## 설정 (File → Settings, macOS ⌘,)
- **Mouse**: 버튼·휠·더블클릭 동작 매핑
- **W/L Presets**: 프리셋 추가/편집/삭제
- **Hanging Protocols**: 모달리티, 부위 키워드, 레이아웃, 칸별 시리즈 키워드
- **DICOM Nodes**: 이 컴퓨터의 AE Title, 전송/인쇄 대상 (AE Title, Host, Port)

설정은 QSettings로 저장됩니다 (macOS: `~/Library/Preferences/com.radiantview.RadiantView.plist`).

## 설치 및 실행

### 소스에서 실행 (macOS / Windows)

```bash
git clone https://github.com/Dabbabbu/RadiantView.git
cd RadiantView
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python create_icon.py           # 아이콘과 시작 화면 로고 생성
python run.py                   # 또는: python run.py /path/to/dicom/folder
```

3D Volume Rendering을 쓰려면 `pip install vtk` (나머지 기능은 VTK 없이 동작).

### 앱 빌드

| 플랫폼 | 명령 | 결과 |
|---|---|---|
| macOS | `./build_app.sh` (py2app) | `dist/RadiantView.app` |
| macOS 대안 | `./build_pyinstaller.sh` | `dist/RadiantView.app` |
| Windows | `build_windows.bat` (PyInstaller) | `dist\RadiantView\RadiantView.exe` |

빌드된 앱은 서명되지 않았습니다. 처음 열 때 macOS는 우클릭 → 열기, Windows는 SmartScreen에서 "추가 정보 → 실행"을 선택하세요.

### 미리 빌드된 앱 받기
push할 때마다 GitHub Actions가 macOS와 Windows용으로 빌드하고, 앱이 실행되는지까지 확인합니다.
[Actions](https://github.com/Dabbabbu/RadiantView/actions) → 최근 빌드 → **Artifacts**에서 zip을 받으세요 (GitHub 로그인 필요, 90일 보관).

## 프로젝트 구조

```
RadiantView/
├── run.py                   # 실행 스크립트
├── create_icon.py           # 아이콘(.icns/.ico) + 로고 생성
├── setup_app.py             # py2app 설정
├── build_app.sh / build_pyinstaller.sh / build_windows.bat
├── .github/workflows/build.yml   # macOS + Windows 자동 빌드
└── radiantview/
    ├── main_window.py       # 메인 윈도우, 툴바, 메뉴
    ├── dicom_loader.py      # 병렬 로딩, 시리즈 분류/정렬, lazy 픽셀 로딩
    ├── dicom_info.py        # 태그 해석, 시퀀스 요약, GE 오버레이 문구, SUV
    ├── geometry.py          # 영상 ↔ 환자 좌표(mm), Reference Line 계산
    ├── viewport.py          # 2D 뷰포트 (도구, 측정, 오버레이, 마우스 매핑)
    ├── multi_viewport.py    # Multi View (레이아웃, 동기화, Reference Line)
    ├── tile_view.py         # Tile 모드 / Key Image 모아보기
    ├── series_panel.py      # INFINITT 스타일 썸네일 패널
    ├── series_tree.py       # 환자/검사/시리즈 트리, 썸네일 생성
    ├── hanging.py           # Hanging Protocol 매칭/배치
    ├── annotations.py       # 주석·Key Image 저장소 (JSON)
    ├── roi.py               # ROI/타원/Cobb 계산
    ├── cursor_sync.py       # Crosslink 컨트롤러
    ├── mpr_viewer.py        # MPR
    ├── volume_renderer.py   # 3D (VTK)
    ├── video_exporter.py    # 동영상 내보내기
    ├── dicom_net.py         # C-ECHO / C-STORE / Print (pynetdicom)
    ├── network_dialogs.py   # DICOM Send / Print 대화상자
    ├── settings_dialog.py   # Settings 대화상자
    ├── app_settings.py      # 설정 저장 (마우스, 프리셋, 프로토콜, 노드)
    ├── image_info_panel.py  # Image 정보 패널
    ├── anonymizer.py        # 익명화
    ├── tag_viewer.py        # DICOM 태그 뷰어
    ├── text_dialog.py       # 텍스트 주석 입력
    └── render.py            # 8비트 렌더링 (내보내기/인쇄)
```

## 향후 추가 예정

- [ ] PACS Query/Retrieve (C-FIND / C-MOVE)
- [ ] DICOM 수신 (Storage SCP)
- [ ] Enhanced 멀티프레임 DICOM의 프레임별 공간 정보 (Crosslink/MPR)
- [ ] 동영상 내보내기에 회전·반전·측정선 반영
- [ ] 코드 서명된 macOS/Windows 배포판
