CREATE TABLE guest_owners (
 owner_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 ip_digest TEXT NOT NULL CHECK(ip_digest ~ '^[a-f0-9]{64}$')
);
CREATE INDEX guest_owners_ip ON guest_owners(ip_digest);
ALTER TABLE auth_attempts DROP CONSTRAINT auth_attempts_action_check;
ALTER TABLE auth_attempts ADD CONSTRAINT auth_attempts_action_check CHECK(action IN
 ('register-ip','login-ip','login-username','api-user','guest-ip','guest-day','guest-global','guest-upload','guest-job','public-ip'));
REVOKE ALL ON guest_owners FROM PUBLIC;
GRANT SELECT,INSERT,DELETE ON guest_owners TO mg_api;
GRANT SELECT ON guest_owners TO mg_worker;
CREATE FUNCTION artifact_descriptor_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.id,NEW.job_id,NEW.kind,NEW.bytes,NEW.sha256,NEW.object_key,NEW.attempt_epoch,NEW.created_at)
    IS DISTINCT FROM
    (OLD.id,OLD.job_id,OLD.kind,OLD.bytes,OLD.sha256,OLD.object_key,OLD.attempt_epoch,OLD.created_at) THEN
  RAISE EXCEPTION 'immutable artifact descriptor';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER artifact_descriptor_frozen BEFORE UPDATE ON artifacts FOR EACH ROW EXECUTE FUNCTION artifact_descriptor_frozen();
