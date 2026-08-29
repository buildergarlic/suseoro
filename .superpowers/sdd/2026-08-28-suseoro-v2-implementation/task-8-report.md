# Task 8 구현 보고서

## 결과

Task 7 종료 검토에서 지정된 upload breaker 세 건을 먼저 닫은 뒤, Vite + React +
TypeScript 기반의 `로그인`, `내 수서 업무`, `수서 작업실` 세 shell을 구현했다. 화면 전환용
`저장`/`다음` 버튼을 만들지 않았고, 담당자의 현재 상태에 맞는 주요 행동 하나만 우선한다.
별도 ASGI pre-parser upload cap은 지시대로 Task 10 범위에 남겼다.

## Task 7 breaker 선행 수정

기존 migration은 수정하지 않고 다음 forward migration과 전용 서비스만 추가했다.

- `backend/src/suseoro/db/migrations/0006b_upload_idempotency_fencing.sql`
- `backend/src/suseoro/services/upload_idempotency.py`

upload fingerprint는 이제 `workspace_id`, 역할, vendor/date 입력과 파일 순서별 filename,
content type, 전체 content SHA-256, byte size만 canonical JSON으로 만든다. 서버가 결정한 accept/error,
detected format, parser/policy 결과는 포함하지 않는다. 따라서 설정 변경이나 재시작 뒤에도 같은 client
input/content는 같은 fingerprint로 replay된다.

mutation transaction 밖에 unique scope의 `upload_idempotency_claims`를 먼저 commit한다. claim은
fingerprint를 영구 보존하고 generation, lease owner, lease expiry로 소유권을 fence한다. 실패/rollback
뒤 다른 body는 계속 409이며, 같은 body만 lease takeover로 generation을 올려 재시도할 수 있다.
mutation commit에는 terminal response와 기존 compatibility idempotency row를 함께 넣는다. 실패 cleanup도
generation/owner를 다시 확인한 뒤 unreferenced blob만 지우고 해당 generation만 release한다.

matching live claim과 unrelated SQLite writer를 구분해 짧은 busy timeout + bounded exponential backoff를
적용했다. deadline 뒤 전자는 `409 IDEMPOTENCY_REQUEST_IN_PROGRESS`, 후자는
`503 UPLOAD_RESERVATION_BUSY`와 `Retry-After: 1`을 돌려준다. 모든 staged file은 route `finally`에서
폐기한다. HTTP exception header가 구조화 오류 handler에서 보존되도록 오류 경계도 보완했다.

### Backend RED

production 변경 전에 다음 다섯 test를 추가하고 focused 실행했다.

```text
test_upload_fingerprint_replays_across_restart_when_server_policy_changes
test_matching_upload_claim_returns_bounded_retryable_in_progress_response
test_failed_upload_keeps_binding_and_allows_only_same_fingerprint_takeover
test_unrelated_sqlite_writer_returns_bounded_upload_busy_response
test_upload_claim_forward_migration_has_durable_generation_and_lease_fencing

5 failed in 6.96s
```

관찰된 실패는 순서대로 재시작 replay가 `409`였던 문제, matching waiter가 deadline을 넘긴 문제,
rollback 뒤 다른 body가 `202`로 허용된 문제, unrelated writer가 deadline을 넘긴 문제, claim
generation/lease column이 없던 문제였다.

### Backend GREEN과 보존 증거

```text
focused breaker tests                    5 passed in 4.75s
upload/foundation/concurrency adjacent  18 passed, 59 deselected
uv run pytest -q --tb=short            385 passed in 73.82s
uv run --with ruff ruff format --check  103 files already formatted
uv run --with ruff ruff check           All checks passed
uv run python -m compileall -q src tests  exit 0
uv build --out-dir .task8-build          sdist + wheel built
```

`git diff --exit-code HEAD -- backend/src/suseoro/db/migrations
':(exclude)backend/src/suseoro/db/migrations/0006b_upload_idempotency_fencing.sql'`로 기존 migration
byte가 그대로임을 확인했다. `python -m suseoro.api.export_openapi` 재생성 전후
`backend/openapi.json` SHA-256은 모두
`4DC3F6ADA23693DD3C9AB84E18364887105640E9027185F0DC6962E77F37BD3F`였다.

## Frontend TDD

component와 API client 전에 Testing Library test/fixture를 먼저 작성했다.

### Frontend RED

최초 전체 실행은 production module이 아직 없어 세 test file 모두 collection 단계에서 실패했다.

```text
npm test -- --run
Test Files  3 failed
Tests       no tests
원인        src/api/client, src/app/App resolve 실패
Duration    26.14s
```

App/login/work-list 최소 구현 뒤 workroom route가 아직 업무함 placeholder를 가리키는 상태에서도 focused
behavior RED를 별도로 확인했다.

```text
npm test -- --run tests/ingestion-candidates.test.tsx
Test Files  1 failed
Tests       6 failed
Duration    8.06s
```

이때 `수서 작업실`, drop/paste, mapping dialog, candidate tabs, autosave/conflict, 승인 요청 주요 copy가
각각 실제 DOM에서 없어서 실패했다. 구현 중 전체 병렬 실행에서 heading이 data보다 먼저 나타나던 업무함
race 한 건도 재현했고, loading 동안 완료 shell heading을 내보내지 않도록 async 경계를 수정했다.

최종 product/React 검토에서 `확인 필요`를 보기만 하고 판정을 끝낼 수 없으며 승인 예산을 숨겨진 값으로
가정하던 workflow gap을 발견했다. 먼저 `확인 필요 판정을 끝내고 보이는 승인 예산을 입력해야 승인
요청할 수 있다` test를 추가했고, approval button이 enabled여서 `1 failed, 6 skipped`가 되는 RED를
확인했다. 그 뒤 행별 포함/제외와 명시적 승인 예산을 구현했다.

### Frontend GREEN

```text
API client focused                         3 passed
login/work-list focused                    5 passed
ingestion/candidates focused               7 passed
npm test -- --run                          3 files, 15 passed
npm run typecheck                          exit 0
npm run lint -- --max-warnings 0           exit 0, warning 0
npm run build                              35 modules, exit 0
dist JS                                    264.93 kB / 83.94 kB gzip
npm ci                                     264 packages, 0 vulnerabilities
```

최종 test는 clean `npm ci` 뒤 다시 실행했으며 15/15가 통과했다.

## OpenAPI 생성 type과 API client

