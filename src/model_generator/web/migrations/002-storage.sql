CREATE TABLE uploads (
 id TEXT PRIMARY KEY CHECK(id ~ '^[a-f0-9]{32}$'),
 owner_id TEXT NOT NULL REFERENCES users(id),
 input_kind TEXT NOT NULL CHECK(input_kind IN ('zip-fbx','portable-package')),
 display_name TEXT NOT NULL CHECK(octet_length(display_name) BETWEEN 1 AND 160),
 declared_bytes BIGINT NOT NULL CHECK(declared_bytes BETWEEN 1 AND 268435456),
 received_bytes BIGINT NOT NULL DEFAULT 0 CHECK(received_bytes>=0),
 sha256 TEXT NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 state TEXT NOT NULL CHECK(state IN ('receiving','ready','consumed','deleting','deleted')),
 reservation_bytes BIGINT NOT NULL CHECK(reservation_bytes>=0),
 active_reserved BOOLEAN NOT NULL DEFAULT TRUE,
 abort_requested BOOLEAN NOT NULL DEFAULT FALSE,
 content_claimed BOOLEAN NOT NULL DEFAULT FALSE,
 writer_epoch TEXT NOT NULL CHECK(writer_epoch ~ '^[a-f0-9]{32}$'),
 api_epoch TEXT CHECK(api_epoch ~ '^[a-f0-9]{32}$'),
 writer_started_at BIGINT CHECK(writer_started_at>=0),
 writer_heartbeat BIGINT CHECK(writer_heartbeat>=0),
 writer_closed BOOLEAN NOT NULL DEFAULT FALSE,
 created_at BIGINT NOT NULL CHECK(created_at>=0),
 expires_at BIGINT NOT NULL CHECK(expires_at>created_at),
 failure_code TEXT,
 object_key TEXT UNIQUE,
 object_bytes BIGINT CHECK(object_bytes>=0),
 object_sha256 TEXT CHECK(object_sha256 ~ '^[a-f0-9]{64}$')
);
CREATE INDEX uploads_owner_state ON uploads(owner_id,state);
CREATE TABLE object_intents (
 object_id TEXT PRIMARY KEY REFERENCES uploads(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES users(id),
 attempt_epoch TEXT NOT NULL CHECK(attempt_epoch ~ '^[a-f0-9]{32}$'),
 key TEXT UNIQUE NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','writing','complete','deleting','deleted')),
 multipart_id TEXT,
 reserved_bytes BIGINT NOT NULL CHECK(reserved_bytes>=0)
);
REVOKE ALL ON uploads,object_intents FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON uploads,object_intents TO mg_api;
GRANT SELECT ON uploads,object_intents TO mg_worker;
