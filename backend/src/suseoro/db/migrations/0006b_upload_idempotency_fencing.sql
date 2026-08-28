CREATE TABLE upload_idempotency_claims (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL REFERENCES schools(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    route TEXT NOT NULL CHECK (length(route) > 0),
    key TEXT NOT NULL CHECK (length(key) > 0),
    request_fingerprint TEXT NOT NULL CHECK (
        length(request_fingerprint) = 64
        AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_owner TEXT,
    lease_expires_at TEXT,
    state TEXT NOT NULL CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
    response_status INTEGER CHECK (
        response_status IS NULL OR response_status BETWEEN 100 AND 599
    ),
    response_body TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (school_id, actor_id, route, key),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (state = 'IN_PROGRESS' AND response_status IS NULL AND response_body IS NULL)
        OR (
            state = 'COMPLETED'
            AND response_status IS NOT NULL
            AND response_body IS NOT NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
    )
);

CREATE INDEX idx_upload_idempotency_claims_lease
    ON upload_idempotency_claims(state, lease_expires_at);

CREATE TRIGGER validate_upload_idempotency_claims_insert
BEFORE INSERT ON upload_idempotency_claims
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.school_id GLOB '????????-????-????-????-????????????' AND NEW.school_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.actor_id GLOB '????????-????-????-????-????????????' AND NEW.actor_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR (NEW.lease_owner IS NOT NULL AND NOT (NEW.lease_owner GLOB '????????-????-????-????-????????????' AND NEW.lease_owner NOT GLOB '*[^0-9A-Fa-f-]*'))
    OR (NEW.lease_expires_at IS NOT NULL AND NOT ((NEW.lease_expires_at GLOB '????-??-??T??:??:??Z' OR NEW.lease_expires_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.lease_expires_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.lease_expires_at) = substr(NEW.lease_expires_at, 1, 19)))
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR NOT ((NEW.updated_at GLOB '????-??-??T??:??:??Z' OR NEW.updated_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.updated_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.updated_at) = substr(NEW.updated_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid upload idempotency claim identifier or UTC timestamp'); END;

CREATE TRIGGER validate_upload_idempotency_claims_update
BEFORE UPDATE ON upload_idempotency_claims
FOR EACH ROW WHEN
    NOT (NEW.id GLOB '????????-????-????-????-????????????' AND NEW.id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.school_id GLOB '????????-????-????-????-????????????' AND NEW.school_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR NOT (NEW.actor_id GLOB '????????-????-????-????-????????????' AND NEW.actor_id NOT GLOB '*[^0-9A-Fa-f-]*')
    OR (NEW.lease_owner IS NOT NULL AND NOT (NEW.lease_owner GLOB '????????-????-????-????-????????????' AND NEW.lease_owner NOT GLOB '*[^0-9A-Fa-f-]*'))
    OR (NEW.lease_expires_at IS NOT NULL AND NOT ((NEW.lease_expires_at GLOB '????-??-??T??:??:??Z' OR NEW.lease_expires_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.lease_expires_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.lease_expires_at) = substr(NEW.lease_expires_at, 1, 19)))
    OR NOT ((NEW.created_at GLOB '????-??-??T??:??:??Z' OR NEW.created_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.created_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.created_at) = substr(NEW.created_at, 1, 19))
    OR NOT ((NEW.updated_at GLOB '????-??-??T??:??:??Z' OR NEW.updated_at GLOB '????-??-??T??:??:??.[0-9]*Z') AND NEW.updated_at NOT GLOB '*[^0-9T:.Z-]*' AND strftime('%Y-%m-%dT%H:%M:%S', NEW.updated_at) = substr(NEW.updated_at, 1, 19))
BEGIN SELECT RAISE(ABORT, 'invalid upload idempotency claim identifier or UTC timestamp'); END;
