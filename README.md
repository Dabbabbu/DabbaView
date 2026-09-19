# DabbaView - Python DICOM Viewer

RadiAnt, GE AW, INFINITT PACS 워크스테이션의 작업 방식을 참고한 Python/PyQt5 기반 DICOM 뷰어입니다.
macOS(.app)와 Windows(.exe)로 빌드됩니다.

<!-- version -->**Version** 1.0.0 — 버전은 `dabbaview/__init__.py`의 `__version__` 하나로 관리합니다 (앱 번들·타이틀 바·About·시작 화면이 이 값을 사용하고, 빌드할 때 이 줄도 자동으로 맞춰집니다).

> ⚠️ 진단용으로 인증된 의료기기가 아닙니다. 학습·연구·참고용으로 사용하세요.

## 주요 기능

### 불러오기 / 시리즈 목록
- **파일·폴더 열기**: 메뉴, 드래그 앤 드롭, Recent Files(최근 10개). 앱 시작 시 자동으로 불러오지 않음
- **다중 포맷**: DICOM 외에 아래 형식도 파일 열기·드래그 앤 드롭·폴더 열기로 불러와 DICOM 시리즈처럼 봅니다 (MPR·3D·AI·측정 모두 사용 가능, 공간 정보 유지)

  | 형식 | 확장자 | 비고 |
  |---|---|---|
  | NIfTI | .nii, .nii.gz | nibabel. 4D는 볼륨마다 시리즈. 이름에 label/mask/seg가 있는 라벨맵은 같은 위치의 영상에 AI 오버레이로 |
  | NRRD | .nrrd, .nhdr | pynrrd (LPS/RAS 공간 자동 변환) |
  | MetaImage | .mha, .mhd | SimpleITK |
  | NumPy | .npy, .npz | (슬라이스, 행, 열). `_mask.npy` / npz의 `mask`는 오버레이로, 옆의 `.json`·`dataset.json`에서 간격 복원 |
  | 이미지 시퀀스 | .png, .jpg, .bmp, .tif | 폴더(또는 여러 파일)를 파일 이름 순서대로 한 시리즈로. `images/` 옆 `masks/`는 오버레이로 |
  | DICOM SEG | .dcm (SEG) | 참조 시리즈 위에 세그멘테이션 오버레이 (참조 시리즈를 나중에 열어도 자동 적용) |
  | STL | .stl | 3D Volume 탭에 메시로 표시 (LPS mm 좌표, 볼륨과 겹쳐 봄) |
- **포맷 변환** (File → Convert / Export As): DICOM → NIfTI / NRRD / MetaImage / NumPy / PNG 시퀀스, NIfTI → DICOM / NumPy / NRRD, NumPy → NIfTI 등 모든 조합. 소스는 현재 시리즈 또는 파일·폴더, 옵션: voxel spacing 변경, 데이터 타입(int16/float32/uint8), 압축, AI 마스크 함께 저장(DICOM은 SEG로), PNG 8/16비트, DICOM Modality·환자 정보. 진행률 표시
- **빠른 로딩**: 메타데이터만 병렬로 먼저 읽고 픽셀은 필요할 때 읽음 (확장자 사전 필터링)
- **시리즈 패널 (INFINITT 스타일)**: 중간 슬라이스 썸네일, `시리즈번호/총 장수`, 시퀀스 이름, 선택 시 노란 테두리, 검사별 묶음
  - **클릭(버튼을 뗄 때) / Enter = 활성 칸에 표시**, 누른 채 **끌면 로드하지 않고 드래그 앤 드롭** → 놓은 칸에 표시 (트리도 동일)
- **환자/검사 트리**: Patient → Study → Series (☰ 버튼으로 전환)
- **패널 접기/펼치기**: 패널과 영상 사이의 ◀/▶ 버튼 또는 **F2** — 슬라이드로 접히면 영상이 전체 너비 사용 (상태·너비는 다음 실행 때도 유지)
- **시퀀스 툴팁**: TR/TE/TI, Flip Angle, ETL, NEX, Bandwidth, 두께/간격, Matrix, FoV, 시퀀스 종류 (SE/FSE/GRE/EPI/IR, FatSat)

