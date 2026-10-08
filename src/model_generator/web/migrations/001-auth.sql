CREATE TABLE schema_meta (
  version INTEGER PRIMARY KEY CHECK (version > 0),
  checksum TEXT NOT NULL CHECK (checksum ~ '^[a-f0-9]{64}$'),
  applied_at BIGINT NOT NULL CHECK (applied_at >= 0)
);
CREATE TABLE users (
  id TEXT PRIMARY KEY CHECK (id ~ '^[a-f0-9]{32}$'),
  username TEXT COLLATE "C" UNIQUE NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
  password_record TEXT NOT NULL CHECK (password_record ~ '^scrypt-v1\$32768\$8\$1\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{43}$'),
  created_at BIGINT NOT NULL CHECK (created_at >= 0),
  disabled BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE sessions (
  hash TEXT PRIMARY KEY CHECK (hash ~ '^[a-f0-9]{64}$'),
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_hash TEXT NOT NULL CHECK (csrf_hash ~ '^[a-f0-9]{64}$'),
  created_at BIGINT NOT NULL CHECK (created_at >= 0),
  expires_at BIGINT NOT NULL CHECK (expires_at > created_at),
  last_seen BIGINT NOT NULL CHECK (last_seen >= created_at)
);
CREATE INDEX sessions_owner ON sessions(user_id);
CREATE TABLE auth_attempts (
  action TEXT NOT NULL CHECK (action IN ('register-ip','login-ip','login-username','api-user')),
  digest TEXT NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
  window_start BIGINT NOT NULL CHECK (window_start >= 0),
  count INTEGER NOT NULL CHECK (count >= 0),
  PRIMARY KEY (action,digest,window_start)
);
CREATE TABLE usage_events (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('register','upload','job')),
  owner_id TEXT NOT NULL REFERENCES users(id),
  timestamp BIGINT NOT NULL CHECK (timestamp >= 0),
  bytes BIGINT NOT NULL DEFAULT 0 CHECK (bytes >= 0)
);
CREATE INDEX usage_day ON usage_events(action,timestamp);
CREATE TABLE quota_scopes (
  scope TEXT PRIMARY KEY CHECK (scope = 'global' OR scope ~ '^[a-f0-9]{32}$'),
  owner_id TEXT UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  registrations BIGINT NOT NULL DEFAULT 0 CHECK (registrations >= 0),
  storage_bytes BIGINT NOT NULL DEFAULT 0 CHECK (storage_bytes >= 0),
  active_uploads INTEGER NOT NULL DEFAULT 0 CHECK (active_uploads >= 0),
  active_jobs INTEGER NOT NULL DEFAULT 0 CHECK (active_jobs >= 0),
  CHECK ((scope='global' AND owner_id IS NULL) OR (scope=owner_id AND owner_id IS NOT NULL))
);
INSERT INTO quota_scopes(scope) VALUES ('global');
CREATE TABLE service_state (
  name TEXT PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{0,31}$'),
  epoch TEXT NOT NULL CHECK (epoch ~ '^[a-f0-9]{32}$'),
  started_at BIGINT NOT NULL CHECK (started_at >= 0)
);
REVOKE ALL ON ALL TABLES IN SCHEMA mg FROM PUBLIC;
GRANT USAGE ON SCHEMA mg TO mg_api, mg_worker;
GRANT SELECT ON schema_meta TO mg_api, mg_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, sessions, auth_attempts, usage_events, quota_scopes, service_state TO mg_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mg TO mg_api;
