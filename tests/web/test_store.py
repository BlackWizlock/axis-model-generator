"""Atomic reservation and private scratch contracts against own PostgreSQL."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import secrets
import tempfile
import unittest
try:
    from model_generator.web.store import Storage, atomic_write
except ModuleNotFoundError:
    Storage=atomic_write=None
from model_generator.web.db import Database
from model_generator.web.auth import hash_password
from model_generator.web.security import ApiError
from web.helpers import reset_database, settings_for

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Storage,'Private upload Storage missing')
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings=replace(settings_for(Path(self.tmp.name)),min_free_disk_bytes=0)
        self.db=Database(self.settings); self.addCleanup(self.db.close); self.store=Storage(self.db,self.settings,'a'*32)
        self.owner='1'*32; self.other='2'*32; self.now=1800000000
        record=hash_password(secrets.token_urlsafe(24))
        with self.db.transaction() as con:
            for user,name in ((self.owner,'owner'),(self.other,'other')):
                con.execute('INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)',(user,name,record,self.now))
                con.execute('INSERT INTO quota_scopes(scope,owner_id) VALUES(%s,%s)',(user,user))
    def reserve(self,owner=None,size=1):
        return self.store.reserve_upload(owner or self.owner,'zip-fbx','../../name.zip',size,hashlib.sha256(b'x'*size).hexdigest(),self.now)
    def test_atomic_same_owner_and_global_last_slot(self):
        def compete(owners,settings):
            barrier=__import__('threading').Barrier(2)
            def reserve(owner):
                db=Database(settings)
                try:
                    barrier.wait()
                    try: return Storage(db,settings,'a'*32).reserve_upload(owner,'zip-fbx','name',1,'0'*64,self.now)['id']
                    except ApiError as error: return error.status
                finally: db.close()
            with ThreadPoolExecutor(2) as pool: results=list(pool.map(reserve,owners))
            self.assertEqual(sum(isinstance(x,str) for x in results),1); self.assertIn(429,results)
        compete([self.owner,self.owner],self.settings)
        with self.db.transaction() as con:
            con.execute('DELETE FROM uploads'); con.execute('DELETE FROM usage_events WHERE action=\'upload\''); con.execute('UPDATE quota_scopes SET storage_bytes=0,active_uploads=0')
        compete([self.owner,self.other],replace(self.settings,uploads_global=1))
    def test_claim_single_use_wrong_owner_and_delete_holds_reserve(self):
        row=self.reserve(size=2)
        with self.assertRaises(ApiError) as wrong: self.store.claim_content(self.other,row['id'])
        self.assertEqual(wrong.exception.status,404)
        claimed=self.store.claim_content(self.owner,row['id'])
        with self.assertRaises(ApiError): self.store.claim_content(self.owner,row['id'])
        self.assertEqual(self.store.request_abort(self.owner,row['id'],self.now)['status'],202)
        with self.assertRaises(ApiError): self.reserve()
        with self.db.connect() as con:
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':2,'active_uploads':1})
        self.store.acknowledge_closed(self.owner,row['id'],claimed['writer_epoch'],'upload_aborted')
        self.store.acknowledge_closed(self.owner,row['id'],claimed['writer_epoch'],'upload_aborted')
        self.reserve()
        with self.db.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) AS n FROM usage_events WHERE action='upload'").fetchone()['n'],2)
    def test_size_daily_storage_limits_and_private_paths(self):
        small=Storage(self.db,replace(self.settings,upload_max_bytes=2), 'a'*32)
        self.assertEqual(small.reserve_upload(self.owner,'zip-fbx','name',2,'0'*64,self.now)['state'],'receiving')
        with self.assertRaises(ApiError) as cap: small.reserve_upload(self.owner,'zip-fbx','name',3,'0'*64,self.now)
        self.assertEqual(cap.exception.status,413)
        for kind in ('raw-rvt','blend'):
            with self.assertRaises(ApiError): small.reserve_upload(self.owner,kind,'name',1,'0'*64,self.now)
        for args in [('uploads','../x','input.part'),('uploads','a'*32,'../x'),('static','a'*32,'input.zip')]:
            with self.assertRaises(ValueError): self.store.private_path(*args)
        path=self.store.private_path('uploads','a'*32,'input.part')
        total,digest=atomic_write(path,[b'x',b'y'],2)
        self.assertEqual((total,digest),(2,hashlib.sha256(b'xy').hexdigest()))
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        path.unlink(); path.symlink_to(Path(self.tmp.name)/'outside')
        with self.assertRaises(OSError): atomic_write(path,[b'x'],2)
    def test_real_api_crash_boundaries_and_lock_proven_recovery(self):
        import subprocess,sys,json
        from web.helpers import TestClient,make_test_app
        for phase in ('before-id','before-complete','after-complete'):
            with self.subTest(phase=phase):
                result=subprocess.run([sys.executable,'-m','web.test_pipeline','--crash-worker',self.tmp.name,phase,self.owner],capture_output=True,timeout=30)
                self.assertEqual(result.returncode,17,result.stderr.decode())
                record=json.loads((Path(self.tmp.name)/'crash-record.json').read_text()); intent=self.store.intent(self.owner,record['id'])
                if phase=='after-complete': self.assertIsNotNone(self.store.objects.head(intent))
                else: self.assertIsNone(self.store.objects.head(intent))
                with self.db.connect() as con:
                    self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':1,'active_uploads':1})
                # Same api.lock inode/path, previous real process is dead. Startup owns the proof.
                with TestClient(make_test_app(self.settings),base_url='https://testserver'):
                    with self.db.connect() as con:
                        self.assertEqual(con.execute('SELECT state,reservation_bytes FROM uploads WHERE id=%s',(record['id'],)).fetchone(),{'state':'deleted','reservation_bytes':0})
                self.assertIsNone(self.store.objects.head(intent))
                with self.db.connect() as con:
                    self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':0,'active_uploads':0})
        with self.db.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) AS n FROM usage_events WHERE action='upload'").fetchone()['n'],3)
    def test_daily_abuse_event_survives_abort(self):
        limited=Storage(self.db,replace(self.settings,accepted_per_user_day=1),'a'*32)
        row=limited.reserve_upload(self.owner,'zip-fbx','synthetic',1,hashlib.sha256(b'x').hexdigest(),self.now)
        limited.delete_unused(self.owner,row['id'],self.now)
        with self.assertRaises(ApiError) as daily: limited.reserve_upload(self.owner,'zip-fbx','synthetic',1,'0'*64,self.now)
        self.assertEqual(daily.exception.status,429)
        self.assertEqual(limited.reserve_upload(self.owner,'zip-fbx','synthetic',1,'0'*64,self.now+86400)['state'],'receiving')
    def test_live_claim_is_never_reclaimed_by_heartbeat_or_different_epoch_without_lock(self):
        row=self.reserve(); claimed=self.store.claim_content(self.owner,row['id'])
        other=Storage(self.db,self.settings,'b'*32)
        other.sweep(self.now+86400,api_lock_owned=False)
        with self.db.connect() as con:
            state=con.execute('SELECT state,writer_closed,reservation_bytes FROM uploads WHERE id=%s',(row['id'],)).fetchone()
        self.assertEqual(state,{'state':'deleting','writer_closed':False,'reservation_bytes':1})
    def test_unused_ready_ttl_removes_object_and_releases_bytes_once(self):
        row=self.reserve(); self.store.claim_content(self.owner,row['id'])
        self.store.private_path('uploads',row['id'],'input.part').write_bytes(b'x')
        self.store.finish_upload(self.owner,row['id'],1,hashlib.sha256(b'x').hexdigest())
        intent=self.store.intent(self.owner,row['id'])
        self.assertIsNotNone(self.store.objects.head(intent))
        self.store.sweep(self.now+self.settings.unused_upload_seconds+1)
        self.store.sweep(self.now+self.settings.unused_upload_seconds+1)
        self.assertIsNone(self.store.objects.head(intent))
        with self.db.connect() as con:
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':0,'active_uploads':0})

    def test_explicit_s3_configuration_fail_closed_no_axis_fallback(self):
        from model_generator.web.config import StorageSettings
        from unittest.mock import patch
        import os
        settings=StorageSettings.from_env()
        for update in ({'endpoint':'http://foreign:9000'},{'proxy_url':''},{'bucket':'axis-production'},{'region':''},{'profile':'unknown'},{'access_key_file':Path('relative')}):
            with self.subTest(update=list(update)),self.assertRaises(ValueError): replace(settings,**update).validate()
        with patch.dict(os.environ,{'YC_ACCESS_KEY_FILE':str(settings.access_key_file),'YC_SECRET_KEY_FILE':str(settings.secret_key_file)},clear=True):
            with self.assertRaises((KeyError,ValueError)): StorageSettings.from_env()
        self.assertNotIn(str(settings.access_key_file),repr(settings))
        self.assertNotIn(str(settings.secret_key_file),repr(settings))

    def test_unavailable_engine_does_not_consume_quota(self):
        with self.assertRaises(ApiError) as error:
            self.store.reserve_upload(self.owner, 'rvt', 'model.rvt', 1,
                                      'a'*64, self.now, descriptor_version=1)
        self.assertEqual(error.exception.code, 'engine_unavailable')
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM uploads').fetchone()['n'], 0)
            self.assertEqual(con.execute('SELECT count(*) AS n FROM usage_events').fetchone()['n'], 0)
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(), {'storage_bytes':0,'active_uploads':0})

    def test_versioned_reservations_and_claim_use_fixed_names(self):
        for version,owner,suffix in ((0,self.owner,'input.zip'),(1,self.other,'input.bin')):
            with self.subTest(version=version):
                result=self.store.reserve_upload(owner,'zip-fbx','e\u0301.zip',1,'a'*64,self.now,descriptor_version=version)
                self.assertEqual(result['descriptorVersion'],version)
                self.assertTrue(self.store.intent(owner,result['id']).key.endswith('/'+suffix))
                claimed=self.store.claim_content(owner,result['id'])
                self.assertTrue(self.store.intent(owner,result['id']).key.endswith('/'+suffix))
                self.assertEqual(claimed['descriptor_version'],version)
                self.assertEqual(claimed['input_descriptor'],None if version==0 else
                    dict(version=1,kind='zip-fbx',displayName='é.zip',bytes=1,sha256='a'*64))
                self.assertEqual(self.store.private_path('uploads',result['id'],suffix).name,suffix)

    def test_invalid_version_fails_before_quota(self):
        for version in (True,False,-1,2,'1',None,1.0):
            with self.subTest(version=version),self.assertRaises(ApiError) as error:
                self.store.reserve_upload(self.owner,'zip-fbx','name.zip',1,'a'*64,self.now,descriptor_version=version)
            self.assertEqual(error.exception.code,'invalid_upload')
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM uploads').fetchone()['n'],0)
