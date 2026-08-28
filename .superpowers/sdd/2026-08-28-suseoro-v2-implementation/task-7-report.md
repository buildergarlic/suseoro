# Task 7 구현 보고서

작성일: 2026-08-28
브랜치: `feature/suseoro-v2`
기준 커밋: `7687a68`

## 결과 요약

제품 명세의 `/api/v2` 도메인 경계를 한국어 summary와 고정 operation ID로 공개하고,
공통 인증/CSRF/역할/멱등성/If-Match/error envelope를 연결했다. multipart 원본은
content-addressed 저장소에 chunk 단위로 저장하며 파일별 성공/실패를 유지하고, 실제
`INGEST`/`PARSE` durable job runner가 parser 결과와 진행 checkpoint를 DB에 보존한다.

- workspace, source/job, candidate, approval, quote/order, delivery/scan, audit/admin route를 추가했다.
- 목록 route는 server filter, bounded limit, stable cursor ordering을 사용한다.
- SSE event는 DB에 내구 저장되고 `Last-Event-ID` 뒤의 같은 학교/workspace event만 순서대로 replay한다.
- job checkpoint/terminal 상태, approval, candidate lock, scan 결과를 event ID와 함께 보낸다.
- event가 없을 때 heartbeat는 15초이며 feed 종료는 job cancel을 요청하지 않는다.
- v1 원본은 read-only로 점검하고 학교/회차/DLS 후보/catalog cache를 분류하여 별도 경로로 복사한다.
- backup은 SQLite online backup API, DB/manifest checksum, schema migration manifest, 임시 DB integrity 검증을 사용한다.
- restore는 checksum/schema 검증 뒤 pre-restore backup을 만들고 원자 교체하며, loopback 요청과 process-local admin token이 모두 필요하다.
- 검증된 앱에서 `backend/openapi.json`을 deterministic하게 생성한다.

## TDD 증거

제품 코드를 추가하기 전에 다음 네 focused module을 먼저 작성했다.

- `backend/tests/test_api_contract.py`
- `backend/tests/test_sse_jobs.py`
- `backend/tests/test_v1_migration.py`
- `backend/tests/test_backup_restore.py`

기준 suite는 `306 passed in 34.42s`였다. 최초 focused RED는 다음과 같았다.

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q
3 collection errors
ModuleNotFoundError: suseoro.api.routes.events
ModuleNotFoundError: suseoro.migration
ModuleNotFoundError: suseoro.backup
```

첫 최소 구현은 `11 passed, 9 failed`였고, executable signature 오인식, 손상 workbook
격리, manifest schema column, Windows restore handle, checksum 검증 순서가 각각 실패로
드러났다. 수정 뒤 최초 요구 focused set은 `20 passed in 6.13s`였다.

자체 검토에서 durable ingestion과 checkpoint event를 고정하는 두 테스트를 먼저 추가했다.

```text
uv run pytest \
  tests/test_api_contract.py::test_uploaded_csv_durable_job_parses_and_persists_rows \
  tests/test_sse_jobs.py::test_durable_job_checkpoints_publish_replayable_progress_events -q
2 failed in 2.07s
```

실패 원인은 `INGEST` handler 미등록과 checkpoint event 미보존이었다. parser dispatch/행
보존/파일별 failure isolation과 repository checkpoint event를 구현한 뒤 `2 passed in
1.68s`가 되었다. source mapping/parse의 실제 replay 테스트도 먼저 `200 != 412`로
실패한 뒤 shared idempotency ledger/audit를 연결해 통과시켰다.

최종 focused suite:

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q
......................... [100%]
25 passed in 7.00s
```

최종 backend 전체 suite:

```text
uv run pytest -q
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 87%]
...........................................                              [100%]
331 passed in 42.58s
```

