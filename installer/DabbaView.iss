; DabbaView Windows 설치 프로그램 (Inno Setup 6)
; - 사용자 폴더(%LOCALAPPDATA%\Programs\DabbaView)에 설치 → 관리자 권한 없이 설치 · 업데이트
; - 바탕화면 · 시작 메뉴 바로가기 (늘 같은 위치라 새 버전을 설치해도 바로가기가 그대로 작동)
; - 새 버전은 이 설치 파일을 그냥 다시 실행하면 같은 자리에 덮어써서 업데이트
; 빌드: ISCC /DAppVersion=4.1.0 installer\DabbaView.iss   (dist\DabbaView 폴더가 있어야 함)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{9D82C1B4-7155-4383-8B8B-E6DB585359D3}
AppName=DabbaView
AppVersion={#AppVersion}
AppVerName=DabbaView {#AppVersion}
AppPublisher=Park Seongho (Dabbabbu)
AppPublisherURL=https://github.com/Dabbabbu/DabbaView
AppSupportURL=https://github.com/Dabbabbu/DabbaView/issues
AppUpdatesURL=https://github.com/Dabbabbu/DabbaView/releases/latest
DefaultDirName={localappdata}\Programs\DabbaView
DefaultGroupName=DabbaView
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist-installer
OutputBaseFilename=DabbaView-Setup
SetupIconFile=..\resources\dabbaview.ico
UninstallDisplayIcon={app}\DabbaView.exe
UninstallDisplayName=DabbaView
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; 실행 중이면 닫고 설치 (업데이트할 때)
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 예전 버전 파일이 섞이지 않게 프로그램 파일은 새로 깔기 (사용자 설정 · 캐시는 다른 폴더라 그대로)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\DabbaView\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DabbaView"; Filename: "{app}\DabbaView.exe"
Name: "{autodesktop}\DabbaView"; Filename: "{app}\DabbaView.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DabbaView.exe"; Description: "{cm:LaunchProgram,DabbaView}"; Flags: nowait postinstall skipifsilent
; 앱 안 업데이트(조용한 설치) 뒤에는 DabbaView를 다시 켬
Filename: "{app}\DabbaView.exe"; Flags: nowait postinstall skipifnotsilent
