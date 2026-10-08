"""Mandatory streamed private uploads, no parser in HTTP."""
from web.helpers import enter_fixture_client, close_fixture_client
import unittest
try:
    from model_generator.web.uploads import receive_content
except ModuleNotFoundError:
    receive_content=None

class UploadContractTests(unittest.TestCase):
    def test_incremental_receiver_contract(self):
        self.assertIsNotNone(receive_content,'Bounded stream receiver missing')
        import inspect
        source=inspect.getsource(receive_content)
        self.assertNotIn('request.body(',source)
        self.assertNotIn('request.json(',source)

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch
from starlette.requests import Request,ClientDisconnect
from model_generator.web.auth import AuthenticatedUser
from model_generator.web.security import ApiError
from web.helpers import TestClient,make_test_app,register_login,reset_database,settings_for

class UploadHTTPTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(receive_content,'Bounded stream receiver missing')
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app=make_test_app(replace(settings_for(Path(self.tmp.name)),min_free_disk_bytes=0,upload_max_bytes=1048576))
        self.client=enter_fixture_client(self,self.app)
        self.user=register_login(self.client,'stream_owner')
        self.headers={'Origin':'https://testserver','X-CSRF-Token':self.user['csrf']}
    def reserve(self,data,**overrides):
        body={'kind':'zip-fbx','displayName':'../../synthetic.zip','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()};body.update(overrides)
        return self.client.post('/api/uploads',json=body,headers=self.headers)
    def put(self,id,data,**headers):
        return self.client.put('/api/uploads/'+id+'/content',content=data,headers={**self.headers,'Content-Type':'application/octet-stream',**headers})
    def test_ready_hash_private_object_delete_and_no_download(self):
        data=b'x'*1048576; row=self.reserve(data)
        self.assertEqual(row.status_code,201,row.text); id=row.json()['id']
        done=self.put(id,data); self.assertEqual(done.status_code,200,done.text)
        self.assertEqual(done.json(),{'id':id,'state':'ready','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        self.assertEqual(self.put(id,data).status_code,409)
        self.assertEqual(self.client.get('/api/uploads/'+id).status_code,200)
        self.assertEqual(self.client.delete('/api/uploads/'+id,headers=self.headers).status_code,204)
        self.assertEqual(self.client.delete('/api/uploads/'+id,headers=self.headers).status_code,204)
    def test_auth_csrf_type_and_exact_dto_before_receive(self):
        row=self.reserve(b'x').json(); id=row['id']
        self.assertEqual(self.client.put('/api/uploads/'+id+'/content',content=b'x').status_code,403)
        self.assertEqual(self.client.put('/api/uploads/'+id+'/content',content=b'x',headers=self.headers).status_code,415)
        self.assertEqual(self.reserve(b'x',kind='raw-rvt').status_code,422)
        self.assertEqual(self.reserve(b'x',extra=True).status_code,422)
        self.assertEqual(self.reserve(b'x',bytes=True).status_code,422)
        self.assertEqual(self.reserve(b'x',displayName='bad\x00name').status_code,422)
        self.assertEqual(self.reserve(b'x',displayName='é'*81).status_code,422)
        self.assertEqual(self.reserve(b'x',bytes=1048577).status_code,413)
    def test_hash_truncation_and_dishonest_lengths_cleanup(self):
        for data,actual,headers,code in ((b'xy',b'x',{},'upload_size_mismatch'),(b'x',b'y',{},'upload_hash_mismatch'),(b'x',b'xy',{'Content-Length':'1'},'upload_size_mismatch'),(b'x',b'x',{'Content-Length':'2'},'upload_size_mismatch')):
            with self.subTest(code=code,headers=headers):
                row=self.reserve(data).json(); result=self.put(row['id'],actual,**headers)
                self.assertEqual(result.status_code,409,result.text); self.assertEqual(result.json()['error']['code'],code)
                self.assertNotIn(self.tmp.name,result.text)
                self.assertFalse(list(Path(self.tmp.name).rglob('*.part')))
                with self.app.state.db.connect() as con:
                    quota=con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone()
                    self.assertEqual(quota,{'storage_bytes':0,'active_uploads':0})

    def test_other_owner_mutations_match_unknown_and_missing_storage_fails_closed(self):
        row=self.reserve(b'x').json()
        saved_cookies=__import__('httpx').Cookies(self.client.cookies)
        other_client=self.client
        self.client.cookies.clear()
        try:
            other=register_login(other_client,'stream_other')
            headers={'Origin':'https://testserver','X-CSRF-Token':other['csrf'],'Content-Type':'application/octet-stream'}
            for id in (row['id'],'0'*32):
                self.assertEqual(other_client.put('/api/uploads/'+id+'/content',content=b'x',headers=headers).status_code,404)
                self.assertEqual(other_client.delete('/api/uploads/'+id,headers=headers).status_code,404)
        finally: self.client.cookies=saved_cookies
        before=self.app.state.storage.objects
        self.app.state.storage.objects=None
        try:
            self.assertEqual(self.reserve(b'y').status_code,503)
            self.assertEqual(self.put(row['id'],b'x').status_code,503)
            self.assertEqual(self.client.delete('/api/uploads/'+row['id'],headers=self.headers).status_code,503)
        finally: self.app.state.storage.objects=before
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT reservation_bytes FROM uploads WHERE id=%s',(row['id'],)).fetchone()['reservation_bytes'],1)

class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(receive_content,'Bounded stream receiver missing')
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings=replace(settings_for(Path(self.tmp.name)),min_free_disk_bytes=0,upload_idle_seconds=.05,upload_wall_seconds=30)
        self.app=make_test_app(self.settings); self.client=enter_fixture_client(self,self.app)
        identity=register_login(self.client,'raw_owner'); self.owner=identity['userId']
        self.user=AuthenticatedUser(self.owner,'raw_owner','0'*64,identity['csrf'])
        self.store=self.app.state.storage
    def assert_cleanup_outcome(self,final,cleanup_failures,release_observations):
        if final['state']=='deleted':
            self.assertEqual(final,{'state':'deleted','reservation_bytes':0,'writer_closed':True})
            self.assertTrue(release_observations,'Successful cleanup must invoke the real quota release')
        else:
            self.assertEqual(final,{'state':'deleting','reservation_bytes':1,'writer_closed':True})
            self.assertTrue(cleanup_failures,'Held reserve requires an observed real cleanup failure')
        self.assertNotIn(True,release_observations,'Quota released while physical producer IO was active')

    async def reserve(self,data):
        return await self.app.state.db.run(self.store.reserve_upload,self.owner,'zip-fbx','synthetic.zip',len(data),hashlib.sha256(data).hexdigest(),int(self.app.state.clock()))
    def request(self,chunks,headers=()):
        iterator=chunks.__aiter__()
        async def receive():
            try: data=await iterator.__anext__()
            except StopAsyncIteration: return {'type':'http.request','body':b'','more_body':False}
            return {'type':'http.request','body':data,'more_body':True}
        req=Request({'type':'http','app':self.app,'method':'PUT','path':'/','headers':list(headers),'query_string':b''},receive)
        async def forbidden(*args): raise AssertionError('Whole-body receive forbidden')
        req.body=req.json=forbidden
        return req
    async def assert_released(self,id):
        def inspect():
            with self.app.state.db.connect() as con:
                return con.execute('SELECT state,reservation_bytes,writer_closed FROM uploads WHERE id=%s',(id,)).fetchone()
        row=await self.app.state.db.run(inspect)
        self.assertEqual(row,{'state':'deleted','reservation_bytes':0,'writer_closed':True})
        self.assertFalse(list(Path(self.tmp.name).rglob('*.part')))
    async def test_sixteen_chunks_unknown_length(self):
        data=b'x'*(16*65536); row=await self.reserve(data)
        async def chunks():
            for _ in range(16): yield b'x'*65536
        result=await receive_content(self.request(chunks()),self.user,row['id'],self.store)
        self.assertEqual(result['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(result['bytes'],len(data))
    async def test_disconnect_idle_and_symlink_cleanup(self):
        for mode in ('disconnect','idle','symlink'):
            row=await self.reserve(b'x'*4)
            async def chunks():
                yield b'x'; yield b'x'
                if mode=='disconnect': raise ClientDisconnect()
                if mode=='idle': await asyncio.sleep(.1)
                yield b'xx'
            if mode=='symlink': self.store.private_path('uploads',row['id'],'input.part').symlink_to(Path(self.tmp.name)/'outside')
            with self.assertRaises((ApiError,ClientDisconnect)):
                await receive_content(self.request(chunks()),self.user,row['id'],self.store)
            await self.assert_released(row['id'])
    async def test_enospc_before_s3_and_actual_overflow_before_extra_write(self):
        row=await self.reserve(b'x')
        async def chunks(): yield b'x'
        with patch('model_generator.web.uploads.write_all',side_effect=OSError(28,'synthetic ENOSPC')):
            with self.assertRaises(ApiError) as failed:
                await receive_content(self.request(chunks()),self.user,row['id'],self.store)
            self.assertEqual(failed.exception.status,507)
        await self.assert_released(row['id'])
    async def test_cancel_blocked_thread_holds_fd_and_quota_until_close(self):
        import threading
        from model_generator.web.uploads import write_all
        row=await self.reserve(b'x'*4); entered=threading.Event(); unblock=threading.Event(); descriptors=[]
        def blocked(fd,chunk):
            descriptors.append(fd); entered.set(); unblock.wait(5); write_all(fd,chunk)
        async def chunks(): yield b'xxxx'
        with patch('model_generator.web.uploads.write_all',side_effect=blocked):
            task=asyncio.create_task(receive_content(self.request(chunks()),self.user,row['id'],self.store))
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            result=await self.app.state.db.run(self.store.request_abort,self.owner,row['id'],int(self.app.state.clock()))
            self.assertEqual(result['status'],202)
            task.cancel(); await asyncio.sleep(.03); self.assertFalse(task.done())
            with self.assertRaises(ApiError): await self.reserve(b'x')
            __import__('os').fstat(descriptors[0])
            unblock.set()
            with self.assertRaises(asyncio.CancelledError): await task
        with self.assertRaises(OSError): __import__('os').fstat(descriptors[0])
        await self.assert_released(row['id']); await self.reserve(b'x')

    async def test_wall_deadline_clock_and_size_overflow_write_zero_bytes(self):
        from types import SimpleNamespace
        row=await self.reserve(b'x'); clock=[0.0]
        async def chunks():
            clock[0]=31; yield b'x'
        with patch('model_generator.web.uploads.time',SimpleNamespace(monotonic=lambda:clock[0])):
            with self.assertRaises(ApiError) as error:
                await receive_content(self.request(chunks()),self.user,row['id'],self.store)
            self.assertEqual(error.exception.status,408)
        await self.assert_released(row['id'])
        row=await self.reserve(b'x')
        async def overflow(): yield b'xy'
        with patch('model_generator.web.uploads.write_all',side_effect=AssertionError('Extra byte written')):
            with self.assertRaises(ApiError) as error:
                await receive_content(self.request(overflow()),self.user,row['id'],self.store)
            self.assertEqual(error.exception.code,'upload_size_mismatch')
        await self.assert_released(row['id'])
    async def test_expiry_during_s3_complete_cannot_publish_ready(self):
        row=await self.reserve(b'x'); intent=await self.app.state.db.run(self.store.claim_content,self.owner,row['id'])
        path=self.store.private_path('uploads',row['id'],'input.part'); path.write_bytes(b'x')
        object_intent=await self.app.state.db.run(self.store.intent,self.owner,row['id'])
        descriptor=await asyncio.to_thread(self.store.objects.put_file,object_intent,path,lambda id:None)
        self.app.state.clock.tick(self.settings.unused_upload_seconds+1)
        with self.assertRaises(ApiError) as expired:
            await self.app.state.db.run(self.store._commit_ready,self.owner,row['id'],object_intent,descriptor,1,hashlib.sha256(b'x').hexdigest())
        self.assertEqual(expired.exception.status,410)
    async def test_cancel_after_s3_complete_prevents_late_ready(self):
        row=await self.reserve(b'x'); await self.app.state.db.run(self.store.claim_content,self.owner,row['id'])
        path=self.store.private_path('uploads',row['id'],'input.part'); path.write_bytes(b'x')
        intent=await self.app.state.db.run(self.store.intent,self.owner,row['id'])
        descriptor=await asyncio.to_thread(self.store.objects.put_file,intent,path,lambda id:None)
        def check():
            with self.app.state.db.connect() as con:
                return con.execute('SELECT state,object_key FROM uploads WHERE id=%s',(row['id'],)).fetchone()
        self.assertEqual(await self.app.state.db.run(check),{'state':'receiving','object_key':None})
        await self.app.state.db.run(self.store.request_abort,self.owner,row['id'],int(self.app.state.clock()))
        with self.assertRaises(ApiError): await self.app.state.db.run(self.store._commit_ready,self.owner,row['id'],intent,descriptor,1,descriptor.sha256)
        from model_generator.web.uploads import owned_io
        await owned_io(self.store,self.store.acknowledge_closed,self.owner,row['id'],intent.attempt_epoch,None)
        self.assertIsNone(await asyncio.to_thread(self.store.objects.head,intent))
        await self.assert_released(row['id'])
    async def test_s3_failed_delete_retains_quota_then_retry_sweep_exactly_once(self):
        from web.helpers import PostgreSQLProxy
        from model_generator.web.s3_store import ObjectStore
        from model_generator.web.uploads import owned_io
        row=await self.reserve(b'x')
        async def chunks(): yield b'x'
        await receive_content(self.request(chunks()),self.user,row['id'],self.store)
        intent=await self.app.state.db.run(self.store.intent,self.owner,row['id'])
        proxy=PostgreSQLProxy('s3-proxy',8080,pause_on=b'DELETE ')
        original=self.store.objects
        self.store.objects=ObjectStore(replace(self.settings.storage,proxy_url=f'http://127.0.0.1:{proxy.address[1]}'))
        try:
            with self.assertRaises(ApiError): await owned_io(self.store,self.store.delete_unused,self.owner,row['id'],int(self.app.state.clock()))
            with self.app.state.db.connect() as con:
                quota=con.execute("SELECT storage_bytes FROM quota_scopes WHERE scope='global'").fetchone()['storage_bytes']
            self.assertEqual(quota,1)
        finally:
            self.store.objects=original; await asyncio.to_thread(proxy.close)
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await self.assert_released(row['id']); self.assertIsNone(await asyncio.to_thread(self.store.objects.head,intent))

    async def test_db_commit_failure_after_fd_close_is_retried_without_restart(self):
        from web.helpers import admin_connect
        from model_generator.web.uploads import owned_io
        row=await self.reserve(b'x')
        async def chunks(): yield b'y'
        with admin_connect() as admin:
            admin.execute("CREATE FUNCTION mg.test_storage_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$")
            admin.execute("CREATE CONSTRAINT TRIGGER test_storage_commit_failure AFTER UPDATE ON mg.uploads DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.writer_closed AND NOT OLD.writer_closed) EXECUTE FUNCTION mg.test_storage_commit_failure()")
        try:
            with self.assertRaises(ApiError): await receive_content(self.request(chunks()),self.user,row['id'],self.store)
            with self.app.state.db.connect() as con:
                held=con.execute('SELECT reservation_bytes,writer_closed FROM uploads WHERE id=%s',(row['id'],)).fetchone()
            self.assertEqual(held,{'reservation_bytes':1,'writer_closed':False})
        finally:
            with admin_connect() as admin:
                admin.execute('DROP TRIGGER test_storage_commit_failure ON mg.uploads')
                admin.execute('DROP FUNCTION mg.test_storage_commit_failure()')
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await self.assert_released(row['id'])


    async def test_s3_timeout_keeps_closed_writer_reserve_until_confirmed_cleanup(self):
        from web.helpers import PostgreSQLProxy
        from model_generator.web.s3_store import ObjectStore
        from model_generator.web.uploads import owned_io
        row=await self.reserve(b'x'); original=self.store.objects
        proxy=PostgreSQLProxy('s3-proxy',8080,pause_on=b'POST ')
        self.store.objects=ObjectStore(replace(self.settings.storage,proxy_url=f'http://127.0.0.1:{proxy.address[1]}'))
        async def chunks(): yield b'x'
        try:
            with self.assertRaises(ApiError): await receive_content(self.request(chunks()),self.user,row['id'],self.store)
            with self.app.state.db.connect() as con:
                held=con.execute('SELECT reservation_bytes,writer_closed FROM uploads WHERE id=%s',(row['id'],)).fetchone()
            self.assertEqual(held,{'reservation_bytes':1,'writer_closed':True})
        finally:
            self.store.objects=original; await asyncio.to_thread(proxy.close)
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await self.assert_released(row['id'])
    async def test_failed_multipart_abort_holds_active_slot_until_retry(self):
        from web.helpers import PostgreSQLProxy
        from model_generator.web.s3_store import ObjectStore
        from model_generator.web.uploads import owned_io
        row=await self.reserve(b'x'); await self.app.state.db.run(self.store.claim_content,self.owner,row['id'])
        intent=await self.app.state.db.run(self.store.intent,self.owner,row['id'])
        created=await asyncio.to_thread(self.store.objects.client.create_multipart_upload,**self.store.objects._key(intent.key))
        await self.app.state.db.run(self.store._persist_multipart,self.owner,row['id'],intent.attempt_epoch,created['UploadId'])
        proxy=PostgreSQLProxy('s3-proxy',8080,pause_on=b'DELETE '); original=self.store.objects
        self.store.objects=ObjectStore(replace(self.settings.storage,proxy_url=f'http://127.0.0.1:{proxy.address[1]}'))
        try:
            with self.assertRaises(ApiError): await owned_io(self.store,self.store.acknowledge_closed,self.owner,row['id'],intent.attempt_epoch,None)
            with self.app.state.db.connect() as con:
                self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':1,'active_uploads':1})
        finally:
            self.store.objects=original; await asyncio.to_thread(proxy.close)
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await self.assert_released(row['id'])
        self.assertFalse((await asyncio.to_thread(original.client.list_multipart_uploads,Bucket=self.settings.storage.bucket,Prefix=intent.key)).get('Uploads'))
    async def test_complete_object_db_commit_failure_never_leaves_ready_or_quota(self):
        from web.helpers import admin_connect
        row=await self.reserve(b'x')
        async def chunks(): yield b'x'
        with admin_connect() as admin:
            admin.execute("CREATE FUNCTION mg.test_ready_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$")
            admin.execute("CREATE CONSTRAINT TRIGGER test_ready_commit_failure AFTER UPDATE ON mg.uploads DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.state='ready') EXECUTE FUNCTION mg.test_ready_commit_failure()")
        try:
            with self.assertRaises(ApiError): await receive_content(self.request(chunks()),self.user,row['id'],self.store)
        finally:
            with admin_connect() as admin:
                admin.execute('DROP TRIGGER test_ready_commit_failure ON mg.uploads')
                admin.execute('DROP FUNCTION mg.test_ready_commit_failure()')
        await self.assert_released(row['id'])
        intent=await self.app.state.db.run(self.store.intent,self.owner,row['id'])
        self.assertIsNone(await asyncio.to_thread(self.store.objects.head,intent))

    async def test_s3_trickle_deadline_joins_producer_before_quota_release(self):
        import time,threading
        from web.test_s3_store import real_s3_trickle_proxy
        from model_generator.web.s3_store import ObjectStore
        server,thread=real_s3_trickle_proxy()
        original=self.store.objects
        objects=ObjectStore(replace(self.settings.storage,proxy_url=f'http://127.0.0.1:{server.server_port}'))
        self.store.objects=objects
        self.store.settings=replace(self.settings,upload_wall_seconds=.2)
        producer=threading.Event(); put=objects.put_file; release=self.store._release
        def physical_put(*args):
            producer.set()
            try: return put(*args)
            finally: producer.clear()
        def checked_release(*args):
            self.assertFalse(producer.is_set(),'Quota released while producer IO is live')
            return release(*args)
        objects.put_file=physical_put; self.store._release=checked_release
        row=await self.reserve(b'x')
        async def chunks(): yield b'x'
        started=time.monotonic()
        try:
            with self.assertRaises(ApiError): await receive_content(self.request(chunks()),self.user,row['id'],self.store)
            self.assertLess(time.monotonic()-started,1.5)
            self.assertFalse(producer.is_set())
            await self.assert_released(row['id'])
        finally:
            self.store.objects=original; self.store._release=release
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)
            thread.join(2)

    async def test_native_dns_and_multiple_connect_addresses_have_physical_deadline(self):
        import time,threading
        from web.test_s3_store import real_dns_fault
        from model_generator.web.s3_store import ObjectStore
        from model_generator.web.uploads import owned_io
        def children():
            return {pid for path in Path('/proc/self/task').glob('*/children') for pid in path.read_text().split()}
        def inspect(query,params=None):
            with self.app.state.db.connect() as con:
                return con.execute(query,params).fetchone()
        for mode in ('dns','addresses'):
            with self.subTest(mode=mode):
                row=await self.reserve(b'x'); original=self.store.objects; before=children()
                release=self.store._release; acknowledge=self.store.acknowledge_closed
                physical=threading.Event(); physical_times=[]; release_observations=[]
                cleanup_active=threading.Event(); cleanup_failures=[]; task=None
                async def chunks(): yield b'x'
                def checked_release(*args):
                    release_observations.append(physical.is_set())
                    return release(*args)
                def checked_cleanup(*args):
                    cleanup_active.set()
                    try: return acknowledge(*args)
                    finally: cleanup_active.clear()
                def observe_cleanup(operation,name):
                    def call(*args,**kwargs):
                        try: return operation(*args,**kwargs)
                        except BaseException as error:
                            if cleanup_active.is_set(): cleanup_failures.append((name,type(error).__name__))
                            raise
                    return call
                self.store._release=checked_release
                self.store.acknowledge_closed=checked_cleanup
                try:
                    with real_dns_fault(mode) as fault:
                        objects=ObjectStore(replace(self.settings.storage,proxy_url=f'http://{fault.name}:{fault.port}'))
                        self.store.objects=objects
                        for name in ('abort_multipart','head','delete'):
                            setattr(objects,name,observe_cleanup(getattr(objects,name),name))
                        self.store.settings=replace(self.settings,upload_wall_seconds=30)
                        put=objects.put_file
                        def producer(*args):
                            physical.set()
                            physical_started=time.monotonic()
                            deadline=physical_started+.6
                            self.store.deadlines[row['id']]=deadline
                            try: return put(replace(args[0],deadline=deadline),*args[1:])
                            finally:
                                physical_times.append(time.monotonic()-physical_started)
                                physical.clear()
                        objects.put_file=producer
                        started=time.monotonic()
                        task=asyncio.create_task(receive_content(self.request(chunks()),self.user,row['id'],self.store))
                        self.assertTrue(await asyncio.to_thread(fault.queried.wait,2))
                        self.assertTrue(physical.is_set())
                        quota=await self.app.state.db.run(inspect,"SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'")
                        self.assertTrue(physical.is_set(),'Quota observation must finish while producer is active')
                        self.assertEqual(quota,{'storage_bytes':1,'active_uploads':1})
                        with self.assertRaises(ApiError): await task
                        self.assertEqual(len(physical_times),1)
                        self.assertLess(physical_times[0],.8)
                        self.assertLess(time.monotonic()-started,3.2)
                        print(f'Physical S3 {mode} exit={physical_times[0]:.3f}s; receiver/cleanup={time.monotonic()-started:.3f}s',flush=True)
                        self.assertFalse(physical.is_set()); self.assertEqual(children(),before)
                        self.assertNotIn(True,release_observations,'Quota released while physical producer IO was active')
                        final=await self.app.state.db.run(inspect,'SELECT state,reservation_bytes,writer_closed FROM uploads WHERE id=%s',(row['id'],))
                        self.assert_cleanup_outcome(final,cleanup_failures,release_observations)
                finally:
                    # Even failed observations wait for physical exit before fixture cleanup.
                    if task is not None and not task.done():
                        task.cancel()
                        try: await task
                        except (asyncio.CancelledError,ApiError): pass
                    self.store.objects=original; self.store.settings=self.settings
                    self.store._release=release; self.store.acknowledge_closed=acknowledge
                    await self.app.state.db.run(self.store.request_abort,self.owner,row['id'],int(self.app.state.clock()))
                    writer=await self.app.state.db.run(inspect,'SELECT writer_epoch FROM uploads WHERE id=%s',(row['id'],))
                    epoch=writer['writer_epoch']
                    await owned_io(self.store,self.store.acknowledge_closed,self.owner,row['id'],epoch)
                await self.assert_released(row['id'])

        # A previous actual cleanup failure does not freeze the final state.
        # Force failure, then run the real successful retry before observing SQL.
        row=await self.reserve(b'x')
        claimed=await self.app.state.db.run(self.store.claim_content,self.owner,row['id'])
        failures=[]; releases=[]; release=self.store._release
        def failed_head(*args):
            failures.append(('head','ApiError'))
            raise ApiError('storage_unavailable','Synthetic cleanup HEAD failure',503)
        def observed_release(*args):
            releases.append(physical.is_set())
            return release(*args)
        async def outcome():
            return await self.app.state.db.run(inspect,'SELECT state,reservation_bytes,writer_closed FROM uploads WHERE id=%s',(row['id'],))
        with patch.object(self.store,'_release',observed_release):
            with patch.object(self.store.objects,'head',failed_head):
                with self.assertRaises(ApiError):
                    await owned_io(self.store,self.store.acknowledge_closed,self.owner,row['id'],claimed['writer_epoch'])
            self.assert_cleanup_outcome(await outcome(),failures,releases)
            await owned_io(self.store,self.store.acknowledge_closed,self.owner,row['id'],claimed['writer_epoch'])
            final=await outcome()
            self.assertEqual(final['state'],'deleted')
            self.assertGreaterEqual(len(failures),1)
            self.assert_cleanup_outcome(final,failures,releases)
        await self.assert_released(row['id'])

    async def test_failed_second_claim_cannot_acknowledge_first_writer(self):
        from model_generator.web.uploads import owned_io
        row=await self.reserve(b'x')
        first=await self.app.state.db.run(self.store.claim_content,self.owner,row['id'])
        async def chunks(): yield b'x'
        with self.assertRaises(ApiError): await receive_content(self.request(chunks()),self.user,row['id'],self.store)
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        with self.app.state.db.connect() as con:
            active=con.execute('SELECT state,writer_epoch,writer_closed,reservation_bytes FROM uploads WHERE id=%s',(row['id'],)).fetchone()
        self.assertEqual(active,{'state':'receiving','writer_epoch':first['writer_epoch'],'writer_closed':False,'reservation_bytes':1})
        self.assertFalse(self.store.closed_writers)
        await owned_io(self.store,self.store.acknowledge_closed,self.owner,row['id'],first['writer_epoch'])
        await self.assert_released(row['id'])

    async def test_cancel_after_claim_commit_before_result_is_reconciled_same_process(self):
        import threading
        from model_generator.web.uploads import owned_io
        row=await self.reserve(b'x'); entered=threading.Event(); release=threading.Event()
        original=self.store.claim_content
        def committed(*args):
            result=original(*args)
            entered.set(); release.wait(1)
            return result
        self.store.claim_content=committed
        async def chunks(): yield b'x'
        task=asyncio.create_task(receive_content(self.request(chunks()),self.user,row['id'],self.store))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            task.cancel(); await asyncio.sleep(.01); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        finally:
            release.set(); self.store.claim_content=original
        await self.app.state.db.run(self.store.request_abort,self.owner,row['id'],int(self.app.state.clock()))
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await owned_io(self.store,lambda:self.store.sweep(int(self.app.state.clock()),api_lock_owned=True))
        await self.assert_released(row['id'])
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute("SELECT storage_bytes,active_uploads FROM quota_scopes WHERE scope='global'").fetchone(),{'storage_bytes':0,'active_uploads':0})
            self.assertEqual(con.execute("SELECT count(*) AS n FROM usage_events WHERE action='upload'").fetchone()['n'],1)


def memory_probe():
    """Current resident memory sampled during a 96 MiB file-backed ASGI stream."""
    import gc
    import threading
    import time
    import signal
    # A physical process deadline bounds the evidence harness too, including IO waits.
    signal.alarm(900)
    reset_database()
    with tempfile.TemporaryDirectory() as tmp:
        settings=replace(settings_for(Path(tmp)),min_free_disk_bytes=0)
        app=make_test_app(settings)
        with TestClient(app,base_url='https://testserver') as client:
            identity=register_login(client,'memory_owner')
            user=AuthenticatedUser(identity['userId'],'memory_owner','0'*64,identity['csrf'])
            source=Path(tmp)/'synthetic-source'; sha=hashlib.sha256()
            with source.open('wb') as file:
                for _ in range(1536): file.write(b'x'*65536); sha.update(b'x'*65536)
            async def upload(path,bytes,hash):
                row=await app.state.db.run(app.state.storage.reserve_upload,user.id,'zip-fbx','synthetic',bytes,hash,int(app.state.clock()))
                file=path.open('rb')
                async def receive():
                    chunk=file.read(65536)
                    return {'type':'http.request','body':chunk,'more_body':bool(chunk)}
                request=Request({'type':'http','app':app,'method':'PUT','path':'/','headers':[],'query_string':b''},receive)
                try: return await receive_content(request,user,row['id'],app.state.storage)
                finally: file.close()
            warm=Path(tmp)/'warm'; warm.write_bytes(b'x')
            warmed=asyncio.run(upload(warm,1,hashlib.sha256(b'x').hexdigest()))
            client.delete('/api/uploads/'+warmed['id'],headers={'Origin':'https://testserver','X-CSRF-Token':user.csrf_token})
            gc.collect()
            def rss():
                return int(Path('/proc/self/statm').read_text().split()[1])*__import__('os').sysconf('SC_PAGE_SIZE')
            base=rss(); peak=[base]; stop=threading.Event()
            def sampler():
                while not stop.wait(.005): peak[0]=max(peak[0],rss())
            thread=threading.Thread(target=sampler); thread.start()
            try: result=asyncio.run(upload(source,source.stat().st_size,sha.hexdigest()))
            finally: stop.set(); thread.join()
            growth=peak[0]-base
            assert result['bytes']==100663296
            assert growth<16*1024**2,(base,peak[0],growth)
            print(f'Actual /proc/self/statm RSS: base={base} peak={peak[0]} growth={growth}; streamed={result["bytes"]} bytes; growth<16MiB passed.')

if __name__=='__main__':
    if __import__('sys').argv[1:]==['--memory-probe']: memory_probe()
    else: unittest.main()
