# 수서로 v2 구현 계획

> **Spec:** `docs/specs/suseoro-v2-product-spec.md`
>
> **Execution:** 이 계획은 `superpowers:subagent-driven-development`와 `superpowers:test-driven-development` 절차로 실행한다. 각 Task는 실패하는 테스트를 먼저 확인하고 구현하며, 커밋 뒤 별도 검토를 통과해야 완료다.

**Goal:** 다양한 추천·장서 자료를 빠르게 비교해 후보를 만들고 승인·발주·납품 검수까지 이어지는 교내망용 수서로 v2를 신규 소스로 제공한다.

**Architecture:** Windows 학교 서버 한 대에서 FastAPI와 내구성 작업 프로세스가 서버 로컬 SQLite WAL 원장을 공유하고, 같은 교내망 사용자는 React·TypeScript 브라우저 화면으로 접속한다. 원본 파일과 파싱 결과는 해시·파서 버전으로 연결하며, 승인과 발주는 불변 revision으로 남긴다. 모든 외부 형식은 표준 도서 계약으로 들어오고 API·화면은 그 계약만 소비한다.

**Technology:** Python 3.11+, uv, FastAPI, stdlib sqlite3, Pydantic, python-calamine, openpyxl, PyMuPDF, lxml, olefile, RapidFuzz, Argon2id, React 19, TypeScript, Vite, Vitest, Testing Library, Playwright, WiX Toolset.

## Global Constraints

- 작업 위치는 격리된 `feature/suseoro-v2` worktree이며 기존 v1 실행본·문서·`workspace` 원본을 수정하지 않는다.
- 백엔드는 Python 3.11 이상에서 설치되고 `uv.lock`으로 재현 가능해야 한다. 프론트엔드는 Node 22 이상에서 잠금 파일로 재현 가능해야 한다.
- 내부 원장은 서버 로컬 SQLite WAL 하나다. 공유폴더에서 DB를 열거나 PostgreSQL·Redis·외부 클라우드를 필수로 만들지 않는다.
- 사용자 화면과 오류 문구는 자연스러운 한국어를 기본으로 하고 기술 설정을 핵심 흐름에서 숨긴다.
- 유효 ISBN이 활성 장서와 정확히 일치할 때만 자동 제외한다. ISBN 없는 제목·저자 일치와 모든 유사 판정은 `NEEDS_REVIEW`다.
- 파싱한 원본 행은 `CANDIDATE + NEEDS_REVIEW + EXCLUDED + ROW_ERROR`로 모두 회계 처리한다. 오류 때문에 조용히 사라지는 행은 허용하지 않는다.
- 모든 원본은 SHA-256, 파일·시트·행 또는 페이지, 원문, 파서 버전과 연결한다. 같은 해시·파서 버전은 재파싱하지 않는다.
- 모든 상태 변경은 멱등키와 사용자 감사기록을 남긴다. 편집 가능한 자원은 행 버전과 `If-Match` 검사를 사용한다.
- 승인 요청 revision은 불변이다. 도서 추가·수량 증가·승인 예산 초과는 재승인을 요구하고 하향 조정은 이력만 남긴다.
- 비승인 DLS API·웹 크롤링과 외부 구매시스템 직접 주문은 구현하지 않는다.
- 학생·대출자·대출이력은 수집하지 않는다. 비밀번호와 비밀값을 로그·설정 JSON·화면·테스트 fixture에 평문으로 남기지 않는다.
- 업로드는 파일 시그니처, ZIP 폭탄, XML 외부 엔터티, 경로 탈출, 수식 주입을 방어한다. 한 파일 실패가 다른 파일 성공을 되돌리지 않는다.
- 웹 UI는 WCAG 2.2 AA를 목표로 키보드만으로 사용할 수 있고, 초점 표시·200% 확대·텍스트 상태 알림을 제공한다. 색만으로 상태를 전달하지 않는다.
- 정식 MSI 서명은 조직 코드서명 인증서가 필요한 외부 릴리스 단계다. 저장소에는 서명 가능한 WiX 구성과 검증 가능한 unsigned CI 산출물을 제공하되 인증서나 개인키를 만들거나 저장하지 않는다.
- 각 Task는 TDD RED와 GREEN 명령·핵심 출력을 report에 남기고, 집중 테스트와 전체 관련 스위트가 경고 없이 통과한 뒤 커밋한다.

## Task 1: 재현 가능한 백엔드 기반과 SQLite 원장

**Purpose:** 이후 모든 기능이 사용하는 설치·설정·DB migration·FastAPI 시작점을 만든다.

**Files:**

- Create: `backend/pyproject.toml`
- Create: `backend/uv.lock`
- Create: `backend/src/suseoro/__init__.py`
- Create: `backend/src/suseoro/config.py`
- Create: `backend/src/suseoro/db/connection.py`
- Create: `backend/src/suseoro/db/migrations.py`
- Create: `backend/src/suseoro/db/migrations/0001_foundation.sql`
- Create: `backend/src/suseoro/api/app.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_foundation.py`
- Modify: `.gitignore`

