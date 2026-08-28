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
