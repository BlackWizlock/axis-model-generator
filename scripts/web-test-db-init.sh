#!/bin/bash
set -eu
# Runs only in the own ephemeral PostgreSQL container.
export PGPASSWORD="$(cat /run/secrets/admin)"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres \
  -v api="$(cat /run/secrets/api)" -v worker="$(cat /run/secrets/worker)" -v migrator="$(cat /run/secrets/migrator)" <<'SQL'
CREATE ROLE mg_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD
-- psql substitutes this value from the mounted generated secret file.
:'migrator';
CREATE ROLE mg_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'api';
CREATE ROLE mg_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'worker';
CREATE DATABASE model_generator OWNER mg_migrator;
CREATE DATABASE sentinel;
REVOKE CONNECT ON DATABASE postgres, template1, sentinel FROM PUBLIC;
REVOKE ALL ON DATABASE model_generator FROM PUBLIC;
GRANT CONNECT ON DATABASE model_generator TO mg_api, mg_worker;
ALTER ROLE mg_api IN DATABASE model_generator SET search_path TO mg, pg_catalog;
ALTER ROLE mg_worker IN DATABASE model_generator SET search_path TO mg, pg_catalog;
ALTER ROLE mg_migrator IN DATABASE model_generator SET search_path TO mg, pg_catalog;
\connect model_generator
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA mg AUTHORIZATION mg_migrator;
CREATE SCHEMA sentinel;
CREATE TABLE sentinel.private_data (value TEXT);
INSERT INTO sentinel.private_data VALUES ('synthetic');
SQL
