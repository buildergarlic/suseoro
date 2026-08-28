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
