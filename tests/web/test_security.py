from web.helpers import enter_fixture_client, close_fixture_client
import asyncio
import json
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch
from web.helpers import TestClient
from starlette.requests import Request
from model_generator.web.security import ApiError,bounded_json,SecurityMiddleware,client_ip
from web.helpers import make_test_app,register_login,settings_for,reset_database


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.app=make_test_app(settings_for(Path(self.tmp.name))); self.client=enter_fixture_client(self,self.app)
    def test_origin_host_before_body_and_error_headers(self):
        with patch.object(Request,'body',side_effect=AssertionError('body buffered')),patch.object(Request,'json',side_effect=AssertionError('json buffered')):
            for origin in (None,'https://evil.invalid','https://testserver/','null'):
                response=self.client.post('/api/auth/register',content=b'{',headers={'Content-Type':'application/json',**({'Origin':origin} if origin else {})})
                self.assertEqual(response.status_code,403)
            response=self.client.post('/api/auth/register',content=b'{',headers={'Host':'evil.invalid','Origin':'https://testserver','Content-Type':'application/json'})
            self.assertEqual(response.status_code,403)
            response=self.client.post('/api/auth/register',content=b'x'*16385,headers={'Origin':'https://testserver','Content-Type':'application/json'})
            self.assertEqual(response.status_code,413)
        self.assertIn('request_id',response.json()['error'])
        for header in ('content-security-policy','x-content-type-options','referrer-policy','cache-control'): self.assertIn(header,response.headers)
        self.assertEqual(response.headers['cache-control'],'no-store')

    def test_liveness_readiness_and_hidden_docs(self):
        self.assertEqual(self.client.get('/health/live').json(),{'status':'ok'})
        response=self.client.get('/health/ready'); self.assertEqual(response.status_code,503); self.assertEqual(response.json()['reason'],'worker_not_ready')
        for path in ('/docs','/openapi.json','/redoc'): self.assertEqual(self.client.get(path).status_code,404)
    def test_unknown_fields_and_json_validation(self):
        for body in ('{"username":"synthetic","password":"abcdefghijkl","extra":1}', '{"x":1,"x":2}', '{"x":NaN}', '[1]', '{"x":'+ '['*17+'0'+']'*17+'}'):
            self.app.state.clock.tick(3600)
            response=self.client.post('/api/auth/register',content=body,headers={'Origin':'https://testserver','Content-Type':'application/json'})
            self.assertIn(response.status_code,(400,422))
    def test_csrf_exact_and_api_user_rate(self):
        user=register_login(self.client,'synthetic')
        response=self.client.post('/api/auth/logout',headers={'Origin':'https://evil.invalid','X-CSRF-Token':user['csrf']}); self.assertEqual(response.status_code,403)
        for _ in range(119): self.assertEqual(self.client.get('/api/auth/me').status_code,200)
        self.assertEqual(self.client.get('/api/auth/me').status_code,429)
        self.app.state.clock.tick(60); self.assertEqual(self.client.get('/api/auth/me').status_code,200)
    def test_safe_logging_does_not_include_path_body_or_cookie(self):
        with self.assertLogs('model_generator.web',level='INFO') as captured:
            self.client.post('/api/auth/login?private=query',content='private-body',headers={'Origin':'https://testserver','Content-Type':'application/json','Cookie':'secret-cookie'})
            self.client.get('/private-file-path')
        data=' '.join(captured.output)
        for secret in ('private=query','private-body','secret-cookie','private-file-path'): self.assertNotIn(secret,data)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def request(self,chunks,headers=None,receive=None):
        iterator=iter(chunks)
        async def recv():
            try: data=next(iterator)
            except StopIteration: return {'type':'http.request','body':b'','more_body':False}
            return {'type':'http.request','body':data,'more_body':True}
        return Request({'type':'http','method':'POST','path':'/','headers':[(b'content-type',b'application/json')]+(headers or [])},receive or recv)
    async def test_stream_cap_stops_before_reading_remaining_body(self):
        seen=0
        async def recv():
            nonlocal seen
            seen+=1
            if seen>2: self.fail('body read after cap')
            return {'type':'http.request','body':b'x'*8193,'more_body':True}
        with self.assertRaises(ApiError) as caught: await bounded_json(self.request([],receive=recv))
        self.assertEqual(caught.exception.status,413); self.assertEqual(seen,2)
    async def test_json_deadlines_with_injected_waiter(self):
        async def timeout(awaitable,timeout):
            awaitable.close(); raise TimeoutError()
        with patch('model_generator.web.security.asyncio.wait_for',side_effect=timeout):
            with self.assertRaises(ApiError) as caught: await bounded_json(self.request([b'{}']))
        self.assertEqual(caught.exception.status,408)
    async def test_wall_deadline_counts_across_chunks(self):
        times=iter((0,0,4,8,11))
        with patch('model_generator.web.security.time.monotonic',side_effect=lambda:next(times)):
            with self.assertRaises(ApiError) as caught: await bounded_json(self.request([b'{',b'"x":',b'1',b'}']))
        self.assertEqual(caught.exception.status,408)
    async def test_http_65th_refused_before_receive(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); app=make_test_app(settings_for(Path(tmp.name))); gate=asyncio.Event(); entered=0
        async def inner(scope,receive,send):
            nonlocal entered
            entered+=1; await gate.wait()
            await send({'type':'http.response.start','status':200,'headers':[]}); await send({'type':'http.response.body','body':b'{}'})
        middleware=SecurityMiddleware(inner,app.state.settings)
        scope={'type':'http','method':'POST','path':'/','headers':[(b'host',b'testserver')],'client':('192.0.2.1',1)}
        async def recv(): self.fail('overloaded body read')
        async def send(message): pass
        tasks=[asyncio.create_task(middleware(dict(scope),recv,send)) for _ in range(64)]
        await asyncio.sleep(0); messages=[]
        async def capture(message): messages.append(message)
        await middleware(dict(scope),recv,capture)
        self.assertEqual(entered,64); self.assertEqual(messages[0]['status'],503)
        gate.set(); await asyncio.gather(*tasks); tmp.cleanup()
    async def test_proxy_requires_explicit_socket_trust(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); settings=make_test_app(settings_for(Path(tmp.name))).state.settings
        scope={'type':'http','method':'GET','path':'/','headers':[(b'x-forwarded-for',b'192.0.2.9')],'client':('192.0.2.1',1)}
        self.assertEqual(client_ip(Request(scope),settings),'192.0.2.1')
        from dataclasses import replace
        self.assertEqual(client_ip(Request(scope),replace(settings,trusted_proxy_ips=('192.0.2.1',))),'192.0.2.9')
        tmp.cleanup()


class ActualReceiveDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_five_second_idle_deadline(self):
        async def receive():
            await asyncio.sleep(6)
            return {'type':'http.request','body':b'{}','more_body':False}
        request=Request({'type':'http','method':'POST','path':'/','headers':[(b'content-type',b'application/json')]},receive)
        import time
        start=time.monotonic()
        with self.assertRaises(ApiError) as caught: await bounded_json(request)
        self.assertEqual(caught.exception.status,408)
        self.assertGreaterEqual(time.monotonic()-start,4.9)
        self.assertLess(time.monotonic()-start,5.5)

    async def test_actual_ten_second_wall_deadline(self):
        async def receive():
            await asyncio.sleep(3)
            return {'type':'http.request','body':b' ','more_body':True}
        request=Request({'type':'http','method':'POST','path':'/','headers':[(b'content-type',b'application/json')]},receive)
        import time
        start=time.monotonic()
        with self.assertRaises(ApiError) as caught: await bounded_json(request)
        self.assertEqual(caught.exception.status,408)
        self.assertGreaterEqual(time.monotonic()-start,9.9)
        self.assertLess(time.monotonic()-start,10.5)