### 화면 구성
- **2D View**: Stack 모드와 Tile 모드(2x2~6x6)
- **Multi View**: 1x1 ~ 4x4 (최대 16칸), 트리/패널이나 Finder에서 칸으로 드래그 앤 드롭
- **레이아웃 드롭다운**: `2D` / `1X1`~`3X3` / `Default`(Hanging Protocol) / `ALL`(검사의 모든 시리즈)
- **Hanging Protocol**: 폴더를 열면 모달리티·부위에 맞춰 자동 배치 (예: Brain MRI → 2x2 T1/T2/FLAIR/DWI, Spine MRI → 1x2 Sag T1/T2). 편집하거나 현재 배치를 프로토콜로 저장 가능
- **MPR**: Axial/Sagittal/Coronal 3평면 (mm 기준 실제 비율) + Oblique: 크로스헤어 끝 핸들 드래그로 각도 회전, Shift+드래그 1° 단위 미세 조절, 각도 오버레이·리셋
- **3D Volume Rendering**: VTK (macOS 앱 번들에 포함)

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
- **GE 스타일 오버레이**: 기관·환자·성별/나이/체중, 날짜·시리즈/영상 번호·위치·FoV·Matrix·두께, 시퀀스·장비·코일·TR/TE/TI·FA/ETL/NEX·W/L, 우하단 W/L + 스케일 바
- **Image 정보 패널**: 현재 영상의 환자·검사·시리즈·획득 파라미터 상세

### Reading (기록) — R
- INFINITT 기록 창 형태: 제목(모달리티, 환자명, ID, 성별, 검사일시, Study Description, Body Part, 임상정보), 기록 편집기(`====== [Conclusion] =======`로 결론 구분), Creator / Approver / Approver2 / My Comment, Study Comment · Exam Date(상태) · Report Date
- 버튼: Edit, Import, Copy, Print, Save, Approve, Close — 저장하면 StudyInstanceUID별 JSON (이 컴퓨터의 앱 데이터 폴더), 다시 열면 불러옴
- **Import**: .txt(UTF-8/CP949) · .rtf → 본문에 삽입, .jpg/.png/.bmp/.tiff/.pdf → 이미지 탭(확대/축소), DICOM SR(.dcm) → 텍스트 추출. 파일명에 다른 환자 ID가 있으면 경고
- **기록 폴더 감시** (Settings → Reading): 새 파일을 파일명·폴더명의 PatientID + 검사일(YYYYMMDD)로 자동 연결, DICOM SR은 StudyInstanceUID로 연결. 예: `1234567_20260917_report.txt`
- Series 탭: 검사의 시리즈 목록과 시퀀스 파라미터 요약

> 기록과 가져온 파일 경로에는 환자 정보가 들어 있습니다. 앱 데이터 폴더(macOS: `~/Library/Application Support/DabbaView`)를 공유하거나 저장소에 올리지 마세요.

### 내보내기 / 네트워크
- 이미지 내보내기, **Capture**(오버레이·측정선 포함), **동영상**(MP4/AVI/GIF)
- **DICOM Send** (C-STORE), **DICOM Print** (Basic Grayscale Print), 연결 확인(C-ECHO) — pynetdicom 사용
- **DICOM 익명화** (툴바 🕶 Anonymize / Tools 메뉴): 개인정보·기관정보·검사정보·촬영 파라미터를 항목별로 선택, 프리셋(최소/표준/완전/연구용), 카테고리별 미리보기에서 변경될 태그 강조, Private 태그 제거, UID 재생성(시리즈 안에서 일관 유지)

> DICOM Send는 원본 파일을 그대로 보내므로 **환자 정보가 포함됩니다.** 외부로 보낼 때는 먼저 익명화하세요.

