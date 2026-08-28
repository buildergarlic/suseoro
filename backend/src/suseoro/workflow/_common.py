"""Shared transaction, authorization, and idempotency helpers."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from suseoro.services.idempotency import (
    complete_idempotent_request,
    reserve_idempotency_key,
)

T = TypeVar("T")


class WorkflowDomainError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@contextmanager
def mutation_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    owns_transaction = not connection.in_transaction
    savepoint = f"workflow_{uuid.uuid4().hex}"
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        if owns_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        raise
    else:
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE {savepoint}")


def require_role(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    actor_roles: Collection[str],
    role: str,
    error_type: type[WorkflowDomainError] = WorkflowDomainError,
) -> None:
    if role not in actor_roles:
        raise error_type(f"{role}_ROLE_REQUIRED")
    assigned = connection.execute(
        """
        SELECT 1 FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.school_id = u.school_id
        WHERE u.id = ? AND u.school_id = ? AND u.is_active = 1 AND ur.role = ?
        """,
        (actor_id, school_id, role),
    ).fetchone()
    if assigned is None:
        raise error_type(f"{role}_ROLE_REQUIRED")


def idempotent_mutation(
    connection: sqlite3.Connection,
    *,
    school_id: str,
    actor_id: str,
    route: str,
    key: str,
    request_body: Any,
    operation: Callable[[], T],
) -> T:
    with mutation_transaction(connection):
        replay = reserve_idempotency_key(
            connection,
            school_id=school_id,
            actor_id=actor_id,
            route=route,
            key=key,
            request_body=request_body,
        )
        if replay is not None:
            return replay.body
        result = operation()
        complete_idempotent_request(
            connection,
            school_id=school_id,
            actor_id=actor_id,
            route=route,
            key=key,
            status=200,
            body=result,
        )
        return result
