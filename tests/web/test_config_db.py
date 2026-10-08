"""Required PostgreSQL configuration and schema contract tests."""
from dataclasses import fields, replace
import inspect
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
import psycopg
from model_generator.web.config import Settings
from model_generator.web.db import Database


def own_dsn():
    return Path(os.environ['MG_DATABASE_URL_FILE']).read_text().strip()


class ConfigDatabaseTests(unittest.TestCase):
    def test_postgres_contract_replaces_local_database(self):
        self.assertIn('database_url', {f.name for f in fields(Settings)}, 'Required own PostgreSQL Settings contract is missing')
        self.assertTrue(hasattr(Database, 'check_schema'), 'Startup must validate PostgreSQL identity/schema without migrating')
        self.assertNotIn('sqlite3', inspect.getsource(__import__('model_generator.web.db', fromlist=['Database'])))

    def test_settings_reject_insecure_or_public_storage_and_dsn(self):
        self.assertIn('database_url', {f.name for f in fields(Settings)}, 'Required own PostgreSQL Settings contract is missing')
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp), 'https://testserver', secrets.token_bytes(32), own_dsn())
            settings.validate()
            for updates in ({'public_origin': 'http://testserver'}, {'public_origin': 'https://testserver/path'}, {'auth_key': b'x'}, {'data_root': Path('relative')}, {'data_root': Path(tmp)/'public'}, {'http_inflight':65}, {'database_url':'postgresql://mg_api:synthetic@postgres/sentinel'}, {'database_url':own_dsn().replace('mg_api:', 'postgres:')}, {'database_url':own_dsn()+'&options=-csearch_path%3Dpublic'}, {'database_url':own_dsn().replace('@postgres/', '@postgres,other/')}):
                with self.subTest(fields=list(updates)), self.assertRaises(ValueError):
                    replace(settings, **updates).validate()
            self.assertNotIn(own_dsn(), repr(settings))

    def test_missing_mg_dsn_never_uses_decoy(self):
        with patch.dict(os.environ, {'DATABASE_URL':'postgresql://decoy:private@sentinel/sentinel'}, clear=True):
            with self.assertRaises(ValueError) as caught:
                Settings.from_env()
            self.assertNotIn('private', str(caught.exception))

    def test_real_postgres_gate_not_optional(self):
        with psycopg.connect(own_dsn(), autocommit=True) as con:
            self.assertEqual(con.execute('SELECT current_database(), current_user').fetchone(), ('model_generator','mg_api'))
            self.assertTrue(con.execute("SELECT version() LIKE 'PostgreSQL 16.%'").fetchone()[0])