### AI Research — 툴바 🧠 AI (Ctrl+Shift+A)
오른쪽 사이드 패널에서 AI 학습 데이터를 만들고 모델을 연동합니다 (3D Slicer Segment Editor · MONAI Label 방식).

- **세그멘테이션**: Brush(D) / Eraser(X, 현재 라벨만 지움) / Magic Wand(W, 클릭한 값 ±허용범위의 연결 영역, 3D 옵션) / Threshold(G, 값 범위 미리보기 후 클릭한 슬라이스 또는 전체 적용, CT 프리셋)
- **슬라이스 보간**: 몇 장만 칠하면 사이 슬라이스를 모양 기반(거리 맵)으로 자동 채움
- **다중 라벨 / 라벨 매니저**: 추가·삭제·이름·색상·표시 여부, 반투명 오버레이(투명도 슬라이더), 되돌리기(Ctrl+Z)
- **통계**: 라벨별 볼륨(mL)·복셀·슬라이스 범위. 마스크는 시리즈별로 자동 저장되어 다시 열면 이어서 작업
- **내보내기**: NIfTI(.nii.gz) · NumPy(.npy) · PNG 시퀀스 · COCO(JSON) · Pascal VOC · DICOM SEG, train/val/test 자동 분할(케이스가 여러 개면 케이스 단위, 하나면 슬라이스 단위)
- **전처리**: 라벨 영역 크롭(+여유 mm), 리샘플링(목표 간격 mm), 가우시안/미디안 필터, 히스토그램 매칭(기준 시리즈), 윈도잉 후 0–1 정규화, 전/후 미리보기
- **MONAI Label**: 서버 연결(Settings → AI), 자동 세그멘테이션 결과를 오버레이로 받아 수정 → 최종 라벨 제출(active learning) → 학습 시작. 서버로는 픽셀 볼륨만 보냄 (환자 정보 제외)
- **ONNX 로컬 추론** (onnxruntime): (N,C,H,W) 모델은 슬라이스별, (N,C,D,H,W)는 3D. 결과를 빈 곳만 채우거나 교체
- **데이터셋 워크리스트**: 여러 환자/검사를 목록으로 관리, 상태(미완/진행중/완료), 라벨 통계, 폴더에서 다시 열기

> NIfTI/NumPy/PNG/COCO/VOC에는 환자 정보가 들어가지 않습니다. DICOM SEG는 원본 검사를 참조하므로 환자 정보가 포함됩니다.

### 분석 (3D Slicer · ImageJ/Fiji 스타일)
AI Research 패널의 **📊 Analysis** 탭, **Process** 메뉴, 하단 **Histogram / Profile** · **Python Console(F3)** 패널.

| 기능 | 내용 |
|---|---|
| Image Registration | 두 시리즈 선택 → Rigid / Affine (SimpleITK, Mutual Information · Mean Squares · Correlation, 다해상도). 결과는 기준 격자에 맞춘 새 시리즈 + 자동 융합 표시. 변환 `.tfm` 저장/불러오기 |
| Image Fusion | 기준 시리즈 위에 다른 시리즈를 컬러로 (예: CT 흑백 + PET Hot). 환자 좌표로 다시 샘플링하므로 해상도·방향이 달라도 겹침. 투명도, Overlay / Add / Multiply / Checkerboard, 컬러바 |
| Landmarks | Landmark 도구(**F**)로 점 찍기 → 이름·라벨 편집, 환자 좌표(mm) CSV / 3D Slicer `.mrk.json` 내보내기·불러오기, 같은 좌표계의 모든 시리즈와 3D 뷰에 표시 |
| Surface Model | AI 세그멘테이션 라벨 → Marching Cubes → 스무딩·데시메이션 → 3D 뷰, STL/OBJ/PLY 내보내기, 표면적·부피 |
| Filters (Process) | Gaussian, Median, Unsharp Mask, Sobel, Canny, Erosion/Dilation/Opening/Closing — 2D(슬라이스별) 또는 3D, 미리보기, **결과는 새 시리즈** (원본 보존) |
| Histogram | 현재 슬라이스 / 전체 볼륨 / ROI / AI 라벨 영역, Mean·StdDev·Min·Max·Median·Mode, log, CSV |
| Line Profile | Profile 도구(**Shift+L**)로 선을 그으면 거리(mm)별 값 그래프, CSV |
| Particle Analysis | 임계값 범위 또는 AI 라벨 → 객체별 면적·둘레·원형도·중심 좌표, 결과 표 + CSV |
| Color Map | Gray, Hot, Cool, Jet, Viridis, Magma, Inferno, Plasma, Bone, Rainbow, Fire + ImageJ `.lut`/텍스트 LUT, 컬러바 |
| Python Console (F3) | `app.current_array`, `app.current_image`, `app.mask`, `app.add_series(배열, "이름")`, `np`, `ndi`, `plt`, `skimage` — plt 그래프는 콘솔 오른쪽에 표시, 구문 강조, 스크립트 열기/저장 |
| Macros | 콘솔 스크립트를 매크로로 저장 → 원클릭 실행 (Process → Macros). 예제: Otsu → 입자 분석, Gaussian → 새 시리즈, 볼륨 통계, 라벨별 부피, MIP |

