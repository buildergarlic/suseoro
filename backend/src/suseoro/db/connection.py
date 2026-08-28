"""SQLite connection construction with the application's durability settings."""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_FTS_PREFIX = "holding_search_fts_index"
_fts_trust: dict[int, int] = {}
_fts_projection_trust: dict[int, int] = {}
_fts_projection_pending: set[int] = set()
_fts_projection_activity: set[int] = set()
_FTS_PROJECTION_TRIGGERS = {
    "normalized_works_fts_insert",
    "normalized_works_fts_update",
    "normalized_works_fts_delete",
}
_FTS_DML_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_FTS_SCHEMA_ACTIONS = {
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
}
_database_condition = threading.Condition()
_database_connections: dict[str, set[ProtectedConnection]] = {}
_restoring_databases: set[str] = set()


def _unregister_database_connection(connection: ProtectedConnection) -> None:
    key = getattr(connection, "_database_path_key", None)
    if key is None:
        return
    with _database_condition:
        connections = _database_connections.get(key)
        if connections is not None:
            connections.discard(connection)
            if not connections:
                _database_connections.pop(key, None)
        _database_condition.notify_all()


@contextmanager
def quiesce_database(database_path: Path):
    """Fence new work and close process-owned handles for an atomic restore swap."""
    key = str(Path(database_path).resolve())
    with _database_condition:
        while key in _restoring_databases:
            _database_condition.wait()
        _restoring_databases.add(key)
        connections = tuple(_database_connections.get(key, ()))
    try:
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                # A statement already executing is allowed to finish/fail at its
                # own checkpoint; all new connects remain fenced.
                pass
        yield
    finally:
        with _database_condition:
            _restoring_databases.discard(key)
            _database_condition.notify_all()


def _clear_fts_connection_state(connection: sqlite3.Connection) -> None:
    """Forget every authorization marker owned by one completed connection scope."""
    key = id(connection)
    _fts_trust.pop(key, None)
    _fts_projection_trust.pop(key, None)
    _fts_projection_pending.discard(key)
    _fts_projection_activity.discard(key)


def _is_protected_fts_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    name = value.casefold()
    return name == "holding_search_fts" or name.startswith("holding_search_fts_")


def _is_fts_projection_trigger(value: object) -> bool:
    return isinstance(value, str) and value.casefold() in _FTS_PROJECTION_TRIGGERS


def _is_projection_source_write(sql: str) -> bool:
    normalized_write = re.search(
        r"(?:insert(?:\s+or\s+\w+)?\s+into|replace\s+into|"
        r"update(?:\s+or\s+\w+)?|delete\s+from)\s+"
        r"(?:main\s*\.\s*)?[\"`\[]?normalized_works\b",
        sql,
        re.IGNORECASE,
    )
    cascading_holding_delete = re.search(
        r"delete\s+from\s+(?:main\s*\.\s*)?[\"`\[]?holdings\b",
        sql,
        re.IGNORECASE,
    )
    return normalized_write is not None or cascading_holding_delete is not None


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
        with _trusted_fts_projection_for(self.connection, sql):
            return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection._guard_sql(sql)
        with _trusted_fts_projection_for(self.connection, sql):
            return super().executemany(sql, parameters)

    def executescript(self, sql_script):
        self.connection._guard_sql(sql_script)
        return super().executescript(sql_script)


class ProtectedConnection(sqlite3.Connection):
    _closed = False

    def _guard_sql(self, sql: str) -> None:
        if _fts_trust.get(id(self), 0) == 0 and _is_fts_write(sql):
            raise sqlite3.DatabaseError("internal FTS tables are protected")

    def execute(self, sql, parameters=()):
        self._guard_sql(sql)
        with _trusted_fts_projection_for(self, sql):
            return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self._guard_sql(sql)
        with _trusted_fts_projection_for(self, sql):
            return super().executemany(sql, parameters)

    def executescript(self, sql_script):
        self._flush_pending_projection()
        self._guard_sql(sql_script)
        return super().executescript(sql_script)

    def cursor(self, factory=ProtectedCursor):
        return super().cursor(factory)

    def __enter__(self):
        return super().__enter__()

    def __exit__(self, exception_type, exception_value, traceback):
        try:
            return super().__exit__(exception_type, exception_value, traceback)
        finally:
            _clear_fts_connection_state(self)

    def close(self):
        closed = False
        try:
            result = super().close()
            closed = True
            return result
        finally:
            if closed:
                self._closed = True
                _unregister_database_connection(self)
            _clear_fts_connection_state(self)

    def commit(self):
        key = id(self)
        with _trusted_fts_projection_for(self, ""):
            result = super().commit()
        _fts_projection_pending.discard(key)
        _fts_projection_activity.discard(key)
        return result

    def rollback(self):
        key = id(self)
        try:
            return super().rollback()
        finally:
            _fts_projection_pending.discard(key)
            _fts_projection_activity.discard(key)

    def _flush_pending_projection(self) -> None:
        key = id(self)
        if key not in _fts_projection_pending:
            return
        with _trusted_fts_projection_for(self, ""):
            super().execute("SELECT 1").fetchone()
        _fts_projection_pending.discard(key)
        _fts_projection_activity.discard(key)


