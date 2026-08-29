"""Process-isolated real API server and deterministic seed for Playwright."""

from __future__ import annotations

import secrets
import tempfile
import threading
import uuid
from base64 import b64encode
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import uvicorn
from openpyxl import Workbook
from workflow_fixtures import NOW, make_workflow_fixture

from suseoro.api.app import create_app
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.jobs.handlers import build_job_runner
from suseoro.security.passwords import hash_password
from suseoro.workflow.approvals import ApprovalService

PASSWORD = f"E2e!{secrets.token_urlsafe(32)}aA9"
_temporary_data = tempfile.TemporaryDirectory(prefix="suseoro-e2e-")
_data_dir = Path(_temporary_data.name)
_fixture = make_workflow_fixture(_data_dir)
_fixture.connection.execute(
    "UPDATE acquisition_workspaces SET name = '핵심 흐름 수서' WHERE id = ?",
    (_fixture.workspace_id,),
)

_fixture.add_candidate(
    title="도서관의 책",
    author="김사서",
    isbn="9788937464010",
    quantity=1,
    unit_price=12_000,
    edition="개정판",
)
_fixture.add_candidate(
    title="차분한 수서",
    author="이담당",
    isbn="9788936434267",
    quantity=1,
    unit_price=15_000,
)
_fixture.add_candidate(
    title="이미 소장한 책",
    author="박제외",
    isbn="9780306406157",
    outcome="EXCLUDED",
    unit_price=10_000,
)

password_hash = hash_password(PASSWORD)
_fixture.connection.execute(
    "UPDATE users SET password_hash = ? WHERE id IN (?, ?)",
    (password_hash, _fixture.operator_id, _fixture.reviewer_id),
)
requested = ApprovalService(_fixture.connection).request_approval(
    school_id=_fixture.school_id,
    workspace_id=_fixture.workspace_id,
    actor_id=_fixture.operator_id,
    actor_roles=("OPERATOR",),
    workspace_version=_fixture.workspace_version(),
    candidate_collection_revision=_fixture.candidate_collection_revision(),
    budget_won=50_000,
    reason="두 권의 구입 승인을 요청합니다.",
    idempotency_key="e2e-seed-approval",
    request_id=str(uuid.uuid4()),
)

accessibility_workspace_id = str(uuid.uuid4())
_fixture.connection.execute(
    """
    INSERT INTO acquisition_workspaces (
        id, school_id, name, status, created_by_user_id, created_at, updated_at
    ) VALUES (?, ?, '접근성 검증 수서', 'CANDIDATE_REVIEW', ?, ?, ?)
    """,
    (
        accessibility_workspace_id,
        _fixture.school_id,
        _fixture.operator_id,
        NOW,
        NOW,
    ),
)
accessibility_fixture = replace(
    _fixture,
    workspace_id=accessibility_workspace_id,
    candidate_ids=list(_fixture.candidate_ids),
)
accessibility_fixture.add_candidate(
    title="접근 가능한 도서관",
    author="한검토",
    isbn="9781861972712",
    unit_price=18_000,
)
ApprovalService(_fixture.connection).request_approval(
    school_id=_fixture.school_id,
    workspace_id=accessibility_workspace_id,
    actor_id=_fixture.operator_id,
    actor_roles=("OPERATOR",),
    workspace_version=accessibility_fixture.workspace_version(),
    candidate_collection_revision=(
        accessibility_fixture.candidate_collection_revision()
    ),
    budget_won=30_000,
    reason="접근성 검증용 승인 요청입니다.",
    idempotency_key="e2e-accessibility-approval",
    request_id=str(uuid.uuid4()),
)

mapping_workspace_id = str(uuid.uuid4())
_fixture.connection.execute(
    """
    INSERT INTO acquisition_workspaces (
        id, school_id, name, status, created_by_user_id, created_at, updated_at
    ) VALUES (?, ?, '열 연결 검증 수서', 'CANDIDATE_REVIEW', ?, ?, ?)
    """,
    (
        mapping_workspace_id,
        _fixture.school_id,
        _fixture.operator_id,
        NOW,
        NOW,
    ),
)
mapping_fixture = replace(
    _fixture,
    workspace_id=mapping_workspace_id,
    candidate_ids=list(accessibility_fixture.candidate_ids),
)
mapping_fixture.add_candidate(
    title="열 연결할 책",
    author="표사서",
    isbn="9788937464010",
    unit_price=12_000,
)
mapping_service = ApprovalService(_fixture.connection)
mapping_request = mapping_service.request_approval(
    school_id=_fixture.school_id,
    workspace_id=mapping_workspace_id,
    actor_id=_fixture.operator_id,
    actor_roles=("OPERATOR",),
    workspace_version=mapping_fixture.workspace_version(),
    candidate_collection_revision=mapping_fixture.candidate_collection_revision(),
    budget_won=20_000,
    reason="열 연결 복구 검증용 승인 요청입니다.",
    idempotency_key="e2e-mapping-approval-request",
    request_id=str(uuid.uuid4()),
)
mapping_service.approve(
    school_id=_fixture.school_id,
    workspace_id=mapping_workspace_id,
    revision_id=mapping_request["revision_id"],
    actor_id=_fixture.reviewer_id,
    actor_roles=("REVIEWER",),
    workspace_version=mapping_fixture.workspace_version(),
    reason="열 연결 복구 검증 승인",
    idempotency_key="e2e-mapping-approval-decision",
    request_id=str(uuid.uuid4()),
)
_fixture.connection.commit()


def _delivery_workbook(row: list[object]) -> str:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "납품명세서"
    sheet.append(["ISBN", "제목", "저자", "수량", "단가"])
    sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return b64encode(buffer.getvalue()).decode("ascii")


METADATA = {
    "school_id": _fixture.school_id,
    "workspace_id": _fixture.workspace_id,
    "accessibility_workspace_id": accessibility_workspace_id,
    "mapping_workspace_id": mapping_workspace_id,
    "approval_revision_id": requested["revision_id"],
    "password": PASSWORD,
    "operator_username": "operator",
    "reviewer_username": "reviewer",
    "isbns": ["9788937464010", "9788936434267"],
    "delivery_partial_a_xlsx_base64": _delivery_workbook(
        ["9788937464010", "도서관의 책", "김사서", 1, 12_000]
    ),
    "delivery_partial_b_xlsx_base64": _delivery_workbook(
        ["9788936434267", "차분한 수서", "이담당", 1, 15_000]
    ),
}
_fixture.connection.close()

settings = Settings(
    data_dir=_data_dir,
    database_path=_data_dir / "workflow.sqlite3",
    secure_cookies=False,
)
app = create_app(settings)


def _run_jobs() -> None:
    with connect(settings.database_path) as worker_connection:
        runner = build_job_runner(worker_connection)
        while True:
            completed = runner.run_once()
            if completed is None:
                threading.Event().wait(0.05)


threading.Thread(target=_run_jobs, name="e2e-job-worker", daemon=True).start()


@app.get("/__e2e__/metadata", include_in_schema=False)
def e2e_metadata() -> dict[str, object]:
    return METADATA


@app.get("/__e2e__/health", include_in_schema=False)
def e2e_health() -> dict[str, bool]:
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