**Required behavior:**

1. `backend/pyproject.toml`은 `suseoro-v2` 패키지, Python `>=3.11`, 런타임·개발 의존성, pytest 설정을 정의하고 `uv.lock`을 생성한다.
2. 설정은 `SUSEORO_` 환경변수와 지정 data 디렉터리를 사용한다. 데이터·DB·원본·내보내기·백업 경로를 작업 디렉터리에 자동 생성하며 저장소 내부의 v1 폴더를 기본값으로 삼지 않는다.
3. SQLite 연결은 `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`을 매 연결마다 적용한다.
4. migration runner는 `schema_migrations`에 체크섬과 적용 시각을 기록하고 동일 migration 재실행은 멱등이며 변경된 과거 migration은 거부한다.
5. `0001_foundation.sql`은 최소한 `schools`, `users`, `user_roles`, `sessions`, `acquisition_workspaces`, `idempotency_keys`, `audit_events`, `durable_jobs`와 필요한 인덱스를 만든다. 시간은 UTC ISO-8601, 식별자는 UUID 문자열이다.
6. 앱 startup이 migration을 적용하고 `/api/v2/health`에서 버전, DB 준비 상태, 작업자 상태를 반환한다. 비밀값과 절대 data 경로는 반환하지 않는다.
7. `.gitignore`에 Python, Node, DB, 업로드, 출력, 비밀 설정, SDD scratch를 추가하되 기존 `.worktrees/` 규칙을 유지한다.

**TDD and verification:**

1. 먼저 `test_foundation.py`에 WAL pragma, migration 멱등성·체크섬 변조 거부, health 응답, data 경로 생성 테스트를 작성한다.
2. `uv run pytest tests/test_foundation.py -q`가 모듈 없음 또는 기대 동작 미구현으로 실패하는 RED를 기록한다.
3. 최소 구현 뒤 같은 명령과 `uv run pytest -q`를 통과시킨다.
4. `uv lock --check`와 `uv run python -m compileall -q src`를 통과시킨다.

## Task 2: 세션 인증, 권한, CSRF, 감사와 동시성 원시 기능

**Purpose:** 공동 사용에서 누가 어떤 변경을 했는지 보장하고, 이후 업무 API가 재사용할 보안 경계를 만든다.

**Files:**

- Create: `backend/src/suseoro/security/passwords.py`
- Create: `backend/src/suseoro/security/sessions.py`
- Create: `backend/src/suseoro/security/csrf.py`
- Create: `backend/src/suseoro/security/secrets.py`
- Create: `backend/src/suseoro/repositories/auth.py`
- Create: `backend/src/suseoro/services/audit.py`
- Create: `backend/src/suseoro/services/idempotency.py`
- Create: `backend/src/suseoro/services/concurrency.py`
- Create: `backend/src/suseoro/api/dependencies.py`
- Create: `backend/src/suseoro/api/routes/auth.py`
- Modify: `backend/src/suseoro/api/app.py`
- Create: `backend/tests/test_auth_security.py`
- Create: `backend/tests/test_concurrency_idempotency.py`

**Required behavior:**

1. 비밀번호는 Argon2id로 해시하고 검증한다. 원문을 DB·로그에 저장하지 않는다.
2. 로그인은 불투명한 256비트 세션 토큰을 생성하고 DB에는 SHA-256 digest만 저장한다. 쿠키는 `HttpOnly`, `SameSite=Lax`, 운영 기본 `Secure`이며 로그아웃·만료 시 폐기한다.
3. 로그인 뒤 별도 CSRF 토큰을 발급하고 모든 변경 요청에서 쿠키/헤더 double-submit과 세션 연결을 검증한다.
4. 역할 검사는 학교별 `OPERATOR`, `REVIEWER`를 지원한다. 자기 승인은 학교 `single_operator_mode=1`일 때만 허용할 수 있는 공통 policy 함수로 제공한다.
5. Windows에서는 DPAPI 현재 머신 범위로 비밀값을 암복호화한다. 비-Windows 테스트용 구현은 명시적으로 주입해야 하며 운영에서 평문 fallback하지 않는다.
6. 감사 서비스는 actor, school, action, entity, before/after JSON, request ID, UTC 시각을 한 트랜잭션에 기록한다. 민감 필드는 저장 전 제거한다.
7. 멱등 서비스는 `(school_id, actor_id, route, key)`를 유일하게 하고 request hash가 같으면 이전 응답을 돌려주며 다르면 409를 반환한다.
8. 동시성 서비스는 정수 `row_version`을 비교해 갱신하고 충돌 시 현재 값과 제출 값을 포함한 구조화된 412 응답을 만든다. 2분 편집 잠금 계약도 제공한다.
9. `/api/v2/auth/login`, `/logout`, `/me`를 연결한다.

**TDD and verification:**

