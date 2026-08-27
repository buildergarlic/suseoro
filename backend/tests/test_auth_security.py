from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suseoro.api.app import create_app
from suseoro.config import Settings
from suseoro.db.connection import connect
from suseoro.db.migrations import apply_migrations
from suseoro.security.passwords import hash_password, verify_password
from suseoro.security.secrets import MachineSecretStore
from suseoro.security.sessions import digest_token
from suseoro.services.audit import record_audit_event

SCHOOL_ID = "550e8400-e29b-41d4-a716-446655440100"
OPERATOR_ID = "550e8400-e29b-41d4-a716-446655440101"
REVIEWER_ID = "550e8400-e29b-41d4-a716-446655440102"
REQUEST_ID = "550e8400-e29b-41d4-a716-446655440103"
LOGOUT_REQUEST_ID = "550e8400-e29b-41d4-a716-446655440106"
NOW = "2026-08-28T12:34:56Z"


def _valid_password() -> str:
    return f"Aa1!{secrets.token_urlsafe(18)}"


def _seed_auth_database(settings: Settings, password: str) -> None:
    with connect(settings.database_path) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO schools (id, name, single_operator_mode, created_at, updated_at)
            VALUES (?, 'Security school', 0, ?, ?)
            """,
            (SCHOOL_ID, NOW, NOW),
        )
        for user_id, username, role in (
            (OPERATOR_ID, "operator", "OPERATOR"),
            (REVIEWER_ID, "reviewer", "REVIEWER"),
        ):
            connection.execute(
                """
                INSERT INTO users (
                    id, school_id, username, password_hash, display_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    SCHOOL_ID,
                    username,
                    hash_password(password),
                    username.title(),
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO user_roles (school_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (SCHOOL_ID, user_id, role, NOW),
            )
        connection.commit()


def _client(data_dir: Path) -> tuple[TestClient, Settings, str]:
    settings = Settings(data_dir=data_dir)
    password = _valid_password()
    _seed_auth_database(settings, password)
    app = create_app(settings)
    app.state.secret_store = MachineSecretStore(protector=_TestProtector())
    return TestClient(app, base_url="https://testserver"), settings, password


def _mutation_headers(key: str, request_id: str = REQUEST_ID) -> dict[str, str]:
    return {"Idempotency-Key": key, "X-Request-ID": request_id}


def _login(
    client: TestClient,
    password: str,
    *,
    key: str = "login-attempt-1",
    request_id: str = REQUEST_ID,
):
    return client.post(
        "/api/v2/auth/login",
        headers=_mutation_headers(key, request_id),
        json={
            "school_id": SCHOOL_ID,
            "username": "operator",
            "password": password,
        },
    )


def test_password_hashing_rejects_weak_passwords_and_verifies_argon2id() -> None:
    """Replacing the password policy or Argon2id with plaintext must fail this test."""
    weak_password = secrets.token_urlsafe(4)
    valid_password = _valid_password()
    wrong_password = _valid_password()
    with pytest.raises(ValueError, match="at least 12 characters"):
        hash_password(weak_password)

    encoded = hash_password(valid_password)

    assert encoded.startswith("$argon2id$")
    assert valid_password not in encoded
    assert verify_password(valid_password, encoded) is True
    assert verify_password(wrong_password, encoded) is False


def test_login_rejects_bad_credentials_without_setting_session(data_dir: Path) -> None:
    """Accepting a wrong password or issuing a cookie on failure must fail this test."""
    client, _, _ = _client(data_dir)

    with client:
        response = _login(client, _valid_password())

    assert response.status_code == 401
    assert "suseoro_session" not in response.headers.get("set-cookie", "")


def test_login_stores_only_session_and_csrf_digests_and_sets_safe_cookies(
    data_dir: Path,
) -> None:
    """Persisting bearer tokens or weakening cookie flags must fail this test."""
    client, settings, password = _client(data_dir)

    with client:
        response = _login(client, password)

    assert response.status_code == 200
    session_token = response.cookies["suseoro_session"]
    csrf_token = response.cookies["suseoro_csrf"]
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if value.startswith("suseoro_session="))
    csrf_cookie = next(value for value in cookies if value.startswith("suseoro_csrf="))
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    assert "Secure" in csrf_cookie

    with connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT token_digest, csrf_token_digest FROM sessions"
        ).fetchone()

    assert row is not None
    assert len(row["token_digest"]) == 64
    assert len(row["csrf_token_digest"]) == 64
    assert session_token not in tuple(row)
    assert csrf_token not in tuple(row)


