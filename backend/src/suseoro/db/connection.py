"""SQLite connection construction with the application's durability settings."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_FTS_PREFIX = "holding_search_fts_index"
_fts_trust: dict[int, int] = {}


def _is_fts_write(sql: str) -> bool:
    if _FTS_PREFIX not in sql.casefold():
        return False
    return (
        re.search(
            rf"(?:insert|update|delete|replace)\b[^;]*\b{_FTS_PREFIX}",
            sql,
            re.IGNORECASE,
        )
        is not None
    )


class ProtectedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection._guard_sql(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection._guard_sql(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql_script):
        self.connection._guard_sql(sql_script)
        return super().executescript(sql_script)


class ProtectedConnection(sqlite3.Connection):
    def _guard_sql(self, sql: str) -> None:
        if _fts_trust.get(id(self), 0) == 0 and _is_fts_write(sql):
            raise sqlite3.DatabaseError("internal FTS tables are protected")

    def execute(self, sql, parameters=()):
        self._guard_sql(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self._guard_sql(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql_script):
        self._guard_sql(sql_script)
        return super().executescript(sql_script)

    def cursor(self, factory=ProtectedCursor):
        return super().cursor(factory)


def install_fts_protection(connection: sqlite3.Connection) -> None:
    """Deny direct virtual-table writes while allowing its maintenance triggers."""
    approved_triggers = {
        "normalized_works_fts_insert",
        "normalized_works_fts_update",
        "normalized_works_fts_delete",
    }

    def authorize(action, table, _column, _database, source):
        if _fts_trust.get(id(connection), 0):
            return sqlite3.SQLITE_OK
        if (
            action
            in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
            and table == _FTS_PREFIX
            and source not in approved_triggers
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)


@contextmanager
def trusted_fts_maintenance(connection: sqlite3.Connection):
    """Narrow scope for migrations and FTS5's external-content integrity command."""
    key = id(connection)
    _fts_trust[key] = _fts_trust.get(key, 0) + 1
    try:
        yield
    finally:
        remaining = _fts_trust.get(key, 1) - 1
        if remaining:
            _fts_trust[key] = remaining
        else:
            _fts_trust.pop(key, None)


def verify_fts_integrity(connection: sqlite3.Connection) -> None:
    with trusted_fts_maintenance(connection):
        connection.execute(
            """
            INSERT INTO holding_search_fts_index(
                holding_search_fts_index, rank
            ) VALUES ('integrity-check', 1)
            """
        )


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection configured for the local shared ledger."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, factory=ProtectedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    install_fts_protection(connection)
    return connection
