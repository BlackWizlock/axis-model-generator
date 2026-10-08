"""Durable HTTP chunk contract against own PostgreSQL and private S3."""
import hashlib
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from web import helpers

CHUNK=8*1024**2

def digest(data): return hashlib.sha256(data).hexdigest()

class ChunkHTTPTests(unittest.TestCase):
    def setUp(self):
        helpers.reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app=helpers.make_test_app(replace(helpers.settings_for(Path(self.tmp.name)),min_free_disk_bytes=0))
        self.client=helpers.enter_fixture_client(self,self.app)
        self.user=helpers.register_login(self.client,'chunk_owner')
        self.headers={'Origin':'https://testserver','X-CSRF-Token':self.user['csrf']}
        self.store=self.app.state.storage
    def reserve(self,data,**kwargs):
        value={'kind':'zip-fbx','displayName':'source.zip','bytes':len(data),'sha256':digest(data)}; value.update(kwargs)
        response=self.client.post('/api/uploads',json=value,headers=self.headers)
        self.assertEqual(response.status_code,201,response.text)
        return response.json()['id']
    def put(self,id,number,data,**headers):
        return self.client.put(f'/api/uploads/{id}/chunks/{number}',content=data,headers={**self.headers,'Content-Type':'application/octet-stream','Upload-Offset':str((number-1)*CHUNK),'Upload-Chunk-SHA256':digest(data),**headers})
    def status(self,id):
        response=self.client.get('/api/uploads/'+id); self.assertEqual(response.status_code,200,response.text); return response.json()
    def ready(self,id):
        response=self.client.post('/api/uploads/'+id+'/complete',headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            row=self.status(id)
            if row['state']=='ready': return row
            time.sleep(.03)
        self.fail('Finalization did not become ready: '+str(row))
    def test_two_requests_durable_ledger_replay_and_final_sha(self):
        data=b'x'*CHUNK+b'tail'; id=self.reserve(data)
        first=self.put(id,1,data[:CHUNK]); self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['acknowledgedBytes'],CHUNK)
        with patch.object(self.store.objects.client,'upload_part',side_effect=AssertionError('ack replay must not write')):
            replay=self.put(id,1,data[:CHUNK]); self.assertEqual(replay.status_code,200,replay.text)
        self.assertEqual(self.put(id,2,data[CHUNK:]).status_code,200)
        row=self.ready(id); self.assertEqual(row['acknowledgedBytes'],len(data))
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT object_sha256 FROM uploads WHERE id=%s',(id,)).fetchone()['object_sha256'],digest(data))
            self.assertEqual(con.execute("SELECT count(*) AS n FROM usage_events WHERE action='upload'").fetchone()['n'],1)
    def test_validation_gap_mode_and_progress_are_private(self):
        data=b'x'*CHUNK+b'y'; id=self.reserve(data)
        self.assertEqual(self.put(id,2,b'y').status_code,409)
        self.assertEqual(self.put(id,1,data[:CHUNK],**{'Upload-Offset':'1'}).status_code,409)
        self.assertEqual(self.put(id,1,data[:CHUNK],**{'Upload-Chunk-SHA256':'F'*64}).status_code,422)
        self.assertEqual(self.put(id,1,data[:CHUNK],**{'Content-Length':'1'}).status_code,409)
        self.assertEqual(self.put(id,1,data[:CHUNK]).status_code,200)
        response=self.client.put('/api/uploads/'+id+'/content',content=data,headers={**self.headers,'Content-Type':'application/octet-stream'})
        self.assertEqual(response.status_code,409)
        row=self.status(id); self.assertEqual(row['nextPart'],2)
        self.assertFalse(set(row)&{'key','multipart_id','endpoint','object_key'})
        self.assertEqual(self.client.get('/api/uploads').json()['uploads'][0]['id'],id)
    def test_actual_chunk_hash_failure_does_not_acknowledge(self):
        id=self.reserve(b'x')
        response=self.put(id,1,b'y',**{'Upload-Chunk-SHA256':digest(b'x')})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.status(id)['acknowledgedBytes'],0)
        self.assertEqual(self.put(id,1,b'x').status_code,200)
        self.ready(id)
    def test_sql_ack_loss_requires_identical_verified_retransmission(self):
        from model_generator.web.security import ApiError
        id=self.reserve(b'x')
        with patch.object(self.store.chunks,'acknowledge',side_effect=ApiError('database_unavailable','Unavailable',503)):
            response=self.put(id,1,b'x'); self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(self.status(id)['acknowledgedBytes'],0)
        intent=self.store.intent(self.user['userId'],id)
        listed=self.store.objects.client.list_parts(**self.store.objects._key(intent.key),UploadId=intent.multipart_id)
        self.assertEqual([(p['PartNumber'],p['Size']) for p in listed['Parts']],[(1,1)])
        self.assertEqual(self.put(id,1,b'y').status_code,409)
        self.assertEqual(self.put(id,1,b'x').status_code,200)
        with self.store.db.connect() as con:
            part=con.execute('SELECT verified,etag FROM upload_parts WHERE upload_id=%s',(id,)).fetchone()
            self.assertTrue(part['verified']); self.assertEqual(part['etag'],listed['Parts'][0]['ETag'])
        self.ready(id)
    def test_response_loss_after_durable_ack_replays_without_s3(self):
        from model_generator.web.security import ApiError
        id=self.reserve(b'x'); original=self.store.chunks.acknowledge
        def lost(*args):
            original(*args); raise ApiError('database_unavailable','Unavailable',503)
        with patch.object(self.store.chunks,'acknowledge',side_effect=lost):
            self.assertEqual(self.put(id,1,b'x').status_code,503)
        self.assertEqual(self.status(id)['acknowledgedBytes'],1)
        with patch.object(self.store.objects,'upload_chunk',side_effect=AssertionError('Second write')):
            self.assertEqual(self.put(id,1,b'x').status_code,200)
        self.ready(id)
    def test_ambiguous_create_recovers_exact_stable_multipart(self):
        from model_generator.web.security import ApiError
        id=self.reserve(b'x'); create=self.store.objects.client.create_multipart_upload; ids=[]
        def lost(**kwargs):
            result=create(**kwargs); ids.append(result['UploadId']); raise ApiError('storage_unavailable','Unavailable',503)
        with patch.object(self.store.objects.client,'create_multipart_upload',side_effect=lost):
            self.assertEqual(self.put(id,1,b'x').status_code,503)
        self.assertEqual(self.status(id)['acknowledgedBytes'],0)
        self.assertEqual(self.put(id,1,b'x').status_code,200)
        self.assertEqual(self.store.intent(self.user['userId'],id).multipart_id,ids[0]); self.ready(id)
    def test_owner_before_headers_body_and_storage_and_csrf(self):
        id=self.reserve(b'x'); cookies=__import__('httpx').Cookies(self.client.cookies)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/uploads/'+id).status_code,401)
        other=helpers.register_login(self.client,'chunk_other')
        other_headers={'Origin':'https://testserver','X-CSRF-Token':other['csrf']}
        with patch.object(self.store.objects,'ensure_multipart',side_effect=AssertionError('Private S3 touched')):
            for candidate in (id,'0'*32,'invalid'):
                self.assertEqual(self.client.get('/api/uploads/'+candidate).status_code,404)
                self.assertEqual(self.client.put('/api/uploads/'+candidate+'/chunks/bad',content=b'x',headers=other_headers).status_code,404)
                self.assertEqual(self.client.post('/api/uploads/'+candidate+'/complete',headers=other_headers).status_code,404)
        self.client.cookies=cookies
        self.assertEqual(self.client.put('/api/uploads/'+id+'/chunks/1',content=b'x').status_code,403)
        self.assertEqual(self.put(id,33,b'x').status_code,409)
        self.app.state.clock.tick(3600)
        self.assertEqual(self.client.get('/api/uploads/'+id).status_code,410)
        self.assertEqual(self.client.post('/api/uploads/'+id+'/complete',headers=self.headers).status_code,410)
    def test_count_short_long_and_fixed_request_cap(self):
        id=self.reserve(b'xx')
        self.assertEqual(self.put(id,1,b'x',**{'Upload-Chunk-SHA256':digest(b'xx')}).status_code,409)
        self.assertEqual(self.put(id,1,b'xxx',**{'Content-Length':'2','Upload-Chunk-SHA256':digest(b'xx')}).status_code,409)
        self.assertEqual(self.put(id,1,b'x',**{'Content-Length':str(CHUNK+1)}).status_code,413)
        self.assertEqual(self.status(id)['acknowledgedBytes'],0)
        self.assertEqual(self.put(id,1,b'xx').status_code,200)
        self.ready(id)
    def test_same_file_resume_after_api_restart_and_staging_loss(self):
        import shutil
        data=b'x'*CHUNK+b'tail'; id=self.reserve(data)
        self.assertEqual(self.put(id,1,data[:CHUNK]).status_code,200)
        cookies=__import__('httpx').Cookies(self.client.cookies)
        helpers.close_fixture_client(self.client)
        shutil.rmtree(Path(self.tmp.name)/'uploads')
        self.app=helpers.make_test_app(self.app.state.settings)
        self.client=helpers.enter_fixture_client(self,self.app,cookies=cookies)
        self.store=self.app.state.storage
        self.assertEqual(self.status(id)['acknowledgedBytes'],CHUNK)
        pending=self.client.get('/api/uploads').json()['uploads'][0]
        self.assertEqual((pending['sha256'],pending['kind'],pending['totalBytes']),(digest(data),'zip-fbx',len(data)))
        self.assertEqual(self.put(id,2,data[CHUNK:]).status_code,200); self.ready(id)
    def test_recreated_api_lock_never_certifies_old_process_exit(self):
        from model_generator.web.app import create_app
        lock=Path(self.tmp.name)/'api.lock'; old=lock.with_name('held.lock'); lock.rename(old)
        second=create_app(self.app.state.settings)
        with self.assertRaisesRegex(RuntimeError,'API lock identity changed'):
            with helpers.TestClient(second,base_url='https://testserver'): pass
        lock.unlink(); old.rename(lock)
    def test_cancel_during_physical_sdk_holds_reservation_and_fences_ack(self):
        import threading
        entered=threading.Event(); release=threading.Event(); result=[]; original=self.store.objects.upload_chunk
        id=self.reserve(b'x')
        def blocked(*args):
            entered.set(); self.assertTrue(release.wait(4)); return original(*args)
        def put(): result.append(self.put(id,1,b'x'))
        with patch.object(self.store.objects,'upload_chunk',side_effect=blocked):
            thread=threading.Thread(target=put); thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.client.delete('/api/uploads/'+id,headers=self.headers).status_code,202)
                with self.store.db.connect() as con:
                    self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':1,'active_uploads':1})
            finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(result[0].status_code,409)
        self.client.delete('/api/uploads/'+id,headers=self.headers)
        self.client.delete('/api/uploads/'+id,headers=self.headers)
        with self.store.db.connect() as con:
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':0,'active_uploads':0})
    def test_full_hash_failure_fences_ready_and_releases_after_cleanup(self):
        id=self.reserve(b'x',sha256=digest(b'y'))
        self.assertEqual(self.put(id,1,b'x').status_code,200)
        response=self.client.post('/api/uploads/'+id+'/complete',headers=self.headers); self.assertEqual(response.status_code,202)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.status(id)['state']=='deleted': break
            time.sleep(.04)
        self.assertEqual(self.status(id)['state'],'deleted')
        with self.store.db.connect() as con:
            self.assertEqual(con.execute('SELECT object_key,reservation_bytes FROM uploads WHERE id=%s',(id,)).fetchone(),{'object_key':None,'reservation_bytes':0})
    def test_stale_request_epoch_cannot_ack_or_close_new_lease(self):
        from model_generator.web.security import ApiError
        id=self.reserve(b'x'); chunks=self.store.chunks; owner=self.user['userId']
        chunks.claim(owner,id,1,0,1,digest(b'x'),'a'*32)
        chunks.close_lease(owner,id,'a'*32)
        chunks.claim(owner,id,1,0,1,digest(b'x'),'b'*32)
        with self.assertRaises(ApiError): chunks.acknowledge(owner,id,'a'*32,1,'etag')
        chunks.close_lease(owner,id,'a'*32)
        self.assertEqual(chunks.check(owner,id,'b'*32)['request_epoch'],'b'*32)
        chunks.close_lease(owner,id,'b'*32)
    def test_range_response_bounds_close_and_incremental_full_digest(self):
        from model_generator.web.security import ApiError
        from model_generator.web.s3_store import ObjectIntent
        import io
        id=self.reserve(b'x'); intent=replace(self.store.intent(self.user['userId'],id),deadline=time.monotonic()+5)
        for response in ({'status':200,'range':'bytes 0-0/1','length':1,'data':b'x'},
                         {'status':206,'range':'bytes 0-1/2','length':1,'data':b'x'},
                         {'status':206,'range':'bytes 0-0/1','length':2,'data':b'x'},
                         {'status':206,'range':'bytes 0-0/1','length':1,'data':b''},
                         {'status':206,'range':'bytes 0-0/1','length':1,'data':b'xx'},
                         {'status':206,'range':'bytes 0-0/1','length':1,'data':b'y'}):
            body=io.BytesIO(response['data'])
            value={'ResponseMetadata':{'HTTPStatusCode':response['status']},'ContentRange':response['range'],'ContentLength':response['length'],'Body':body}
            with patch.object(self.store.objects.client,'get_object',return_value=value):
                with self.assertRaises(ApiError): self.store.objects.verify_ranges(intent,1,digest(b'x'),lambda:None)
            self.assertTrue(body.closed)
    def test_complete_returns_promptly_and_cancel_wins_during_verification(self):
        import threading
        entered=threading.Event(); release=threading.Event(); original=self.store.objects.verify_ranges
        id=self.reserve(b'x'); self.assertEqual(self.put(id,1,b'x').status_code,200)
        def blocked(*args):
            entered.set(); self.assertTrue(release.wait(4)); return original(*args)
        with patch.object(self.store.objects,'verify_ranges',side_effect=blocked):
            started=time.monotonic()
            result=self.client.post('/api/uploads/'+id+'/complete',headers=self.headers)
            self.assertEqual(result.status_code,202); self.assertLess(time.monotonic()-started,.5)
            try:
                self.assertTrue(entered.wait(3)); self.assertEqual(self.status(id)['state'],'finalizing')
                self.assertEqual(self.client.delete('/api/uploads/'+id,headers=self.headers).status_code,202)
                self.assertEqual(self.status(id)['state'],'deleting')
            finally: release.set()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.status(id)['state']=='deleted': break
            time.sleep(.04)
        self.assertEqual(self.status(id)['state'],'deleted')

    def test_degraded_startup_with_replaced_lock_cannot_reclaim_live_lease(self):
        from model_generator.web.db import Database
        id=self.reserve(b'x'); chunks=self.store.chunks; owner=self.user['userId']
        chunks.claim(owner,id,1,0,1,digest(b'x'),'c'*32)
        lock=Path(self.tmp.name)/'api.lock'; old=lock.with_name('held.lock'); lock.rename(old)
        second=helpers.make_test_app(self.app.state.settings)
        try:
            with patch.object(Database,'check_schema',side_effect=RuntimeError('Own PostgreSQL unavailable')):
                with helpers.TestClient(second,base_url='https://testserver'):
                    time.sleep(1.2)
                    with self.store.db.connect() as con:
                        row=con.execute('SELECT request_epoch FROM uploads WHERE id=%s',(id,)).fetchone()
                    self.assertEqual(row['request_epoch'],'c'*32)
        finally:
            lock.unlink(); old.rename(lock); chunks.close_lease(owner,id,'c'*32)


    def test_expiry_during_finalization_cannot_publish_and_quota_waits(self):
        import threading
        entered=threading.Event(); release=threading.Event(); original=self.store.objects.verify_ranges
        id=self.reserve(b'x'); self.assertEqual(self.put(id,1,b'x').status_code,200)
        def blocked(*args):
            entered.set(); self.assertTrue(release.wait(4)); return original(*args)
        with patch.object(self.store.objects,'verify_ranges',side_effect=blocked):
            self.assertEqual(self.client.post('/api/uploads/'+id+'/complete',headers=self.headers).status_code,202)
            try:
                self.assertTrue(entered.wait(3)); self.app.state.clock.tick(3600)
                self.assertEqual(self.client.get('/api/uploads/'+id).status_code,410)
                with self.store.db.connect() as con:
                    self.assertEqual(con.execute('SELECT reservation_bytes FROM uploads WHERE id=%s',(id,)).fetchone()['reservation_bytes'],1)
            finally: release.set()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with self.store.db.connect() as con: row=con.execute('SELECT state,object_key,reservation_bytes FROM uploads WHERE id=%s',(id,)).fetchone()
            if row['state']=='deleted': break
            time.sleep(.04)
        self.assertEqual(row,{'state':'deleted','object_key':None,'reservation_bytes':0})

    def test_range_read_error_and_total_deadline_close_body(self):
        import io
        from model_generator.web.security import ApiError
        id=self.reserve(b'x'); intent=replace(self.store.intent(self.user['userId'],id),deadline=time.monotonic()+1)
        class Broken(io.BytesIO):
            def read(self,size): raise OSError('Test broken stream')
        for body in (Broken(b'x'),io.BytesIO(b'x')):
            def get(**kwargs):
                if type(body) is io.BytesIO: time.sleep(.03)
                return {'ResponseMetadata':{'HTTPStatusCode':206},'ContentRange':'bytes 0-0/1','ContentLength':1,'Body':body}
            bounded=replace(intent,deadline=time.monotonic()+(.01 if type(body) is io.BytesIO else 1))
            with patch.object(self.store.objects.client,'get_object',side_effect=get):
                with self.assertRaises(ApiError): self.store.objects.verify_ranges(bounded,1,digest(b'x'),lambda:None)
            self.assertTrue(body.closed)


    def test_ready_inputs_are_rediscovered_until_consumed_without_s3_listing(self):
        ids=[]
        for data in (b'x',b'y',b'z'):
            id=self.reserve(data); self.assertEqual(self.put(id,1,data).status_code,200); self.ready(id); ids.append(id)
        with patch.object(self.store.objects.client,'list_parts',side_effect=AssertionError('Status must not list S3')),patch.object(self.store.objects,'head',side_effect=AssertionError('Status must not read S3')):
            rows=self.client.get('/api/uploads').json()['uploads']
            self.assertEqual({r['id'] for r in rows},set(ids))
            matched=next(r for r in rows if r['id']==ids[0])
            self.assertEqual((matched['state'],matched['sha256'],matched['totalBytes'],matched['kind']),('ready',digest(b'x'),1,'zip-fbx'))
        response=self.client.post('/api/jobs',json={'uploadId':ids[0],'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-06'},headers=self.headers)
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual({r['id'] for r in self.client.get('/api/uploads').json()['uploads']},set(ids[1:]))
        self.client.delete('/api/uploads/'+ids[1],headers=self.headers)
        self.assertEqual({r['id'] for r in self.client.get('/api/uploads').json()['uploads']},{ids[2]})
        cookies=__import__('httpx').Cookies(self.client.cookies); self.client.cookies.clear()
        try:
            self.assertEqual(self.client.get('/api/uploads').status_code,401)
            helpers.register_login(self.client,'pending_neighbour')
            self.assertEqual(self.client.get('/api/uploads').json(),{'uploads':[]})
        finally: self.client.cookies=cookies
        self.app.state.clock.tick(3600)
        self.assertEqual(self.client.get('/api/uploads').json(),{'uploads':[]})

    def test_retained_ready_inventory_cannot_starve_later_finalization(self):
        from model_generator.web.security import ApiError
        id=self.reserve(b'x'); self.assertEqual(self.put(id,1,b'x').status_code,200); self.ready(id)
        # Real PG boundary inventory. Each synthetic retained owner reserves one
        # byte; no object IO is appropriate for any nonexpired ready descriptor.
        now=int(self.app.state.clock())
        with helpers.admin_connect() as con:
            con.execute("INSERT INTO mg.users SELECT md5('retained-owner-'||n),'retained_'||n,password_record,%s,FALSE FROM generate_series(1,100) n CROSS JOIN mg.users WHERE id=%s",(now-2,self.user['userId']))
            con.execute("INSERT INTO mg.quota_scopes(scope,owner_id,storage_bytes) SELECT md5('retained-owner-'||n),md5('retained-owner-'||n),1 FROM generate_series(1,100) n")
            con.execute("INSERT INTO mg.uploads(id,owner_id,input_kind,display_name,declared_bytes,received_bytes,sha256,state,reservation_bytes,active_reserved,writer_epoch,writer_closed,created_at,expires_at,protocol) SELECT md5('retained-upload-'||n),md5('retained-owner-'||n),'zip-fbx','retained.zip',1,1,%s,'ready',1,FALSE,md5('retained-attempt-'||n),TRUE,%s,%s,'chunks-v1' FROM generate_series(1,100) n",(digest(b'x'),now-1,now+3599))
            con.execute("UPDATE mg.quota_scopes SET storage_bytes=storage_bytes+100 WHERE scope='global'")
        next_id=self.reserve(b'y'); self.assertEqual(self.put(next_id,1,b'y').status_code,200)
        with patch.object(self.store.chunks,'finalize',side_effect=ApiError('storage_unavailable','Test barrier',503)):
            self.store.chunks.schedule(self.user['userId'],next_id)
            pending=self.store.chunks.reconcile(True)
            self.assertIn((self.user['userId'],next_id),pending)

    def test_expired_ready_chunk_is_cleaned_and_released_exactly_once(self):
        id=self.reserve(b'x'); self.assertEqual(self.put(id,1,b'x').status_code,200); self.ready(id)
        intent=self.store.intent(self.user['userId'],id); self.assertIsNotNone(self.store.objects.head(intent))
        self.app.state.clock.tick(3600)
        self.store.chunks.reconcile(True); self.store.chunks.reconcile(True)
        self.assertIsNone(self.store.objects.head(intent))
        with self.store.db.connect() as con:
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':0,'active_uploads':0})


class ChunkPhysicalIOTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from model_generator.web.auth import AuthenticatedUser
        helpers.reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app=helpers.make_test_app(replace(helpers.settings_for(Path(self.tmp.name)),min_free_disk_bytes=0,upload_idle_seconds=.05))
        self.client=helpers.enter_fixture_client(self,self.app)
        identity=helpers.register_login(self.client,'physical_chunk')
        self.user=AuthenticatedUser(identity['userId'],'physical_chunk','0'*64,identity['csrf'])
        self.store=self.app.state.storage
    async def reserve(self,data):
        return await self.store.db.run(self.store.reserve_upload,self.user.id,'zip-fbx','source.zip',len(data),digest(data),int(self.app.state.clock()))
    def request(self,chunks,sha):
        from starlette.requests import Request
        iterator=chunks.__aiter__()
        async def receive():
            try: data=await iterator.__anext__()
            except StopAsyncIteration: return {'type':'http.request','body':b'','more_body':False}
            return {'type':'http.request','body':data,'more_body':True}
        return Request({'type':'http','app':self.app,'method':'PUT','path':'/','query_string':b'','headers':[(b'content-type',b'application/octet-stream'),(b'upload-offset',b'0'),(b'upload-chunk-sha256',sha.encode())]},receive)
    async def test_missing_length_disconnect_timeout_then_resume_keeps_quota_and_no_fd(self):
        import asyncio,os
        from starlette.requests import ClientDisconnect
        from model_generator.web.upload_chunks import receive_chunk
        from model_generator.web.security import ApiError
        row=await self.reserve(b'xx'); before=len(list(Path('/proc/self/fd').iterdir()))
        async def disconnected():
            yield b'x'; raise ClientDisconnect
        async def stalled():
            yield b'x'; await asyncio.sleep(.2); yield b'x'
        for source,code in ((disconnected,'upload_disconnected'),(stalled,'upload_timeout')):
            with self.assertRaises(ApiError) as caught:
                await receive_chunk(self.request(source(),digest(b'xx')),self.user,row['id'],'1',self.store)
            self.assertEqual(caught.exception.code,code)
            status=await self.store.db.run(self.store.chunks.status,self.user.id,row['id']); self.assertEqual(status['acknowledgedBytes'],0)
            self.assertFalse(list(Path(self.tmp.name).rglob('*.part')))
        async def complete(): yield b'x'; yield b'x'
        result=await receive_chunk(self.request(complete(),digest(b'xx')),self.user,row['id'],'1',self.store)
        self.assertEqual(result['acknowledgedBytes'],2)
        self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())),before+2)
        with self.store.db.connect() as con:
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':2,'active_uploads':1})
    async def test_cancellation_joins_sdk_before_releasing_physical_lease(self):
        import asyncio,threading
        from model_generator.web.upload_chunks import receive_chunk
        row=await self.reserve(b'x'); entered=threading.Event(); release=threading.Event(); exited=threading.Event()
        original=self.store.objects.upload_chunk
        def blocked(*args):
            entered.set()
            try:
                if not release.wait(4): raise RuntimeError('Test barrier deadline')
                return original(*args)
            finally: exited.set()
        async def body(): yield b'x'
        with patch.object(self.store.objects,'upload_chunk',side_effect=blocked):
            task=asyncio.create_task(receive_chunk(self.request(body(),digest(b'x')),self.user,row['id'],'1',self.store))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,2)); task.cancel(); await asyncio.sleep(.03)
                self.assertFalse(task.done()); self.assertFalse(exited.is_set())
                with self.store.db.connect() as con:
                    current=con.execute('SELECT request_epoch,writer_closed,reservation_bytes FROM uploads WHERE id=%s',(row['id'],)).fetchone()
                self.assertIsNotNone(current['request_epoch']); self.assertFalse(current['writer_closed']); self.assertEqual(current['reservation_bytes'],1)
            finally: release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(exited.is_set())
        self.assertFalse(list(Path(self.tmp.name).rglob('*.part')))
        with self.store.db.connect() as con:
            self.assertIsNone(con.execute('SELECT request_epoch FROM uploads WHERE id=%s',(row['id'],)).fetchone()['request_epoch'])