`npm run generate:types`는 checked-in `backend/openapi.json`에
`openapi-typescript 7.13.0`을 실행한다. 생성 전후 `frontend/src/api/types.ts` SHA-256은 모두
`7576FBF547AC3C9E8E697D96405F067A7448FCB2529ACD14437B24A9E9B02C66`이었다. UI와 fixture는
`components["schemas"]`의 `UserResponse`, `WorkspaceResponse`, `UploadResponse`, `JobResponse`,
`SourceMapping`, `CandidateResponse`, `CandidateUpdate`, `ApprovalRequest` 등을 직접 alias하며 서버 field를
별도로 발명하지 않는다.

API client는 모든 request에 cookie credential을 포함한다. mutation은 CSRF cookie/header,
`Idempotency-Key`, `X-Request-ID`를 넣고, row mutation은 quoted `If-Match`를 보내며 response `ETag`를
보존한다. 412의 구조화 detail은 typed error로 유지한다. `navigator.onLine === false`이면 network 전에
mutation을 막되 textarea/수량 state는 메모리에 그대로 둔다. 이 동작은 mock 호출 횟수가 아니라 실제
`Request` header/body 경계와 화면 입력값으로 검증했다.

## 세 shell과 UX 결정

- 로그인은 학교 코드/아이디/비밀번호와 자연어 오류만 보여주며 실패해도 입력을 보존한다.
- 업무함은 학교, 작업명, 상태, 다음 담당, 마지막 저장 다섯 column만 사용한다. 담당자는 이어갈 작업,
  검토자는 승인 대기 작업을 먼저 정렬하며 주요 link는 하나다.
- 작업실은 후보 만들기/승인·발주/납품 검수 세 region을 항상 같은 순서로 보여주되 현재 과정만 펼친다.
  완료 과정은 한 줄 요약, 미래 과정은 `aria-disabled`다.
- drop zone은 다중 파일과 글 붙여넣기를 함께 받고 지원 형식을 화면에 쓴다. 파일마다 역할, 단계,
  progress, 읽은 권수, 확인 필요 수, 실패 문구와 재시도/취소를 text로 표시한다.
- 낮은 신뢰도 자료는 최대 20 data row와 header를 semantic table로 미리 보고 제목/저자 열을 연결한다.
  `이 양식 기억`은 기본 선택이다.
- 후보는 `확인 필요`를 기본 tab으로 하고 세 outcome count와 제목/저자/ISBN filter를 제공한다.
  확인 필요 행은 수서 후보 포함/제외를 명시적으로 판정한다. exact ISBN 제외는 사람말 이유와
  되돌리기를 함께 표시한다. 확인 필요가 0권이고 화면에 보이는 승인 예산을 입력한 뒤에만 승인 요청을
  활성화한다.
- 수량은 focus 때 editing lease를 표시하고 blur/Enter 확정 때 자동 저장한다. 성공하면 `저장됨 HH:MM`,
  offline이면 입력 유지 안내를 보여준다. 412는 현재 저장 내용/내 변경을 나란히 보여주며 dialog를 닫은
  뒤 원래 수량 input으로 focus를 복원한다.
- 주요 copy는 정확히 `우리 도서관에 없는 책 찾기`, `후보 확정하고 승인 요청`이다.
- warm cream/green, 고대비 focus ring, 20rem 최소 폭, flexible grids와 horizontal table scroll을 사용했다.
  skip link, semantic headings/regions/tables/tabs, focus trap/Escape, aria-live를 포함해 keyboard, 200% zoom,
  mobile reflow를 지원한다.

## 산출물과 범위

Vite entry/router, package lock, generated API type, API client, 공통 shell/dialog, auth/workspace/ingestion/
candidate feature와 세 Testing Library suite를 `frontend/` 아래에 추가했다. 별도 admin dashboard나 기술
용어 중심 화면을 만들지 않았다. plan과 progress ledger는 수정하지 않았다. 최종 blocker는 없다.

## 검토 수정 라운드 1 (2026-08-29)

초기 Task 8 commit `c1bef5adca062858df60f6238297275777f87e94`에 대한 1 Critical + 10
Important 검토를 다시 TDD로 닫았다. 이 라운드에서는 기존 migration을 한 byte도 바꾸지 않았다. 특히
이미 배포 가능한 `0006b_upload_idempotency_fencing.sql`은 그대로 유지했다. 독립 최종 diff 검토에서
완료 key의 replay probe를 과거 request shape/size로 제한해야 한다는 경계를 발견해, 기존 claim을
재작성하지 않고 nullable request metadata만 더하는 forward-only
`0007_upload_replay_metadata.sql`을 추가했다.

### Backend 계약과 RED/GREEN

- upload 완료 claim은 저장된 filename 순서와 과거 file/aggregate byte bound 안에서 body를 drain/hash한
  뒤 exact replay한다. 저장 shape와 다른 file count에는 현재 정책을 즉시 적용하고, 같은 filename을
  이용한 임의 대용량 changed body도 과거 bound를 확장하지 않는다. 새 claim에는 현재 정책을 적용한다.
- 실제 0006a 구버전 모양인 accepted `{filename, sha256}` / rejected `{filename, error}` fingerprint를
  canonical claim으로 안전하게 bridge한다. accepted content digest나 accepted/rejected 분류가 달라지면
  영구 409여서 changed-body 충돌을 숨기지 않는다. legacy `FILE_TOO_LARGE`는 blanket replay allowance로
  성공 파일처럼 바꾸지 않고 현재 stream limit에서 같은 rejection 분류를 보존한다.
- cleanup은 원래 contention deadline 안에서 generation/owner를 다시 fence하고 각 DB/path 단계마다
  deadline을 확인하는 best effort다. cleanup 자체가 기본 SQLite timeout을 다시 소비해
  `UPLOAD_RESERVATION_BUSY`를 `DATABASE_UNAVAILABLE`로 바꾸지 않는다.
- mapping 필요 상태를 `JobFileResult.mapping_required`로 명시하고 server parser가 만든 header와 최대
  20 preview row를 durable result에 둔다. mapping 저장 뒤 cached parse를 새 PARSE job으로 재개한다.
- 후보 목록은 cursor, literal title/author/ISBN search, server filter와 authoritative 전체 summary/count를
  제공한다. `%`와 `_`도 SQL wildcard가 아닌 검색 문자로 취급한다. 단건 후보 GET은 full row와 ETag를
  제공한다.
- job/parser 내부 예외는 `JOB_FAILED`/`PARSER_FAILURE`의 고정된 사람말 envelope로 변환한다. private
  table 이름, schema, DB path를 durable job/source/API 응답에 저장하지 않는다.