1. 약한 비밀번호·잘못된 로그인·세션 digest·쿠키 flag·CSRF 누락·권한 거부·DPAPI 주입·민감 감사 필드 제거 테스트를 먼저 작성한다.
2. 같은 멱등키 같은 요청, 같은 키 다른 요청, stale `If-Match`, 잠금 만료 테스트를 먼저 작성한다.
3. 집중 테스트가 구현 전 실패하는 RED를 기록하고 최소 구현 후 `uv run pytest tests/test_auth_security.py tests/test_concurrency_idempotency.py -q`를 통과시킨다.
4. `uv run pytest -q`를 통과시킨다.

## Task 3: 안전한 공통 입력기, 양식 기억과 표 형식 파서

**Purpose:** Excel 병목을 없애고 다양한 표 자료를 원본 보존 계약으로 빠르게 읽는 공통 입력 계층을 만든다.

**Files:**

- Create: `backend/src/suseoro/ingestion/contracts.py`
- Create: `backend/src/suseoro/ingestion/file_store.py`
- Create: `backend/src/suseoro/ingestion/safety.py`
- Create: `backend/src/suseoro/ingestion/detection.py`
- Create: `backend/src/suseoro/ingestion/mapping.py`
- Create: `backend/src/suseoro/ingestion/templates.py`
- Create: `backend/src/suseoro/ingestion/parsers/tabular.py`
- Create: `backend/src/suseoro/ingestion/parsers/text.py`
- Create: `backend/src/suseoro/db/migrations/0003_ingestion.sql`
- Create: `backend/tests/fixtures/ingestion/README.md`
- Create: `backend/tests/test_ingestion_safety.py`
- Create: `backend/tests/test_tabular_parsers.py`
- Create: `backend/tests/test_mapping_templates.py`

**Required behavior:**

1. 표준 계약은 문서 역할, 정규 필드, provenance, 원문, 필드 경고, parser/template version, 처리 결과를 typed model로 표현한다.
2. 업로드는 스트리밍 SHA-256으로 불변 원본 저장소에 원자적으로 저장한다. 허용 크기, 실제 signature, 압축 비율·총 해제 크기·엔트리 수, 안전한 ZIP 경로와 XML parser 설정을 검사한다.
3. 동일 `(sha256, parser_version, role)`은 저장된 parsing 결과를 재사용하고 cached 여부를 반환한다.
4. 탐지는 확장자가 아니라 signature와 내용으로 CSV/TSV/TXT/XLS/XLSX/XLSB/ODS를 판정하고 역할·header row·열 의미 신뢰도를 산출한다.
5. CSV/TSV/TXT는 BOM, UTF-8, CP949/EUC-KR 후보와 구분자를 탐지하고 큰 파일을 스트리밍한다. 붙여넣기 텍스트는 동일 계약을 사용한다.
6. XLS/XLSX/XLSB/ODS는 python-calamine을 우선한다. Calamine이 거부한 호환 가능한 XLSX만 openpyxl `read_only=True`, `data_only=True`로 fallback하고 수식을 실행하지 않는다.
7. 병합셀, 여러 시트, 빈 행, 중복 header, 앞부분 설명 행을 처리한다. 각 데이터 행은 성공 또는 `ROW_ERROR` 중 하나로 반드시 반환한다.
8. 열 연결은 ISBN, 제목, 저자, 출판사, 수량, 단가, 등록번호, 청구기호 등 동의어를 사용한다. 낮은 신뢰도는 최대 20행 미리보기와 필요한 질문을 반환한다.
9. 양식 signature는 정규화 header와 문서 역할, 학교/업체 범위로 만들며 순서 변경과 비필수 열 추가를 견딘다. 필수 의미가 바뀌면 조용히 재사용하지 않는다.
10. `0003_ingestion.sql`은 `source_files`, `source_documents`, `source_rows`, `parser_runs`, `mapping_templates`와 캐시·provenance 인덱스를 만든다.

**TDD and verification:**

1. 지원 형식별 최소 fixture와 병합셀·다중시트·깨진 파일·암호화 ZIP·ZIP 폭탄 metadata·경로 탈출 fixture를 테스트가 직접 안전하게 생성한다.
2. 구현 전 각 포맷 테스트 RED를 기록한다.
3. `uv run pytest tests/test_ingestion_safety.py tests/test_tabular_parsers.py tests/test_mapping_templates.py -q`를 통과시킨다.
4. 20,000행 XLSX fixture를 생성해 첫 파싱 시간과 캐시 재사용 시간을 측정하되 CI 변동을 고려해 하드 성능 gate는 Task 10에서 실행한다.
5. `uv run pytest -q`를 통과시킨다.

## Task 4: 문서·PDF·HWP·KORMARC 파서와 부분 OCR

**Purpose:** 추천도서 및 장서 현장에서 실제로 쓰는 비표 형식도 같은 표준 행 계약으로 읽고 실패 위치를 보존한다.

**Files:**