class SocketAPI:
    """Actual separate API process and independent HTTP connections, no ASGI shortcut."""
    def __init__(self,root,phase='none'):
        import socket,subprocess,sys
        self.root=Path(root); self.cookies=''; self.csrf=''
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); self.port=listener.getsockname()[1]
        self.log=(self.root/'server.log').open('ab')
        self.process=subprocess.Popen([sys.executable,'-m','web.test_upload_chunks','--server',str(root),str(self.port),phase],stdout=self.log,stderr=self.log)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if self.process.poll() is not None: raise AssertionError('API process exited: '+(self.root/'server.log').read_text())
            try:
                if self.request('GET','/health/live')[0]==200: return
            except OSError: pass
            time.sleep(.03)
        self.close(); raise AssertionError('API startup deadline')
    def request(self,method,path,data=None,headers=None,size=None,drop_response=False):
        import http.client,json
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        supplied={'Host':'testserver','Origin':'https://testserver','Cookie':self.cookies,'X-CSRF-Token':self.csrf,**(headers or {})}
        if isinstance(data,dict):
            data=json.dumps(data).encode(); supplied['Content-Type']='application/json'
        if size is None: size=len(data) if isinstance(data,bytes) else 0
        supplied['Content-Length']=str(size)
        try:
            connection.putrequest(method,path,skip_host=True,skip_accept_encoding=True)
            for key,value in supplied.items(): connection.putheader(key,value)
            connection.endheaders()
            if isinstance(data,bytes): connection.send(data)
            elif data is not None:
                for block in data: connection.send(block)
            if drop_response: return None
            response=connection.getresponse(); body=response.read(65537)
            assert len(body)<=65536
            cookie=response.getheader('Set-Cookie')
            if cookie: self.cookies=cookie.split(';',1)[0]
            return response.status,json.loads(body) if body else None
        finally: connection.close()
    def guest(self):
        status,user=self.request('POST','/api/auth/guest')
        assert status==200,(status,user)
        self.csrf=user['csrfToken']; return user
    def reserve(self,size,sha):
        status,row=self.request('POST','/api/uploads',{'kind':'zip-fbx','displayName':'synthetic.zip','bytes':size,'sha256':sha})
        assert status==201,(status,row); return row['id']
    def put(self,id,number,data,size,sha):
        return self.request('PUT',f'/api/uploads/{id}/chunks/{number}',data,{'Content-Type':'application/octet-stream','Upload-Offset':str((number-1)*CHUNK),'Upload-Chunk-SHA256':sha},size)
    def status(self,id):
        status,row=self.request('GET','/api/uploads/'+id); assert status==200,(status,row); return row
    def ready(self,id):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            row=self.status(id)
            if row['state']=='ready': return row
            time.sleep(.03)
        raise AssertionError(row)
    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(5)
            except __import__('subprocess').TimeoutExpired:
                self.process.kill(); self.process.wait(2)
        self.log.close()