def test_login_replays_one_audited_session_without_plaintext_bearers(data_dir: Path) -> None:
    """A duplicate login must replay one encrypted result, not mint another session."""
    client, settings, password = _client(data_dir)

    with client:
        first = _login(client, password, key="replayed-login")
        first_session = first.cookies["suseoro_session"]
        first_csrf = first.cookies["suseoro_csrf"]
        second = _login(client, password, key="replayed-login")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.cookies["suseoro_session"] == first_session
    assert second.cookies["suseoro_csrf"] == first_csrf
    with connect(settings.database_path) as connection:
        sessions = connection.execute("SELECT id FROM sessions").fetchall()
        idempotency = connection.execute(
            "SELECT response_status, response_body FROM idempotency_keys"
        ).fetchone()
        audit = connection.execute(
            """
            SELECT actor_id, school_id, action, entity_type, entity_id, request_id
            FROM audit_events WHERE action = 'AUTH_LOGIN'
            """
        ).fetchall()

    assert len(sessions) == 1
    assert idempotency is not None
    assert idempotency["response_status"] == 200
    assert first_session not in idempotency["response_body"]
    assert first_csrf not in idempotency["response_body"]
    assert password not in idempotency["response_body"]
    assert len(audit) == 1
    assert dict(audit[0]) == {
        "actor_id": OPERATOR_ID,
        "school_id": SCHOOL_ID,
        "action": "AUTH_LOGIN",
        "entity_type": "session",
        "entity_id": sessions[0]["id"],
        "request_id": REQUEST_ID,
    }


def test_login_rolls_back_reservation_and_session_when_audit_fails(data_dir: Path) -> None:
    """Committing the session before audit insertion must fail this transaction test."""
    client, settings, password = _client(data_dir)
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_login_audit
            BEFORE INSERT ON audit_events
            FOR EACH ROW WHEN NEW.action = 'AUTH_LOGIN'
            BEGIN SELECT RAISE(ABORT, 'audit rejected for transaction test'); END
            """
        )
        connection.commit()
    failure_client = TestClient(
        client.app,
        base_url="https://testserver",
        raise_server_exceptions=False,
    )

    with failure_client:
        response = _login(failure_client, password, key="rollback-login")

    assert response.status_code == 500
    with connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_logout_requires_session_bound_double_submit_csrf_and_revokes_session(
    data_dir: Path,
) -> None:
    """Skipping either CSRF proof or durable session revocation must fail this test."""
    client, settings, password = _client(data_dir)

    with client:
        login = _login(client, password)
        missing = client.post("/api/v2/auth/logout")
        csrf = login.cookies["suseoro_csrf"]
        success = client.post(
            "/api/v2/auth/logout",
            headers={
                **_mutation_headers("logout-attempt-1", LOGOUT_REQUEST_ID),
                "X-CSRF-Token": csrf,
            },
        )
        after_logout = client.get("/api/v2/auth/me")

    assert missing.status_code == 403
    assert success.status_code == 204
    assert after_logout.status_code == 401
    with connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT revoked_at FROM sessions"
        ).fetchone()["revoked_at"].endswith("Z")


def test_logout_replay_is_audited_once_after_session_revocation(data_dir: Path) -> None:
    """A duplicate attributed logout must replay despite the first call revoking its session."""
    client, settings, password = _client(data_dir)

    with client:
        login = _login(client, password)
        session_token = login.cookies["suseoro_session"]
        csrf_token = login.cookies["suseoro_csrf"]
        headers = {
            **_mutation_headers("replayed-logout", LOGOUT_REQUEST_ID),
            "X-CSRF-Token": csrf_token,
        }
        first = client.post("/api/v2/auth/logout", headers=headers)
        client.cookies.set("suseoro_session", session_token)
        client.cookies.set("suseoro_csrf", csrf_token)
        second = client.post("/api/v2/auth/logout", headers=headers)

    assert first.status_code == 204
    assert second.status_code == 204
    with connect(settings.database_path) as connection:
        audit = connection.execute(
            """
            SELECT actor_id, school_id, request_id
            FROM audit_events WHERE action = 'AUTH_LOGOUT'
            """
        ).fetchall()
        completed = connection.execute(
            """
            SELECT response_status FROM idempotency_keys
            WHERE route = 'POST /api/v2/auth/logout'
            """
        ).fetchone()

    assert len(audit) == 1
    assert dict(audit[0]) == {
        "actor_id": OPERATOR_ID,
        "school_id": SCHOOL_ID,
        "request_id": LOGOUT_REQUEST_ID,
    }
    assert completed["response_status"] == 204


def test_csrf_boundary_protects_every_mutating_route(data_dir: Path) -> None:
    """A future mutating route that forgets a local CSRF dependency must still be blocked."""
    client, _, password = _client(data_dir)

    @client.app.post("/api/v2/security-probe")
    def security_probe() -> dict[str, bool]:
        return {"changed": True}

    with client:
        login = _login(client, password)
        missing = client.post("/api/v2/security-probe")
        accepted = client.post(
            "/api/v2/security-probe",
            headers={"X-CSRF-Token": login.cookies["suseoro_csrf"]},
        )

    assert missing.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {"changed": True}


def test_session_without_csrf_binding_is_rejected(data_dir: Path) -> None:
    """A legacy session without a session-bound CSRF digest must not remain usable."""
    client, settings, _ = _client(data_dir)
    session_token = secrets.token_urlsafe(32)
    with connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                id, school_id, user_id, token_digest, csrf_token_digest,
                expires_at, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "550e8400-e29b-41d4-a716-446655440105",
                SCHOOL_ID,
                OPERATOR_ID,
                digest_token(session_token),
                "2099-08-28T12:34:56Z",
                NOW,
            ),
        )
        connection.commit()

    with client:
        client.cookies.set("suseoro_session", session_token)
        response = client.get("/api/v2/auth/me")

    assert response.status_code == 401


def test_me_exposes_school_roles_and_role_policy_denies_wrong_role(
    data_dir: Path,
) -> None:
    """Dropping school-scoped roles or allowing an operator into a reviewer probe fails."""
    client, _, password = _client(data_dir)

    with client:
        login = _login(client, password)
        me = client.get("/api/v2/auth/me")
    from suseoro.api.dependencies import AuthenticatedUser, enforce_role

    assert login.status_code == 200
    assert me.status_code == 200
    assert me.json() == {
        "id": OPERATOR_ID,
        "school_id": SCHOOL_ID,
        "username": "operator",
        "display_name": "Operator",
        "roles": ["OPERATOR"],
    }
    principal = AuthenticatedUser(
        id=OPERATOR_ID,
        school_id=SCHOOL_ID,
        username="operator",
        display_name="Operator",
        roles=("OPERATOR",),
        session_id="550e8400-e29b-41d4-a716-446655440104",
        csrf_token_digest=digest_token(secrets.token_urlsafe(32)),
    )
    with pytest.raises(Exception) as denied:
        enforce_role(principal, "REVIEWER")
    assert denied.value.status_code == 403


def test_self_approval_policy_is_enabled_only_for_single_operator_school() -> None:
    """Allowing self-approval unconditionally must fail this shared policy test."""
    from suseoro.api.dependencies import may_approve_own_change

    assert may_approve_own_change(OPERATOR_ID, REVIEWER_ID, False) is True
    assert may_approve_own_change(OPERATOR_ID, OPERATOR_ID, False) is False
    assert may_approve_own_change(OPERATOR_ID, OPERATOR_ID, True) is True


class _TestProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"test-protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        prefix = b"test-protected:"
        if not ciphertext.startswith(prefix):
            raise ValueError("invalid test ciphertext")
        return ciphertext[len(prefix) :][::-1]


def test_non_windows_secret_protection_requires_explicit_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plaintext or implicit non-Windows production fallback must fail this test."""
    monkeypatch.setattr("suseoro.security.secrets.os.name", "posix")

    with pytest.raises(RuntimeError, match="explicit secret protector"):
        MachineSecretStore()

    store = MachineSecretStore(protector=_TestProtector())
    plaintext = secrets.token_urlsafe(32)
    encrypted = store.encrypt(plaintext)
    assert encrypted != plaintext
    assert store.decrypt(encrypted) == plaintext


