"""Durable, replayable server-sent event feed."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from suseoro.api.dependencies import AuthenticatedUser, current_user
from suseoro.db.connection import connect
from suseoro.security.sessions import format_utc, utc_now

router = APIRouter(prefix="/api/v2/events", tags=["events"])


@dataclass(frozen=True)
class ApiEvent:
    id: str
    event_type: str
    data: dict[str, Any]
    created_at: str


def publish_event(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    workspace_id: str | None,
    event_type: str,
    data: dict[str, Any],
    deduplication_key: str | None = None,
) -> str:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO api_events (
            school_id, workspace_id, event_type, deduplication_key,
            data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            school_id,
            workspace_id,
            event_type,
            deduplication_key,
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            format_utc(utc_now()),
        ),
    )
    if cursor.rowcount == 1:
        return str(cursor.lastrowid)
    row = connection.execute(
        """
        SELECT id FROM api_events
        WHERE school_id = ? AND event_type = ? AND deduplication_key = ?
        """,
        (school_id, event_type, deduplication_key),
    ).fetchone()
    if row is None:
        raise RuntimeError("deduplicated event could not be found")
    return str(row["id"])


def replay_events(
    database_path: Path,
    *,
    school_id: str,
    workspace_id: str | None,
    last_event_id: str | None,
    limit: int = 100,
) -> list[ApiEvent]:
    try:
        after = int(last_event_id or 0)
    except ValueError:
        after = 0
    with connect(database_path) as connection:
        if workspace_id is None:
            rows = connection.execute(
                """
                SELECT id, event_type, data_json, created_at FROM api_events
                WHERE school_id = ? AND id > ? ORDER BY id LIMIT ?
                """,
                (school_id, after, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, event_type, data_json, created_at FROM api_events
                WHERE school_id = ? AND workspace_id = ? AND id > ? ORDER BY id LIMIT ?
                """,
                (school_id, workspace_id, after, limit),
            ).fetchall()
    return [
        ApiEvent(
            str(row["id"]),
            row["event_type"],
            json.loads(row["data_json"]),
            row["created_at"],
        )
        for row in rows
    ]


class EventFeed:
    def __init__(
        self,
        database_path: Path,
        *,
        school_id: str,
        workspace_id: str | None,
        last_event_id: str | None,
        heartbeat_seconds: float = 15,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.database_path = database_path
        self.school_id = school_id
        self.workspace_id = workspace_id
        self.last_event_id = last_event_id
        self.heartbeat_seconds = heartbeat_seconds
        self.wait = wait
        self.pending: list[ApiEvent] = []
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if self.closed:
            raise StopIteration
        if not self.pending:
            self.pending = replay_events(
                self.database_path,
                school_id=self.school_id,
                workspace_id=self.workspace_id,
                last_event_id=self.last_event_id,
            )
        if self.pending:
            event = self.pending.pop(0)
            self.last_event_id = event.id
            payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
            return f"id: {event.id}\nevent: {event.event_type}\ndata: {payload}\n\n"
        self.wait(self.heartbeat_seconds)
        return ": heartbeat\n\n"

    def close(self) -> None:
        self.closed = True


@router.get("", summary="실시간 작업 알림 받기", operation_id="streamEvents")
def stream_events(
    request: Request,
    user: AuthenticatedUser = Depends(current_user),
    workspace_id: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    feed = EventFeed(
        request.app.state.settings.database_path,
        school_id=user.school_id,
        workspace_id=workspace_id,
        last_event_id=last_event_id,
    )
    return StreamingResponse(
        feed,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