- candidate mutation은 이제 같은 actor가 소유한 unexpired `edit_locks` lease를 mutation transaction 안에서
  원자적으로 확인한다. 만료 뒤 다른 operator가 인수하면 이전 actor의 autosave/bulk decision은 409다.
  이미 완료된 idempotent replay는 lock 재검사보다 먼저 반환된다.

검토 시작 시 작성한 backend behavior 묶음은 8개 실패였고, 이후 좁혀 낸 경계도 각각 실제 RED로
확인했다.

```text
초기 round-1 backend behavior 묶음                 8 failed
author/ISBN server search                           1 failed
정확한 0006a accepted/partial upgrade bridge         2 failed
job/API 내부 DB 오류 비공개                          2 failed
edit lease 없음 / 만료 뒤 takeover                   2 failed
literal LIKE wildcard                                1 failed
독립 최종 검토: legacy changed-first + replay bound    2 failed
최종 재검토: legacy FILE_TOO_LARGE 분류 보존             1 failed
forward 0007 integration latest assertions            3 failed, 395 passed
```

최종 결과는 다음과 같다.

```text
historical 0006a bridge focused                      2 passed
changed-first / bounded / FILE_TOO_LARGE focused      5 passed
populated 0006b -> 0007 migration focused             3 passed
DB failure sanitization focused                      3 passed
candidate workflow + lease                          13 passed
candidate/mapping/replay/cleanup focused             10 passed
job recovery                                         13 passed
uv run pytest -q                                    400 passed in 77.35s
uvx ruff format --check src tests                   104 files already formatted
uvx ruff check src tests                            All checks passed
uv run python -m compileall -q src tests            exit 0
uv lock --check                                     exit 0
uv build --out-dir .task8-round1-build              sdist + wheel built
```

build 산출물은 확인 뒤 검증된 전용 임시 directory만 삭제했다. diff 대상의 dangerous execution과 private
key/token signature scan, `git diff --check`도 모두 0건/exit 0이었다.

### Frontend 흐름, concurrency와 RED/GREEN

CTA `우리 도서관에 없는 책 찾기`는 이제 upload만 기다리지 않는다. accepted source별 durable 결과를
합쳐 ingest/parse를 완료하고, mapping이 필요하면 server preview dialog queue를 모두 처리한 뒤 명시적으로
comparison command를 만든다. comparison job 성공과 workspace 재조회가 끝나야 `CANDIDATE_REVIEW`를
연다. comparison 성공 뒤 workspace 조회만 실패한 경우 완료 job을 retry하지 않고 `후보 화면 다시
불러오기`만 제공한다. StrictMode setup-cleanup-setup에서도 mounted fence를 다시 세워 실제 dev build가
중간에 멈추지 않는다.

파일 status는 source identity의 최신 parser 결과를 합치며 batch progress/action과 file retry를 구분한다.
usable `PARTIAL` 결과도 비교에 포함하고, 실패/누락 source를 완료라고 말하지 않는다. 서로 다른 여러
mapping dialog는 source id key로 local select state를 초기화한다. mapping 저장/재처리 실패는 aria-live의
자연어로 남고 dialog를 닫거나 parse/poll이 실패해도 같은 파일에서 다시 열 수 있다. multi-file 207은
모든 rejected local file이 재접수되고 accepted source가 준비될 때까지 성공 subset만으로 비교하지 않는다.
rejected file의 `다시 올리기`는 같은 immutable bytes를 되풀이하지 않고 수정한 replacement file을 다시
선택하게 하며 filename이 달라도 이전 실패 item을 교체한다. mapping source 상세 조회가 일시 실패해도
row의 `열 연결 다시 확인`이 source/version을 다시 조회해 dialog를 복구한다.
workspace id가 바뀌면 선택 파일, upload item, source/result ref와 진행 상태를 모두 비워 이전 작업 자료가
새 작업에 섞이지 않는다. polling은 bounded exponential retry 뒤 멈춘 사실과 수동 재확인 행동을 보여준다.

후보 화면은 100개에서 자르지 않고 cursor page를 이어 붙인다. tab 수, 미해결 gate, 예상 금액은 loaded
row가 아니라 server summary를 쓴다. autosave는 candidate별 generation/queue로 직렬화하고, blur 직후 판정은
quantity 저장과 새 row version을 기다린다. input/판정/제외 되돌리기는 lease를 먼저 얻고 lease expiry 때
input을 다시 잠근다. 412는 단건 authoritative GET으로 server/local 값을 비교한다. mutation response의
row/version/quantity와 outcome 이동을 panel summary에 반영한다.

API client는 login/upload/autosave/approve/retry/cancel 등의 logical action + payload fingerprint별 key를
유지한다. network/retryable 응답뿐 아니라 malformed/HTML/truncated 2xx도 ambiguous로 보고 성공 확정 전에
같은 key를 보존한다. 동시에 같은 payload가 나가더라도 한 response가 다른 in-flight action의 key를 먼저
지우지 않는다. payload가 달라질 때만 새 key다. approval transition response의 state/row version은 즉시
적용하고, 후속 workspace refresh 실패를 이미 commit된 mutation 실패로 말하지 않는다.

초기 round-1 Testing Library 추가분은 production 변경 전에 다음 RED였다.

```text
npm test -- --run (초기 round-1)                    19 failed, 12 passed
StrictMode + 완료 후 refresh + malformed 2xx          3 failed, 27 passed
blur/decision + lease expiry + restore lock            3 failed
ANALYZING poll exhaustion/retry                         1 failed
최종 검토 mapping recovery / partial gate / workspace   3 failed
최종 재검토 real replacement / mapping detail recovery   2 failed
```

최종 clean install 결과는 다음과 같다.

```text
npm ci                                                audited 265, 0 vulnerabilities
npm test -- --run                                     3 files, 44 passed
npm run typecheck                                     exit 0
npm run lint                                          exit 0, warning 0
npm run build                                         36 modules, exit 0
dist JS                                               282.71 kB / 88.35 kB gzip
```

### 생성 계약과 보존 증거

OpenAPI와 TypeScript client type을 각각 연속 두 번 생성해 deterministic hash를 확인했다.

```text
backend/openapi.json SHA-256
A9D008B0815A051AC0C2EBFD56CAFCF16C788DF85B474DDFE9ECA26FE2893ECF

frontend/src/api/types.ts SHA-256
6EBC009BF992098ADECC007178830145EA842A832A7F6CACE0CB12309DB4D38E
```

