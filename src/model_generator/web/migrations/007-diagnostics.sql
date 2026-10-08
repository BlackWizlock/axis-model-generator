-- Separate tables preserve predecessor positional INSERT contracts.
CREATE TABLE job_progress (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
 attempt TEXT NOT NULL CHECK(attempt ~ '^[a-f0-9]{32}$'),
 source_sha256 TEXT NOT NULL CHECK(source_sha256 ~ '^[a-f0-9]{64}$'),
 sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 10000),
 snapshot JSONB NOT NULL CHECK(octet_length(snapshot::text)<=131072),
 observed_at BIGINT NOT NULL CHECK(observed_at>=0)
);
REVOKE ALL ON job_progress FROM PUBLIC;
GRANT SELECT ON job_progress TO mg_api;
GRANT SELECT,INSERT,UPDATE,DELETE ON job_progress TO mg_worker;
