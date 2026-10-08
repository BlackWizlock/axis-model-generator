"""Actual S3 owner-first streaming, complete-body integrity and closure."""
import asyncio
import hashlib
import io
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4
from pathlib import Path
import unittest
from contextlib import closing
from web import test_jobs as job_helpers
from model_generator.web.security import ApiError

class ArtifactTests(unittest.TestCase):
    setUp=job_helpers.JobTests.setUp
    upload=job_helpers.JobTests.upload
    create=job_helpers.JobTests.create
    def artifact(self,kind='report',data=b'{}'):
        dto=self.create(self.upload()).json(); epoch=uuid4().hex; artifact=uuid4().hex
        suffix={'report':'report.json','preview':'preview-input.json','thumbnail':'thumbnail.png'}[kind]
        key=f"owners/{self.owner['userId']}/jobs/{dto['id']}/{epoch}/{suffix}"
        mime='image/png' if kind=='thumbnail' else 'application/json'
        sha=hashlib.sha256(data).hexdigest()
        self.app.state.storage.objects.client.put_object(Bucket=self.settings.storage.bucket,Key=key,Body=data,ContentType=mime,Metadata={'sha256':sha})
        with self.app.state.db.transaction() as con:
            con.execute("UPDATE jobs SET state='completed',stage='done',active_reserved=FALSE WHERE id=%s",(dto['id'],))
            con.execute("UPDATE quota_scopes SET active_jobs=active_jobs-1 WHERE scope IN ('global',%s)",(self.owner['userId'],))
            con.execute("INSERT INTO artifacts VALUES(%s,%s,%s,%s,%s,'ready',%s,%s,%s)",(artifact,dto['id'],kind,len(data),sha,key,epoch,int(self.app.state.clock())))
        return dto['id'],artifact,key,data
    def test_all_private_kinds_fixed_headers_bytes_and_owner_before_s3(self):
        for kind,data in [('report',b'{"synthetic":true}'),('preview',b'{"schemaVersion":1}'),('thumbnail',b'\x89PNG\r\n\x1a\n')]:
            with self.subTest(kind=kind):
                job,artifact,key,data=self.artifact(kind,data); url=f'/api/jobs/{job}/artifacts/{artifact}'
                response=self.client.get(url); self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.content,data); self.assertEqual(response.headers['content-length'],str(len(data)))
                self.assertEqual(response.headers['content-type'],'image/png' if kind=='thumbnail' else 'application/json; charset=utf-8' if kind=='report' else 'application/json')
                self.assertEqual(response.headers['x-content-type-options'],'nosniff'); self.assertEqual(response.headers['cache-control'],'no-store')
                self.assertIn('attachment; filename="model-generator-',response.headers['content-disposition'])
                with patch.object(self.app.state.storage.objects,'open_stream',side_effect=AssertionError('Unexpected S3 GET')):
                    self.assertEqual(self.client.get(f'/api/jobs/{"b"*32}/artifacts/{artifact}').status_code,404)
                    self.assertEqual(self.client.get(f'/api/jobs/{job}/artifacts/bad').status_code,404)
                    self.assertEqual(self.client.get(url,headers={'Cookie':'__Host-mg_session=invalid'}).status_code,401)
                with self.app.state.db.transaction() as con: con.execute("UPDATE jobs SET state='deleted' WHERE id=%s",(job,))
    def test_corrupt_actual_object_final_hash_never_completes(self):
        from model_generator.web.artifacts import open_owned_artifact,verified_chunks
        data=b'a'*140000; job,artifact,key,_=self.artifact('report',data)
        self.app.state.storage.objects.client.put_object(Bucket=self.settings.storage.bucket,Key=key,Body=b'b'*len(data),ContentType='application/json',Metadata={'sha256':hashlib.sha256(data).hexdigest()})
        stream,item=open_owned_artifact(self.app.state.db,self.app.state.storage,self.owner['userId'],job,artifact,int(self.app.state.clock()))
        received=bytearray()
        with self.assertRaises(ApiError):
            for chunk in verified_chunks(stream,item): received.extend(chunk)
        self.assertLess(len(received),len(data)); self.assertTrue(stream.closed)
    def test_early_close_short_long_and_finally(self):
        from model_generator.web.artifacts import verified_chunks
        for data,expected in [(b'abc',4),(b'abcde',4),(b'abcd',4)]:
            stream=io.BytesIO(data); item={'bytes':expected,'sha256':hashlib.sha256(b'abcd').hexdigest()}
            if data==b'abcd': self.assertEqual(b''.join(verified_chunks(stream,item)),data)
            else:
                with self.assertRaises(ApiError): list(verified_chunks(stream,item))
            self.assertTrue(stream.closed)
        stream=io.BytesIO(b'a'*200000); gen=verified_chunks(stream,{'bytes':200000,'sha256':hashlib.sha256(b'a'*200000).hexdigest()})
        next(gen); gen.close(); self.assertTrue(stream.closed)
    def test_actual_sdk_disconnect_closes_without_complete_body(self):
        from model_generator.web.artifacts import open_owned_artifact,ArtifactResponse
        job,artifact,key,data=self.artifact('report',b'a'*200000)
        stream,item=open_owned_artifact(self.app.state.db,self.app.state.storage,self.owner['userId'],job,artifact,int(self.app.state.clock()))
        closed=[]; real_close=stream.raw.close
        def close(): closed.append(True); real_close()
        stream.raw.close=close
        sent=[]
        async def send(message):
            sent.append(message)
            if message['type']=='http.response.body': raise OSError('Synthetic client disconnected')
        async def receive(): return {'type':'http.disconnect'}
        from starlette.requests import ClientDisconnect
        with self.assertRaises(ClientDisconnect):
            asyncio.run(ArtifactResponse(self.app.state.storage,stream,item)({'type':'http','asgi':{'spec_version':'2.4'}},receive,send))
        self.assertTrue(stream.closed); self.assertEqual(closed,[True])
        self.assertLess(sum(len(m.get('body',b'')) for m in sent),len(data))
        self.assertFalse(any(m['type']=='http.response.body' and not m.get('more_body',False) for m in sent))
    def test_neighbour_cookie_jars_denied_before_head_get_and_expiry(self):
        from web.helpers import TestClient,make_test_app,settings_for
        job,artifact,key,data=self.artifact(); url=f'/api/jobs/{job}/artifacts/{artifact}'
        with closing(TestClient(self.app,base_url='https://testserver')) as other:
            other.portal=self.client.portal  # Independent cookies, same live API event loop.
            guest=other.post('/api/auth/guest',headers={'Origin':'https://testserver'})
            self.assertEqual(guest.status_code,200)
            with patch('model_generator.web.s3_store.ObjectStore.head',side_effect=AssertionError('Wrong owner S3 HEAD')),patch('model_generator.web.s3_store.ObjectStore.open_stream',side_effect=AssertionError('Wrong owner S3 GET')):
                self.assertEqual(other.get(url).status_code,404)
                self.assertEqual(other.get('/api/jobs/'+job).status_code,404)
                self.assertEqual(other.delete('/api/jobs/'+job,headers={'Origin':'https://testserver','X-CSRF-Token':guest.json()['csrfToken']}).status_code,404)
        self.app.state.clock.tick(self.settings.retention_seconds)
        # Registered sessions expire at12h. Refresh only this test's auth session
        # expiry to test the independent own-result TTL gate before any sweep.
        with self.app.state.db.transaction() as con: con.execute('UPDATE sessions SET expires_at=%s',(int(self.app.state.clock())+100,))
        with patch.object(self.app.state.storage.objects,'head',side_effect=AssertionError('Expired S3 HEAD')):
            self.assertEqual(self.client.get(url).status_code,410)
    def test_migration_fences_immutable_descriptor_and_pre_iteration_disconnect(self):
        import psycopg
        from model_generator.web.artifacts import open_owned_artifact,ArtifactResponse
        job,artifact,key,data=self.artifact()
        with self.assertRaises(psycopg.Error):
            with self.app.state.db.transaction() as con: con.execute('UPDATE artifacts SET sha256=%s WHERE id=%s',('0'*64,artifact))
        stream,item=open_owned_artifact(self.app.state.db,self.app.state.storage,self.owner['userId'],job,artifact,int(self.app.state.clock()))
        async def send(message): raise OSError('Synthetic disconnect before first block')
        async def receive(): return {'type':'http.disconnect'}
        from starlette.requests import ClientDisconnect
        with self.assertRaises(ClientDisconnect):
            asyncio.run(ArtifactResponse(self.app.state.storage,stream,item)({'type':'http','asgi':{'spec_version':'2.4'}},receive,send))
        self.assertTrue(stream.closed)