`0007_upload_replay_metadata.sql`만 새 forward migration이며 SHA-256은
`E882B0E68B302829CE4BAD50955CB93404A50163E434491182D46C55CF35B28B`이다. 이를 제외한
`git diff --exit-code c1bef5ad... -- backend/src/suseoro/db/migrations
':(exclude)backend/src/suseoro/db/migrations/0007_upload_replay_metadata.sql'`는 exit 0이었다.
`0006b_upload_idempotency_fencing.sql` SHA-256은
`5DF3A11F75D27E4BE6E490FBD93F574756D456D4AA225F743314FD285D492710`으로 baseline과 같다.
populated 0006b claim의 fingerprint/generation/terminal response를 그대로 보존하고 새 metadata를 NULL로
올리는 forward upgrade test도 통과했다.
plan/progress ledger는 수정하지 않았고, Task 10으로 미룬 ASGI pre-parser cap도 건드리지 않았다.

## 검토 수정 라운드 2 (2026-08-29)

검토 commit `4ccff0b`의 12개 Important 지적을 behavior-first RED로 재현한 뒤 닫았다. 기존
`0001`~`0007` migration은 수정하지 않고, 거절 파일 복구 의무와 재시작 발견 계약만 추가하는
forward-only `0008_upload_repair_and_job_discovery.sql`, terminal 분석 복구 전이를 추가하는
`0009_repair_recovery_transition.sql`, source 교정 전이를 더 좁게 fence하는
`0010_source_correction_recovery.sql`을 만들었다. 기존의 세 화면 구조와 한국어 주요 copy는 유지했으며
새 화면이나 관리자 dashboard는 추가하지 않았다.

### Backend: 복구 의무, 발견 계약, 실행 fence

- 부분 업로드의 거절 항목은 workspace/logical upload claim/file ordinal 단위 복구 의무로 영속화한다.
  요청자가 성공 source id만 골라도 미해결 의무가 있으면 비교를 시작하지 못한다. 교체 파일은 원래
  role/config를 지키며 generation과 active claim으로 fence된다. 더 늦게 선택한 교체가 우선하고, 실패한
  새 교체는 이전에 성공한 source를 지우지 않는다. 성공 시에만 source link와 의무를 원자적으로 바꾼다.
- terminal FAILED/PARTIAL COMPARE에서 잘못 읽은 source를 고치면, 하나의 multipart command가 terminal
  분석을 DRAFT로 되돌리고 새 source/INGEST job을 만들며 기존 source 역할을 `UNKNOWN`으로 내리는 과정을
  같은 transaction에서 처리한다. 같은 key replay는 demotion 뒤에도 정확한 202를 재현하고, 다른 key의
  stale completion은 409다. 여러 workspace가 공유하는 source는 전역 role을 바꾸지 못하도록 거절하며
  다른 workspace의 `CANDIDATE_REVIEW` 상태와 결과를 그대로 보존한다.
- migration에서 역할을 알 수 없었던 복구 의무는 첫 유효 generation에서 role/vendor/date 계약을 한 번만
  bind한다. 그 전에 실패한 UNKNOWN generation이 있어도 다음 generation에서 bind할 수 있지만, 이후
  scope 변경은 영구 거절한다.
- source 목록은 최신 INGEST/PARSE job id와 typed `mapping_required`, server header, 최대 20 preview row를
  함께 노출한다. 결과 row가 아직 없는 QUEUED/RUNNING job도 발견된다. workspace job 목록은 tenant scope,
  cursor, job type/status filter를 제공해 reload 뒤 mapping과 COMPARE를 다시 찾는다.
- FAILED/PARTIAL parser 결과는 성공 권수로 환산하지 않는다. 실패 row의 processed count는 0이고, public
  source/job 응답에는 stable code와 자연어 문구만 남는다.
- 비교 시작 전 선택 source에 active INGEST/PARSE가 있거나 미해결 복구 의무가 있으면 거절한다. COMPARE
  payload에는 정확한 source id/role, file SHA-256, mapping/config row version, parser version/completed marker,
  source-row count/digest와 catalog version을 snapshot한다. ANALYZING 동안 upload/repair/parse/mapping 변경을
  막고 handler가 실행 직전과 각 file/batch 경계에서 snapshot을 재검증한다.
- upload/mapping/parse/retry는 idempotency reservation 뒤 writer transaction 안에서 workspace/source 상태를
  다시 읽는다. 따라서 reservation 직후 다른 actor가 비교를 시작하거나 job 상태가 RUNNING으로 바뀌면
  stale request가 source/config/job을 바꾸지 못한다. PURCHASE_REQUEST 변경은 DRAFT에만 허용한다.
- 비교 command가 보낸 source 일부만 신뢰하지 않는다. workspace에 현재 연결된 모든 `PURCHASE_REQUEST`
  source 집합과 정확히 같아야 하며, concurrent 교체/업로드로 집합이 달라졌으면
  `COMPARISON_SOURCE_SET_CHANGED`로 거절한다. client는 이 응답에서 source와 최신 parse job을 다시 찾아
  모두 준비된 authoritative 집합으로 비교를 한 번만 다시 시작한다.
- snapshot 계약 도입 전에 만들어진 COMPARE job은 실행 때 안전하게 실패한다. 사용자가 같은 job을 명시적으로
  재시도하면 ANALYZING 및 tenant/source/role/parse/repair/catalog 상태를 다시 확인하고 versioned snapshot을
  한 번 만든 뒤 같은 durable job을 재개한다. 따라서 안전한 fail-closed가 복구 불가능한 ANALYZING 상태를
  만들지 않는다.
- bulk decision의 lease/takeover 오류는 item별 savepoint 안에서 격리된다. 잠긴 한 항목 때문에 앞뒤의
  유효한 항목이 rollback되지 않으며 partial 결과와 audit가 idempotent replay된다.
- pre-0006b 거절 row에 digest/size가 없어 실제 body를 확인할 수 없으면 filename/error equivalence로
  canonical claim을 만들지 않고 `LEGACY_UPLOAD_IDENTITY_UNVERIFIABLE` 경계를 반환한다. 복구 가능한 accepted
  legacy digest의 exact replay는 유지한다.
- comparison producer와 serializer 양쪽에서 예외 class, SQL/table/schema, DB/filesystem path를 제거한다.
  과거에 저장된 raw 비교 오류도 API 경계에서는 `COMPARISON_FILE_FAILED`의 고정된 한국어 envelope로 보인다.

### Frontend: reload, truthful recovery, race fence

