from web.helpers import enter_fixture_client, close_fixture_client
import asyncio
import secrets
import tempfile
from contextlib import closing
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from web.helpers import TestClient
from model_generator.web.auth import hash_password, verify_password, KdfPool
from model_generator.web.security import ApiError
from web.helpers import make_test_app, register_login, settings_for, reset_database


class PasswordTests(unittest.TestCase):
    def test_boundaries_random_salt_fixed_parameters(self):
        password = secrets.token_urlsafe(24)
        one, two = hash_password(password), hash_password(password)
        self.assertNotEqual(one, two)
        self.assertTrue(one.startswith('scrypt-v1$32768$8$1$'))
        self.assertTrue(verify_password(password, one))
        self.assertFalse(verify_password(password+'x', one))
        for value in ('x'*11, 'x'*129, '\U0001f600'*128+'x'):
            with self.assertRaises(ValueError): hash_password(value)
        for value in ('x'*12, 'x'*128, '\U0001f600'*128):
            self.assertTrue(verify_password(value, hash_password(value)))
        self.assertFalse(verify_password(password, 'scrypt-v1$1048576$8$1$x$x'))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        reset_database(); self.app = make_test_app(settings_for(self.root))
        self.client = enter_fixture_client(self,self.app)
        self.headers = {'Origin': 'https://testserver'}

    def test_session_cookie_logout_and_expiry(self):
        user = register_login(self.client, 'synthetic_a')
        raw = self.client.cookies.get('__Host-mg_session')
        response = self.client.get('/api/auth/me'); self.assertEqual(response.json()['id'],user['userId'])
        with self.app.state.db.connect() as con:
            row = con.execute('SELECT * FROM sessions').fetchone()
            self.assertNotIn(raw, tuple(row.values())); self.assertNotIn(user['csrf'],tuple(row.values()))
        self.app.state.clock.tick(12*3600)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        user = register_login(self.client,'synthetic_b')
        self.assertEqual(self.client.post('/api/auth/logout',headers=self.headers).status_code,403)
        self.assertEqual(self.client.post('/api/auth/logout',headers={**self.headers,'X-CSRF-Token':user['csrf']}).status_code,204)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)

    def test_cookie_flags_and_rotation(self):
        password = secrets.token_urlsafe(24)
        self.client.post('/api/auth/register',json={'username':'synthetic','password':password},headers=self.headers)
        payload={'username':'synthetic','password':password}
        response=self.client.post('/api/auth/login',json=payload,headers=self.headers)
        cookie=response.headers['set-cookie']
        for flag in ('__Host-mg_session=', 'HttpOnly','Secure','SameSite=lax','Path=/','Max-Age=43200'): self.assertIn(flag,cookie)
        self.assertNotIn('Domain',cookie)
        old=self.client.cookies.get('__Host-mg_session')
        self.client.post('/api/auth/login',json=payload,headers=self.headers)
        new=self.client.cookies.get('__Host-mg_session'); self.assertNotEqual(old,new)
        self.assertEqual(self.client.get('/api/auth/me',headers={'Cookie':'__Host-mg_session='+old}).status_code,401)

    def test_session_survives_restart_and_api_lock_exclusive(self):
        register_login(self.client,'synthetic'); cookie=self.client.cookies.get('__Host-mg_session')
        with self.assertRaises(RuntimeError):
            with TestClient(make_test_app(settings_for(self.root)),base_url='https://testserver'): pass
        close_fixture_client(self.client)
        from dataclasses import replace
        from model_generator.web.app import create_app
        app=create_app(replace(self.app.state.settings))
        app.state.clock=self.app.state.clock
        with TestClient(app,base_url='https://testserver') as client:
            client.cookies.set('__Host-mg_session',cookie)
            self.assertEqual(client.get('/api/auth/me').status_code,200)
        self.client=enter_fixture_client(self,self.app)

    def test_unknown_username_has_dummy_cost_and_safe_error(self):
        with patch('model_generator.web.auth_routes.verify_password', wraps=verify_password) as verify:
            response=self.client.post('/api/auth/login',json={'username':'missing','password':secrets.token_urlsafe(24)},headers=self.headers)
        self.assertEqual(response.status_code,401); self.assertEqual(verify.call_count,1)
        self.assertEqual(response.json()['error']['code'],'invalid_credentials')

    def test_login_limits_durable_and_spoofed_proxy_ignored(self):
        payload={'username':'missing','password':secrets.token_urlsafe(24)}
        with patch('model_generator.web.auth_routes.verify_password',return_value=False) as verify:
            for i in range(10):
                self.assertEqual(self.client.post('/api/auth/login',json=payload,headers={**self.headers,'X-Forwarded-For':f'192.0.2.{i+1}'}).status_code,401)
            response=self.client.post('/api/auth/login',json=payload,headers=self.headers)
            self.assertEqual(response.status_code,429); self.assertIn('retry-after',response.headers); self.assertEqual(verify.call_count,10)
            self.app.state.clock.tick(900)
            self.assertEqual(self.client.post('/api/auth/login',json=payload,headers=self.headers).status_code,401)
        with self.app.state.db.connect() as con:
            data=str([tuple(r.values()) for r in con.execute('SELECT * FROM auth_attempts')])
            self.assertNotIn('missing',data); self.assertNotIn('testclient',data); self.assertNotIn('192.0.2.',data)

    def test_registration_limit_and_validation(self):
        for name in ('a<b','../x','UPPER','x'):
            self.assertEqual(self.client.post('/api/auth/register',json={'username':name,'password':secrets.token_urlsafe(24)},headers=self.headers).status_code,422)
            self.app.state.clock.tick(3600)
        for i in range(3):
            self.assertEqual(self.client.post('/api/auth/register',json={'username':f'synthetic_{i}','password':secrets.token_urlsafe(24)},headers=self.headers).status_code,201)
        self.assertEqual(self.client.post('/api/auth/register',json={'username':'synthetic_4','password':secrets.token_urlsafe(24)},headers=self.headers).status_code,429)


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_active_two_queued_fifth_rejected_and_cancel_reclaims(self):
        pool=KdfPool(active=2,queued=2,acquire_seconds=0.2)
        event=threading.Event(); entered=threading.Event(); count=0; lock=threading.Lock()
        def work():
            nonlocal count
            with lock:
                count+=1
                if count==2: entered.set()
            event.wait(2)
            return True
        try:
            tasks=[asyncio.create_task(pool.run(work)) for _ in range(2)]
            await asyncio.to_thread(entered.wait,1)
            waiting=[asyncio.create_task(pool.run(work)) for _ in range(2)]
            await asyncio.sleep(0)
            with self.assertRaises(ApiError) as caught: await pool.run(work)
            self.assertEqual(caught.exception.status,503); self.assertEqual(count,2)
            waiting[0].cancel()
            with self.assertRaises(asyncio.CancelledError): await waiting[0]
            self.assertEqual(pool.admitted,3)
            event.set(); self.assertTrue(all(await asyncio.gather(*tasks,waiting[1])))
            self.assertEqual(pool.admitted,0)
        finally:
            event.set(); await pool.close()

    async def test_queue_timeout_does_not_execute_hash(self):
        pool=KdfPool(active=2,queued=2,acquire_seconds=0.01)
        event=threading.Event()
        try:
            tasks=[asyncio.create_task(pool.run(event.wait,1)) for _ in range(2)]
            await asyncio.sleep(0.01)
            with self.assertRaises(ApiError): await pool.run(lambda: self.fail('queued hash executed'))
            event.set(); await asyncio.gather(*tasks)
        finally: event.set(); await pool.close()