- Create: `backend/src/suseoro/ingestion/parsers/docx.py`
- Create: `backend/src/suseoro/ingestion/parsers/hwpx.py`
- Create: `backend/src/suseoro/ingestion/parsers/pdf.py`
- Create: `backend/src/suseoro/ingestion/parsers/hwp.py`
- Create: `backend/src/suseoro/ingestion/parsers/marc.py`
- Create: `backend/src/suseoro/ingestion/ocr.py`
- Modify: `backend/src/suseoro/ingestion/detection.py`
- Create: `backend/tests/test_document_parsers.py`
- Create: `backend/tests/test_pdf_ocr.py`
- Create: `backend/tests/test_hwp_parser.py`
- Create: `backend/tests/test_marc_parser.py`

**Required behavior:**

1. DOCX와 HWPX는 안전한 ZIP/XML 도구만 사용하고 문단과 표 셀을 문서 순서, page/section 또는 node provenance와 함께 반환한다.
2. PDF는 페이지별 text density를 산출한다. 충분한 텍스트가 있는 페이지는 OCR하지 않고, 부족한 페이지만 OCR queue에 넣는다. 기본 OCR 동시성은 2다.
3. OCR 엔진은 주입 가능한 port이고 Tesseract 발견·언어 확인·timeout·취소를 지원한다. OCR이 없어도 텍스트 PDF 처리는 되고 필요한 페이지만 `ROW_ERROR`와 설치 안내를 남긴다.
4. HWP parser는 OLE signature, FileHeader 암호화·압축 flag, BodyText Section stream, record header와 `HWPTAG_PARA_TEXT`를 안전하게 읽고 표 셀 텍스트의 원래 순서를 보존한다. 손상·암호화·지원하지 않는 특수 개체는 구조화된 변환 안내와 provenance를 반환한다.
5. MARC parser는 ISO 2709 leader/directory/field 경계를 스트리밍 검증한다. KORMARC의 001, 005, 020$a, 245$a/b/n/p, 260/264$b/c, 100/110/700, 049$l/c, 056, 등록·갱신일 후보를 표준 장서 change로 매핑한다.
6. MARC는 안정적인 source item ID가 없으면 증분을 거부한다. 레코드 하나가 손상돼도 위치와 오류를 남기되 파일 구조 전체가 신뢰 불가능하면 활성화용 결과를 실패시킨다.
7. 어떤 parser도 전역 temp 이름이나 원본 경로 기반 shell 명령을 사용하지 않는다.

**TDD and verification:**

1. 메모리에서 생성한 DOCX/HWPX/PDF와 최소 OLE HWP fixture, ISO 2709 fixture로 정상·손상·암호화·페이지별 OCR 선택 테스트를 먼저 작성한다.
2. 구현 전 `uv run pytest tests/test_document_parsers.py tests/test_pdf_ocr.py tests/test_hwp_parser.py tests/test_marc_parser.py -q` RED를 기록한다.
3. 최소 구현 후 같은 명령과 `uv run pytest -q`를 통과시킨다.
4. parser 출력 수와 성공+오류 수가 fixture의 논리 행/레코드 수와 같은지 검증한다.

## Task 5: 장서 스냅샷·증분, 정규화, 검색 인덱스와 비교 작업

**Purpose:** 기존 전수 비교를 SQLite 인덱스와 후보 제한 방식으로 바꾸고 DLS 장서를 안전하게 교체한다.

**Files:**

- Create: `backend/src/suseoro/catalog/contracts.py`
- Create: `backend/src/suseoro/catalog/normalization.py`
- Create: `backend/src/suseoro/catalog/repository.py`
- Create: `backend/src/suseoro/catalog/sync.py`
- Create: `backend/src/suseoro/matching/engine.py`
- Create: `backend/src/suseoro/jobs/repository.py`
- Create: `backend/src/suseoro/jobs/runner.py`
- Create: `backend/src/suseoro/services/comparison.py`
- Create: `backend/src/suseoro/db/migrations/0004_catalog_matching.sql`
- Create: `backend/tests/test_normalization_matching.py`
- Create: `backend/tests/test_catalog_sync.py`
- Create: `backend/tests/test_comparison_accounting.py`
- Create: `backend/tests/test_job_recovery.py`

**Required behavior:**

1. ISBN-10/13 검증·변환, 제목/부제/저자/출판사/권차/판 정규화 키를 만들되 원문은 보존한다.
2. `0004`는 immutable catalog versions, holdings, normalized works, recommendations, candidate decisions, FTS 검색 인덱스와 정확 ISBN/정규화 키 인덱스를 만든다.
3. 전체 스냅샷은 staging version을 검증하고 한 트랜잭션으로 활성화한다. 0건과 이전 대비 ±30%는 확인 없이는 차단한다. 학교별 활성 source type은 하나다.
4. MARC 증분은 2일 overlap, Asia/Seoul 양끝 포함, 안정 ID upsert, 갱신 파일 우선, 두 파일 성공 뒤 watermark 전진, 90일 전체 요구 규칙을 구현한다. 재실행은 멱등이다.
5. 정확 유효 ISBN 일치는 `EXCLUDED`. ISBN 없는 exact title+author와 유사 후보는 `NEEDS_REVIEW`. 나머지는 `CANDIDATE`다. 판·권차 충돌은 자동 제외하지 않는다.
6. 유사 비교는 FTS/인덱스 상위 30건에만 RapidFuzz 정밀 점수를 계산한다. 전체 장서 Python 이중 loop는 금지한다.
7. comparison job은 단계, 진행량, 취소 요청, 재시도, heartbeat, 오류를 DB에 저장하며 재시작 시 running job을 재개 가능한 상태로 회수한다.
8. 파일별 부분 성공과 모든 원본 행의 accounting을 보장한다. comparison 결과에 판단 이유와 근거 장서 ID를 저장한다.

