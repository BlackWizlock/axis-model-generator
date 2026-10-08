ALTER TABLE uploads ADD COLUMN descriptor_version INTEGER NOT NULL DEFAULT 0
 CHECK(descriptor_version IN (0,1));
ALTER TABLE uploads ADD COLUMN input_descriptor JSONB;
ALTER TABLE uploads ADD CONSTRAINT uploads_descriptor_consistency CHECK (
 (descriptor_version=0 AND input_descriptor IS NULL) OR
 (descriptor_version=1 AND input_descriptor IS NOT NULL AND
  input_descriptor = jsonb_build_object('version',1,'kind',input_kind,
   'displayName',display_name,'bytes',declared_bytes,'sha256',sha256))
);