def test_audit_event_recursively_removes_sensitive_fields_in_callers_transaction(
    data_dir: Path,
) -> None:
    """Leaking nested credentials or committing outside the caller transaction fails."""
    settings = Settings(data_dir=data_dir)
    _seed_auth_database(settings, _valid_password())
    sensitive_value = secrets.token_urlsafe(24)
    with connect(settings.database_path) as connection:
        connection.execute("BEGIN")
        record_audit_event(
            connection,
            actor_id=OPERATOR_ID,
            school_id=SCHOOL_ID,
            action="USER_UPDATED",
            entity_type="user",
            entity_id=OPERATOR_ID,
            before={
                "display_name": "Old",
                "password": sensitive_value,
                "passphrase": sensitive_value,
            },
            after={
                "display_name": "New",
                "profile": {
                    "access_token": sensitive_value,
                    "apiKey": sensitive_value,
                    "privateKey": sensitive_value,
                    "safe": "retained",
                },
            },
            request_id=REQUEST_ID,
        )
        row = connection.execute(
            "SELECT before_json, after_json, occurred_at FROM audit_events"
        ).fetchone()
        connection.rollback()
        persisted = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    before = json.loads(row["before_json"])
    after = json.loads(row["after_json"])
    assert before == {"display_name": "Old"}
    assert after == {"display_name": "New", "profile": {"safe": "retained"}}
    assert row["occurred_at"].endswith("Z")
    assert persisted == 0


@pytest.mark.parametrize("missing_field", ["actor_id", "school_id", "request_id"])
def test_user_audit_event_requires_complete_attribution(
    data_dir: Path, missing_field: str
) -> None:
    """Persisting a user-initiated event without full attribution must fail."""
    settings = Settings(data_dir=data_dir)
    _seed_auth_database(settings, _valid_password())
    attribution = {
        "actor_id": OPERATOR_ID,
        "school_id": SCHOOL_ID,
        "request_id": REQUEST_ID,
    }
    attribution[missing_field] = None

    with (
        connect(settings.database_path) as connection,
        pytest.raises(ValueError, match=missing_field),
    ):
        record_audit_event(
            connection,
            actor_id=attribution["actor_id"],
            school_id=attribution["school_id"],
            action="USER_UPDATED",
            entity_type="user",
            entity_id=OPERATOR_ID,
            before=None,
            after={"display_name": "Changed"},
            request_id=attribution["request_id"],
        )
