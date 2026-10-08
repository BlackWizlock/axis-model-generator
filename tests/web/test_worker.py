"""Actual guarded child, durable diagnostics and measured resource termination."""
from web.helpers import enter_fixture_client, close_fixture_client
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from model_generator.web.worker import run_child, run_once, worker_lease, recover, sweep
from model_generator.web.db import Database
from web.helpers import secret,admin_connect
from web import test_jobs as job_helpers


def preview_budget_cases():
    from fixtures.package_builders import make_package,make_scene
    scene=make_scene()
    second=json.loads(json.dumps(scene['instances'][0]))
    second['instance_id']='second'; second['element_key']['unique_id']='second'
    scene['instances'].append(second)
    single=make_package(); double=make_package(scene_updates=scene)
    return [('vertices',{'preview_max_vertices':2},single),
            ('triangles',{'preview_max_triangles':1},double),
            ('instances',{'preview_max_instances':1},double),
            ('wire',{'preview_max_bytes':1},single)]


class WorkerTests(unittest.TestCase):
    upload=job_helpers.JobTests.upload
    create=job_helpers.JobTests.create
    def setUp(self):
        from web.helpers import reset_database,settings_for,make_test_app,TestClient,register_login
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings=settings_for(Path(self.tmp.name)); self.app=make_test_app(self.settings)
        self.app.state.clock.now=int(time.time())
        self.client=enter_fixture_client(self,self.app)
        self.owner=register_login(self.client,'job_owner')
        self.headers={'Origin':'https://testserver','X-CSRF-Token':self.owner['csrf']}
    def worker_settings(self):
        return replace(self.settings,db_role='mg_worker',database_url=secret('MG_TEST_WORKER_DATABASE_URL'))
    def kill_dedicated_lease(self, db, *, wait=True):
        # Only the actual advisory-lock backend dies; API/S3/PostgreSQL stay live.
        with admin_connect() as con:
            rows=con.execute("SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted").fetchall()
            self.assertEqual(len(rows),1)
            self.assertTrue(con.execute('SELECT pg_terminate_backend(%s)',(rows[0][0],)).fetchone()[0])
        if wait: self.assertTrue(db.worker_failed.wait(7),'Lease heartbeat did not detect backend loss')

    def test_healthy_quota_lock_wait_overlapping_heartbeat_keeps_worker_alive(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        heartbeat_ready=threading.Event(); heartbeat_go=threading.Event(); failures=[]
        original=Database._worker_connect
        @contextmanager
        def coordinated(database,*args,**kwargs):
            if threading.current_thread().name=='mg-worker-lease':
                heartbeat_ready.set()
                if not heartbeat_go.wait(3): raise RuntimeError('Synthetic heartbeat barrier timeout')
            with original(database,*args,**kwargs) as con: yield con
        def compute():
            try: run_once(db,settings,'9'*32)
            except Exception as error: failures.append(error)
        compute_thread=None
        try:
            with patch.object(Database,'_worker_connect',coordinated),worker_lease(db,settings,'9'*32):
                self.assertTrue(heartbeat_ready.wait(1))
                with admin_connect() as blocker:
                    blocker.autocommit=False
                    blocker.execute("SELECT scope FROM mg.quota_scopes WHERE scope='global' FOR UPDATE")
                    compute_thread=threading.Thread(target=compute)
                    compute_thread.start()
                    with admin_connect() as observer:
                        deadline=time.monotonic()+1
                        while time.monotonic()<deadline:
                            row=observer.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid IN (SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted)").fetchone()
                            if row and row[0]=='Lock': break
                            time.sleep(.005)
                        else: self.fail('Worker did not enter actual PostgreSQL quota-lock wait')
                    heartbeat_go.set()
                    # Below the preserved 500ms lock limit, above heartbeat's
                    # former 200ms mutex limit: no backend or service is lost.
                    time.sleep(.35)
                    blocker.commit()
                compute_thread.join(5)
                self.assertFalse(compute_thread.is_alive())
                self.assertEqual(failures,[])
                self.assertFalse(db.worker_failed.is_set())
                with db.connect() as con:
                    self.assertEqual(con.execute('SELECT state FROM jobs WHERE id=%s',(dto['id'],)).fetchone()['state'],'completed')
        finally:
            heartbeat_go.set()
            if compute_thread: compute_thread.join(5)
            db.close()

    def test_dedicated_backend_loss_during_post_child_multipart_fences_report(self):
        from unittest.mock import patch
        import psycopg
        from model_generator.web.s3_store import ObjectStore
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        original=ObjectStore._put_file
        def transfer(objects,intent,path,persist):
            killed=False
            def callback(multipart):
                nonlocal killed
                if not killed:
                    killed=True; self.kill_dedicated_lease(db)
                persist(multipart)
            return original(objects,intent,path,callback)
        try:
            with self.assertRaises((RuntimeError,psycopg.Error)):
                with worker_lease(db,settings,'7'*32),patch.object(ObjectStore,'_put_file',transfer):
                    with self.assertRaises((RuntimeError,psycopg.Error)):
                        run_once(db,settings,'7'*32)
            with admin_connect() as con:
                row=con.execute('SELECT state,checkpoint,reservation_bytes FROM mg.jobs WHERE id=%s',(dto['id'],)).fetchone()
                self.assertEqual(row,('running',None,64*1024**2))
                self.assertEqual(con.execute("SELECT count(*) FROM mg.artifacts WHERE job_id=%s AND state='ready'",(dto['id'],)).fetchone()[0],0)
            self.assertEqual(self.client.get('/api/jobs/'+dto['id']).status_code,200)
        finally: db.close()

    def test_dedicated_backend_loss_before_terminal_commit_rolls_back_completion(self):
        from unittest.mock import patch
        import psycopg
        from model_generator.web.db import _BoundConnection
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        original=_BoundConnection.commit
        killed=False
        def commit(connection):
            nonlocal killed
            row=connection.execute('SELECT state FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
            if row and row['state']=='completed' and not killed:
                killed=True; self.kill_dedicated_lease(db,wait=False)
            return original(connection)
        try:
            with self.assertRaises((RuntimeError,psycopg.Error)):
                with worker_lease(db,settings,'8'*32),patch.object(_BoundConnection,'commit',commit):
                    with self.assertRaises((RuntimeError,psycopg.Error)):
                        run_once(db,settings,'8'*32)
            with admin_connect() as con:
                row=con.execute('SELECT state,checkpoint FROM mg.jobs WHERE id=%s',(dto['id'],)).fetchone()
                self.assertEqual(row[0],'running')
                self.assertIsNotNone(row[1])
            self.assertEqual(self.client.get('/api/jobs/'+dto['id']).status_code,200)
        finally: db.close()
    def test_real_malformed_child_report_completed_and_durable(self):
        dto=self.create(self.upload()).json()
        # Worker and HTTP use actual UTC acceptance/deadline during compute.
        real=int(time.time())
        settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'c'*32):
                self.assertTrue(run_once(db,settings,'c'*32))
            with db.connect() as con:
                job=con.execute('SELECT * FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                self.assertEqual(job['state'],'completed',job)
                report=con.execute("SELECT * FROM artifacts WHERE job_id=%s AND kind='report'",(dto['id'],)).fetchone()
                self.assertEqual(report['state'],'ready')
            result=self.app.state.jobs.get_owned(self.owner['userId'],dto['id'],real)
            self.assertEqual(len(result['artifacts']),1)
            self.assertFalse(list((settings.data_root/'jobs'/dto['id']).glob('*/report.json')))
            artifact=result['artifacts'][0]
            response=self.client.get(artifact['url'])
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(hashlib.sha256(response.content).hexdigest(),artifact['sha256'])
            self.assertEqual(self.client.get(artifact['url'],headers={'Range':'bytes=0-10'}).status_code,416)
            with db.connect() as con:
                quota=con.execute('SELECT storage_bytes,active_jobs FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone()
                self.assertEqual(quota,{'storage_bytes':len(b'not-a-zip')+artifact['bytes'],'active_jobs':0})
            future=real+settings.retention_seconds+1
            sweep(db,settings,future); sweep(db,settings,future)
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT storage_bytes FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone()['storage_bytes'],0)
        finally: db.close()

    def test_real_portable_render_double_checkpoint_private_artifacts_and_quota(self):
        from fixtures.package_builders import make_package
        from model_generator.web.s3_store import ObjectStore,ObjectDescriptor
        from model_generator.web.preview import decode_preview_json,PreviewLimits
        wire=make_package(); dto=self.create(self.upload(wire,'portable-package')).json()
        settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'a'*32): self.assertTrue(run_once(db,settings,'a'*32))
            with db.connect() as con:
                job=con.execute('SELECT * FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                artifacts=con.execute('SELECT * FROM artifacts WHERE job_id=%s ORDER BY kind',(dto['id'],)).fetchall()
                quota=con.execute('SELECT storage_bytes FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone()['storage_bytes']
            self.assertEqual(job['state'],'completed')
            self.assertEqual(set(job['checkpoint']),{'schema','fingerprint','inputHash','artifact','previewInput','thumbnail'})
            self.assertEqual(job['checkpoint']['inputHash'],hashlib.sha256(wire).hexdigest())
            self.assertEqual({a['kind'] for a in artifacts},{'report','preview','thumbnail'})
            self.assertTrue(all(a['state']=='ready' for a in artifacts))
            self.assertTrue(all(a['object_key'].startswith(f"owners/{self.owner['userId']}/jobs/{dto['id']}/") for a in artifacts))
            self.assertEqual(quota,len(wire)+sum(a['bytes'] for a in artifacts))
            objects=ObjectStore(settings.storage)
            item=next(a for a in artifacts if a['kind']=='preview')
            with objects.open_stream(ObjectDescriptor(item['object_key'],item['bytes'],item['sha256'],'application/json')) as stream:
                preview=decode_preview_json(stream.read(item['bytes']+1),PreviewLimits())
            self.assertEqual((preview['vertexCount'],preview['triangleCount']),(3,1))
            result=self.app.state.jobs.get_owned(self.owner['userId'],dto['id'],int(time.time()))
            self.assertEqual(result['capabilities']['preview'],{'availability':'available','reason':None})
            self.assertEqual(result['coverage']['technical'],'partial')
            self.assertEqual(result['coverage']['profile'],'research')
            self.assertTrue(all(result['capabilities'][kind]['availability']=='unavailable' for kind in ('npm','vpm','ifc')))
        finally: db.close()

    def test_reduced_preview_limits_keep_durable_report_and_exact_quota(self):
        from model_generator.package_validator import validate_package_path
        for name,updates,wire in preview_budget_cases():
            with self.subTest(limit=name):
                dto=self.create(self.upload(wire,'portable-package')).json()
                settings=replace(self.worker_settings(),**updates); settings.validate()
                db=Database(settings)
                try:
                    with worker_lease(db,settings,'f'*32): self.assertTrue(run_once(db,settings,'f'*32))
                    with db.connect() as con:
                        job=con.execute('SELECT * FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                        artifacts=con.execute('SELECT * FROM artifacts WHERE job_id=%s',(dto['id'],)).fetchall()
                        quota=con.execute('SELECT storage_bytes,active_jobs FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone()
                        global_quota=con.execute("SELECT storage_bytes,active_jobs FROM quota_scopes WHERE scope='global'").fetchone()
                    self.assertEqual((job['state'],job['failure_code'],job['preview_failure_code']),('completed',None,'preview_budget'))
                    self.assertEqual([(item['kind'],item['state']) for item in artifacts],[('report','ready')])
                    self.assertNotIn('previewInput',job['checkpoint']); self.assertNotIn('thumbnail',job['checkpoint'])
                    response=self.client.get(f"/api/jobs/{dto['id']}/artifacts/{artifacts[0]['id']}")
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(len(response.content),artifacts[0]['bytes'])
                    self.assertEqual(hashlib.sha256(response.content).hexdigest(),artifacts[0]['sha256'])
                    report=response.json()
                    with tempfile.TemporaryDirectory() as tmp:
                        path=Path(tmp)/'input.zip'; path.write_bytes(wire)
                        expected=validate_package_path(path).coverage
                    self.assertEqual(report['coverage'],expected)
                    actual=self.client.get('/api/jobs/'+dto['id']).json()
                    self.assertEqual(actual['capabilities']['preview'],{'availability':'unavailable','reason':'preview_budget'})
                    self.assertEqual(actual['coverage'],{key:expected[key] for key in actual['coverage']})
                    self.assertEqual(quota['active_jobs'],0)
                    with db.connect() as con:
                        retained=con.execute('SELECT COALESCE(sum(bytes),0) AS bytes FROM artifacts WHERE job_id IN (SELECT id FROM jobs WHERE owner_id=%s)',(self.owner['userId'],)).fetchone()['bytes']
                        input_bytes=con.execute("SELECT COALESCE(sum(object_bytes),0) AS bytes FROM uploads WHERE owner_id=%s AND state='consumed'",(self.owner['userId'],)).fetchone()['bytes']
                    self.assertEqual(quota['storage_bytes'],input_bytes+retained)
                    self.assertEqual(global_quota,quota)
                finally: db.close()

    def test_double_checkpoint_preview_hash_change_revalidates_both(self):
        from unittest.mock import patch
        from fixtures.package_builders import make_package
        from psycopg.types.json import Jsonb
        dto=self.create(self.upload(make_package(),'portable-package')).json()
        settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'b'*32):
                with patch('model_generator.web.worker._preview_stage',side_effect=RuntimeError('Synthetic render boundary crash')):
                    with self.assertRaises(RuntimeError): run_once(db,settings,'b'*32)
            with admin_connect() as con:
                checkpoint=con.execute('SELECT checkpoint FROM mg.jobs WHERE id=%s',(dto['id'],)).fetchone()[0]
                self.assertIn('previewInput',checkpoint)
                checkpoint['previewInput']['sha256']='0'*64
                con.execute('UPDATE mg.jobs SET checkpoint=%s WHERE id=%s',(Jsonb(checkpoint),dto['id']))
            with worker_lease(db,settings,'c'*32):
                recover(db,settings,'c'*32,int(time.time()))
                self.assertTrue(run_once(db,settings,'c'*32))
            with db.connect() as con:
                job=con.execute('SELECT * FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
            self.assertEqual((job['state'],job['attempts']),('completed',2))
            self.assertNotEqual(job['checkpoint']['previewInput']['sha256'],'0'*64)
            self.assertIn('thumbnail',job['checkpoint'])
        finally: db.close()

    def test_double_checkpoint_resumes_without_reparsing_and_failed_preview_preserves_report(self):
        from unittest.mock import patch
        from fixtures.package_builders import make_package
        from model_generator.web.preview import PreviewError
        dto=self.create(self.upload(make_package(),'portable-package')).json()
        settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'d'*32):
                with patch('model_generator.web.worker._preview_stage',side_effect=RuntimeError('Synthetic render boundary crash')):
                    with self.assertRaises(RuntimeError): run_once(db,settings,'d'*32)
            with worker_lease(db,settings,'e'*32):
                recover(db,settings,'e'*32,int(time.time()))
                with (patch('model_generator.web.worker.run_child',side_effect=AssertionError('Recovered input was reparsed')),
                      patch('model_generator.web.worker.run_preview',side_effect=PreviewError('preview_runtime_unavailable','Unavailable'))):
                    self.assertTrue(run_once(db,settings,'e'*32))
            result=self.app.state.jobs.get_owned(self.owner['userId'],dto['id'],int(time.time()))
            self.assertEqual(result['state'],'completed')
            self.assertEqual(result['capabilities']['preview'],{'availability':'unavailable','reason':'preview_runtime_unavailable'})
            self.assertEqual([a['kind'] for a in result['artifacts']],['report'])
            with db.connect() as con:
                job=con.execute('SELECT reservation_bytes FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                used=con.execute('SELECT sum(bytes) AS bytes FROM artifacts WHERE job_id=%s',(dto['id'],)).fetchone()['bytes']
            self.assertEqual(job['reservation_bytes'],used)
        finally: db.close()

    def test_preview_and_thumbnail_publication_cancel_cas_never_exposes_partial_preview(self):
        from unittest.mock import patch
        from fixtures.package_builders import make_package
        from model_generator.web.s3_store import ObjectStore
        for suffix in ('preview-input.json','thumbnail.png'):
            with self.subTest(suffix=suffix):
                dto=self.create(self.upload(make_package(),'portable-package')).json()
                settings=self.worker_settings(); db=Database(settings)
                original=ObjectStore._put_file; cancelled=False
                def transfer(objects,intent,path,persist):
                    nonlocal cancelled
                    def callback(multipart):
                        nonlocal cancelled
                        if intent.key.endswith(suffix) and not cancelled:
                            cancelled=True
                            self.app.state.jobs.cancel(self.owner['userId'],dto['id'],int(time.time()))
                        persist(multipart)
                    return original(objects,intent,path,callback)
                try:
                    with worker_lease(db,settings,'f'*32),patch.object(ObjectStore,'_put_file',transfer):
                        self.assertTrue(run_once(db,settings,'f'*32))
                    self.assertTrue(cancelled)
                    with db.connect() as con:
                        job=con.execute('SELECT state,checkpoint FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                        previews=con.execute("SELECT count(*) AS count FROM artifacts WHERE job_id=%s AND kind IN ('preview','thumbnail') AND state='ready'",(dto['id'],)).fetchone()['count']
                    self.assertEqual(job['state'],'cancelled')
                    self.assertEqual(previews,0)
                    result=self.app.state.jobs.get_owned(self.owner['userId'],dto['id'],int(time.time()))
                    self.assertEqual(result['artifacts'],[])
                finally: db.close()

    def test_actual_preflight_heartbeat_identity_and_readiness_are_bound(self):
        from model_generator.web.preview_runner import preflight_preview
        from psycopg.types.json import Jsonb
        # The real worker heartbeat advances wall time during native preflight.
        self.app.state.clock=time.time
        settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'6'*32):
                db.worker_guard_verified.set()
                db.worker_runtime_identity=preflight_preview(settings)['identity']
                deadline=time.monotonic()+7
                while time.monotonic()<deadline:
                    response=self.client.get('/health/ready')
                    if response.status_code==200: break
                    time.sleep(.05)
                else: self.fail('Actual verified runtime heartbeat never became ready: '+response.text)
                from unittest.mock import patch
                from model_generator.web.preview import PreviewError
                with patch('model_generator.web.preview_runner.installed_preview_fingerprint',side_effect=PreviewError('preview_runtime_unavailable','Synthetic missing runtime')):
                    response=self.client.get('/health/ready')
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json()['reason'],'runtime_not_verified')
                with admin_connect() as con:
                    con.execute('UPDATE mg.worker_state SET runtime_fingerprint=%s',(Jsonb({'engine':'untrusted'}),))
                response=self.client.get('/health/ready')
                self.assertEqual(response.status_code,503)
                self.assertEqual(response.json()['reason'],'runtime_not_verified')
        finally: db.close()
    def test_second_independent_worker_cannot_claim_under_held_lease(self):
        dto=self.create(self.upload()).json(); settings=self.worker_settings()
        first=Database(settings); second=Database(settings)
        try:
            with worker_lease(first,settings,'d'*32):
                with self.assertRaises((BlockingIOError,RuntimeError)):
                    with worker_lease(second,settings,'e'*32): self.fail('Second lease entered')
                separate_settings=replace(settings,data_root=settings.data_root/'independent-worker')
                separate=Database(separate_settings)
                try:
                    with self.assertRaises(RuntimeError):
                        with worker_lease(separate,separate_settings,'6'*32): self.fail('Second PostgreSQL lease entered')
                finally: separate.close()
                with first.connect() as con:
                    self.assertEqual(con.execute('SELECT state,attempts FROM jobs WHERE id=%s',(dto['id'],)).fetchone(),{'state':'queued','attempts':0})
        finally: first.close(); second.close()
    def test_orphan_output_cleanup_holds_quota_until_confirmed_storage_cleanup(self):
        from unittest.mock import patch
        from model_generator.web.security import ApiError
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        try:
            epoch='f'*32; job=self.app.state.jobs.claim_next(epoch,int(time.time()))
            self.app.state.jobs.finish(job['id'],epoch,'failed','validation_failed',int(time.time()))
            now=int(time.time())+901
            with patch('model_generator.web.worker._clean_artifacts',side_effect=ApiError('storage_unavailable','Unavailable.',503)):
                with self.assertRaises(ApiError): sweep(db,settings,now)
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT reservation_bytes FROM jobs WHERE id=%s',(dto['id'],)).fetchone()['reservation_bytes'],64*1024**2)
            sweep(db,settings,now); sweep(db,settings,now)
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT storage_bytes FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone()['storage_bytes'],len(b'not-a-zip'))
        finally: db.close()
    def test_stale_checkpoint_hash_revalidates_and_second_attempt_is_last(self):
        from unittest.mock import patch
        from psycopg.types.json import Jsonb
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'1'*32):
                with patch('model_generator.web.worker.JobRepository.finish',side_effect=RuntimeError('Synthetic post-checkpoint crash')):
                    with self.assertRaises(RuntimeError): run_once(db,settings,'1'*32)
            with admin_connect() as con:
                checkpoint=con.execute('SELECT checkpoint FROM mg.jobs WHERE id=%s',(dto['id'],)).fetchone()[0]
                checkpoint['artifact']['sha256']='0'*64
                con.execute('UPDATE mg.jobs SET checkpoint=%s WHERE id=%s',(Jsonb(checkpoint),dto['id']))
            with worker_lease(db,settings,'2'*32):
                recover(db,settings,'2'*32,int(time.time()))
                self.assertTrue(run_once(db,settings,'2'*32))
            with db.connect() as con:
                job=con.execute('SELECT state,attempts,checkpoint FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                self.assertEqual((job['state'],job['attempts']),('completed',2))
                self.assertNotEqual(job['checkpoint']['artifact']['sha256'],'0'*64)
        finally: db.close()
    def test_valid_checkpoint_recovery_preserves_json_and_downloads_again(self):
        from unittest.mock import patch
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'3'*32):
                with patch('model_generator.web.worker.JobRepository.finish',side_effect=RuntimeError('Synthetic post-checkpoint crash')):
                    with self.assertRaises(RuntimeError): run_once(db,settings,'3'*32)
            with db.connect() as con:
                original=con.execute('SELECT checkpoint FROM jobs WHERE id=%s',(dto['id'],)).fetchone()['checkpoint']
            with worker_lease(db,settings,'4'*32):
                recover(db,settings,'4'*32,int(time.time()))
                self.assertTrue(run_once(db,settings,'4'*32))
            with db.connect() as con:
                job=con.execute('SELECT state,attempts,checkpoint FROM jobs WHERE id=%s',(dto['id'],)).fetchone()
                self.assertEqual((job['state'],job['attempts']),('completed',2))
                self.assertEqual(job['checkpoint'],original)
        finally: db.close()


class ChildControlTests(unittest.TestCase):
    def test_compressed_oversized_member_fails_inside_guarded_child(self):
        import zipfile
        from web.helpers import settings_for
        with tempfile.TemporaryDirectory() as tmp:
            scratch=Path(tmp)
            with zipfile.ZipFile(scratch/'input.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
                with archive.open('synthetic.fbx','w') as stream:
                    for _ in range(65): stream.write(b'0'*1024**2)
            self.assertLess((scratch/'input.zip').stat().st_size,100000)
            start=time.monotonic()
            result,code=run_child(settings_for(scratch),scratch,'zip-fbx',lambda:False)
            self.assertIsNone(code)
            self.assertTrue(any(f['status']=='fail' for f in result['report']['findings']))
            self.assertLess(time.monotonic()-start,10)
    def test_guarded_portable_only_strict_success_has_checked_preview(self):
        from fixtures.package_builders import make_package
        from web.helpers import settings_for
        for data,passed in ((make_package(),True),(b'not-a-zip',False)):
            with self.subTest(passed=passed),tempfile.TemporaryDirectory() as tmp:
                scratch=Path(tmp); (scratch/'input.zip').write_bytes(data)
                result,code=run_child(settings_for(scratch),scratch,'portable-package',lambda:False)
                self.assertIsNone(code)
                if passed:
                    item=result['message']['previewInput']
                    self.assertEqual(item['kind'],'preview')
                    wire=(scratch/'preview-input.json').read_bytes()
                    self.assertEqual(hashlib.sha256(wire).hexdigest(),item['sha256'])
                    from model_generator.web.preview import decode_preview_json,PreviewLimits
                    preview=decode_preview_json(wire,PreviewLimits())
                    self.assertEqual((preview['vertexCount'],preview['triangleCount']),(3,1))
                else:
                    self.assertIsNone(result['message']['previewInput'])
                    self.assertEqual(result['message']['failureCode'],'preview_unsupported')
                self.assertEqual(any(f['status']=='fail' for f in result['report']['findings']),not passed)
    def test_reduced_preview_limits_guarded_child_keeps_report(self):
        from model_generator.package_validator import validate_package_path
        from web.helpers import settings_for
        for name,updates,wire in preview_budget_cases():
            with self.subTest(limit=name),tempfile.TemporaryDirectory() as tmp:
                scratch=Path(tmp); path=scratch/'input.zip'; path.write_bytes(wire)
                expected=validate_package_path(path).coverage
                settings=replace(settings_for(scratch),**updates); settings.validate()
                result,code=run_child(settings,scratch,'portable-package',lambda:False)
                self.assertIsNone(code,result)
                self.assertIsNotNone(result)
                self.assertIsNone(result['message']['previewInput'])
                self.assertEqual(result['message']['failureCode'],'preview_budget')
                self.assertEqual(result['report']['coverage'],expected)
                self.assertEqual(json.loads((scratch/'report.json').read_bytes()),result['report'])
                self.assertFalse((scratch/'preview-input.json').exists())
                self.assertFalse((scratch/'thumbnail.png').exists())

    def test_real_guard_positive_input_and_all_negative_controls(self):
        from web.helpers import settings_for
        with tempfile.TemporaryDirectory() as tmp:
            scratch=Path(tmp); (scratch/'input.zip').write_bytes(b'not-a-zip')
            result,code=run_child(settings_for(scratch),scratch,'zip-fbx',lambda:False,probe='guard')
            self.assertIsNone(code,result)
            proof=json.loads((scratch/'guard.json').read_text())
            self.assertTrue(proof['ownInput']); self.assertIn('io_uring_setup',proof['denied']); self.assertIn('parentfd',proof['denied'])
    def test_cpu_address_space_wall_crash_and_oversized_are_physical_failures(self):
        from web.helpers import settings_for
        for mode in ('cpu','memory','wall','crash','oversized','oversized_file'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                scratch=Path(tmp); (scratch/'input.zip').write_bytes(b'not-a-zip')
                settings=replace(settings_for(scratch),validation_cpu_seconds=1,validation_wall_seconds=3,validation_memory_bytes=128*1024**2)
                start=time.monotonic(); result,code=run_child(settings,scratch,'zip-fbx',lambda:False,probe=mode)
                self.assertIsNone(result); self.assertIn(code,{'validation_resource','validation_failed'})
                self.assertLess(time.monotonic()-start,10)
                if mode=='oversized_file': self.assertLessEqual((scratch/'report.json').stat().st_size,4*1024**2)
    def test_cancel_kills_reaps_stubborn_descendant_under_30_seconds(self):
        import ctypes
        self.assertEqual(ctypes.CDLL(None).prctl(36,1,0,0,0),0)
        from web.helpers import settings_for
        with tempfile.TemporaryDirectory() as tmp:
            scratch=Path(tmp); (scratch/'input.zip').write_bytes(b'not-a-zip'); start=time.monotonic()
            result,code=run_child(settings_for(scratch),scratch,'zip-fbx',lambda:(scratch/'descendant.pid').exists(),probe='descendant')
            self.assertIsNone(result); self.assertEqual(code,'cancelled'); self.assertLess(time.monotonic()-start,30)
            pid=int((scratch/'descendant.pid').read_text())
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)
