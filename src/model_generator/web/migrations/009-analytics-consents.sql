-- Analytics proof has no relationship to owners, models, sessions or usage journals.
CREATE TABLE mg.analytics_consent_versions (
 version INTEGER PRIMARY KEY CHECK(version>0),
 document_sha256 TEXT NOT NULL CHECK(document_sha256 ~ '^[a-f0-9]{64}$'),
 content_text TEXT NOT NULL CHECK(octet_length(content_text)>0 AND octet_length(content_text)<=65536)
);
CREATE TABLE mg.analytics_consents (
 receipt_hash TEXT PRIMARY KEY CHECK(receipt_hash ~ '^[a-f0-9]{64}$'),
 version INTEGER NOT NULL REFERENCES mg.analytics_consent_versions(version),
 granted_at BIGINT NOT NULL CHECK(granted_at>=0),
 expires_at BIGINT NOT NULL CHECK(expires_at=granted_at+2592000),
 revoked_at BIGINT CHECK(revoked_at IS NULL OR revoked_at>=granted_at),
 purge_at BIGINT NOT NULL CHECK(purge_at=granted_at+94608000)
);
CREATE INDEX analytics_consents_purge ON mg.analytics_consents(purge_at);
CREATE FUNCTION mg.analytics_consent_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.receipt_hash,NEW.version,NEW.granted_at,NEW.expires_at,NEW.purge_at)
    IS DISTINCT FROM (OLD.receipt_hash,OLD.version,OLD.granted_at,OLD.expires_at,OLD.purge_at)
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
  RAISE EXCEPTION 'immutable analytics consent proof';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER analytics_consent_frozen BEFORE UPDATE ON mg.analytics_consents
 FOR EACH ROW EXECUTE FUNCTION mg.analytics_consent_frozen();
REVOKE ALL ON mg.analytics_consent_versions,mg.analytics_consents FROM PUBLIC;
GRANT SELECT,INSERT ON mg.analytics_consent_versions TO mg_api;
GRANT SELECT,INSERT,UPDATE,DELETE ON mg.analytics_consents TO mg_api;
ALTER TABLE mg.auth_attempts DROP CONSTRAINT auth_attempts_action_check;
ALTER TABLE mg.auth_attempts ADD CONSTRAINT auth_attempts_action_check CHECK(action IN
 ('register-ip','login-ip','login-username','api-user','guest-ip','guest-day','guest-global',
  'guest-upload','guest-job','public-ip','analytics-ip','analytics-grant','analytics-global'));