def run_server(root,port,phase):
    import os,uvicorn
    from model_generator.web.chunk_uploads import ChunkUploads
    from model_generator.web.s3_store import ObjectStore
    if phase in ('before-complete','after-complete'):
        original=ObjectStore.complete_chunks
        def boundary(self,*args):
            if phase=='before-complete': os._exit(17)
            original(self,*args); os._exit(17)
        ObjectStore.complete_chunks=boundary
    if phase in ('before-ready','after-ready'):
        original=ChunkUploads.ready
        def boundary(self,*args):
            if phase=='before-ready': os._exit(17)
            original(self,*args); os._exit(17)
        ChunkUploads.ready=boundary
    app=helpers.make_test_app(replace(helpers.settings_for(Path(root)),min_free_disk_bytes=0))
    uvicorn.run(app,host='127.0.0.1',port=int(port),access_log=False,log_level='warning')


class PhysicalRecoveryTests(unittest.TestCase):
    def test_api_death_at_completion_boundaries_recovers_from_s3_not_scratch(self):
        import shutil
        for phase in ('before-complete','after-complete','before-ready','after-ready'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as root:
                helpers.reset_database(); api=SocketAPI(root,phase)
                try:
                    api.guest(); id=api.reserve(1,digest(b'x'))
                    self.assertEqual(api.put(id,1,b'x',1,digest(b'x'))[0],200)
                    cookies,csrf=api.cookies,api.csrf
                    self.assertEqual(api.request('POST','/api/uploads/'+id+'/complete')[0],202)
                    self.assertEqual(api.process.wait(8),17)
                finally: api.close()
                shutil.rmtree(Path(root)/'uploads')
                api=SocketAPI(root); api.cookies,api.csrf=cookies,csrf
                try:
                    self.assertEqual(api.ready(id)['acknowledgedBytes'],1)
                    self.assertEqual(api.request('DELETE','/api/uploads/'+id)[0],204)
                finally: api.close()


def memory_probe():
    import os,signal,threading
    signal.alarm(180)
    helpers.reset_database()
    with tempfile.TemporaryDirectory() as root:
        api=SocketAPI(root)
        try:
            api.guest()
            warm=api.reserve(1,digest(b'x')); assert api.put(warm,1,b'x',1,digest(b'x'))[0]==200
            assert api.request('POST','/api/uploads/'+warm+'/complete')[0]==202; api.ready(warm)
            assert api.request('DELETE','/api/uploads/'+warm)[0]==204
            block=b'x'*65536; partsha=hashlib.sha256(); fullsha=hashlib.sha256()
            for _ in range(CHUNK//len(block)): partsha.update(block)
            for _ in range(64*1024**2//len(block)): fullsha.update(block)
            total=64*1024**2; id=api.reserve(total,fullsha.hexdigest())
            def rss(): return int(Path(f'/proc/{api.process.pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
            base=rss(); peak=[base]; stop=threading.Event()
            def sample():
                while not stop.wait(.005): peak[0]=max(peak[0],rss())
            thread=threading.Thread(target=sample); thread.start()
            try:
                for number in range(1,9):
                    status,row=api.put(id,number,(block for _ in range(128)),CHUNK,partsha.hexdigest())
                    assert status==200,(status,row)
                    assert row['acknowledgedBytes']==number*CHUNK
                    assert api.status(id)['acknowledgedBytes']==number*CHUNK
                assert api.request('POST','/api/uploads/'+id+'/complete')[0]==202
                assert api.ready(id)['acknowledgedBytes']==total
            finally: stop.set(); thread.join()
            growth=peak[0]-base
            assert growth<16*1024**2,(base,peak[0],growth)
            print(f'Actual HTTP API child RSS base={base} peak={peak[0]} growth={growth}<16MiB; eight independent 8MiB PUTs=67108864 bytes; actual fullSHA verified.',flush=True)
        finally: api.close()


def restart_probe():
    """Host stops own PG/S3 while durable parts and guest cookie survive."""
    import shutil,signal
    signal.alarm(180); helpers.reset_database()
    state=Path('/proof/state'); command=Path('/proof/command')
    def signal_state(value):
        tmp=state.with_suffix('.tmp'); tmp.write_text(value); tmp.replace(state)
    def wait(value):
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if command.exists() and command.read_text()==value: return
            time.sleep(.05)
        raise AssertionError('Restart command deadline')
    with tempfile.TemporaryDirectory() as root:
        api=SocketAPI(root)
        try:
            api.guest(); part=b'x'*65536; sha=hashlib.sha256()
            for _ in range(128): sha.update(part)
            full=sha.copy(); full.update(b'y')
            id=api.reserve(CHUNK+1,full.hexdigest())
            assert api.put(id,1,(part for _ in range(128)),CHUNK,sha.hexdigest())[0]==200
            cookies,csrf=api.cookies,api.csrf; signal_state('chunks-seeded'); wait('chunks-offline')
            assert api.request('GET','/api/uploads/'+id)[0]==503
            signal_state('chunks-offline-passed'); wait('chunks-recovered')
            assert api.status(id)['acknowledgedBytes']==CHUNK
        finally: api.close()
        shutil.rmtree(Path(root)/'uploads')
        api=SocketAPI(root); api.cookies,api.csrf=cookies,csrf
        try:
            assert api.status(id)['acknowledgedBytes']==CHUNK
            assert api.put(id,2,b'y',1,digest(b'y'))[0]==200
            assert api.request('POST','/api/uploads/'+id+'/complete')[0]==202
            api.ready(id); signal_state('chunks-restart-passed'); wait('chunks-finish')
        finally: api.close()
    print('Actual PG/S3 restart + new API process + lost staging recovered acknowledged chunks, cookie, final SHA and ready.',flush=True)


if __name__=='__main__':
    import sys
    if sys.argv[1:2]==['--server']: run_server(*sys.argv[2:])
    elif sys.argv[1:]==['--memory-probe']: memory_probe()
    elif sys.argv[1:]==['--restart-probe']: restart_probe()
    else: unittest.main()