중간 full run의 세 실패는 과거 migration fixture가 새 forward migration까지 잘못 복사한
것이었고, historical setup에서는 `0006`을 제외하고 current upgrade 뒤에는 `0006`을
기대하도록 고쳤다. 그 다음 full run의 유일한 실패(`328 passed, 1 failed`)는 source role
enum 추가 뒤 stale OpenAPI artifact였으며 재생성 후 통과했다. 마지막으로 restore API
동일 key 재시도가 두 번째 pre-restore backup을 만드는 RED를 추가하고 restored DB의
idempotency ledger에 완료 응답을 기록해 고쳤다. SSE side effect 자체 검토에서는 workflow
service가 replay 응답을 반환한 뒤 route가 동일 event를 다시 publish하는 RED를 추가했다.
`api_events` idempotent side-effect key를 unique하게 보존하고 같은 key에는 기존 event ID를
반환하도록 고친 뒤 위 331개가 모두 통과했다.

## API와 운영 결정

- 변경 route는 unsafe-method CSRF middleware와 route dependency를 함께 사용한다. 역할은
  공통 dependency 또는 기존 workflow service의 school-scoped policy로 다시 확인한다.
- source upload/mapping/parse/job retry/cancel은 idempotency reservation, domain change,
  audit, stored response를 한 transaction으로 완료한다. 기존 Task 6 mutation은 그 서비스의
  동일한 idempotent transaction을 그대로 사용한다.
- upload는 `UploadFile.file.read(64 KiB)` iterator를 immutable file store에 넘겨 전체
  request body를 메모리에 올리지 않는다. 허용 파일은 유지하고 거부 파일은 item error로
  반환하므로 mixed batch는 HTTP 207이다.
- source document의 immutable identity trigger를 존중한다. 사용자 mapping/role은 별도
  versioned `source_configurations`에 저장하고 parser는 configured role을 사용한다.
- `INGEST`/`PARSE`는 문서마다 parser를 선택하고 source row provenance/field/warning/error를
  보존한다. 한 문서 parser failure가 같은 batch의 다른 문서를 rollback하지 않는다.
- SSE disconnect는 iterator만 닫고 durable job에는 cancel mutation을 보내지 않는다.
- v1 destination이 source tree 아래면 거부한다. 복사는 충돌 시 digest suffix를 사용하고
  source delete를 수행하지 않으며 두 실제 회차 검증 전 보존 안내를 보고한다.
- restore 검증이 끝나기 전 현재 DB를 건드리지 않는다. 검증 성공 뒤에만 pre-restore
  online backup, WAL checkpoint, `os.replace`를 수행한다.

## Migration과 checksum 증거

새 forward migration은 `backend/src/suseoro/db/migrations/0006_api_operations.sql` 하나다.
`api_events`, `workspace_sources`, `source_configurations`와 school/workspace scope trigger를
추가한다.

```text
Get-FileHash backend/src/suseoro/db/migrations/0006_api_operations.sql -Algorithm SHA256
ce06c0866f14a1e2932cc21ce18c80f239b97e927ac14fb6c9e32e7cb1797ebf

git diff --exit-code HEAD -- backend/src/suseoro/db/migrations
exit 0 for every migration already committed at the Task 7 baseline
```

fresh DB, repeated apply/checksum rejection, populated prior-schema forward upgrade를 포함한 전체
suite가 통과했다. 기존 committed migration은 byte-for-byte 수정하지 않았다.

## 주요 구현 파일

추가:

- `backend/src/suseoro/api/{common.py,errors.py,export_openapi.py}`
- `backend/src/suseoro/api/routes/{workspaces,sources,candidates,approvals,procurement,deliveries,events,audit}.py`
- `backend/src/suseoro/backup/{__init__.py,service.py}`
- `backend/src/suseoro/migration/{__init__.py,v1.py}`
- `backend/src/suseoro/db/migrations/0006_api_operations.sql`
- `backend/openapi.json`
- 위 네 focused test module

변경:

- `backend/src/suseoro/api/app.py`, `dependencies.py`, `routes/auth.py`
- `backend/src/suseoro/jobs/handlers.py`, `repository.py`
- `backend/src/suseoro/ingestion/file_store.py`
- `backend/pyproject.toml`, `backend/uv.lock` (`python-multipart` 추가)
- migration forward-upgrade fixture와 Ruff formatter가 정리한 기존 Python 파일

