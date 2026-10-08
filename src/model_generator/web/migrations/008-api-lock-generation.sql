-- Legacy rows remain NULL and require the explicit physically fenced offline transition.
ALTER TABLE mg.api_lock_identity ADD COLUMN generation TEXT;
ALTER TABLE mg.api_lock_identity ADD CONSTRAINT api_lock_generation_format
    CHECK (generation IS NULL OR generation ~ '^[0-9a-f]{32}$');