- 새로 연 작업실은 server source/job discovery로 mapping dialog와 진행 중 parser job을 복원한다. browser
  storage나 client-side delimiter/file parsing을 사용하지 않는다. mapping 저장에는 발견된 source의 실제
  role/version을 사용한다.
- 파일별 표시를 durable source identity의 최신 terminal 결과로 합친다. FAILED는 `읽기 실패`와 0권,
  PARTIAL은 읽은 권수와 확인 필요 권수를 분리해 표시하며 batch 취소와 file 교체 행동을 구분한다.
- bounded poll이 끊기면 `상태 다시 확인`이 같은 job을 먼저 조회한다. RUNNING/SUCCEEDED를 retry하지 않고
  terminal retryable 상태에만 같은 job retry를 허용한다. ANALYZING reload도 filtered COMPARE discovery로
  실패 원인과 안전한 재확인/재시도를 복원한다.
- reload 때 source별 최신 job id로 현재 INGEST/PARSE를 모두 모아 병렬로 확인하고, 모든 현재 작업이 끝난
  뒤에만 한 번 비교한다. 이미 SUCCEEDED인 COMPARE를 발견하면 stale ANALYZING 화면에 머물지 않도록
  workspace를 즉시 다시 읽는다. 실패 화면의 재시도도 click 시점의 job을 먼저 조회해 다른 client가 이미
  재개하거나 완료한 작업을 다시 retry하지 않는다.
- 여러 복원 parse poll이 각각 bounded retry를 소진하면 job id별 재확인 queue를 보존한다. 한 번의
  `자료 읽기 상태 다시 확인`으로 빠진 job을 모두 먼저 조회하고, 모든 terminal 결과를 합친 뒤에만 한 번
  비교한다. 견적서/보유장서 교체는 원래 role로 다시 올리고 화면 role도 갱신하되 비교 source에는 넣지 않는다.
- 한 복구 의무에 대한 두 교체 선택이 겹치면 generation fence로 최신 선택만 화면/accepted source set에
  남긴다. stale completion은 announcement와 source를 되돌리지 않고 comparison command는 한 번만 만든다.
- terminal COMPARE의 PARTIAL source 행은 `수정한 파일로 교체`로 복구한다. 교체 input은 filename이 아니라
  source/obligation identity로 key를 만들고 source별 single-flight를 적용한다. 사용자가 화면의 전체 역할
  selector를 바꿔도 교체는 서버에서 발견한 원래 역할을 사용하며, 성공한 원자 교체 뒤 최신 workspace와
  source 집합을 다시 읽고 비교를 정확히 한 번 실행한다.
- candidate 검색/더 보기 요청은 query generation과 mutation epoch로 fence한다. 오래된 page/search 응답이
  새 검색 결과나 autosave가 올린 authoritative row/version/summary/예상 금액을 되돌리지 못한다.
- API client는 `(logical action, payload fingerprint)`별 key를 유지하고 active caller와 ambiguous caller를
  따로 센다. 동시 요청에서 malformed 2xx가 먼저, success가 나중에 와도 ambiguous retry는 같은 key를
  쓰고 payload가 바뀔 때만 새 key를 발급한다.

### RED / GREEN 증거

초기 backend reviewer probe는 6개 모두 실패했다. 확장 round-2 묶음은 처음 `5 failed, 2 passed`였고,
이 중 한 건은 terminal row를 금지된 방식으로 직접 바꾸던 fixture여서 실제 historical row를 안전하게
seed하도록 바로잡았다. legacy rejected identity 묶음은 `2 failed, 1 passed`였다. Frontend는 logical-action
concurrency, fresh mapping reload, poll recheck, failed compare reload, terminal file truth, replacement generation
fencing의 6개 behavior가 production 수정 전에 각각 RED였다. load-more/search 및 mutation race는 실제 deferred
response 순서를 쓰는 regression test로 고정했다. 최종 read-only 재검토에서 legacy snapshot-less COMPARE의
복구 probe는 `1 failed`, terminal comparison 발견/복수 parser job/재시도 preflight UI 묶음은 `3 failed`로
추가 RED를 확인한 뒤 각각 `1 passed`, `3 passed`로 닫았다. 이어 과거 worker가 남긴 unfenced comparison
row를 새 snapshot이 재사용하는 probe도 `1 failed`를 확인했고, 선택 source의 stale 결과를 원자적으로
지운 뒤 새 catalog snapshot 아래 다시 만드는 동작으로 `1 passed`가 됐다. 실행 중 catalog 교체와 versioned
COMPARE retry의 stale catalog probe는 `2 failed`에서 inner claim-batch fence와 retry snapshot refresh 후
`2 passed`가 됐고, migrated `UNKNOWN` repair role probe도 `1 failed`에서 `1 passed`가 됐다. 마지막으로
authoritative source-set, source-set client refresh, non-purchase repair role/filter, 복수 poll recovery 네 behavior는
production 수정 전에 `4 failed`였고 수정 뒤 `4 passed`였다.

마지막 state/race audit에서도 production 변경 전에 각각 실제 RED를 확인했다.

```text
reservation 뒤 retry/state 재검사                         2 failed -> 2 passed
실패 UNKNOWN generation 뒤 최초 scope bind                1 failed -> 1 passed
terminal PARTIAL source 원자 교체                         1 failed -> 1 passed
rendered 교체의 원래 role + source별 single-flight          1 failed -> 1 passed
공유 source의 다른 CANDIDATE_REVIEW workspace 보존         1 failed -> 1 passed
```

전체 backend 첫 실행에서 기존 API fixture가 빈 workspace를 `CANDIDATE_REVIEW`로 직접 만들고 upload하던
모순이 드러나 `26 failed, 403 passed`였다. production fence를 약화하지 않고 빈 fixture는 DRAFT, 실제
candidate fixture 다섯 개만 CANDIDATE_REVIEW로 seed하도록 고쳤고 API 계약 묶음이 `62 passed`, 전체가
`429 passed`가 됐다. 독립 최종 diff 검토도 P1/P2 behavior, migration, tenant/state, idempotency, security,
race blocker가 없다는 CLEAN 판정을 냈다.

최종 결과는 다음과 같다.

```text
tests/test_task8_round2.py                           27 passed in 18.51s
task8 round2 + API contract focused                  89 passed in 53.55s
uv run pytest -q                                    429 passed in 97.69s
npm ci                                               audited 265, 0 vulnerabilities
npm test -- --run                                    3 files, 75 passed in 75.28s
npm run typecheck                                    exit 0
npm run lint                                         exit 0, warning 0
npm run build                                        36 modules, exit 0
dist JS                                              299.19 kB / 92.25 kB gzip
uvx ruff@0.16.5 format --check src tests             106 files already formatted
uvx ruff@0.16.5 check src tests                     All checks passed
python -m compileall -q src tests                    exit 0
uv lock --check                                      resolved 36 packages
uv build --out-dir .build-check-round2-final         sdist + wheel built
npm ls --all                                         exit 0
dangerous-execution diff scan                        0 matches
private-key/token signature diff scan                0 matches
git diff --check                                     exit 0
```