## 도구 검증

```text
uvx ruff format --check backend
101 files already formatted

uvx ruff check backend
All checks passed!

uv run python -m compileall -q src tests
exit 0

uv lock --check
Resolved 36 packages in 1ms

uv build
Successfully built dist\suseoro_v2-0.1.0.tar.gz
Successfully built dist\suseoro_v2-0.1.0-py3-none-any.whl
```

`backend/dist/`, `__pycache__/`, `*.pyc`는 ignore 상태이며 commit 대상이 아니다.
OpenAPI 재생성/diff 안정성, `git diff --check`, 비밀 패턴 scan과 최종 clean status는 commit
직전/직후에 다시 확인한다.

## 독립 검토 수정 라운드 1/5

기준 커밋은 `855343e`이다. 독립 검토의 Critical 1건, Important 15건과 인접 Minor를
행동별 회귀 테스트로 먼저 고정한 뒤 최소 제품 구현을 연결했다. 이 라운드는 기존
`0001`~`0006` migration을 수정하지 않고 새 forward migration만 추가했다.

### 라운드 TDD RED/GREEN 증거

아래 네 focused module에 acquisition composition, mapping/cache, delta window, per-file job
result, pagination, OpenAPI, error envelope, v1 migration, backup/restore, SSE session revalidation
probe를 먼저 추가했다.

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q
22 failed, 24 passed in 18.79s
```

첫 RED에는 catalog staging/activation 및 compare route 부재, delta DB trigger 위반,
ParserCache/template 미사용, per-file 결과 부재, 잘못된 cursor/source role 계약, untyped
OpenAPI, v1 app visibility 부재, future/old schema restore 검증 부재, SSE revoked-session 미종료가
각각 실제 assertion failure로 포함됐다. v1 후보 활성화 confirmation boundary를 마지막으로
추가했을 때도 다음의 별도 RED를 확인했다.

```text
uv run pytest tests/test_api_contract.py::test_v1_admin_routes_require_local_confirmation_and_configured_path_roots -q
1 failed (expected activation HTTP 200, received HTTP 404)
```

최종 fresh focused GREEN:

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q
..............................................                           [100%]
46 passed in 19.45s
```

최종 fresh backend 전체 GREEN:

```text
uv run pytest -q
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 61%]
........................................................................ [ 81%]
................................................................         [100%]
352 passed in 59.16s
```

### 검토 항목별 구현 결정

- catalog full/delta staging과 확인 activation, comparison durable job route를 실제
  `CatalogSyncService`/`ComparisonService`에 연결했다. compare의 모든 항목이 성공 완료된
  transaction에서만 workspace가 `CANDIDATE_REVIEW`로 전이된다.
- configured `mapping_json`을 parser 결과에 적용하고, header fingerprint/vendor/role별
  `MappingTemplateStore` 조회·저장 및 `remember_template`을 parse/retry 경로에 연결했다.
  원본 parser 결과는 `(sha256, parser_version, role)` `ParserCache.get_or_parse`와
  claim token/generation fencing으로 한 번만 계산한다.
- catalog/holdings delta role은 `requested_start_date`와 `requested_through_date`를 둘 다
  요구하고 inclusive window를 그대로 보존한다. 역전 window는 durable mutation 전에
  거부한다.
- `INGEST`/`PARSE`가 문서별 durable result와 오류를 남기고 집계 상태를
  `SUCCEEDED`/`PARTIAL`/`FAILED`로 계산하며 `getJob.items`로 공개한다.
- source configured-role filter, audit qualified cursor, receiving difference의 독립
  filter와 `(created_at,id)` cursor를 구현했다. 모든 목록은 bounded limit와 stable tie-breaker를
  갖는다.
