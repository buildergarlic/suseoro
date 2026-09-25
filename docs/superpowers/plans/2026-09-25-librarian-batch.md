# 추천목록 통합 구현 계획

**Goal:** 여러 파일을 표준화·중복 통합하고 ISBN 없는 신뢰 가능한 도서를 일괄 검토한다.

**Architecture:** 기존 React 개인용 앱과 FastAPI/SQLite에 기능을 추가한다. 문서 파싱, 원자적 저장·되돌리기, 가져오기 화면, 목록 일괄 작업을 분리해 병렬 구현하고 통합 검증한다.

**Tech Stack:** 기존 Python, FastAPI, SQLite, React, TypeScript, pytest, Vitest. 새 제품 의존성 없음.

**Spec:** ../specs/2026-09-25-librarian-batch-design.md

## 공통 제약

- 개인용 제품 경로 `backend/src/suseoro/simple`, `frontend/src/simple`만 확장한다.
- 기존 원본·수동 편집·구입 수량·비공개 자료를 보존한다.
- 외부 서지 조회를 필수 단계로 추가하지 않는다.
- 일괄 입력 20,000행 제한과 페이지당 100행을 적용한다.

## 작업과 검증

- [x] `documents.py`: ISBN 누락과 무효 값을 분리. 제목·저자·출판사 있는 무ISBN 행 준비 완료, 불완전한 서지·OCR·수식은 확인 유지. `test_simple_isbn_optional.py` 실패 확인 후 수정.
- [x] `batch.py`, `store.py`, `app.py`: `POST /lists/{id}/imports/batch`와 `books/bulk`, `operations/{operation_id}/undo` 제공. ISBN/엄격 서지 인덱스, 출처 기록, 원자성, 요청 재시도, 오래된 되돌리기 검증. `test_simple_batch.py`에서 교차 파일·가격 충돌·다른 ISBN·200권·혼합 목록 ID 검사.
- [x] `ImportDialog.tsx`: 다중 파일 대기열, 개별 실패, 파일별 편집/열 연결, 한 번에 저장, 합친 수 안내, 페이지 제한. 일괄 API 계약은 `api.ts`에 정의하고 가져오기 테스트로 검증.
- [x] `SimpleLibraryApp.tsx`와 일괄 작업 컴포넌트: 작업 선택과 구입 선택 분리, 검색 전체 선택, ISBN 없음 필터, 서지 확인·구입·보류, 결과와 되돌리기. 서버가 건너뛴 행을 표시. 큰 목록은 페이지 표시.
- [x] 사용자 설명서 갱신. 백엔드 전체 pytest, 프런트엔드 Vitest·타입·lint·build 실행. 로컬 합성자료로 실제 브라우저 흐름 확인.

검증 결과: [2026-09-25 검증 보고서](../validation/2026-09-25-librarian-batch.md).

## 집중 확인할 실패 조건

1. ISBN 없는 동일 제목의 다른 권차/출판사는 합치지 않는다.
2. 중간 파일 실패를 성공처럼 표시하지 않고 성공한 미리보기는 보존한다.
3. 재시도는 중복 입력을 만들지 않으며 편집 이후 되돌리기는 최신 변경을 지우지 않는다.
4. 검색 전체 선택이 숨겨진 다른 검색 결과·다른 목록까지 적용되지 않는다.
5. 20,000행은 모든 행을 DOM에 그리지 않으며 잘못된 입력을 일부만 저장하지 않는다.
