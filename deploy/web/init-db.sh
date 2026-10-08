#!/bin/bash
set -eu
# Runs only in the own ephemeral PostgreSQL container.
export PGPASSWORD="$(cat /run/secrets/admin)"
export MG_INIT_BACKUP_PASSWORD="$(cat /run/secrets/backup)"
export MG_INIT_API_PASSWORD="$(cat /run/secrets/api)"
export MG_INIT_WORKER_PASSWORD="$(cat /run/secrets/worker)"
export MG_INIT_MIGRATOR_PASSWORD="$(cat /run/secrets/migrator)"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
\getenv backup MG_INIT_BACKUP_PASSWORD
\getenv api MG_INIT_API_PASSWORD
\getenv worker MG_INIT_WORKER_PASSWORD
\getenv migrator MG_INIT_MIGRATOR_PASSWORD
CREATE ROLE mg_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD
-- psql substitutes this value from the mounted generated secret file.
:'migrator';
CREATE ROLE mg_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'api';
CREATE ROLE mg_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'worker';
CREATE ROLE mg_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'backup';
CREATE DATABASE model_generator OWNER mg_migrator;
REVOKE ALL ON DATABASE model_generator FROM PUBLIC;
GRANT CONNECT ON DATABASE model_generator TO mg_api, mg_worker, mg_backup;
ALTER ROLE mg_api IN DATABASE model_generator SET search_path TO mg, pg_catalog;
ALTER ROLE mg_worker IN DATABASE model_generator SET search_path TO mg, pg_catalog;
ALTER ROLE mg_migrator IN DATABASE model_generator SET search_path TO mg, pg_catalog;
\connect model_generator
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA mg AUTHORIZATION mg_migrator;
GRANT USAGE ON SCHEMA mg TO mg_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE mg_migrator IN SCHEMA mg GRANT SELECT ON TABLES TO mg_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE mg_migrator IN SCHEMA mg GRANT SELECT ON SEQUENCES TO mg_backup;
SQL