- 모든 JSON operation의 typed success `$ref`와 공통 typed error envelope를 OpenAPI에
  생성하고 CSRF, request ID, idempotency, If-Match, local confirmation required header를
  client-visible required parameter로 고정했다. operation ID와 한국어 summary는 안정적이다.
- `RuntimeError`, retry/cancel/claim, SQLite integrity, filesystem, migration/backup validation이
  request ID 포함 error envelope로 수렴한다. domain role denial은 원래 403/code를 유지한다.
- v1 migration은 source를 read-only로 유지하면서 앱에 보이는 school과 read-only history,
  artifact copy, 최신 healthy DLS pending candidate를 만든다. 후보는 row-count/local-admin
  confirmation route 전에는 active catalog가 아니며 `catalog_master`는 cache로만 복사한다.
- DLS 선택은 지원 spreadsheet parser로 실제 1개 이상 행을 읽은 파일만 healthy로 보며
  날짜/natural filename 순서(`DLS_10 > DLS_9`)를 사용한다. 임의 bytes와 zero-row 파일은
  후보에서 제외한다.
- v1 admin은 OPERATOR + loopback + process-local confirmation과 configured import/destination
  root resolved containment를 모두 요구해 arbitrary server path와 escape를 차단한다.
- restore는 process-wide lock과 operation replay cache, connection quiescence로 Windows handle과
  worker를 fencing한다. 검증 후 prebackup은 한 번만 만들고 atomic swap 뒤 system audit는
  restored DB에 기존 actor가 없어도 안전하다.
- restore schema history는 bundled migration ID/checksum의 exact prefix만 허용한다. future,
  incomplete, altered history를 swap 전에 거부하고 compatible older backup은 temporary DB에서
  forward upgrade한 뒤 교체한다.
- upload는 batch count/file/aggregate byte limit를 streaming 중 집행하고 idempotency/hash replay를
  확인해 orphan publish를 제거한다. quote/delivery/candidate bulk 배열은 Pydantic 상한으로 durable
  mutation 전에 거부한다.
- v1 migration, backup creation, catalog activation, candidate edit-lock acquisition에 actor/time,
  before/after, request/version/idempotency audit를 남긴다.
- SSE는 매 poll마다 session 존재, expiry, revocation을 재검증하고 revoked stream만 닫는다.
  malformed/negative `Last-Event-ID`는 structured validation error이며 disconnect는 job cancel을
  일으키지 않는다.
- backup kind를 enum으로 제한하고 목록을 stable cursor page로 바꿨으며 scheduled backup 생성 시
  daily/weekly/monthly retention을 실제 파일/manifest에 집행한다.

### 라운드 migration/checksum 증거

추가 migration은 `backend/src/suseoro/db/migrations/0006a_api_hardening.sql` 하나다.
job item results, comparison/staging, v1 read-only history/candidate 및 관련 무결성 trigger/index를
forward-only로 추가한다.

```text
git diff --exit-code 855343e -- backend/src/suseoro/db/migrations \
  ':(exclude)backend/src/suseoro/db/migrations/0006a_api_hardening.sql'
exit 0

0006_api_operations.sql
CE06C0866F14A1E2932CC21CE18C80F239B97E927AC14FB6C9E32E7CB1797EBF

0006a_api_hardening.sql
4E18920AA31AA5129CC23E123681062CDDE79E9329EB7B90307C27C80A021F86
```

### 라운드 변경 파일과 품질 gate

주요 제품 변경은 API app/error 및
`routes/{audit,candidates,deliveries,events,procurement,sources,workspaces}.py`,
`backup/service.py`, `migration/v1.py`, `db/{connection,migrations}.py`,
`ingestion/{mapping,templates}.py`, `jobs/{handlers,repository,runner}.py`, catalog/config와
generated `backend/openapi.json`이다. focused 네 test module과 migration-version fixture 두 곳을
갱신했다.

