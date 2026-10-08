ALTER TABLE uploads ADD COLUMN protocol TEXT CHECK(protocol IN ('single-put','chunks-v1'));
UPDATE uploads SET protocol='single-put' WHERE content_claimed;
ALTER TABLE uploads ADD COLUMN request_epoch TEXT CHECK(request_epoch ~ '^[a-f0-9]{32}$');
ALTER TABLE uploads DROP CONSTRAINT uploads_state_check;
ALTER TABLE uploads ADD CONSTRAINT uploads_state_check CHECK(state IN ('receiving','finalizing','ready','consumed','deleting','deleted'));
CREATE TABLE upload_parts (
 upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
 part_number INTEGER NOT NULL CHECK(part_number BETWEEN 1 AND 32),
 byte_offset BIGINT NOT NULL CHECK(byte_offset=(part_number-1)::bigint*8388608),
 bytes INTEGER NOT NULL CHECK(bytes BETWEEN 1 AND 8388608),
 sha256 TEXT NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 verified BOOLEAN NOT NULL DEFAULT FALSE,
 etag TEXT CHECK(octet_length(etag) BETWEEN 1 AND 256),
 PRIMARY KEY(upload_id,part_number),
 CHECK(etag IS NULL OR verified)
);
REVOKE ALL ON upload_parts FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON upload_parts TO mg_api;
GRANT SELECT ON upload_parts TO mg_worker;
-- Kept separate: predecessor uses positional INSERT INTO service_state.
CREATE TABLE api_lock_identity (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
 service_name TEXT NOT NULL DEFAULT 'api' REFERENCES service_state(name) ON DELETE CASCADE,
 device BIGINT NOT NULL,
 inode BIGINT NOT NULL
);
REVOKE ALL ON api_lock_identity FROM PUBLIC;
GRANT SELECT,INSERT ON api_lock_identity TO mg_api;