OpenAPI와 생성 TypeScript type은 각각 연속 두 번 생성해 같은 SHA-256을 확인했다.

```text
backend/openapi.json
5385C472B1B9278E57D2C8292B5A4E7BE1E203C81329EAB9CAD66FB860C0F78D

frontend/src/api/types.ts
D296F53AAB8FD5208F90F00A0011FE9E4A6123CDD29898BA0A399A094509A203
```

새 migration `0008_upload_repair_and_job_discovery.sql`의 SHA-256은
`8CA289F58AE22F4B6AE325DA1A3CC971ECE9E08C8A5142CBC5C1A48D7A05849D`,
`0009_repair_recovery_transition.sql`은
`7D020CD41D6C7A3AB9FAC0F43DAA873336ACACAC889FD5FA171E19A4459D6383`,
`0010_source_correction_recovery.sql`은
`53935B8765516454A85FF1E13253180139A4E36978F575D3E562EFD05FAF9C79`이다. `0001`~`0007`의 hash는
기존 기준과 같고, 특히 `0006b_upload_idempotency_fencing.sql`은
`5DF3A11F75D27E4BE6E490FBD93F574756D456D4AA225F743314FD285D492710`,
`0007_upload_replay_metadata.sql`은
`E882B0E68B302829CE4BAD50955CB93404A50163E434491182D46C55CF35B28B`로 보존됐다. populated 0007에서
0008→0009→0010으로 올린 뒤 foreign-key/integrity, 복구 의무 backfill, transition trigger를 검증했다.
세 새 migration만 제외한 `git diff --exit-code 4ccff0b -- backend/src/suseoro/db/migrations`는 exit 0이었다.
plan/progress ledger와 Task 10의 ASGI pre-parser cap은 수정하지 않았다. 최종 blocker는 없다.

## 검토 수정 라운드 3 (2026-08-29)

검토 commit `84b00db`의 11개 Important 지적을 behavior-specific regression으로 재현하고 닫았다.
배포된 `0008`~`0010`은 byte-for-byte 보존하고, parse/config generation과 복구 pending source/count
무결성만 더하는 forward-only `0011_task8_round3_integrity.sql`을 추가했다. 기존 세 화면과 주요 한국어
copy는 바꾸지 않았고 새 화면도 만들지 않았다.

### Backend: server 권위 source 집합과 원자적 실행 fence

- 비교 command의 source 목록은 browser가 전송한 최대 100개 배열에 의존하지 않는다. 서버가 workspace의
  모든 `PURCHASE_REQUEST` source를 결정론적으로 도출하며 101개 regression으로 고정했다. legacy 배열이
  오면 현재 권위 집합과 정확히 같을 때만 허용한다. 대규모 id 집합은 SQLite bind placeholder가 아니라
  `json_each`로 조회한다.
- migrated `UNKNOWN` 복구 의무는 사용자가 role을 명시적으로 확인해야 한다. vendor scope와 inclusive delta
  날짜까지 같은 writer fence에서 한 번 bind하고, replacement upload 성공만으로는 의무를 닫지 않는다.
  replacement는 `pending_source_document_id`로 남고 현재 config generation 아래 parse가 끝나며 성공 logical
  row가 있을 때만 `RESOLVED`가 된다. mapping-required, parser 실패, 전 행 오류는 계속 비교를 막는다.
- mapping PATCH는 `source_rows`와 parse readiness를 원자적으로 무효화하고 config `row_version`을 올린다.
  `source_documents.parsed_config_version`이 현재 config version과 일치하는 성공 reparse만 비교 source가 될 수
  있다. parser는 읽기 시작과 저장 직전에 generation을 다시 확인한다.
- COMPARE 각 claim batch의 `BEGIN IMMEDIATE` 안에서 source membership/role/config generation/parsed generation,
  file SHA-256, parser/completion marker, logical-row count/digest, unresolved repair, 전역 parser job, active catalog를
  다시 확인한 뒤에만 comparison/candidate row를 쓴다. drift는 output row 0건으로 원자 실패한다.
- 한 source가 둘 이상의 workspace에 연결됐으면 source-global parse/mapping을 시작하지 않는다. 단일 owner로
  queue된 뒤 공유된 경우 worker가 다시 차단하고, 어느 workspace에서 시작했든 같은 school의 active
  INGEST/PARSE가 모든 linked workspace의 비교를 막는다.
- 파일 결과의 `processed_rows`는 성공 logical book row만 세고 `row_error_count`를 별도로 영속/노출한다.
  성공 1행 + 오류 1행은 정확히 `1권 읽음 · 확인 필요 1`로 표시된다.
- parsed source 교체는 client가 추측한 role/config를 받지 않고 서버의 role, vendor scope, inclusive delta 날짜,
  mapping과 remembered template를 모두 이어받는다. VENDOR_QUOTE와 delta replacement regression을 추가했다.

### Frontend: 전체 reload, mutation validator와 후보 reconciliation

- source/repair/job discovery는 cursor를 끝까지 순회하고 id를 deduplicate하며 cursor cycle은 안전하게 중단한다.
  mapping이 여러 개 있어도 다른 active parse job을 병렬로 bounded polling한다. failed COMPARE retry는 모든
  repair page를 읽기 전에는 fail-closed이며, server-derived comparison command body는 `{}`다.
- migrated UNKNOWN 복구 행은 자료 종류 selector를 먼저 보여주고 vendor/delta이면 서버가 보존한 scope/date를
  그대로 확인한다. 명시 확인 없이는 파일 picker가 upload를 시작하지 않는다.
- 모든 mutation success는 generated type만 믿고 key를 지우지 않는다. login/upload/retry/cancel/parse/mapping/
  comparison/lock/autosave/approval response의 runtime shape를 확인하며 `{}`나 잘린 2xx는 ambiguous로 취급해
  동일 logical-action key를 유지한다. 동시 malformed-first/success-last 순서에서도 retry는 같은 key다.
