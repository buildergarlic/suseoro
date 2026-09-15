; -*- coding: utf-8 -*-
Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!ifndef APP_VERSION
  !define APP_VERSION "2.0.0"
!endif
!ifndef SOURCE_DIR
  !define SOURCE_DIR "..\dist\Suseoro"
!endif
!ifndef OUTPUT_DIR
  !define OUTPUT_DIR "..\dist"
!endif
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Suseoro"

Name "수서로"
OutFile "${OUTPUT_DIR}\Suseoro-Setup-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Suseoro"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "수서로 — 학교도서관 도서 구입 도우미"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=1042 "ProductName" "수서로"
VIAddVersionKey /LANG=1042 "FileDescription" "수서로 설치 프로그램"
VIAddVersionKey /LANG=1042 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1042 "LegalCopyright" "Suseoro contributors"

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "수서로 설치"
!define MUI_WELCOMEPAGE_TEXT "학교도서관의 도서 구입 업무를 이 PC에서 편하게 정리합니다.$\r$\n$\r$\n관리자 권한이나 로그인이 필요하지 않습니다.$\r$\n기존에 저장한 자료는 업데이트와 제거 후에도 유지됩니다."
!define MUI_FINISHPAGE_RUN "$INSTDIR\Suseoro.exe"
!define MUI_FINISHPAGE_RUN_TEXT "수서로 실행"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"

!macro CheckRunning
  check_running:
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\Suseoro.App") p.r0'
  ${If} $0 != 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_RETRYCANCEL|MB_ICONINFORMATION "수서로가 실행 중입니다. 수서로 창을 닫은 후 다시 시도해 주세요." IDRETRY check_running
    Abort
  ${EndIf}
!macroend

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "수서로는 64비트 Windows 10 이상에서 사용할 수 있습니다."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "수서로는 Windows 10 이상에서 사용할 수 있습니다."
    Abort
  ${EndIf}
  ; Keep the install tree distinct from the user's data and arbitrary /D values.
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Suseoro"
  !insertmacro CheckRunning
FunctionEnd

Function un.onInit
  SetShellVarContext current
  ${If} $INSTDIR != "$LOCALAPPDATA\Programs\Suseoro"
    MessageBox MB_OK|MB_ICONSTOP "설치 폴더를 확인할 수 없어 제거를 중단했습니다."
    Abort
  ${EndIf}
  !insertmacro CheckRunning
FunctionEnd

Section "수서로" SEC_MAIN
  SetOutPath "$INSTDIR"
  File /r "${SOURCE_DIR}\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\수서로"
  CreateShortcut "$SMPROGRAMS\수서로\수서로.lnk" "$INSTDIR\Suseoro.exe"
  CreateShortcut "$SMPROGRAMS\수서로\수서로 (브라우저).lnk" "$INSTDIR\Suseoro.exe" "--browser"
  CreateShortcut "$SMPROGRAMS\수서로\수서로 제거.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\수서로.lnk" "$INSTDIR\Suseoro.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "수서로"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Suseoro"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\Suseoro.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "https://github.com/buildergarlic/suseoro"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ; Only the validated fixed application tree is removed; LOCALAPPDATA\Suseoro stays.
  Delete "$INSTDIR\Suseoro.exe"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\quick-start.md"
  RMDir /r "$INSTDIR\_internal"
  RMDir "$INSTDIR"
  Delete "$DESKTOP\수서로.lnk"
  Delete "$SMPROGRAMS\수서로\수서로.lnk"
  Delete "$SMPROGRAMS\수서로\수서로 (브라우저).lnk"
  Delete "$SMPROGRAMS\수서로\수서로 제거.lnk"
  RMDir "$SMPROGRAMS\수서로"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
SectionEnd
