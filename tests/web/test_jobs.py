"""Real PostgreSQL/S3 API jobs and concurrency boundaries."""
from web.helpers import enter_fixture_client, close_fixture_client
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import asyncio
import json
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing
from web.helpers import admin_connect, TestClient, make_test_app, settings_for, reset_database, register_login
from model_generator.web.jobs import JobRepository
from model_generator.web.security import ApiError


class JobTests(unittest.TestCase):
    def setUp(self):
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings=settings_for(Path(self.tmp.name)); self.app=make_test_app(self.settings)
        self.client=enter_fixture_client(self,self.app)
        self.owner=register_login(self.client,'job_owner')
        self.headers={'Origin':'https://testserver','X-CSRF-Token':self.owner['csrf']}
    def upload(self,data=b'not-a-zip',kind='zip-fbx'):
        response=self.client.post('/api/uploads',json={'kind':kind,'displayName':'<synthetic>.zip','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},headers=self.headers)
        self.assertEqual(response.status_code,201,response.text)
        id=response.json()['id']
        response=self.client.put('/api/uploads/'+id+'/content',content=data,headers={**self.headers,'Content-Type':'application/octet-stream'})
        self.assertEqual(response.status_code,200,response.text)
        return id
    def create(self,id,**extra):
        return self.client.post('/api/jobs',json={'uploadId':id,'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-06',**extra},headers=self.headers)
    def test_empty_queue_does_not_lock_global_quota(self):
        # An idle poll must not touch a quota tuple held by another transaction.
        with admin_connect() as blocker:
            blocker.autocommit=False
            blocker.execute("SELECT scope FROM mg.quota_scopes WHERE scope='global' FOR UPDATE")
            started=time.monotonic()
            self.assertIsNone(self.app.state.jobs.claim_next('8'*32,int(self.app.state.clock())))
            self.assertLess(time.monotonic()-started,.3)
            blocker.rollback()

    def test_ready_single_use_owner_scoped_and_output_reserve(self):
        upload=self.upload(); response=self.create(upload)
        self.assertEqual(response.status_code,201,response.text)
        dto=response.json(); self.assertEqual(dto['coverage']['profile'],'research')
        self.assertEqual(dto['capabilities']['preview']['availability'],'unavailable')
        self.assertEqual(dto['artifacts'],[])
        self.assertEqual(self.create(upload).status_code,409)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT storage_bytes,active_jobs FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone(),{'storage_bytes':len(b'not-a-zip')+64*1024**2,'active_jobs':1})
            self.assertEqual(con.execute('SELECT state FROM uploads WHERE id=%s',(upload,)).fetchone()['state'],'consumed')
        self.assertEqual(self.client.get('/api/jobs/'+dto['id']).json(),dto)
        self.assertEqual(len(self.client.get('/api/jobs').json()['items']),1)
        with closing(TestClient(self.app,base_url='https://testserver')) as other:
            other.portal=self.client.portal  # Independent cookies, same live API event loop.
            neighbour=register_login(other,'job_neighbour')
            self.assertEqual(other.get('/api/jobs/'+dto['id']).status_code,404)
            self.assertEqual(other.post('/api/jobs',json={'uploadId':upload,'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-06'},headers={'Origin':'https://testserver','X-CSRF-Token':neighbour['csrf']}).status_code,404)
    def test_invalid_dates_unready_expiry_and_request_bounds(self):
        upload=self.upload()
        for day in ('2026-02-30','2026-2-06','not-a-date'):
            self.assertEqual(self.create(upload,submissionDate=day).status_code,422)
        self.assertEqual(self.create(upload,procedure='npm').status_code,422)
        self.app.state.clock.tick(self.settings.unused_upload_seconds)
        self.assertEqual(self.create(upload).status_code,410)
    def _job_request(self,upload):
        from starlette.requests import Request
        body=json.dumps({'uploadId':upload,'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-06'}).encode()
        async def receive():return {'type':'http.request','body':body,'more_body':False}
        headers={**self.headers,'Content-Type':'application/json',
            'Cookie':'__Host-mg_session='+self.client.cookies.get('__Host-mg_session')}
        return Request({'type':'http','method':'POST','scheme':'https','path':'/api/jobs',
            'headers':[(key.lower().encode(),value.encode()) for key,value in headers.items()],
            'app':self.app,'client':('127.0.0.1',1234),'server':('testserver',443),'query_string':b''},receive)
    def test_real_http_concurrent_jobs_hash_outside_database_budget(self):
        ids=[self.upload() for _ in range(2)]
        from model_generator.web.jobs import fingerprint
        digest=fingerprint(hashlib.sha256(b'not-a-zip').hexdigest(),'moscow','diagnostic','2026-10-06')
        barrier=threading.Barrier(2)
        def create(upload):
            barrier.wait(timeout=3)
            return self.create(upload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(create,ids))
        self.assertEqual([response.status_code for response in responses],[201,201],
            [response.text for response in responses])
        snapshot=self._job_snapshot()
        self.assertEqual(len(snapshot['jobs']),2)
        self.assertEqual(len(snapshot['usage']),2)
        self.assertEqual({row['upload_id'] for row in snapshot['jobs']},set(ids))
        self.assertTrue(all(row['fingerprint']==digest for row in snapshot['jobs']))
        self.assertTrue(all(row['reservation_bytes']==self.settings.output_reserve_bytes for row in snapshot['jobs']))
        self.assertTrue(all(row['active_jobs']==2 for row in snapshot['quota']))
        self.assertTrue(all(row['state']=='consumed' for row in snapshot['uploads']))
        self.assertEqual(self.app.state.job_fingerprint_active,0)
    def test_cancelled_hash_holds_admission_until_physical_exit(self):
        from model_generator.web.job_routes import create
        ids=[self.upload() for _ in range(3)]; before=self._job_snapshot()
        entered=threading.Event(); release=threading.Event(); exited=[]
        guard=threading.Lock(); active=0
        original=self.app.state.jobs.fingerprint_prepared
        def held(prepared):
            nonlocal active
            with guard:
                active+=1
                if active==2:entered.set()
            try:
                if not release.wait(4):raise RuntimeError('Synthetic hash barrier deadline')
                return original(prepared)
            finally:
                with guard:exited.append(prepared.upload_id)
        async def scenario():
            tasks=[asyncio.create_task(create(self._job_request(upload))) for upload in ids[:2]]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                self.assertEqual(self.app.state.job_fingerprint_active,2)
                for task in tasks:task.cancel()
                await asyncio.sleep(.03)
                # Repeated cancellation must not free a physically occupied hash slot.
                for task in tasks:task.cancel()
                await asyncio.sleep(.03)
                self.assertTrue(all(not task.done() for task in tasks))
                self.assertEqual(exited,[])
                self.assertEqual(self.app.state.job_fingerprint_active,2)
                with self.assertRaises(ApiError) as caught:await create(self._job_request(ids[2]))
                self.assertEqual((caught.exception.code,caught.exception.status),('service_busy',503))
                self.assertEqual(self._job_snapshot(),before)
            finally:
                release.set()
                results=await asyncio.gather(*tasks,return_exceptions=True)
            self.assertTrue(all(isinstance(result,asyncio.CancelledError) for result in results))
            self.assertEqual(self.app.state.job_fingerprint_active,0)
        with patch.object(self.app.state.jobs,'fingerprint_prepared',side_effect=held),patch.object(self.app.state.jobs,'create_prepared',wraps=self.app.state.jobs.create_prepared) as final:
            self.client.portal.call(scenario)
            final.assert_not_called()
        self.assertEqual(set(exited),set(ids[:2]))
        self.assertEqual(self._job_snapshot(),before)
        self.assertEqual(self.create(ids[2]).status_code,201)
        self.assertEqual(self.app.state.job_fingerprint_active,0)
    def test_prepared_job_rechecks_upload_and_fresh_expiry_without_mutation(self):
        upload=self.upload(); repo=self.app.state.jobs; now=int(self.app.state.clock())
        prepared=self.client.portal.call(self.app.state.db.run,repo.prepare_create,
            self.owner['userId'],upload,'moscow','diagnostic','2026-10-06')
        digest=repo.fingerprint_prepared(prepared)
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError):prepared.upload_id='0'*32
        with closing(TestClient(self.app,base_url='https://testserver')) as other:
            other.portal=self.client.portal
            neighbour=register_login(other,'job_prepared_neighbour')
        cases=[('sha256','f'*64,409),('object_sha256','f'*64,409),
            ('state','finalizing',409),('writer_closed',False,409),
            ('abort_requested',True,409),('owner_id',neighbour['userId'],404)]
        original=self._job_snapshot()['uploads'][0]
        for field,value,status in cases:
            with self.subTest(field=field):
                with admin_connect() as con:
                    con.execute('UPDATE mg.uploads SET '+field+'=%s WHERE id=%s',(value,upload))
                before=self._job_snapshot()
                try:
                    with self.assertRaises(ApiError) as caught:
                        self.client.portal.call(self.app.state.db.run,repo.create_prepared,prepared,digest,now)
                    self.assertEqual(caught.exception.status,status)
                    self.assertEqual(self._job_snapshot(),before)
                finally:
                    with admin_connect() as con:
                        con.execute('UPDATE mg.uploads SET '+field+'=%s WHERE id=%s',(original[field],upload))
        before=self._job_snapshot()
        for extra in ({'fingerprint':digest},{'prepared':{'uploadId':upload}}):
            self.assertEqual(self.create(upload,**extra).status_code,422)
            self.assertEqual(self._job_snapshot(),before)
        original_hash=repo.fingerprint_prepared
        def hash_then_expire(value):
            result=original_hash(value)
            self.app.state.clock.tick(self.settings.unused_upload_seconds)
            return result
        with patch.object(repo,'fingerprint_prepared',side_effect=hash_then_expire):
            self.assertEqual(self.create(upload).status_code,410)
        self.assertEqual(self._job_snapshot(),before)
    def _job_snapshot(self):
        from psycopg.rows import dict_row
        with admin_connect() as con:
            con.row_factory=dict_row
            return {
                'jobs':con.execute('SELECT * FROM mg.jobs ORDER BY id').fetchall(),
                'usage':con.execute("SELECT * FROM mg.usage_events WHERE action='job' ORDER BY id").fetchall(),
                'uploads':con.execute('SELECT * FROM mg.uploads ORDER BY id').fetchall(),
                'quota':con.execute('SELECT * FROM mg.quota_scopes ORDER BY scope').fetchall(),
            }
    def _sql_fingerprint(self):
        # These two tests isolate SQL admission from measured runtime hashing cost.
        # Compute actual runtime bytes first; never alter production fingerprinting.
        from model_generator.web.jobs import fingerprint
        args=(hashlib.sha256(b'not-a-zip').hexdigest(),'moscow','diagnostic','2026-10-06')
        value=fingerprint(*args)
        def strict(*actual,**kwargs):
            self.assertEqual(actual,args)
            self.assertEqual(kwargs,{})
            return value
        return value,patch('model_generator.web.jobs.fingerprint',side_effect=strict)
    def test_concurrent_same_upload_exactly_one_job(self):
        upload=self.upload(); now=int(self.app.state.clock())
        real_digest,sql_fingerprint=self._sql_fingerprint()
        def create():
            try:
                return self.client.portal.call(self.app.state.db.run,self.app.state.jobs.create,
                    self.owner['userId'],upload,'moscow','diagnostic','2026-10-06',now)['id']
            except ApiError as error:
                self.assertIn((error.code,error.status),
                    {('upload_conflict',409),('database_unavailable',503)})
                return error.status
        before=self._job_snapshot()
        with sql_fingerprint:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:create(),range(2)))
            winners=[item for item in results if isinstance(item,str)]
            self.assertEqual(len(winners),1,results)
            self.assertEqual(sum(item in (409,503) for item in results),1)
            accepted=self._job_snapshot()
            self.assertEqual(len(accepted['jobs']),1)
            self.assertEqual(accepted['jobs'][0]['id'],winners[0])
            self.assertEqual(accepted['jobs'][0]['upload_id'],upload)
            self.assertEqual(accepted['jobs'][0]['fingerprint'],real_digest)
            self.assertEqual(accepted['jobs'][0]['reservation_bytes'],self.settings.output_reserve_bytes)
            self.assertEqual(len(accepted['usage']),1)
            self.assertEqual(accepted['usage'][0]['bytes'],self.settings.output_reserve_bytes)
            self.assertEqual(accepted['uploads'][0]['state'],'consumed')
            self.assertEqual(len(accepted['quota']),len(before['quota']))
            for old,new in zip(before['quota'],accepted['quota']):
                expected={**old,'active_jobs':old['active_jobs']+1,
                    'storage_bytes':old['storage_bytes']+self.settings.output_reserve_bytes}
                self.assertEqual(new,expected)
            self.assertEqual(create(),409)
            self.assertEqual(self._job_snapshot(),accepted)
    def test_two_active_and_daily_quota_rollback_and_epoch_fencing(self):
        ids=[self.upload() for _ in range(3)]
        self.assertEqual(self.create(ids[0]).status_code,201)
        self.assertEqual(self.create(ids[1]).status_code,201)
        self.assertEqual(self.create(ids[2]).status_code,429)
        repo=self.app.state.jobs; epoch='a'*32
        claimed=repo.claim_next(epoch,int(self.app.state.clock()))
        self.assertIsNotNone(claimed)
        self.assertIsNone(repo.claim_next('b'*32,int(self.app.state.clock())))
        with self.assertRaises(ApiError): repo.finish(claimed['id'],'b'*32,'failed','validation_failed',int(self.app.state.clock()))
        repo.finish(claimed['id'],epoch,'failed','validation_failed',int(self.app.state.clock()))
        limited=JobRepository(self.app.state.db,replace(self.settings,accepted_per_user_day=2))
        with self.assertRaises(ApiError) as caught: limited.create(self.owner['userId'],ids[2],'moscow','diagnostic','2026-10-06',int(self.app.state.clock()))
        self.assertEqual(caught.exception.status,429)
    def test_expiry_denies_before_sweep_and_cancel_ack_bounded(self):
        dto=self.create(self.upload()).json(); start=time.monotonic()
        response=self.client.post('/api/jobs/'+dto['id']+'/cancel',headers=self.headers)
        self.assertEqual(response.status_code,202); self.assertLess(time.monotonic()-start,2)
        self.app.state.clock.tick(self.settings.retention_seconds)
        with self.assertRaises(ApiError) as caught:
            self.app.state.jobs.get_owned(self.owner['userId'],dto['id'],int(self.app.state.clock()))
        self.assertEqual(caught.exception.status,410)
    def test_concurrent_global_last_slot_and_daily_limit_roll_back_consumption(self):
        ids=[self.upload() for _ in range(2)]; now=int(self.app.state.clock())
        limited=JobRepository(self.app.state.db,replace(self.settings,jobs_global=1))
        snapshot=self._job_snapshot
        real_digest,sql_fingerprint=self._sql_fingerprint()
        def create(repo,upload):
            try:
                return self.client.portal.call(self.app.state.db.run,repo.create,
                    self.owner['userId'],upload,'moscow','diagnostic','2026-10-06',now)['id']
            except ApiError as error:
                self.assertIn((error.code,error.status),
                    {('job_limited',429),('database_unavailable',503)})
                return error.status
        with sql_fingerprint:
            before=snapshot()
            from psycopg.errors import LockNotAvailable
            original_locks=limited._locks; lock_errors=[]
            def observed_locks(*args):
                try:return original_locks(*args)
                except LockNotAvailable:
                    lock_errors.append('global-quota-lock-timeout')
                    raise
            with patch.object(limited,'_locks',side_effect=observed_locks),admin_connect() as holder:
                with holder.transaction():
                    holder.execute("SELECT scope FROM mg.quota_scopes WHERE scope='global' FOR UPDATE")
                    self.assertEqual(create(limited,ids[0]),503)
                    self.assertEqual(snapshot(),before)
            self.assertEqual(lock_errors,['global-quota-lock-timeout'])
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda upload:create(limited,upload),ids))
            winners=[item for item in results if isinstance(item,str)]
            self.assertEqual(len(winners),1,results)
            self.assertEqual(sum(item in (429,503) for item in results),1)
            winner=ids[results.index(winners[0])]; unused=ids[1-results.index(winners[0])]
            accepted=snapshot()
            self.assertEqual(len(accepted['jobs']),1)
            self.assertEqual(accepted['jobs'][0]['id'],winners[0])
            self.assertEqual(accepted['jobs'][0]['upload_id'],winner)
            self.assertEqual(accepted['jobs'][0]['fingerprint'],real_digest)
            self.assertEqual(accepted['jobs'][0]['reservation_bytes'],self.settings.output_reserve_bytes)
            self.assertEqual(len(accepted['usage']),1)
            self.assertEqual(accepted['usage'][0]['owner_id'],self.owner['userId'])
            self.assertEqual(accepted['usage'][0]['bytes'],self.settings.output_reserve_bytes)
            self.assertEqual({row['id']:row['state'] for row in accepted['uploads']},
                             {winner:'consumed',unused:'ready'})
            self.assertEqual(len(accepted['quota']),len(before['quota']))
            for old,new in zip(before['quota'],accepted['quota']):
                expected={**old,'active_jobs':old['active_jobs']+1,
                    'storage_bytes':old['storage_bytes']+self.settings.output_reserve_bytes}
                self.assertEqual(new,expected)
            self.assertEqual(create(limited,unused),429)
            self.assertEqual(snapshot(),accepted)
            claimed=limited.claim_next('7'*32,now)
            self.assertEqual(claimed['id'],winners[0])
            limited.finish(claimed['id'],'7'*32,'failed','validation_failed',now)
            finished=snapshot()
            daily=JobRepository(self.app.state.db,replace(self.settings,accepted_global_day=1))
            self.assertEqual(create(daily,unused),429)
            self.assertEqual(snapshot(),finished)
            self.assertEqual({row['id']:row['state'] for row in finished['uploads']}[unused],'ready')