## 마우스 조작 (PACS 표준, Settings에서 변경 가능)

| 조작 | 기능 |
|---|---|
| 좌클릭 드래그 | 선택한 도구 (기본: Selector = 선택만) |
| **우클릭 드래그** | **항상 W/L** (좌우 = Width, 상하 = Level) |
| **가운데 버튼 드래그** | **항상 Pan** |
| Ctrl(⌘) + 좌클릭 드래그 | **ROI 자동 W/L**: 사각형을 그리면 그 영역으로 W/L 설정 (Min–Max 또는 Mean±2SD, Settings에서 선택) |
| Alt(⌥) + 좌클릭 드래그 | Pan (도구 무관) |
| 휠 | 슬라이스 이동 (위 = 이전, 아래 = 다음) |
| Shift + 휠 | 5장씩 빠르게 이동 |
| Ctrl(⌘) + 휠 | Zoom (위 = 확대, 커서 위치 기준) |
| 좌측 더블클릭 | 화면에 맞춤 (Fit to Window) |
| 우측 더블클릭 | W/L을 DICOM 기본값으로 리셋 |

macOS에서는 표의 `Ctrl` 자리에 **⌘ (Command)** 와 **Control** 키 모두 쓸 수 있습니다.

## 키보드 단축키

한/영 입력 상태와 관계없이 동작합니다 (한글 입력 중 T → ㅅ 으로 들어와도 T 단축키로 처리).

| 키 | 기능 | 키 | 기능 |
|---|---|---|---|
| S | Selector | 1 | W/L |
| 2 | Pan | 3 | Zoom |
| 4 | 거리 | 5 | 각도 |
| B | Cobb 각 | 6 | 3D Cursor |
| 7 | 돋보기 | 8 | Freehand ROI |
| E | 타원 ROI (Shift: 원) | 9 | 면적 |
| 0 | 화살표 | A | 텍스트 메모 |
| V / H | 상하 / 좌우 반전 | [ / ] | 왼쪽 / 오른쪽 90° 회전 |
| I | 흑백 반전 | Shift+R | 회전·반전 초기화 + 화면 맞춤 |
| C | Crosslink | L | HU Lens (커서 옆 픽셀 값) |
| K | Key Image 표시/해제 | Shift+K | Key Image 모아보기 |
| Shift+T | Stack ↔ Tile | T / O | 환자 정보 + 측정/주석 표시/숨김 |
| Space | Multi View: 선택한 칸만 크게 ↔ 복귀 | P | 시네 재생/정지 |
| R | Reading(기록) 창 | Delete | 현재 영상의 마지막 주석 삭제 |
| Esc | 그리던 측정 취소 / 3D Cursor 지우기 / 세그멘테이션 도구 해제 | Ctrl+T | DICOM 태그 |
| Ctrl+I | Image 정보 패널 | Ctrl+Shift+S | Capture |
| Ctrl+O | 파일 열기 | Ctrl+Shift+O | 폴더 열기 |
| Ctrl+S | 이미지 내보내기 | Ctrl+Shift+E | 동영상 내보내기 |
| F2 | 시리즈 패널 접기/펼치기 | ⌘, (macOS) | Settings (Windows는 File → Settings) |
| Ctrl+Shift+A | AI Research 패널 | D / X | 세그멘테이션 Brush / Eraser |
| W / G | Magic Wand / Threshold | Ctrl+Z | 세그멘테이션 되돌리기 |
| F | Landmark (점 찍기) | Shift+L | Line Profile |
| F3 | Python 콘솔 | | |

