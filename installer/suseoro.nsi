; -*- coding: utf-8 -*-
Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "FileFunc.nsh"
!ifndef APP_VERSION
  !define APP_VERSION "2.0.3"
!endif
!ifndef SOURCE_DIR
  !define SOURCE_DIR "..\dist\Suseoro"
!endif
!ifndef OUTPUT_DIR
  !define OUTPUT_DIR "..\dist"
!endif
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Suseoro"
!ifndef INSTALLER_TEST_ROOT
  !define APP_ROOT "$LOCALAPPDATA\Programs\Suseoro"
  !define DATA_ROOT "$LOCALAPPDATA\Suseoro"
!else
  ; Only the installer integration test supplies this compile-time path.
  !define APP_ROOT "${INSTALLER_TEST_ROOT}"
  !define DATA_ROOT "${INSTALLER_TEST_ROOT}.data"
!endif
Var AutoUpdate
Var UpdateMutex
Var DataLock
Var StageDir
Var PreviousDir

Name "수서로"
OutFile "${OUTPUT_DIR}\Suseoro-Setup-${APP_VERSION}.exe"
InstallDir "${APP_ROOT}"
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
    MessageBox MB_RETRYCANCEL|MB_ICONINFORMATION "수서로가 실행 중입니다. 수서로 창을 닫은 후 다시 시도해 주세요." /SD IDCANCEL IDRETRY check_running
    SetErrorLevel 20
    Abort
  ${EndIf}
!macroend

!macro RejectLinkedDirectory TARGET
  System::Call 'kernel32::GetFileAttributesW(w "${TARGET}") i.r0'
  ${If} $0 != -1
    IntOp $0 $0 & 0x400
    ${If} $0 != 0
      SetErrorLevel 26
      Abort
    ${EndIf}
  ${EndIf}
!macroend

Function .onInit
  SetShellVarContext current
  StrCpy $DataLock 0
  StrCpy $AutoUpdate 0
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/AUTOUPDATE" $1
  ${IfNot} ${Errors}
    StrCpy $AutoUpdate 1
    SetSilent silent
  ${EndIf}
  ; Serialize installers and let the new app refuse to start during replacement.
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\Suseoro.Update") p.r0 ?e'
  Pop $1
  StrCpy $UpdateMutex $0
  ${If} $0 == 0
  ${OrIf} $1 == 183
    SetErrorLevel 21
    Abort
  ${EndIf}
  ${If} $AutoUpdate == 1
    ${GetParameters} $0
    ClearErrors
    ${GetOptions} $0 "/UPDATEPID=" $1
    ${If} ${Errors}
      SetErrorLevel 22
      Abort
    ${EndIf}
    ; A strict positive decimal PID prevents accepting malformed wait targets.
    StrCpy $2 0
    StrLen $3 $1
    ${If} $3 == 0
    ${OrIf} $3 > 10
      SetErrorLevel 22
      Abort
    ${EndIf}
    pid_digit:
      StrCpy $4 $1 1 $2
      StrCmp $4 "0" digit_valid
      StrCmp $4 "1" digit_valid
      StrCmp $4 "2" digit_valid
      StrCmp $4 "3" digit_valid
      StrCmp $4 "4" digit_valid
      StrCmp $4 "5" digit_valid
      StrCmp $4 "6" digit_valid
      StrCmp $4 "7" digit_valid
      StrCmp $4 "8" digit_valid
      StrCmp $4 "9" digit_valid
      SetErrorLevel 22
      Abort
    digit_valid:
      IntOp $2 $2 + 1
      IntCmp $2 $3 pid_valid pid_digit pid_valid
    pid_valid:
    IntCmp $1 0 pid_invalid pid_invalid pid_open
    pid_invalid:
      SetErrorLevel 22
      Abort
    pid_open:
    System::Call 'kernel32::OpenProcess(i 0x100000, i 0, i r1) p.r0 ?e'
    Pop $2
    ${If} $0 == 0
      ; ERROR_INVALID_PARAMETER means the process has already exited.
      ${If} $2 != 87
        SetErrorLevel 23
        Abort
      ${EndIf}
    ${Else}
      System::Call 'kernel32::WaitForSingleObject(p r0, i 30000) i.r1'
      System::Call 'kernel32::CloseHandle(p r0)'
      ${If} $1 != 0
        SetErrorLevel 24
        Abort
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "수서로는 64비트 Windows 10 이상에서 사용할 수 있습니다." /SD IDOK
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "수서로는 Windows 10 이상에서 사용할 수 있습니다." /SD IDOK
    Abort
  ${EndIf}
  ; Keep the install tree distinct from the user's data and arbitrary /D values.
  StrCpy $INSTDIR "${APP_ROOT}"
  !insertmacro CheckRunning
  ; Older releases also honor this byte lock, closing the restart race.
  CreateDirectory "${DATA_ROOT}"
  System::Call 'kernel32::CreateFileW(w "${DATA_ROOT}\desktop.lock", i 0xC0000000, i 3, p 0, i 4, i 0, p 0) p.r0'
  StrCpy $DataLock $0
  ${If} $0 == -1
    SetErrorLevel 25
    Abort
  ${EndIf}
  System::Call 'kernel32::LockFile(p r0, i 0, i 0, i 1, i 0) i.r1'
  ${If} $1 == 0
    SetErrorLevel 25
    Abort
  ${EndIf}