**TDD and verification:**

1. ISBN 체크섬, ISBN-10 변환, 한글 띄어쓰기·기호, 무ISBN, 권차·개정판, 제적·불확실 상태 골든셋 테스트를 먼저 작성한다.
2. snapshot 원자성, 0건/±30%, 증분 overlap·멱등·watermark 실패 보존, 단일 active source 테스트를 먼저 작성한다.
3. accounting과 job 재시작 테스트 RED 뒤 구현한다.
4. `uv run pytest tests/test_normalization_matching.py tests/test_catalog_sync.py tests/test_comparison_accounting.py tests/test_job_recovery.py -q`와 전체 스위트를 통과시킨다.
5. query plan 테스트에서 정확 ISBN 조회가 인덱스를 사용하고, 유사 엔진이 30개 넘는 후보를 Python으로 평가하지 않음을 검증한다.

## Task 6: 후보 검토, 승인, 견적, 발주, 납품과 내보내기

**Purpose:** 수서 업무의 불변 revision과 승인 범위를 지키며 주문·검수까지 완결되는 도메인 서비스를 만든다.

**Files:**

- Create: `backend/src/suseoro/workflow/states.py`
- Create: `backend/src/suseoro/workflow/candidates.py`
- Create: `backend/src/suseoro/workflow/approvals.py`
- Create: `backend/src/suseoro/workflow/quotes.py`
- Create: `backend/src/suseoro/workflow/orders.py`
- Create: `backend/src/suseoro/workflow/receiving.py`
- Create: `backend/src/suseoro/exports/safe_cells.py`
- Create: `backend/src/suseoro/exports/dls.py`
- Create: `backend/src/suseoro/exports/orders.py`
- Create: `backend/src/suseoro/db/migrations/0005_workflow.sql`
- Create: `backend/tests/test_candidate_workflow.py`
- Create: `backend/tests/test_approval_rules.py`
- Create: `backend/tests/test_quotes_orders.py`
- Create: `backend/tests/test_receiving_scans.py`
- Create: `backend/tests/test_exports.py`

**Required behavior:**

1. 상태 전이는 제품 명세의 상태표 외 경로를 거부하고 actor role, school, reason, before/after, row version을 감사한다.
2. 후보 수정은 자동 저장 가능한 작은 command이며 일괄 결정도 각 대상의 최종 결과를 반환한다. `NEEDS_REVIEW`가 미처리면 승인 요청을 막는다.
3. 승인 요청은 후보·수량·단가·예상금액·예산의 canonical JSON hash와 불변 rows를 저장한다. 자기 승인 policy, 수정 사유, 중복 승인 최초 성공을 적용한다.
4. 승인 뒤 추가·수량 증가·예산 초과·예산 한도 변경은 `APPROVAL_PENDING` 새 revision을 요구한다. 제외·수량/가격 감소는 승인 유지와 감사 기록이다.
5. 견적 import는 ISBN 우선, 무ISBN 제목+저자는 확인 필요로 연결하고 총액, 할인, 품절, 가격 누락, 불일치를 계산한다. 기본 한 업체와 선택적 분할 발주를 지원한다.
6. 발주 revision은 승인 범위를 검증하고 파일을 immutable path에 생성한다. 전달 뒤 수정은 새 revision이며 이전 파일을 덮지 않는다.
7. 납품은 여러 차수를 누적하고 주문 누락, 초과, 수량·단가, ISBN·판본 차이를 계산한다. 한 작업에 활성 scan session 하나다.
8. 같은 scan idempotency key는 한 번만 수량을 올린다. 정상, 초과, 미주문, 판본차이를 텍스트 코드로 반환한다. 모든 수량 확인 또는 모든 차이 disposition 지정 전 완료를 막는다.
9. DLS ISBN TXT와 서명 XLSX는 제품 명세의 byte/시트/열/기본값 계약을 정확히 지킨다. 모든 spreadsheet 내보내기는 수식 주입을 막는다.

**TDD and verification:**

1. 상태표의 허용·금지 전이, 자기 승인, 재승인 trigger와 하향 조정 테스트를 먼저 작성한다.
2. 견적 불일치, 예산 초과 비자동삭제, immutable 발주 revision, 부분납품·중복/초과/미주문 scan 테스트를 먼저 작성한다.
3. ISBN TXT를 byte 단위로, XLSX를 sheet/열/문자열 타입 단위로 검증하는 테스트를 먼저 작성한다.
4. 구현 전 집중 스위트 RED, 구현 후 `uv run pytest tests/test_candidate_workflow.py tests/test_approval_rules.py tests/test_quotes_orders.py tests/test_receiving_scans.py tests/test_exports.py -q`와 전체 스위트 GREEN을 기록한다.

