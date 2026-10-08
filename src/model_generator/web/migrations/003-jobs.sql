CREATE TABLE jobs (
 id TEXT PRIMARY KEY CHECK(id ~ '^[a-f0-9]{32}$'),
 owner_id TEXT NOT NULL REFERENCES users(id),
 upload_id TEXT UNIQUE NOT NULL REFERENCES uploads(id),
 input_kind TEXT NOT NULL CHECK(input_kind IN ('zip-fbx','portable-package')),
 region TEXT NOT NULL CHECK(region IN ('moscow','moscow-oblast')),
 procedure TEXT NOT NULL CHECK(procedure='diagnostic'),
 submission_date DATE NOT NULL,
 profile JSONB NOT NULL,
 fingerprint TEXT NOT NULL CHECK(fingerprint ~ '^[a-f0-9]{64}$'),
 input_sha256 TEXT NOT NULL CHECK(input_sha256 ~ '^[a-f0-9]{64}$'),
 state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled','interrupted','deleting','deleted')),
 stage TEXT NOT NULL CHECK(stage IN ('input_check','preview','report','done')),
 cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 2),
 created_at BIGINT NOT NULL CHECK(created_at>=0),
 updated_at BIGINT NOT NULL CHECK(updated_at>=created_at),
 deadline_at BIGINT NOT NULL CHECK(deadline_at>=created_at),
 expires_at BIGINT NOT NULL CHECK(expires_at>created_at),
 worker_epoch TEXT CHECK(worker_epoch ~ '^[a-f0-9]{32}$'),
 checkpoint JSONB,
 failure_code TEXT,
 reservation_bytes BIGINT NOT NULL CHECK(reservation_bytes>=0),
 active_reserved BOOLEAN NOT NULL DEFAULT TRUE,
 coverage JSONB NOT NULL DEFAULT '{"technical":"partial","profile":"research","procedure":"unknown","external":"not_checked"}'
);
CREATE UNIQUE INDEX jobs_one_running ON jobs((1)) WHERE state='running';
CREATE INDEX jobs_queue ON jobs(created_at,id) WHERE state='queued';
CREATE INDEX jobs_owner ON jobs(owner_id,created_at,id);
CREATE FUNCTION jobs_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.owner_id,NEW.upload_id,NEW.input_kind,NEW.region,NEW.procedure,NEW.submission_date,NEW.profile,NEW.fingerprint,NEW.input_sha256)
    IS DISTINCT FROM
    (OLD.owner_id,OLD.upload_id,OLD.input_kind,OLD.region,OLD.procedure,OLD.submission_date,OLD.profile,OLD.fingerprint,OLD.input_sha256) THEN
  RAISE EXCEPTION 'immutable job metadata';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER jobs_frozen BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION jobs_frozen();
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY CHECK(id ~ '^[a-f0-9]{32}$'),
 job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('report','preview','thumbnail')),
 bytes BIGINT NOT NULL CHECK(bytes BETWEEN 1 AND 67108864),
 sha256 TEXT NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 state TEXT NOT NULL CHECK(state IN ('staging','ready','deleted')),
 object_key TEXT UNIQUE NOT NULL,
 attempt_epoch TEXT NOT NULL CHECK(attempt_epoch ~ '^[a-f0-9]{32}$'),
 created_at BIGINT NOT NULL CHECK(created_at>=0),
 UNIQUE(job_id,kind)
);
CREATE TABLE job_object_intents (
 object_id TEXT PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES users(id),
 attempt_epoch TEXT NOT NULL CHECK(attempt_epoch ~ '^[a-f0-9]{32}$'),
 key TEXT UNIQUE NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','writing','complete','deleting','deleted')),
 multipart_id TEXT,
 reserved_bytes BIGINT NOT NULL CHECK(reserved_bytes BETWEEN 0 AND 67108864)
);
CREATE TABLE worker_state (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
 epoch TEXT NOT NULL CHECK(epoch ~ '^[a-f0-9]{32}$'),
 heartbeat BIGINT NOT NULL CHECK(heartbeat>=0),
 guard_verified BOOLEAN NOT NULL DEFAULT FALSE,
 runtime_verified BOOLEAN NOT NULL DEFAULT FALSE
);
REVOKE ALL ON jobs,artifacts,job_object_intents,worker_state FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON jobs,artifacts,job_object_intents TO mg_api,mg_worker;
GRANT SELECT ON worker_state TO mg_api;
GRANT SELECT,INSERT,UPDATE,DELETE ON worker_state TO mg_worker;
GRANT SELECT ON users,quota_scopes,usage_events TO mg_worker;
GRANT UPDATE ON uploads,object_intents,quota_scopes TO mg_worker;
GRANT INSERT ON usage_events TO mg_worker;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA mg TO mg_worker;