FunctionEnd

Function ReleaseInstallLocks
  ${If} $DataLock != 0
    System::Call 'kernel32::CloseHandle(p $DataLock)'
    StrCpy $DataLock 0
  ${EndIf}
  ${If} $UpdateMutex != 0
    System::Call 'kernel32::CloseHandle(p $UpdateMutex)'
    StrCpy $UpdateMutex 0
  ${EndIf}
FunctionEnd

Function un.onInit
  SetShellVarContext current
  ${If} $INSTDIR != "${APP_ROOT}"
    MessageBox MB_OK|MB_ICONSTOP "설치 폴더를 확인할 수 없어 제거를 중단했습니다."
    Abort
  ${EndIf}
  !insertmacro CheckRunning
FunctionEnd

Section "수서로" SEC_MAIN
  ; Extract completely before touching the installed version. A failed switch
  ; restores the old tree. All paths are derived from the fixed application root.
  StrCpy $PreviousDir "$INSTDIR.previous"
  StrCpy $StageDir "$INSTDIR.update-${APP_VERSION}"
  !insertmacro RejectLinkedDirectory "$INSTDIR"
  !insertmacro RejectLinkedDirectory "$PreviousDir"
  !insertmacro RejectLinkedDirectory "$StageDir"
  ${If} ${FileExists} "$PreviousDir\Suseoro.exe"
    ${IfNot} ${FileExists} "$INSTDIR\Suseoro.exe"
      Rename "$PreviousDir" "$INSTDIR"
    ${EndIf}
  ${EndIf}
  ${If} ${FileExists} "$StageDir\*.*"
    RMDir /r "$StageDir"
  ${EndIf}
  SetOutPath "$StageDir"
  ClearErrors
  File /r "${SOURCE_DIR}\*"
  ${If} ${Errors}
    SetErrorLevel 30
    Abort
  ${EndIf}
  WriteUninstaller "$StageDir\Uninstall.exe"
  ${If} ${Errors}
    SetErrorLevel 30
    Abort
  ${EndIf}
  SetOutPath "$TEMP"
  ${If} ${FileExists} "$INSTDIR\*.*"
    RMDir /r "$PreviousDir"
    ClearErrors
    Rename "$INSTDIR" "$PreviousDir"
    ${If} ${Errors}
      SetErrorLevel 31
      Abort
    ${EndIf}
  ${EndIf}
  ClearErrors
  Rename "$StageDir" "$INSTDIR"
  ${If} ${Errors}
    Rename "$PreviousDir" "$INSTDIR"
    SetErrorLevel 32
    Abort
  ${EndIf}
!ifndef INSTALLER_TEST_ROOT
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
!endif
  ; Keep the previous tree until replacement has succeeded.
  RMDir /r "$PreviousDir"
  ; The MUI Finish page can start the app before .onInstSuccess is called.
  Call ReleaseInstallLocks
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
!ifndef INSTALLER_TEST_ROOT
  Delete "$DESKTOP\수서로.lnk"
  Delete "$SMPROGRAMS\수서로\수서로.lnk"
  Delete "$SMPROGRAMS\수서로\수서로 (브라우저).lnk"
  Delete "$SMPROGRAMS\수서로\수서로 제거.lnk"
  RMDir "$SMPROGRAMS\수서로"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
!endif
SectionEnd