## Task 7: `/api/v2`, SSE 작업 알림, 이전과 백업 API

**Purpose:** 도메인 서비스를 일관된 HTTP 계약으로 공개하고 재시도·공동사용·운영 기능을 연결한다.

**Files:**

- Create: `backend/src/suseoro/api/errors.py`
- Create: `backend/src/suseoro/api/routes/workspaces.py`
- Create: `backend/src/suseoro/api/routes/sources.py`
- Create: `backend/src/suseoro/api/routes/candidates.py`
- Create: `backend/src/suseoro/api/routes/approvals.py`
- Create: `backend/src/suseoro/api/routes/procurement.py`
- Create: `backend/src/suseoro/api/routes/deliveries.py`
- Create: `backend/src/suseoro/api/routes/events.py`
- Create: `backend/src/suseoro/api/routes/audit.py`
- Create: `backend/src/suseoro/api/export_openapi.py`
- Create: `backend/openapi.json`
- Create: `backend/src/suseoro/migration/v1.py`
- Create: `backend/src/suseoro/backup/service.py`
- Modify: `backend/src/suseoro/api/app.py`
- Create: `backend/tests/test_api_contract.py`
- Create: `backend/tests/test_sse_jobs.py`
- Create: `backend/tests/test_v1_migration.py`
- Create: `backend/tests/test_backup_restore.py`

**Required behavior:**

1. 제품 명세에 열거된 `/api/v2` 경계를 구현하고 OpenAPI schema에 한국어 summary와 stable operation ID를 제공한다.
2. 모든 변경 route는 인증, CSRF, 역할, `Idempotency-Key`, 필요한 `If-Match`를 공통 dependency로 검증한다. 오류는 code, 한국어 message, request ID, field details를 가진다.
3. 업로드는 multipart를 스트리밍 저장하고 즉시 durable job ID를 반환한다. 파일별 실패는 207 또는 job item 결과로 표현하며 다른 파일 성공을 유지한다.
4. SSE는 job 단계·진행량, 승인, 잠금, scan 결과를 event ID와 함께 보내고 `Last-Event-ID` 재연결을 지원한다. heartbeat는 15초 간격이며 끊긴 client 때문에 job을 취소하지 않는다.
5. 목록 route는 cursor pagination, 서버 필터, 안정 정렬을 지원해 20만 장서를 브라우저에 모두 보내지 않는다.
6. v1 migration은 입력 경로를 읽기 전용으로 다루고 학교·회차·최신 정상 DLS 후보·catalog cache 구분, 복사, 건수 보고서를 제공한다. source 파일을 수정/삭제하지 않는다.
7. backup은 SQLite online backup API로 일관된 snapshot과 manifest/checksum을 만들고 임시 복원 검증 뒤 성공 표시한다. 일간 7·주간 4·월간 12 보존 선택 함수를 제공한다.
8. restore는 현재 DB의 사전 백업, checksum·schema 검증, 원자 교체 경계를 제공한다. API에서 실제 restore 실행은 관리자 로컬 확인 token 없이는 거부한다.
9. OpenAPI와 TypeScript client 생성에 사용할 `backend/openapi.json`을 테스트된 앱에서 생성한다.

**TDD and verification:**

1. 역할별 happy/error path, CSRF, 멱등 재시도, 412 충돌 payload, pagination, partial upload 테스트를 먼저 작성한다.
2. SSE replay/heartbeat, migration 원본 불변, backup checksum/retention/restore 실패 원자성 테스트를 먼저 작성한다.
3. 집중 테스트 RED 뒤 구현하고 `uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q`를 통과시킨다.
4. `uv run pytest -q`, `uv run python -m suseoro.api.export_openapi`, `git diff --exit-code backend/openapi.json`을 통과시킨다.

## Task 8: React 기반 로그인·업무함·후보 만들기 작업실

**Purpose:** 사서가 설명서 없이 자료를 넣고 없는 책 후보를 확인·승인 요청할 수 있는 첫 세 화면을 구현한다.

**Files:**

- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/app/App.tsx`
- Create: `frontend/src/app/router.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/styles/tokens.css`
- Create: `frontend/src/styles/global.css`
- Create: `frontend/src/components/*`
- Create: `frontend/src/features/auth/*`
- Create: `frontend/src/features/workspaces/*`
- Create: `frontend/src/features/ingestion/*`
- Create: `frontend/src/features/candidates/*`
- Create: `frontend/src/test/*`
- Create: `frontend/tests/login-worklist.test.tsx`
- Create: `frontend/tests/ingestion-candidates.test.tsx`

**Required behavior:**

1. 화면은 `로그인`, `내 수서 업무`, `수서 작업실` 세 shell로만 보이고 역할·현재 상태에 맞는 주요 행동 하나를 우선한다.
2. 업무함은 담당자에게 이어서 할 작업, 검토자에게 승인할 작업을 먼저 보이며 학교, 작업명, 상태, 다음 담당, 마지막 저장만 표시한다.
3. 작업실 상단에 후보 만들기/승인·발주/납품 검수 세 과정을 표시하되 현재 과정만 펼치고 완료 과정은 한 줄 요약, 미래 과정은 비활성이다.
4. 공통 drop zone은 파일 여러 개와 텍스트 붙여넣기를 받고 지원 형식을 명시한다. 각 파일의 역할, 단계, 진행률, 읽은 수, 확인 필요, 실패, 재시도·취소를 텍스트로 보여준다.
5. 낮은 신뢰도 양식은 최대 20행 미리보기와 열 연결 dialog를 열며 `이 양식 기억`이 기본 선택이다.
6. 후보 화면은 수서 후보/확인 필요/제외된 책 count와 필터를 제공하고 기본적으로 확인 필요를 먼저 처리하게 한다. exact ISBN 제외 사유와 되돌리기를 표시한다.
7. 행 수정은 blur 또는 명시적 값 확정 시 자동 저장하고 현재/내 변경 충돌 dialog를 제공한다. 다른 사용자 편집 잠금과 `저장됨 HH:MM`을 표시한다.
8. 화면 전환용 저장·다음 버튼을 두지 않는다. 주요 카피는 `우리 도서관에 없는 책 찾기`, `후보 확정하고 승인 요청`을 그대로 사용한다.
9. API 요청은 cookie session, CSRF, idempotency, ETag를 처리하고 네트워크 단절 시 상태 변경을 막되 입력 중 문구를 메모리에 보존한다.
10. 키보드 순서, visible focus, skip link, semantic heading/table, aria-live 상태, 200% 확대와 모바일 최소 폭을 지원한다.

**TDD and verification:**

1. Testing Library로 로그인 오류/성공, 역할별 업무함 우선순위, 한 개 주요 행동 테스트를 먼저 작성한다.
2. upload 진행·부분 실패, 열 연결, 후보 분류, 자동저장, 412 충돌, 키보드 탐색/aria-live 테스트를 먼저 작성한다.
3. 구현 전 `npm test -- --run` RED를 기록하고 구현 후 같은 명령을 통과시킨다.
4. `npm run typecheck`, `npm run lint`, `npm run build`를 경고 없이 통과시킨다.
5. 생성된 `frontend/package-lock.json`을 커밋한다.

## Task 9: 승인·견적·발주·납품·바코드 UI와 브라우저 검증

**Purpose:** 같은 작업실에서 검토자 승인부터 실물 도서 스캔 완료까지 주요 조작 10회 이내 흐름을 완성한다.

**Files:**

- Create: `frontend/src/features/approvals/*`
- Create: `frontend/src/features/quotes/*`
- Create: `frontend/src/features/orders/*`
- Create: `frontend/src/features/receiving/*`
- Create: `frontend/tests/approval-procurement.test.tsx`
- Create: `frontend/tests/receiving-scanner.test.tsx`
- Create: `frontend/e2e/core-flow.spec.ts`
- Create: `frontend/e2e/accessibility.spec.ts`
- Create: `frontend/playwright.config.ts`
- Modify: `frontend/src/app/App.tsx`
- Modify: `frontend/src/components/*`

**Required behavior:**

1. 승인자는 후보 수, 예상금액/예산, 장서 기준일, 확인 필요 완료, 이전 revision 변화, 자동 제외 요약만 먼저 보고 `이 목록 승인` 또는 사유 필수 `수정 요청 보내기`를 한다.
2. 담당자는 수정 요청 내용을 보고 재요청하며 승인 뒤 여러 견적 카드를 총액, 할인, 품절, 가격 누락, 불일치로 비교한다.
3. 예산은 `승인 예산 · 선택 견적 · 차이`를 항상 보여준다. 초과 시 자동 삭제하지 않고 조정 또는 재승인을 안내한다.
4. `발주파일 받기` 한 행동으로 생성·다운로드하고 외부 직접 전달 문구를 표시한다. `업체에 전달했어요` 뒤 납품 단계로 이동한다.
5. 명세서 비교는 누락, 초과, 수량·단가, ISBN·판본 차이를 요약하고 차이 disposition을 선택하게 한다.
6. 바코드 집중 모드는 mount와 매 scan 뒤 input focus를 유지한다. Enter 종료 입력을 ISBN으로 정규화하고 정상/초과/미주문/판본차이를 큰 문구, aria-live, 서로 다른 음향 pattern으로 전달한다.
7. 부분 납품 진행량을 누적하고 완료 조건이 충족되지 않으면 완료 버튼을 비활성화하며 이유를 텍스트로 표시한다.
8. 리뷰어 2회, 담당자 정상 흐름 10회 이하의 주요 조작을 Playwright 테스트가 명시적으로 count한다. 도서 수만큼의 scan 사이 추가 클릭은 0이다.
9. 200% 확대, 키보드 전용, focus trap 복귀, 색상 외 상태 정보, axe의 critical/serious 위반 0을 자동 검증한다.

**TDD and verification:**

1. component 테스트를 먼저 작성하고 구현 전 RED를 기록한다.
2. 승인 재요청, 예산 초과, 발주 download, scan focus/중복/오류/부분납품 테스트를 구현 후 통과시킨다.
3. `npm test -- --run`, `npm run typecheck`, `npm run lint`, `npm run build`를 통과시킨다.
4. 백엔드 test server와 함께 `npm run e2e`를 실행해 핵심 흐름과 접근성 테스트를 통과시킨다.

## Task 10: 성능 합격선, 운영 도구, WiX 설치 구성과 사용자 문서

**Purpose:** 실제 학교 PC에 설치·복구·검증할 수 있는 릴리스 후보를 만들고 명세의 성능과 운영 기준을 자동 확인한다.

**Files:**

- Create: `backend/tests/performance/test_school_scale.py`
- Create: `backend/tests/integration/test_end_to_end.py`
- Create: `scripts/dev.ps1`
- Create: `scripts/test-all.ps1`
- Create: `scripts/benchmark.ps1`
- Create: `scripts/backup.ps1`
- Create: `scripts/restore.ps1`
- Create: `scripts/install-certificate.ps1`
- Create: `installer/Product.wxs`
- Create: `installer/Bundle.wxs`
- Create: `installer/build.ps1`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/windows-package.yml`
- Modify: `README.md`
- Create: `docs/quick-start.md`
- Create: `docs/admin-guide.md`
- Create: `docs/migration-guide.md`
- Create: `docs/security.md`
- Create: `docs/parser-support.md`
- Create: `docs/release-checklist.md`
- Modify: backend/frontend production files only when a failing performance or end-to-end test proves the change is required

**Required behavior:**

1. 성능 fixture는 실제 개인정보 없이 결정적으로 20,000 장서와 1,000 추천, 1,000 발주행을 생성한다. 표준 학교 PC profile에서 제품 명세의 10초/1초/3초/200ms/5초 기준을 측정하고 JSON·사람용 표를 남긴다.
2. CI의 일반 test는 flaky 시간 gate를 제외하고 query count·후보 30 제한·캐시 hit를 검증한다. Windows benchmark job에서만 절대 시간 gate를 실행하고 machine profile을 산출물에 기록한다.
3. end-to-end backend 테스트는 학교/사용자 생성, 장서 import, 추천 import, 비교, 검토, 승인, 견적, 발주, 부분 납품, scan, 완료, 감사 조회를 실제 SQLite와 파일로 수행한다.
4. PowerShell 도구는 사용자 data 경로를 명시적으로 검증하며 repository root, 사용자 home, unresolved variable을 파괴 대상으로 사용하지 않는다. restore는 확인 가능한 backup manifest만 받는다.
5. WiX 구성은 서버 서비스, 작업 프로세스, 정적 UI, data 디렉터리 ACL, 방화벽 교내망 rule, 시작 메뉴, 제거 시 사용자 data 보존, 업그레이드 전 backup custom action 경계를 정의한다.
6. package workflow는 unsigned MSI와 checksum/SBOM을 만든다. release checklist는 코드서명 인증서로 서명·검증되지 않은 MSI를 정식 배포하지 못하게 한다. 인증서·비밀키는 저장소에 두지 않는다.
7. README는 v1 설명을 v2 중심으로 바꾸되 v1 실행본의 존재와 v2 source build 상태를 구분한다. 6단계 반복 사용법, 설치/관리/이전/보안/지원 형식 링크, DLS file-first 정책, 외부 주문 미전송을 명시한다.
8. admin guide는 HTTPS 인증서, 교내망 방화벽, 최초 학교/사용자, 1인 운영, 백업/복원, 로그와 장애 복구를 비개발자 언어로 설명한다.
9. parser support 문서는 각 형식의 정상 지원, 확인 필요, 변환 안내와 원본 미삭제 원칙을 표로 제공한다.

**TDD and verification:**

1. 성능·end-to-end 테스트를 먼저 작성해 아직 없는 기능/기준에서 RED를 확인한 뒤 필요한 최소 최적화와 도구를 추가한다.
2. `backend`에서 `uv run pytest -q`와 명시적 performance profile을 통과시킨다.
3. `frontend`에서 `npm ci`, `npm test -- --run`, `npm run typecheck`, `npm run lint`, `npm run build`, `npm run e2e`를 통과시킨다.
4. `scripts/test-all.ps1`이 깨끗한 checkout에서 백엔드·프론트엔드 전체 검증을 한 번에 수행한다.
5. WiX가 설치된 Windows CI에서 `installer/build.ps1`이 unsigned MSI를 만들고 ICE validation, checksum, SBOM을 통과시킨다. 로컬에 WiX가 없으면 script unit/static 검증과 CI workflow validation을 수행하고 제약을 report에 기록한다.
6. `git diff --check`, 비밀 패턴 scan, `git status --short`, 생성물 ignore 검사를 통과시킨다.
