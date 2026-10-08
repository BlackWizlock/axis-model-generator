ALTER TABLE jobs ADD COLUMN preview_failure_code TEXT
 CHECK(preview_failure_code IS NULL OR preview_failure_code IN
 ('preview_unsupported','preview_budget','preview_roundtrip_error','preview_resource','preview_runtime_unavailable'));
ALTER TABLE worker_state ADD COLUMN runtime_version TEXT;
ALTER TABLE worker_state ADD COLUMN runtime_fingerprint JSONB;
ALTER TABLE worker_state ADD CONSTRAINT verified_runtime_identity
 CHECK(NOT runtime_verified OR (runtime_version='python-cpu-1' AND runtime_fingerprint IS NOT NULL));
