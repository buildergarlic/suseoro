from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from suseoro.catalog.normalization import (
    canonical_isbn13,
    normalize_author,
    normalize_key,
)
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations

NOW = "2026-08-28T00:00:00.000000Z"


@dataclass
class WorkflowFixture:
    connection: object
    school_id: str
    other_school_id: str
    operator_id: str
    reviewer_id: str
    dual_role_id: str
    other_operator_id: str
    workspace_id: str
    candidate_ids: list[str]

    def add_candidate(
        self,
        *,
        title: str,
        author: str,
        isbn: str | None,
        outcome: str = "CANDIDATE",
        quantity: int = 1,
        unit_price: int = 10_000,
        edition: str | None = None,
    ) -> str:
        source_row_id = str(uuid.uuid4())
        recommendation_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        source_document_id = self.connection.execute(
            "SELECT id FROM source_documents WHERE school_id = ? LIMIT 1",
            (self.school_id,),
        ).fetchone()["id"]
        fields = {
            "title": title,
            "author": author,
            "isbn": isbn,
            "edition": edition,
            "price": unit_price,
        }
        self.connection.execute(
            """
            INSERT INTO source_rows (
                id, source_document_id, source_row, status, raw_json,
                fields_json, warnings_json, created_at
            ) VALUES (?, ?, ?, 'SUCCESS', ?, ?, '[]', ?)
            """,
            (
                source_row_id,
                source_document_id,
                len(self.candidate_ids) + 1,
                json.dumps(fields, ensure_ascii=False),
                json.dumps(fields, ensure_ascii=False),
                NOW,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO recommendations (
                id, school_id, workspace_id, source_document_id, source_row_id,
                isbn_input, isbn13, original_title, original_authors_json,
                original_edition, original_json, title_key, subtitle_key,
                author_key, publisher_key, volume_key, edition_key, series_key,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', '', ?, '', ?)
            """,
            (
                recommendation_id,
                self.school_id,
                self.workspace_id,
                source_document_id,
                source_row_id,
                isbn,
                canonical_isbn13(isbn),
                title,
                json.dumps([author], ensure_ascii=False),
                edition,
                json.dumps(fields, ensure_ascii=False, sort_keys=True),
                normalize_key(title),
                normalize_author(author),
                normalize_key(edition),
                NOW,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO candidate_decisions (
                id, school_id, workspace_id, recommendation_id, outcome,
                reason, quantity, unit_price, modified_by_user_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'fixture', ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                self.school_id,
                self.workspace_id,
                recommendation_id,
                outcome,
                quantity,
                unit_price,
                self.operator_id,
                NOW,
                NOW,
            ),
        )
        self.candidate_ids.append(candidate_id)
        self.connection.commit()
        return candidate_id

    def set_state(self, state: str) -> int:
        self.connection.execute(
            """
            UPDATE acquisition_workspaces
            SET status = ?, row_version = row_version + 1, updated_at = ?
            WHERE id = ?
            """,
            (state, NOW, self.workspace_id),
        )
        self.connection.commit()
        return self.connection.execute(
            "SELECT row_version FROM acquisition_workspaces WHERE id = ?",
            (self.workspace_id,),
        ).fetchone()["row_version"]

    def workspace_version(self) -> int:
        return self.connection.execute(
            "SELECT row_version FROM acquisition_workspaces WHERE id = ?",
            (self.workspace_id,),
        ).fetchone()["row_version"]


def make_workflow_fixture(
    tmp_path: Path,
    *,
    state: str = "CANDIDATE_REVIEW",
    single_operator_mode: bool = False,
    migrations_dir: Path | None = None,
) -> WorkflowFixture:
    connection = connect(tmp_path / "workflow.sqlite3")
    apply_migrations(connection, migrations_dir)
    school_id = str(uuid.uuid4())
    other_school_id = str(uuid.uuid4())
    operator_id = str(uuid.uuid4())
    reviewer_id = str(uuid.uuid4())
    dual_role_id = str(uuid.uuid4())
    other_operator_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    source_file_id = str(uuid.uuid4())
    source_document_id = str(uuid.uuid4())
    connection.executemany(
        """
        INSERT INTO schools (
            id, name, single_operator_mode, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (school_id, "첫 학교", int(single_operator_mode), NOW, NOW),
            (other_school_id, "다른 학교", 0, NOW, NOW),
        ),
    )
    users = (
        (operator_id, school_id, "operator", "담당자"),
        (reviewer_id, school_id, "reviewer", "검토자"),
        (dual_role_id, school_id, "dual", "겸임자"),
        (other_operator_id, other_school_id, "other", "타교 담당자"),
    )
    connection.executemany(
        """
        INSERT INTO users (
            id, school_id, username, password_hash, display_name,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'hash', ?, ?, ?)
        """,
        (
            (user_id, sid, username, display, NOW, NOW)
            for user_id, sid, username, display in users
        ),
    )
    connection.executemany(
        "INSERT INTO user_roles (school_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
        (
            (school_id, operator_id, "OPERATOR", NOW),
            (school_id, reviewer_id, "REVIEWER", NOW),
            (school_id, dual_role_id, "OPERATOR", NOW),
            (school_id, dual_role_id, "REVIEWER", NOW),
            (other_school_id, other_operator_id, "OPERATOR", NOW),
        ),
    )
    connection.execute(
        """
        INSERT INTO acquisition_workspaces (
            id, school_id, name, status, created_by_user_id, created_at, updated_at
        ) VALUES (?, ?, '2026-2차 수서', ?, ?, ?, ?)
        """,
        (workspace_id, school_id, state, operator_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO source_files (
            id, sha256, size_bytes, storage_path, original_filename,
            detected_format, created_at
        ) VALUES (?, ?, 1, 'fixture.xlsx', 'fixture.xlsx', 'XLSX', ?)
        """,
        (source_file_id, "a" * 64, NOW),
    )
    connection.execute(
        """
        INSERT INTO source_documents (
            id, source_file_id, school_id, role, parser_version, status,
            detected_format, created_at, completed_at
        ) VALUES (?, ?, ?, 'PURCHASE_REQUEST', 'test', 'SUCCESS', 'XLSX', ?, ?)
        """,
        (source_document_id, source_file_id, school_id, NOW, NOW),
    )
    connection.commit()
    return WorkflowFixture(
        connection=connection,
        school_id=school_id,
        other_school_id=other_school_id,
        operator_id=operator_id,
        reviewer_id=reviewer_id,
        dual_role_id=dual_role_id,
        other_operator_id=other_operator_id,
        workspace_id=workspace_id,
        candidate_ids=[],
    )