def install_fts_protection(connection: sqlite3.Connection) -> None:
    """Default-deny DML and schema changes for the FTS projection and shadows."""

    def authorize(action, first, second, _database, source):
        if _fts_trust.get(id(connection), 0):
            return sqlite3.SQLITE_OK
        if (
            action == sqlite3.SQLITE_PRAGMA
            and str(first).casefold() == "writable_schema"
        ):
            return sqlite3.SQLITE_DENY
        if action in _FTS_DML_ACTIONS and _is_protected_fts_name(first):
            projection_trusted = _fts_projection_trust.get(id(connection), 0)
            if (
                projection_trusted
                and first == _FTS_PREFIX
                and _is_fts_projection_trigger(source)
            ):
                _fts_projection_activity.add(id(connection))
                return sqlite3.SQLITE_OK
            if projection_trusted and first != _FTS_PREFIX:
                _fts_projection_activity.add(id(connection))
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in _FTS_SCHEMA_ACTIONS:
            # SQLite's ALTER_TABLE callback exposes the old table but not a
            # rename destination. Application connections never need ALTER;
            # migrations run in the explicit trusted scope below.
            if action == sqlite3.SQLITE_ALTER_TABLE:
                return sqlite3.SQLITE_DENY
            if any(
                _is_protected_fts_name(value) or _is_fts_projection_trigger(value)
                for value in (first, second)
            ):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)


@contextmanager
def _trusted_fts_projection_for(connection: sqlite3.Connection, sql: str):
    """Allow shadow writes only while one normalized-work projection runs."""
    key = id(connection)
    source_write = _is_projection_source_write(sql)
    if not source_write and key not in _fts_projection_pending:
        yield
        return
    outermost = _fts_projection_trust.get(key, 0) == 0
    if outermost:
        _fts_projection_activity.discard(key)
    _fts_projection_trust[key] = _fts_projection_trust.get(key, 0) + 1
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        remaining = _fts_projection_trust.get(key, 1) - 1
        if remaining:
            _fts_projection_trust[key] = remaining
        else:
            _fts_projection_trust.pop(key, None)
            activity = key in _fts_projection_activity
            _fts_projection_activity.discard(key)
            if source_write and succeeded:
                _fts_projection_pending.add(key)
            elif activity:
                _fts_projection_pending.discard(key)


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
            # Do not retain cached statements authorized during maintenance.
            if not getattr(connection, "_closed", False):
                install_fts_protection(connection)


def verify_fts_integrity(connection: sqlite3.Connection) -> None:
    with trusted_fts_maintenance(connection):
        connection.execute(
            """
            INSERT INTO holding_search_fts_index(
                holding_search_fts_index, rank
            ) VALUES ('integrity-check', 1)
            """
        )


def rebuild_fts_index(connection: sqlite3.Connection) -> None:
    """Rebuild the external-content FTS index in an explicit maintenance scope."""
    with trusted_fts_maintenance(connection):
        connection.execute(
            """
            INSERT INTO holding_search_fts_index(holding_search_fts_index)
            VALUES ('rebuild')
            """
        )


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection configured for the local shared ledger."""
    database_path = Path(database_path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(database_path)
    with _database_condition:
        while key in _restoring_databases:
            _database_condition.wait()
        connection = sqlite3.connect(
            database_path, factory=ProtectedConnection, check_same_thread=False
        )
        connection._database_path_key = key
        _database_connections.setdefault(key, set()).add(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        install_fts_protection(connection)
    return connection
