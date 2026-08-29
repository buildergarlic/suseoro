-- Legacy error_json is untrusted. Only mapping payloads written through the
-- current server path receive this separately stored provenance marker.
ALTER TABLE job_file_results
    ADD COLUMN public_mapping_payload_version INTEGER
        CHECK (
            public_mapping_payload_version IS NULL
            OR public_mapping_payload_version = 1
        );