- 후보 initial page와 load-more는 id/row version으로 canonicalize한다. mutation이 확정한 outcome은 이전 page가
  다시 넣지 못하고, server response outcome/quantity/version이 panel row와 authoritative summary를 갱신한다.
  검색/page generation 및 mutation epoch fence로 오래된 응답이 count와 예상 금액을 되돌리지 못한다.

### RED / GREEN과 전체 gate

production 변경 전에 새 backend round-3 묶음은 `8 failed`였고, 101 source, UNKNOWN confirmation,
mapping generation, locked compare drift, shared source, mixed counts, vendor/delta replacement를 각각 RED로
확인했다. 전역 shared-job과 mapping-required repair probe를 추가해 최종 묶음은 `10 passed`다. Frontend
round-3 behavior는 처음 `6 failed`였고, 기존 unresolved-repair retry regression도 함께 실패했다. 별도
UNKNOWN confirmation UI probe도 `1 failed -> 1 passed`였으며 최종 focused behavior 8개가 모두 통과했다.

첫 full compatibility 실행은 새 의도와 맞지 않는 과거 exact fixture/assertion 때문에
`17 failed, 420 passed`였다. active-processing/readiness precedence 두 곳은 production에서 바로잡고, 나머지는
초기 INGEST를 끝내지 않은 mapping fixture, 0011을 prerequisite 없이 복사한 reduced migration fixture,
parsed generation/catalog이 없는 recovery snapshot, 즉시 RESOLVED를 기대한 구계약 assertion만 수정했다.
17개 focused rerun은 모두 통과했고 최종 결과는 다음과 같다.

```text
tests/test_task8_round3.py                          10 passed in 6.32s
backend full pytest                                439 passed in 113.68s
npm ci                                               audited 265, 0 vulnerabilities
npm test -- --run                                    3 files, 81 passed in 53.40s
npm run typecheck                                    exit 0
npm run lint                                         exit 0, warning 0
npm run build                                        36 modules, exit 0
dist JS                                              304.88 kB / 93.59 kB gzip
uvx ruff@0.16.5 format --check src tests             107 files already formatted
uvx ruff@0.16.5 check src tests                      All checks passed
python -m compileall -q src tests                    exit 0
uv lock --check                                      resolved 36 packages
uv build                                             sdist + wheel built
npm ls --all                                         exit 0
dangerous-execution diff scan                        0 matches
private-key/token signature diff scan                0 matches
git diff --check                                     exit 0
```

OpenAPI와 generated TypeScript type은 각각 연속 두 번 생성해 deterministic hash를 확인했다.

```text
backend/openapi.json
03C47B144A4FA6A7CE5B7AD1505E48488DD16A86F9C8267164A9AB2FAA4B25AB

frontend/src/api/types.ts
CF0AB91AA9B19D2233516856D09D67EDF2C70314049E882072052D0A0BE2776B
```

새 `0011_task8_round3_integrity.sql`의 SHA-256은
`EB22DB853D9488C3FDC0A95AD0EAFD2E4C79884D41D6D313B272CB49822242B0`이다. 기존 migration은
`git diff --exit-code 84b00db -- backend/src/suseoro/db/migrations
':(exclude)backend/src/suseoro/db/migrations/0011_task8_round3_integrity.sql'` exit 0으로 보존을 확인했다.
특히 `0008`은 `8CA289F58AE22F4B6AE325DA1A3CC971ECE9E08C8A5142CBC5C1A48D7A05849D`,
`0009`는 `7D020CD41D6C7A3AB9FAC0F43DAA873336ACACAC889FD5FA171E19A4459D6383`, `0010`은
`53935B8765516454A85FF1E13253180139A4E36978F575D3E562EFD05FAF9C79`로 round-2 기준과 같다.
plan/progress ledger와 Task 10 ASGI pre-parser cap은 수정하지 않았고 blocker는 없다.

## 검토 수정 라운드 4 (2026-08-29)

검토 commit `30da997`의 Critical 1개와 Important 4개를 전용 RED regression으로 재현하고 닫았다.
이미 배포된 `0011_task8_round3_integrity.sql`은 byte-for-byte 보존했다. `0011`보다 먼저 정렬되는
checksum-safe prelude와 그 뒤의 correction/restoration만 새 forward migration으로 추가했으며,
plan/progress ledger는 수정하지 않았다.

### Critical: populated 0010의 원자적 0011 upgrade

- `0010a_task8_round4_upgrade_prelude.sql`은 충돌하는 legacy
  `job_file_results_scope_update` trigger 하나만 `DROP TRIGGER IF EXISTS`로 중립화한다.
- migration runner는 prelude, committed `0011`, `0012`를 명시적 atomic bundle로 취급한다. 실행 전에
  directory에 있는 모든 applied migration checksum을 먼저 검증한다. 정상 history에서는 pending member를
  고정 순서로 실행하고, lower-sorted member가 postlude 뒤에 새로 발견된 비정상 순서에서는 idempotent
  prelude/postlude를 필요한 만큼 같은 transaction 안에서 다시 실행해 최종 guard를 복원한다. postlude 실패 시
  SQL, ledger row, trigger 변경이 함께 rollback된다.
- 이미 `0011`이 기록된 DB에서도 새로 발견한 lower-sorted prelude를 건너뛰지 않는다. prelude와 postlude를
  같은 bundle로 적용하고 기존 `0011` rowid/checksum은 그대로 둔다. 반대로 새 guard migration 중 하나만
  배포된 불완전 directory는 present applied member checksum은 검증하되 bundle 실행은 보류하여 trigger가
  제거된 채 stranded되지 않는다. 과거 release의 `0011` 단독 directory는 기존 동작을 유지한다.
- `0012_task8_round4_integrity.sql`은 count를 교정한 뒤 `0006a`와 같은 scope/claim trigger를 정확히 복원한다.
  fresh DB, populated 0010, already-applied 0011, incomplete bundle checksum drift, 0010 failure rollback,
  already-0011 failure rollback, lower-sorted prelude 재발견과 8개 ledger subset, final trigger/foreign-key/ledger
  checksum을 각각 검증했다. 지원할 수 없는 `0011` ledger 유실 상태는 guard를 훼손하지 않고 fail closed한다.
- round-4 이전의 checksum-valid `0011` backup manifest는 새 lower-sorted migration이 없다는 이유만으로
  거절하지 않는다. restore history validator는 정확히 그 historical prefix만 별도 허용하고 altered/future/
  임의 incomplete history는 계속 거절한다.

### Count truth와 response contract

