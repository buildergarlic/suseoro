ALTER TABLE upload_idempotency_claims
    ADD COLUMN request_metadata_json TEXT
    CHECK (
        request_metadata_json IS NULL
        OR json_valid(request_metadata_json)
    );
