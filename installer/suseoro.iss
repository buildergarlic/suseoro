; Archived manual-only Inno Setup installer. Official automatic-update releases
; use suseoro.nsi, which implements the parent-PID wait and installation lock.
#ifndef AppVersion
  #define AppVersion "2.0.3"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Suseoro"
#endif
#ifndef OutputDirPath
  #define OutputDirPath "..\dist"
#endif

[Setup]
AppId={{B6F7B643-9227-469E-AAD6-E942DB8FC109}
AppName=수서로
AppVersion={#AppVersion}
AppPublisher=Suseoro
AppPublisherURL=https://github.com/buildergarlic/suseoro
AppSupportURL=https://github.com/buildergarlic/suseoro/issues
DefaultDirName={localappdata}\Programs\Suseoro
DefaultGroupName=수서로
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDirPath}
OutputBaseFilename=Suseoro-ManualSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Suseoro.exe
AppMutex=Local\Suseoro.App
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 수서로 바로 가기 만들기"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\수서로"; Filename: "{app}\Suseoro.exe"
Name: "{group}\수서로 (브라우저)"; Filename: "{app}\Suseoro.exe"; Parameters: "--browser"
Name: "{autodesktop}\수서로"; Filename: "{app}\Suseoro.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Suseoro.exe"; Description: "수서로 실행"; Flags: nowait postinstall skipifsilent

; No UninstallDelete on LOCALAPPDATA\Suseoro: all saved user data is preserved.
