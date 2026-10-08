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

    def test_schema_nine_legacy_upload_and_consent_survive_ten_twice(self):
        from model_generator.web import migrate as module
        from model_generator.web.auth import hash_password
        from web.helpers import admin_connect, secret, reset_database
        inventory=module.migrations()
        baseline={1: '3d8cf51da208cbbcd8dfc26415ea0e3c7bfbc325f3ace7af11c1c0a8292c91ac', 2: '209b73a7607a1dd17d96190f65f1fd2a786c1e269493dfbe51ed45d876f8a19a', 3: '366d302d34f7b2e0708c0062f814137b03537381bc93c777ce1a68508cca5e3f', 4: '6221d3736e54cf0367ba63a824fa27acb13079517373a16a205c874834c02ab2', 5: 'c9cf0c04324766a3f7ee3315c68aa3c80d27772ebc3331478fcc3df8be6bed1e', 6: 'b9b5e5cb415b96a2d6a16a4bc022b2825e9c17bd1a7067b7de9dc0189e86d8d1', 7: '6a7391cee1d3f1e2e18933caae5ee34678269211d447bc0a9b5bcfbd2e5b5179', 8: '66ac811d8510b2a9f71bb72090e19e2a693f95c01e06986a6d61e12adc72e30f', 9: 'b1b5ac4a4ee8cef3bd875fd3ab4ed05f72ef7681054a721f6cbcf374e96c3038'}
        self.assertEqual({v:c for v,c,_ in inventory if v<=9},baseline)
        self.assertEqual(module.EXPECTED_SCHEMA_VERSION,10)
        def empty_schema():
            with admin_connect() as admin:
                admin.execute('DROP SCHEMA mg CASCADE')
                admin.execute('CREATE SCHEMA mg AUTHORIZATION mg_migrator')
        try:
            empty_schema()
            with patch.object(module,'migrations',return_value=inventory[:9]):
                module.migrate(secret('MG_MIGRATION_DATABASE_URL'))
            owner='1'*32; id='2'*32; epoch='3'*32
            key=f'owners/{owner}/uploads/{id}/{epoch}/input.zip'
            with admin_connect() as admin:
                self.assertEqual(admin.execute('SELECT max(version) FROM mg.schema_meta').fetchone()[0],9)
                admin.execute('INSERT INTO mg.users(id,username,password_record,created_at) VALUES(%s,%s,%s,1)',(owner,'legacy',hash_password(secrets.token_urlsafe(24))))
                admin.execute("INSERT INTO mg.uploads(id,owner_id,input_kind,display_name,declared_bytes,sha256,state,reservation_bytes,writer_epoch,created_at,expires_at,object_key) VALUES(%s,%s,'zip-fbx','legacy.zip',1,%s,'ready',1,%s,1,2,%s)",(id,owner,'a'*64,epoch,key))
                admin.execute("INSERT INTO mg.object_intents VALUES(%s,%s,%s,%s,'complete',NULL,1)",(id,owner,epoch,key))
                admin.execute("INSERT INTO mg.analytics_consent_versions VALUES(1,%s,'legacy consent')",('c'*64,))
                admin.execute("INSERT INTO mg.analytics_consents VALUES(%s,1,1,2592001,NULL,94608001)",('b'*64,))
                admin.execute("INSERT INTO mg.auth_attempts VALUES('analytics-ip',%s,1,1)",('d'*64,))
            for _ in range(2): module.migrate(secret('MG_MIGRATION_DATABASE_URL'))
            with admin_connect() as admin:
                self.assertEqual(admin.execute('SELECT descriptor_version,input_descriptor,object_key FROM mg.uploads WHERE id=%s',(id,)).fetchone(),(0,None,key))
                self.assertEqual(admin.execute('SELECT key FROM mg.object_intents WHERE object_id=%s',(id,)).fetchone()[0],key)
                self.assertEqual(dict(admin.execute('SELECT version,checksum FROM mg.schema_meta WHERE version<=9').fetchall()),baseline)
                self.assertEqual(admin.execute('SELECT max(version) FROM mg.schema_meta').fetchone()[0],10)
                self.assertEqual(admin.execute('SELECT version,granted_at,expires_at,revoked_at,purge_at FROM mg.analytics_consents WHERE receipt_hash=%s',('b'*64,)).fetchone(),(1,1,2592001,None,94608001))
                self.assertEqual(admin.execute("SELECT count(*) FROM mg.auth_attempts WHERE action='analytics-ip'").fetchone()[0],1)
            db=Database(Settings(Path('/tmp/schema-proof'),'https://testserver',secrets.token_bytes(32),own_dsn()))
            try: db.check_schema()
            finally: db.close()
        finally:
            empty_schema()
            module.migrate(secret('MG_MIGRATION_DATABASE_URL'))
            reset_database()

    def test_descriptor_consistency_is_enforced_in_postgres(self):
        from web.helpers import reset_database, settings_for
        from model_generator.web.store import Storage
        from model_generator.web.auth import hash_password
        from psycopg.types.json import Jsonb
        reset_database()
        with tempfile.TemporaryDirectory() as tmp:
            settings=replace(settings_for(Path(tmp)),min_free_disk_bytes=0)
            db=Database(settings); self.addCleanup(db.close)
            owner='1'*32
            with db.transaction() as con:
                con.execute('INSERT INTO users(id,username,password_record,created_at) VALUES(%s,%s,%s,1)',(owner,'descriptor',hash_password(secrets.token_urlsafe(24))))
            row=Storage(db,settings).reserve_upload(owner,'zip-fbx','source.zip',1,'a'*64,1,descriptor_version=1)
            invalid=[None,{},dict(version=1,kind='zip-fbx',displayName='source.zip',bytes=1,sha256='a'*64,resources=[])]
            for value in invalid:
                with self.subTest(value=value),self.assertRaises(psycopg.IntegrityError):
                    with db.transaction() as con:
                        con.execute('UPDATE uploads SET input_descriptor=%s WHERE id=%s',(None if value is None else Jsonb(value),row['id']))
            with self.assertRaises(psycopg.IntegrityError):
                with db.transaction() as con:
                    con.execute('UPDATE uploads SET descriptor_version=0 WHERE id=%s',(row['id'],))