## 설정 (File → Settings, macOS ⌘,)
- **Mouse**: 버튼·휠·더블클릭 동작 매핑
- **W/L Presets**: 프리셋 추가/편집/삭제
- **Hanging Protocols**: 모달리티, 부위 키워드, 레이아웃, 칸별 시리즈 키워드
- **DICOM Nodes**: 이 컴퓨터의 AE Title, 전송/인쇄 대상 (AE Title, Host, Port)
- **Reading**: 기록 폴더(감시), 기본 Creator
- **Deploy Web**(Help → Deploy Web): 저장소·워크플로·브랜치, GitHub 토큰
- **AI**: MONAI Label 서버 주소, Access Token

설정은 QSettings로 저장됩니다 (macOS: `~/Library/Preferences/com.dabbaview.DabbaView.plist`).

## 설치 및 실행

### 소스에서 실행 (macOS / Windows)

```bash
git clone https://github.com/Dabbabbu/DabbaView.git
cd DabbaView
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python create_icon.py           # 아이콘과 시작 화면 로고 생성
python run.py                   # 또는: python run.py /path/to/dicom/folder
```

VTK는 3D Volume Rendering에만 쓰이며, 없으면 3D 탭만 비활성화되고 나머지 기능은 동작합니다.

### 앱 빌드

| 플랫폼 | 명령 | 결과 |
|---|---|---|
| macOS | `./build_app.sh` (py2app) | `dist/DabbaView.app` |
| macOS 대안 | `./build_pyinstaller.sh` | `dist/DabbaView.app` |
| Windows | `build_windows.bat` (PyInstaller) | `dist\DabbaView\DabbaView.exe` |

빌드된 앱은 서명되지 않았습니다. 처음 열 때 macOS는 우클릭 → 열기, Windows는 SmartScreen에서 "추가 정보 → 실행"을 선택하세요.