```text
uv run --with ruff ruff format --check src tests
101 files already formatted

uv run --with ruff ruff check src tests
All checks passed!

uv run python -m compileall -q src tests
exit 0

uv lock --check
Resolved 36 packages in 2ms

uv build --out-dir .task7-build
Successfully built .task7-build\suseoro_v2-0.1.0.tar.gz
Successfully built .task7-build\suseoro_v2-0.1.0-py3-none-any.whl

uv run python -m suseoro.api.export_openapi  # twice
first=14007715585CB5037D54226E50611CAFB649A336F64999BFDC4F734494BF31DE
second=14007715585CB5037D54226E50611CAFB649A336F64999BFDC4F734494BF31DE

git diff --check
exit 0
```

build 산출물은 검증 후 명시 경로에서 제거했다. OpenAPI 두 번 생성의 SHA256이 동일해
generator stability도 확인했다.

## 독립 검토 수정 라운드 3 (2026-08-29, 기준 `b49ad13`)

### focused RED/GREEN 증거

이번 라운드의 다섯 OPEN Important finding을 고정하는 회귀 테스트를 제품 코드보다 먼저
추가했다. 증분 두 파일의 unsafe parse, OpenAPI security/nested schema/runtime nullability,
restore의 post-swap crash, rejected upload tail aggregate, middleware DB failure를 각각 실제
boundary에서 검증했다.

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q --tb=short
10 failed, 54 passed in 30.36s
```

RED 실패는 다음 열 건이었다.

- `test_candidate_patch_returns_etag_and_structured_412_conflict`
- `test_multipart_upload_streams_each_file_and_keeps_partial_success`
- `test_delta_batch_records_partial_without_applying_or_advancing_watermark` 세 parameter case
  (`ROW_ERROR` document, empty parse, partial rows)
- `test_openapi_security_and_every_reachable_nested_json_schema_are_concrete`
- `test_upload_drains_rejected_file_tails_before_deciding_aggregate_result`
- `test_middleware_session_database_failures_use_the_request_id_json_envelope` 두 parameter case
- `test_restore_recovers_post_swap_crash_without_second_swap_or_prebackup`

TypeScript declaration 생성 검증 중 required header가 property는 required지만 nullable인 추가
contract 결함을 발견해 assertion을 먼저 추가했고, 다음 별도 RED를 확인한 뒤 schema를 고쳤다.

```text
uv run pytest tests/test_api_contract.py::test_openapi_security_and_every_reachable_nested_json_schema_are_concrete -q --tb=short
1 failed in 3.46s
(If-Match schema was string | null instead of non-nullable string)
```

partial batch의 source row accounting까지 적용 여부와 일치시키는 assertion을 추가했을 때도
네 parameter case가 모두 `APPLIED` row를 발견하는 별도 RED를 확인했다. 성공 parse row라도
두 파일 batch가 partial이면 `ROW_ERROR/BATCH_NOT_APPLIED`로 기록하도록 수정한 뒤 direct
service/API narrow 회귀는 `5 passed in 3.46s`였다.

```text
uv run pytest tests/test_api_contract.py::test_delta_batch_records_partial_without_applying_or_advancing_watermark -q --tb=short
4 failed in 3.58s
```

첫 backend 전체 실행은 기존 catalog unit test가 `ROW_ERROR` document를 성공 적용하는 이전
기대를 유지하고 있어 `1 failed, 369 passed in 68.13s`였다. 검토 ruling에 맞게 해당 direct
service contract를 `FAILED` input/`PARTIAL_FAILURE` accounting/no watermark advance로 갱신했다.
최종 fresh GREEN은 다음과 같다.

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q --tb=short
................................................................         [100%]
64 passed in 28.04s

uv run pytest -q --tb=short
........................................................................ [ 19%]
........................................................................ [ 38%]
........................................................................ [ 58%]
........................................................................ [ 77%]
........................................................................ [ 97%]
..........                                                               [100%]
370 passed in 63.58s (0:01:03)
```

### 수정 결정과 제품 동작