- `SUCCESS` file result는 `total_rows/0`, `FAILED`는 `0/total_rows`를 durable status로 복원한다.
  `PARTIAL + MAPPING_REQUIRED`는 이후 같은 source가 성공 parse됐더라도 `0/0`을 유지한다. 다른 `PARTIAL`은
  현재 durable SUCCESS row 증거를 사용하되 최소 한 행은 확인 필요로 남기고, 증거 없는 나머지도 read로
  추측하지 않는다. 같은 source의 과거 SUCCESS와 PARTIAL을 최신 source row로 덮어쓰지 않는 regression을
  추가했다.
- mutation response guard는 generated TypeScript schema의 모든 key를 mapped type으로 열거한다. exact object,
  required-nullable, string array/record, nested `UploadItemError`, `JobError`, `JobFileResult`, `MappingRequired`,
  JSON scalar preview, OpenAPI integer를 재귀 검증한다. 추가 key, 잘못된 array 원소, fractional integer,
  near-valid truncation도 성공으로 확정하지 않는다.
- login/upload/retry/cancel/parse/mapping/comparison/lock/autosave/approval을 table-driven public API로 검증했다.
  malformed 2xx 뒤 같은 논리 행동은 같은 `Idempotency-Key`를 쓰고, 완전한 응답으로 ambiguity를 해소한 뒤의
  다음 행동만 새 key를 쓴다. 특히 upload item의 `error`, `repair_obligation_id`, `repair_generation` 누락과
  source의 nested latest result 누락을 고정했다.
- OpenAPI response model의 nullable default를 제거해 `UploadItem`, `SourceResponse`, `JobError`의 모든 property가
  명시적 required-nullable이 되게 했다. job endpoint뿐 아니라 source discovery의 `latest_result.error`도
  `{type, code, message}`를 항상 내보내 runtime과 generated contract를 맞췄다.

### Vendor mapping과 candidate row-version reconciliation

- `MappingState`는 server source detail의 role과 `vendor_scope`를 함께 보존한다. reload hydration, ingest 완료,
  수동 reopen 세 경로가 같은 constructor를 쓰며 mapping PATCH는 더 이상 `vendor_scope: "*"`를 보내지 않는다.
  PATCH 응답의 role/scope/version도 queue에 다시 반영한다. `bookstore-a` VENDOR_QUOTE replacement가
  mapping-required → save → reparse를 거친 뒤에도 같은 scope를 전송하고 추천자료 비교 대상에는 들어가지 않는
  end-to-end regression이 통과한다.
- candidate mutation fence는 outcome 문자열만이 아니라 full candidate와 `row_version`을 저장한다. list/search/
  load-more에서 server row가 fence보다 클 때만 local state를 대체하고, older/equal response는 local commit을
  되돌리지 않는다. 새 server row를 받아들이면 기존 모든 tab에서 같은 id를 제거하고 authoritative outcome에
  한 번만 넣으며 summary count, unresolved count, expected amount와 page total을 함께 맞춘다. local v2
  CANDIDATE 뒤 server v3 EXCLUDED가 cursor에서 도착해도 숨지 않고, equal v2 stale search는 되돌리지 않는다.
  서로 다른 outcome request가 v2/v3 row와 서로 다른 summary snapshot을 돌려주거나 cursor row가 summary보다
  새로울 때도 summary의 기준 row에서 authoritative row로 한 번만 보정한다.

### RED / GREEN과 최종 gate

핵심 migration 네 경우는 처음 모두 실패했고, historical backup prefix, 같은 source의 과거 SUCCESS count,
incomplete bundle과 historical PARTIAL도 각각 독립 RED였다. mutation contract table은 `12 failed`, vendor scope는
`bookstore-a` 대신 `*`가 전송되어 `1 failed`, candidate fence는 v3 row가 보이지 않아 `1 failed`였다. 독립 최종
review에서 source discovery error key 누락, deferred checksum 검증 누락, postlude 뒤 lower-sorted prelude 재발견 시
guard 유실, initial/cursor row-summary snapshot skew를 추가로 RED로 재현한 뒤 모두 닫았다.
구현 뒤 focused 결과와 최종 gate는 다음과 같다.

```text
tests/test_task8_round4.py                          10 passed in 3.11s
Task 8 round2 + round3 + round4                     47 passed in 28.33s
API contract + round4                               73 passed in 40.29s
backend full pytest                                450 passed in 112.77s
npm ci                                               audited 265, 0 vulnerabilities
npm test -- --run                                   3 files, 100 passed in 53.91s
ingestion/candidate frontend                         62 passed in 52.81s
npm run typecheck                                    exit 0
npm run lint                                         exit 0, warning 0
npm run build                                        36 modules, exit 0
dist JS                                              306.95 kB / 94.30 kB gzip
uvx ruff@0.16.5 format --check .                     108 files already formatted
uvx ruff@0.16.5 check .                              All checks passed
python -m compileall -q src tests                    exit 0
uv lock --check                                      resolved 36 packages
uv build                                             sdist + wheel built
npm ls --all                                         exit 0
dangerous-execution diff scan                        0 matches
private-key/token signature diff scan                0 matches
merge-marker scan                                    0 matches
```

OpenAPI와 generated TypeScript type은 각각 연속 두 번 생성해 같은 SHA-256을 확인했다.

```text
backend/openapi.json
8FCAEF6CE73F4A748A7A07DBE4136B81D208B5B17E0853BF3C102C9E6D1F0245

frontend/src/api/types.ts
8D2E1441CB5826211F579ED1BF581EB14F56E617CDC3A8B50C0D04B0B13256F9
```

새 prelude의 SHA-256은
`4B39FE51FCCEF6105511B71CC99917CCDD3CDB54269B2D1E5DDE02DD5C80E5B9`, 새 postlude는
`D236B01E9FC31013C5C183FF1536C5815BADD47ACCE6CE5C01DC700B77DCB462`이다. committed `0011`은
`EB22DB853D9488C3FDC0A95AD0EAFD2E4C79884D41D6D313B272CB49822242B0`로 보존됐고,
두 새 migration을 제외한 `git diff --exit-code 30da997 -- backend/src/suseoro/db/migrations`는 exit 0이다.
독립 재검토 결과 남은 Critical/Important finding은 없다. server summary 자체에는 snapshot/version이 없으므로
서로 상쇄되는 다중 concurrent 변경이 완전히 같은 aggregate를 만드는 경우는 원천적으로 판별할 수 없고,
DDL은 있으나 committed `0011` ledger row만 유실된 비정상 DB는 self-heal 대신 fail closed한다. 둘 다 현재
요구 범위의 data/guard를 손상하지 않는 residual risk이며 blocker는 없다.
