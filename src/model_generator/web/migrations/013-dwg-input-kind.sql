ALTER TABLE uploads DROP CONSTRAINT uploads_input_kind_check;
ALTER TABLE uploads ADD CONSTRAINT uploads_input_kind_check
 CHECK(input_kind IN ('zip-fbx','portable-package','fbx','dwg'));
ALTER TABLE jobs DROP CONSTRAINT jobs_input_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_input_kind_check
 CHECK(input_kind IN ('zip-fbx','portable-package','fbx','dwg'));
ALTER TABLE uploads ADD CONSTRAINT uploads_dwg_descriptor
 CHECK(input_kind<>'dwg' OR descriptor_version=1);