- delta source는 document `SUCCESS`, `activation_allowed`, non-empty row set, 모든 row
  `SUCCESS`를 동시에 만족해야만 `ParserStatus.SUCCESS`와 records를 제공한다. `ROW_ERROR`,
  empty, partial, activation-blocked 중 하나라도 있으면 두 파일 batch 전체가
  `PARTIAL_FAILURE`로 영속화되고 active holdings/version/watermark는 바뀌지 않는다. 동일
  idempotency key retry는 같은 partial 결과와 단일 batch를 돌려준다.
- OpenAPI에 `SessionCookie`, `CsrfCookie`, `CsrfHeader` security scheme을 추가했다. health/login은
  public exception이고, 보호된 read는 session cookie, mutation은 session + CSRF cookie + CSRF
  header를 AND security로 요구한다. candidate/quote/order/delivery request와 job/approval/quote/
  order/receiving/audit response의 arbitrary `Any` object를 concrete nested Pydantic model로
  교체했다. accepted/rejected upload item은 `source_id`와 `error`를 항상 명시하며 nullable
  경계가 runtime과 schema에서 같다. `ApiErrorField.message/value`는 실제 envelope처럼 optional이다.
  모든 required contract header는 non-nullable string으로 정규화했다.
- restore sidecar journal은 `PRAGMA synchronous=FULL`과 `RUNNING -> SWAP_READY -> SWAPPED ->
  SUCCEEDED` phase를 사용한다. prebackup manifest, restore manifest, upgraded target SHA-256를
  active DB swap 전에 commit한다. `os.replace` 직후 process crash가 나도 재시작은 active DB hash를
  committed target과 비교해 성공을 확정하며 두 번째 prebackup이나 swap을 실행하지 않는다.
- per-file limit로 거부된 upload도 남은 stream을 같은 aggregate counter로 끝까지 drain한다.
  따라서 100 KiB 두 파일/파일 제한 1 KiB/aggregate 150 KiB 조합은 207 partial이 아니라 경계를
  넘는 즉시 413이며 DB/idempotency/artifact orphan을 남기지 않는다.
- CSRF/session middleware 내부의 `sqlite3.OperationalError`와 `ProgrammingError`도 endpoint error
  handler와 같은 503 `DATABASE_UNAVAILABLE`, stable Korean message, request-ID JSON envelope로
  반환하며 SQLite 내부 메시지나 경로를 노출하지 않는다.

주요 변경 파일은 `api/{app,schemas}.py`,
`api/routes/{audit,candidates,deliveries,procurement,sources}.py`, `backup/service.py`,
`catalog/sync.py`, generated `backend/openapi.json`과 회귀 테스트 세 파일이다.

### migration/checksum 및 최종 gate 증거

이 라운드는 schema migration을 추가하지 않았다. 기준 `b49ad13` 대비 migrations diff가 없고
기존 migration 전부를 byte-for-byte 보존했다.

```text
git diff --exit-code b49ad13 -- backend/src/suseoro/db/migrations
exit 0

0006_api_operations.sql
CE06C0866F14A1E2932CC21CE18C80F239B97E927AC14FB6C9E32E7CB1797EBF

0006a_api_hardening.sql
4E18920AA31AA5129CC23E123681062CDDE79E9329EB7B90307C27C80A021F86

uv run --with ruff ruff format --check src tests
102 files already formatted

uv run --with ruff ruff check src tests
All checks passed!

uv run python -m compileall -q src tests
exit 0

uv lock --check
Resolved 36 packages in 2ms

uv build --out-dir .task7-round3-build
Successfully built .task7-round3-build\suseoro_v2-0.1.0.tar.gz
Successfully built .task7-round3-build\suseoro_v2-0.1.0-py3-none-any.whl

uv run python -m suseoro.api.export_openapi  # twice
first=0F800B973F1DEED03B938E12C13C37580C1BDDF06535F892FFB9D9F7F73E0186
second=0F800B973F1DEED03B938E12C13C37580C1BDDF06535F892FFB9D9F7F73E0186

npx --yes openapi-typescript openapi.json -o .task7-round3-ts/schema.d.ts
npx --yes --package typescript tsc --noEmit --skipLibCheck .task7-round3-ts/schema.d.ts
openapi-typescript 7.13.0; exit 0
required mutation header types are non-nullable

dangerous execution scan: no matches
embedded secret scan: no matches

git diff --check
exit 0
```