### 미리 빌드된 앱 받기
push할 때마다 GitHub Actions가 macOS와 Windows용으로 빌드하고, 앱이 실행되는지까지 확인합니다.
[Actions](https://github.com/Dabbabbu/DabbaView/actions) → 최근 빌드 → **Artifacts**에서 zip을 받으세요 (GitHub 로그인 필요, 90일 보관).

## 프로젝트 구조

```
DabbaView/
├── run.py                   # 실행 스크립트
├── create_icon.py           # 아이콘(.icns/.ico) + 로고 생성
├── setup_app.py             # py2app 설정
├── build_app.sh / build_pyinstaller.sh / build_windows.bat
├── .github/workflows/build.yml   # macOS + Windows 자동 빌드
└── dabbaview/
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
    ├── render.py            # 8비트 렌더링 (내보내기/인쇄)
    ├── shortcut_fallback.py # 한글 입력 상태에서도 단축키 동작
    ├── about_dialog.py      # Help → About
    ├── deploy_web.py        # Help → Deploy Web (GitHub Actions)
    ├── net_ssl.py           # HTTPS 인증서 (certifi)
    ├── analysis/            # 3D Slicer · ImageJ 스타일 분석
    │   ├── processing.py    # 필터 (Gaussian, Median, Unsharp, Sobel, Canny, Morphology)
    │   ├── measure.py       # 히스토그램 통계, 라인 프로파일, 입자 분석
    │   ├── registration.py  # SimpleITK 정합
    │   ├── fusion.py        # 영상 융합
    │   ├── surface.py       # Marching Cubes 표면 모델
    │   ├── landmarks.py     # 랜드마크 (Slicer Markups JSON)
    │   ├── colormaps.py     # 컬러맵 / LUT
    │   ├── console.py       # Python 콘솔
    │   ├── macros.py        # 매크로
    │   ├── plots.py         # Histogram / Profile 패널
    │   ├── process_dialog.py
    │   └── tab.py           # AI 패널 Analysis 탭
    ├── formats/             # DICOM 외 포맷
    │   ├── readers.py       # NIfTI/NRRD/MetaImage/NumPy/이미지 → 시리즈
    │   ├── volume_series.py # 메모리 볼륨을 DICOM 시리즈처럼
    │   ├── seg_reader.py    # DICOM SEG → 오버레이
    │   ├── writers.py       # 변환 (NIfTI/NRRD/MHA/NumPy/PNG/DICOM)
    │   └── convert_dialog.py
    └── ai/                  # AI Research
        ├── panel.py         # 사이드 패널 UI
        ├── segmentation.py  # 마스크 편집 (Brush/Eraser/Wand/Threshold/보간)
        ├── labels.py        # 라벨 목록
        ├── export.py        # NIfTI/NumPy/PNG/COCO/VOC/DICOM SEG + 분할
        ├── nifti.py         # NIfTI-1 읽기/쓰기
        ├── dicom_seg.py     # DICOM Segmentation 생성
        ├── preprocess.py    # 크롭/리샘플링/필터/히스토그램 매칭/정규화
        ├── monai_label.py   # MONAI Label REST 클라이언트
        ├── onnx_infer.py    # ONNX 로컬 추론
        ├── worklist.py      # 데이터셋 워크리스트
        └── volume.py        # 시리즈 → 3D 볼륨 + 좌표
```

## Help 메뉴
- **About DabbaView**: 버전, 빌드 날짜·커밋, 저작권, 라이선스, GitHub 링크, 포함된 라이브러리 버전 (macOS는 앱 메뉴 → About)
- **Deploy Web**: [DabbaView-Web](https://github.com/Dabbabbu/DabbaView-Web)(GitHub Pages)을 GitHub Actions `deploy.yml`로 다시 배포하고, 진행 상태(Deploying... → Deploy complete!)와 사이트 주소를 표시
  - 선택: 배포 전에 웹 `package.json`·`package-lock.json` 버전을 앱 버전으로 맞춤 (`[skip ci]` 커밋 하나)
  - 토큰: `gh auth login`이 되어 있으면 gh CLI 토큰을 사용(저장 안 함). 아니면 한 번 입력 → 설정 파일에 **평문** 저장. 필요한 권한: Actions·Contents 쓰기, Pages 읽기
  - 배포되는 것은 GitHub의 브랜치 내용입니다 (로컬에서 커밋·푸시하지 않은 변경은 포함되지 않음)

## 향후 추가 예정

- [ ] PACS Query/Retrieve (C-FIND / C-MOVE)
- [ ] DICOM 수신 (Storage SCP)
- [ ] Enhanced 멀티프레임 DICOM의 프레임별 공간 정보 (Crosslink/MPR)
- [ ] 동영상 내보내기에 회전·반전·측정선 반영
- [ ] 코드 서명된 macOS/Windows 배포판

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

Copyright (c) 2026 Park Seongho ([Dabbabbu](https://github.com/Dabbabbu))

이 프로젝트는 GPL-3.0 라이선스를 따릅니다. 사용하는 주요 라이브러리 중 PyQt5는 GPL-3.0이며, pydicom·pynetdicom(MIT), NumPy·SciPy(BSD), OpenCV(Apache-2.0), pypdfium2(Apache-2.0/BSD) 등은 GPL-3.0과 함께 배포할 수 있는 라이선스입니다.
