# RadiantView - Python DICOM Viewer

RadiAnt DICOM Viewer에서 영감을 받은 Python 기반 DICOM 뷰어입니다.

## 주요 기능

- **DICOM 파일/폴더 열기**: 파일, 폴더, 드래그 앤 드롭 지원
- **시리즈 자동 분류**: SeriesInstanceUID 기반 자동 분류 및 정렬
- **윈도잉 (W/L)**: 마우스 드래그로 Window/Level 실시간 조절
- **줌/팬**: Ctrl+휠 줌, 우클릭 드래그 팬
- **슬라이스 스크롤**: 마우스 휠로 슬라이스 이동
- **시네 재생**: Space 키로 시네 재생/정지, FPS 조절 가능
- **측정 도구**: 거리(mm), 각도 측정
- **윈도우 프리셋**: Brain, Bone, Lung, Abdomen 등 프리셋 내장
- **DICOM 태그 뷰어**: 전체 DICOM 메타데이터 검색/조회
- **이미지 내보내기**: PNG, JPEG, BMP
- **영상 반전**: I 키로 흑백 반전
- **다중 뷰포트**: 1x1, 1x2, 2x1, 2x2 레이아웃 전환
- **2D MPR**: Axial/Sagittal/Coronal 3평면 재구성, 크로스헤어 연동
- **3D Volume Rendering**: VTK 기반, 7종 프리셋 (CT Bone/Skin/Lung/Angio 등), MIP
- **DICOM 익명화**: 환자 정보 제거, Private 태그 제거, UID 재생성 옵션
- **다크 테마**: 기본 다크 UI

## 설치 및 실행

### macOS (맥미니)

```bash
cd RadiantView
chmod +x setup.sh
./setup.sh
source venv/bin/activate
python run.py
```

### 수동 설치

```bash
pip install pydicom numpy Pillow PyQt5 scipy
python run.py
```

### DICOM 폴더 지정

```bash
python run.py /path/to/dicom/folder
```

## 키보드 단축키

| 키 | 기능 |
|---|---|
| Ctrl+O | DICOM 파일 열기 |
| Ctrl+Shift+O | DICOM 폴더 열기 |
| 1 | 윈도잉 도구 |
| 2 | 팬 도구 |
| 3 | 줌 도구 |
| 4 | 거리 측정 |
| 5 | 각도 측정 |
| Space | 시네 재생/정지 |
| R | 뷰 리셋 (화면에 맞춤) |
| I | 영상 반전 |
| O | 오버레이 토글 |
| Ctrl+T | DICOM 태그 보기 |
| Ctrl+S | 이미지 내보내기 |
| Delete | 마지막 측정 삭제 |

## 마우스 조작

| 조작 | 기능 |
|---|---|
| 좌클릭 드래그 (윈도잉 모드) | W/L 조절 (좌우=Width, 상하=Level) |
| 우클릭 드래그 | 팬 (모든 모드) |
| 휠 스크롤 | 슬라이스 이동 |
| Ctrl+휠 | 줌 |

## 뷰 모드 (탭)

| 탭 | 기능 |
|---|---|
| 2D View | 기본 단일 뷰포트 (윈도잉, 측정 등) |
| Multi View | 다중 뷰포트 (1x1, 1x2, 2x1, 2x2 레이아웃) |
| MPR | 3평면 재구성 (Axial/Sagittal/Coronal + 크로스헤어 연동) |
| 3D Volume | VTK 기반 3D 볼륨 렌더링 (CT Bone/Skin/Lung 등 7종 프리셋, MIP) |

## 3D Volume Rendering (선택사항)

VTK가 필요합니다. 나머지 기능은 VTK 없이도 동작합니다.

```bash
pip install vtk
```

## 프로젝트 구조

```
RadiantView/
├── run.py                      # 실행 스크립트
├── setup.sh                    # 설치 스크립트
├── requirements.txt            # Python 의존성
├── README.md
└── radiantview/
    ├── __init__.py             # 패키지 초기화
    ├── __main__.py             # 엔트리포인트
    ├── dicom_loader.py         # DICOM 로딩/시리즈 분류
    ├── viewport.py             # 이미지 뷰포트 위젯
    ├── multi_viewport.py       # 다중 뷰포트 레이아웃
    ├── mpr_viewer.py           # 2D MPR 3평면 재구성
    ├── volume_renderer.py      # 3D Volume Rendering (VTK)
    ├── anonymizer.py           # DICOM 익명화
    ├── tag_viewer.py           # DICOM 태그 뷰어
    └── main_window.py          # 메인 윈도우/UI
```

## 향후 추가 예정

- [ ] ROI 측정 (원형, 사각형, 통계)
- [ ] PACS Query/Retrieve
- [ ] DICOM Send/Receive
- [ ] 동기화 스크롤 (Multi View)
- [ ] 어노테이션 저장/불러오기