build와 TypeScript 임시 산출물은 검증 후 resolved backend 하위 명시 경로에서 제거했다.

## 독립 검토 수정 라운드 2 (2026-08-29, 기준 `2484a6f`)

### focused RED/GREEN 증거

아래 8개 검토 항목을 고정하는 회귀 테스트를 제품 코드보다 먼저 추가했다. 장서 full/delta
안전성, COMPARE/빈 ingestion terminal truth, 구체 OpenAPI schema, restore durable replay와 보안
선행조건, v1 tenant discovery/activation, 실제 parser 기반 DLS 건강성, rejected stream byte까지
포함한 aggregate cap, SQLite connection 오류 envelope를 각각 직접 검증한다.

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q
12 failed, 43 passed in 24.96s
```

RED 실패는 다음 12개 behavior probe였다.

- `test_catalog_full_staging_rejects_non_full_partial_and_activation_blocked_sources`
- `test_catalog_delta_api_applies_distinct_inclusive_registration_and_update_files`
- `test_partial_compare_is_retryable_and_does_not_advance_workspace`
- `test_empty_ingestion_is_failed_with_a_durable_zero_row_item`
- `test_openapi_has_typed_json_responses_errors_and_required_mutation_headers`
- `test_upload_aggregate_limit_counts_bytes_from_per_file_rejections`
- `test_expected_sqlite_failures_use_structured_request_id_envelope` 두 parameter case
- `test_v1_pending_candidates_and_legacy_history_are_tenant_scoped_and_paginated`
- `test_inspect_selects_latest_semantically_healthy_dls_candidate`
- `test_restore_replay_does_not_bypass_authentication_when_restored_actor_is_absent`
- `test_restore_replay_is_durable_fingerprint_bound_and_never_bypasses_security`

최초 구현 후 focused는 `6 failed, 49 passed`였다. 이 실행에서 OpenAPI missing operation
binding, v1 import ownership, restore exception import shadowing을 확인해 원인을 수정했다. 최종
focused GREEN은 다음과 같다.

```text
uv run pytest tests/test_api_contract.py tests/test_sse_jobs.py tests/test_v1_migration.py tests/test_backup_restore.py -q
.......................................................                  [100%]
55 passed in 22.14s
```

전체 suite 첫 실행은 custom COMPARE handler가 production per-file result protocol을 사용하지
않는 기존 계약 두 건을 찾아 `2 failed, 359 passed in 57.19s`였다. item result가 한 건이라도
발행된 production job에는 completeness를 강제하되, protocol을 사용하지 않는 custom runner에는
기존 semantics를 보존했다. 최종 전체 GREEN:

```text
uv run pytest -q
........................................................................ [ 19%]
........................................................................ [ 39%]
........................................................................ [ 59%]
........................................................................ [ 79%]
........................................................................ [ 99%]
.                                                                        [100%]
361 passed in 54.97s
```

### 수정 결정과 제품 동작

- full staging은 tenant의 `CATALOG_FULL` source만 허용하며 document `SUCCESS`, non-empty,
  모든 row `SUCCESS`, `activation_allowed`, parser/file-format binding을 모두 검사한다. 추천/증분,
  partial/empty/blocked source는 staging하지 않는다. registration/update delta는 서로 다른 source와
  정확한 inclusive 요청 구간을 검증해 `CatalogSyncService.apply_delta`로만 적용하고 full snapshot으로
  오인하지 않는다.
- `COMPARE`도 durable item result로 `PARTIAL`/`FAILED`/`SUCCEEDED`를 산출한다. partial은 retry할
  수 있고 workspace는 `ANALYZING`에 남으며, 성공한 complete compare만 기존 guard를 통해
  `CANDIDATE_REVIEW`로 간다. 빈 parse/ingestion은 `NO_LOGICAL_ROWS` item failure다.
- `api/schemas.py`에 workspace/source/job/catalog/candidate/approval/procurement/receiving/audit/v1/
  backup page와 mutation, nested error를 구체 Pydantic model로 정의했다. 모든 JSON operation ID는
  fail-closed registry에 binding되며 새 operation이 schema 없이 추가되면 OpenAPI 생성이 실패한다.
  `ApiErrorField`, job item 배열, required/type와 nullable 경계가 TypeScript client에서 보존된다.
- restore replay는 session/expiry/revocation, CSRF, request ID, local confirmation을 통과한 뒤에만
  진입한다. backup 폴더의 별도 SQLite operation journal이 operation key와 body fingerprint를
  영속화하고 process lock/`BEGIN IMMEDIATE`로 same-key concurrent 실행과 prebackup을 한 번으로
  제한한다. restart 뒤 replay도 manifest를 다시 검증해 결과를 복원하며 다른 body는 409다.
- v1 migration 실행 결과는 인증 tenant가 소유하고, pending catalog candidate와 read-only legacy
  history를 tenant-scoped stable cursor API로 조회한다. 후보 조회/활성화/update 모두 `school_id`를
  함께 조건으로 사용해 다른 학교 ID를 알아도 볼 수 없고 활성화할 수 없다.
- healthy DLS 선정은 readable row 수가 아니라 production `CATALOG_FULL` parser/normalization의
  모든 row 성공과 usable title을 요구한다. arbitrary/zero/unrelated workbook은 격리하고 자연수
  순서에서 최신 valid 파일을 고른다.
- upload stream wrapper가 accepted/rejected file에 관계없이 실제 읽은 모든 byte를 aggregate에
  더하고 한계를 넘는 첫 chunk에서 413으로 중단한다. 기존 per-file limit, orphan 정리, hash/
  idempotency semantics는 유지한다.
- 예상 가능한 `sqlite3.OperationalError`/`ProgrammingError`는 내부 오류문을 노출하지 않고 503
  `DATABASE_UNAVAILABLE` + 동일 request ID envelope로 변환한다. 기존 role 403 경계는 유지한다.

주요 변경 파일은 `api/{app,errors,schemas}.py`,
`api/routes/{audit,procurement,sources}.py`, `backup/service.py`, `catalog/sync.py`,
`jobs/{handlers,repository}.py`, `migration/v1.py`, generated `backend/openapi.json`과 focused
회귀 테스트 세 파일이다.

### migration/checksum 및 최종 gate 증거

이 라운드는 schema migration을 추가하지 않았다. 기준 `2484a6f` 대비 migrations diff가 없으며
기존 `0006`/`0006a`를 포함해 모두 byte-for-byte 보존했다.

```text
git diff --exit-code 2484a6f -- backend/src/suseoro/db/migrations
exit 0

0006_api_operations.sql
ce06c0866f14a1e2932cc21ce18c80f239b97e927ac14fb6c9e32e7cb1797ebf

0006a_api_hardening.sql
4e18920aa31aa5129cc23e123681062cdde79e9329eb7b90307c27c80a021f86

uv run --with ruff ruff format --check src tests
102 files already formatted

uv run --with ruff ruff check src tests
All checks passed!

uv run python -m compileall -q src tests
exit 0

uv lock --check
Resolved 36 packages in 1ms

uv build --out-dir .task7-round2-build
Successfully built .task7-round2-build\suseoro_v2-0.1.0.tar.gz
Successfully built .task7-round2-build\suseoro_v2-0.1.0-py3-none-any.whl

uv run python -m suseoro.api.export_openapi  # twice
first=C96090ECE9BC49BBDF34C7E155250D469D1D794494FF50C6A3A844F748AE4169
second=C96090ECE9BC49BBDF34C7E155250D469D1D794494FF50C6A3A844F748AE4169

rg dangerous execution / embedded secret patterns backend/src/suseoro
no matches

git diff --check
exit 0
```
