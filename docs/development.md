# 개발·검증·Windows 빌드

## 구조

- `frontend/src/simple`: React 개인용 화면. `main.tsx`의 앱 진입점입니다.
- `backend/src/suseoro/simple`: 개인용 SQLite 저장소, 문서 정렬, ISBN 조회, 발주서, 백업, 데스크톱 실행과 업데이트.
- `backend/src/suseoro/ingestion`: 기존 자료 해석기를 재사용합니다.
- `desktop`, `installer`, `scripts/build-desktop.ps1`: PyInstaller와 사용자별 Windows 설치본.

이전 다중 사용자 API와 화면은 문서 파서 회귀 검증 및 개발 참고용으로 보존합니다. 제품 진입점에서 해당 업무 흐름을 사용하지 않습니다.

## 소스에서 실행

Python 3.11, Node.js 22 이상, uv가 필요합니다. 저장소를 복제한 후 PowerShell에서 실행합니다.

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ../backend
uv sync --locked --extra desktop
uv run suseoro --browser
```

네이티브 창으로 실행하려면 마지막 명령을 `uv run suseoro`로 바꿉니다. 자료 저장 위치를 분리하려면 `--data-dir C:\원하는\폴더`를 지정합니다. 기본 자료 위치는 `%LOCALAPPDATA%\Suseoro`입니다.

개발 중 화면 변경 후에는 frontend를 다시 빌드합니다. API는 무작위 포트의 127.0.0.1에만 열립니다. Vite 개발 서버를 별도로 사용할 경우 `SUSEORO_DEV_ORIGIN`을 해당 로컬 주소로 명시하고 API를 8000 포트로 실행해야 합니다.

## 검증

```powershell
Set-Location backend
uv run pytest -q
Set-Location ../frontend
npm run test -- --run
npm run typecheck
npm run lint
npm run build
```

구입 선택·금액·가격 누락·ISBN 판본 일치·파일 해석·학교 양식·백업 원자성·안전한 업데이트를 검사합니다. 실제 학교 자료 대신 합성 테스트 자료를 사용합니다. 기존 `frontend/e2e`는 이전 다중 사용자 화면의 참고용 테스트입니다.

## Windows 배포물 만들기

공식 [NSIS portable](https://nsis.sourceforge.io/Download) 컴파일러 또는 이미 설치된 Inno Setup 컴파일러를 준비합니다. 빌드 스크립트가 컴파일러를 시스템에 설치하지는 않습니다.

저장소 루트에서:

```powershell
./scripts/build-desktop.ps1 -TesseractDir ./portable_tesseract -MakensisPath C:\도구\NSIS\makensis.exe -PortableZip
```

`dist`에 설치 파일, Portable ZIP, SHA-256 체크섬이 생깁니다. 설치 파일은 프로그램을 `%LOCALAPPDATA%\Programs\Suseoro`에 설치하고, 자료는 `%LOCALAPPDATA%\Suseoro`에 유지합니다. 제거 프로그램은 자료 폴더를 삭제하지 않습니다.

OCR 없이 빌드하려면 `-TesseractDir`를 생략합니다. 이 경우 스캔 PDF에서 문자 인식 기능을 사용할 수 없습니다. 공식 릴리스에는 한국어·영어 OCR을 포함합니다. 소스 저장소의 OCR 바이너리·학습 데이터는 원래 배포본의 필수 구성만 보존합니다.

## 새 버전 배포

Python 프로젝트, `simple.VERSION`, frontend package/lock의 버전을 함께 변경하고 검증합니다. GitHub Actions의 Windows 패키징 작업에서 해당 버전을 지정해 배포물을 만들 수 있습니다. `v2.x.y` 정식 릴리스에는 `Suseoro-Setup-2.x.y.exe`, 해당 `.sha256`, `SHA256SUMS`를 올립니다. 선택적으로 Portable ZIP도 올립니다. 업데이트는 공식 저장소의 정식 릴리스와 체크섬을 확인한 뒤 설치합니다.

## 데이터 보호

API는 동일 출처와 실행마다 다른 요청 토큰을 확인합니다. 일반 업로드는 50 MiB, 백업 JSON은 300 MiB로 제한합니다. 백업 첨부는 파일당 50 MiB, 합계 200 MiB, 2,000개까지입니다. 백업에 경로나 API 비밀키를 포함하지 않으며, 복원 경로는 앱이 새로 생성합니다. 문서 전체를 외부 AI에 전송하지 않습니다.
