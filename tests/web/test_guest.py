"""Real durable cookie jars, bounded minting and owner continuity."""
from web.helpers import enter_fixture_client, close_fixture_client
import hashlib
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from web.helpers import TestClient,make_test_app,settings_for,reset_database,admin_connect

class GuestTests(unittest.TestCase):
    def setUp(self):
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app=make_test_app(settings_for(Path(self.tmp.name)))
        self.client=enter_fixture_client(self,self.app)
        self.origin={'Origin':'https://testserver'}
    def guest(self,client=None): return (client or self.client).post('/api/auth/guest',headers=self.origin)
    def test_bootstrap_flags_hash_reuse_and_csrf(self):
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        self.assertEqual(self.client.post('/api/auth/guest').status_code,403)
        response=self.guest(); self.assertEqual(response.status_code,200,response.text)
        cookie=response.headers['set-cookie']
        for flag in ('__Host-mg_session=','Secure','HttpOnly','SameSite=lax','Path=/','Max-Age=172800'): self.assertIn(flag,cookie)
        self.assertNotIn('Domain=',cookie)
        token=self.client.cookies.get('__Host-mg_session'); me=self.client.get('/api/auth/me').json()
        with self.app.state.db.connect() as con:
            self.assertNotIn(token,str(con.execute('SELECT * FROM sessions').fetchall()))
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],1)
        self.assertEqual(self.guest().json()['id'],me['id'])
        self.assertEqual(self.client.cookies.get('__Host-mg_session'),token)
        self.assertEqual(self.client.post('/api/uploads',json={},headers=self.origin).status_code,403)
        self.app.state.clock.tick(86400)
        response=self.guest(); self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['id'],me['id'])
        self.assertEqual(self.client.get('/api/auth/me').json()['csrfToken'],me['csrfToken'])
        self.app.state.clock.tick(172800)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
    def test_independent_jars_mint_ip_limits_and_no_quota_reset(self):
        first=self.guest().json(); self.client.cookies.clear()
        second=self.guest().json(); self.assertNotEqual(first['id'],second['id'])
        self.client.cookies.clear(); self.assertEqual(self.guest().status_code,200)
        self.client.cookies.clear(); self.assertEqual(self.guest().status_code,429)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],3)
        self.app.state.clock.tick(3600)
        for n in range(7):
            self.assertEqual(self.guest().status_code,200); self.client.cookies.clear()
            if n%3==2: self.app.state.clock.tick(3600)
        self.assertEqual(self.guest().status_code,429)
    def test_lost_cookie_cannot_reset_guest_accepted_quota(self):
        self.guest(); one=self.client.get('/api/auth/me').json()
        headers={**self.origin,'X-CSRF-Token':one['csrfToken']}
        for _ in range(10):
            response=self.client.post('/api/uploads',json={'kind':'zip-fbx','displayName':'synthetic.zip','bytes':1,'sha256':hashlib.sha256(b'x').hexdigest()},headers=headers)
            self.assertEqual(response.status_code,201,response.text)
            self.assertEqual(self.client.delete('/api/uploads/'+response.json()['id'],headers=headers).status_code,204)
        self.client.cookies.clear(); self.guest(); me=self.client.get('/api/auth/me').json()
        response=self.client.post('/api/uploads',json={'kind':'zip-fbx','displayName':'synthetic.zip','bytes':1,'sha256':hashlib.sha256(b'x').hexdigest()},headers={**self.origin,'X-CSRF-Token':me['csrfToken']})
        self.assertEqual(response.status_code,429,response.text)
    def test_guest_is_not_password_login_and_logout_really_clears_cookie(self):
        from model_generator.web.auth import DUMMY_RECORD
        from unittest.mock import patch
        guest=self.guest().json(); before=self.client.cookies.get('__Host-mg_session')
        with patch('model_generator.web.auth_routes.verify_password',return_value=True):
            response=self.client.post('/api/auth/login',json={'username':guest['username'],'password':'synthetic-safe-password'},headers=self.origin)
        self.assertEqual(response.status_code,401)
        self.assertNotIn('set-cookie',response.headers)
        response=self.client.post('/api/auth/logout',headers={**self.origin,'X-CSRF-Token':guest['csrfToken']})
        self.assertEqual(response.status_code,204)
        self.assertIn('Max-Age=0',response.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        self.assertEqual(self.client.get('/api/auth/me',headers={'Cookie':'__Host-mg_session='+before}).status_code,401)
    def test_idle_cleanup_avoids_locked_global_quota_but_reclaims_old_auth(self):
        from model_generator.web.auth import cleanup_guests
        now=int(self.app.state.clock())
        # The holder stays locked until the actual bounded cleanup returns.
        # A FOR UPDATE in cleanup cannot complete here before lock timeout.
        def idle_cleanup():
            with admin_connect() as holder:
                with holder.transaction():
                    holder.execute("SELECT scope FROM mg.quota_scopes WHERE scope='global' FOR UPDATE")
                    self.assertIsNone(self.client.portal.call(self.app.state.db.run,cleanup_guests,self.app.state.db,now))
        with self.subTest(state='no guests or stale authentication'):idle_cleanup()
        me=self.guest().json()
        with self.subTest(state='live guest and current authentication'):idle_cleanup()
        # No expired guest alone is not enough to skip real auth cleanup.
        digest=hashlib.sha256(b'idle-cleanup-old-auth').hexdigest()
        with admin_connect() as con:
            con.execute("INSERT INTO mg.auth_attempts VALUES('login-ip',%s,%s,1)",(digest,now-86401))
        self.client.portal.call(self.app.state.db.run,cleanup_guests,self.app.state.db,now)
        with self.app.state.db.connect() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM auth_attempts WHERE digest=%s',(digest,)).fetchone())
            self.assertIsNotNone(con.execute('SELECT 1 FROM users WHERE id=%s',(me['id'],)).fetchone())

    def test_cleanup_preserves_live_writer_reserves_and_daily_caps(self):
        from model_generator.web.auth import cleanup_guests
        me=self.guest().json(); headers={**self.origin,'X-CSRF-Token':me['csrfToken']}
        response=self.client.post('/api/uploads',json={'kind':'zip-fbx','displayName':'synthetic.zip','bytes':1,'sha256':hashlib.sha256(b'x').hexdigest()},headers=headers)
        self.assertEqual(response.status_code,201); upload=response.json()['id']
        self.app.state.clock.tick(172801); now=int(self.app.state.clock())
        cleanup_guests(self.app.state.db,now)
        with self.app.state.db.connect() as con: self.assertIsNotNone(con.execute('SELECT 1 FROM users WHERE id=%s',(me['id'],)).fetchone())
        # API reconciliation confirms closed producer/deletion before reclaim.
        self.app.state.storage.sweep(now,api_lock_owned=True)
        cleanup_guests(self.app.state.db,now)
        with self.app.state.db.connect() as con: self.assertIsNone(con.execute('SELECT 1 FROM users WHERE id=%s',(me['id'],)).fetchone())
    def test_global_mint_cap_concurrent_last_slot_and_cookie_expiry_matches_pg(self):
        from concurrent.futures import ThreadPoolExecutor
        from model_generator.web.auth import rate_digest
        now=int(self.app.state.clock())
        with admin_connect() as con:
            con.execute("INSERT INTO mg.auth_attempts VALUES('guest-global',%s,%s,99)",(rate_digest(self.app.state.settings.auth_key,'guest-global','global'),now//86400*86400))
        def mint(index):
            with closing(TestClient(self.app,base_url='https://testserver')) as client:
                client.portal=self.client.portal  # Independent cookies, same live API event loop.
                return client.post('/api/auth/guest',headers=self.origin).status_code
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(mint,range(2)))
        self.assertEqual(sorted(results),[200,429])
        with self.app.state.db.connect() as con:
            row=con.execute('SELECT expires_at FROM sessions').fetchone()
            self.assertEqual(row['expires_at'],now+172800)
    def test_retired_owner_cleanup_keeps_current_day_mint_counter(self):
        from model_generator.web.auth import cleanup_guests
        me=self.guest().json(); now=int(self.app.state.clock())
        with admin_connect() as con:
            con.execute('UPDATE mg.sessions SET created_at=%s,expires_at=%s WHERE user_id=%s',(now-2,now-1,me['id']))
        cleanup_guests(self.app.state.db,now)
        with self.app.state.db.connect() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM users WHERE id=%s',(me['id'],)).fetchone())
            self.assertEqual(con.execute("SELECT count FROM auth_attempts WHERE action='guest-day'").fetchone()['count'],1)
    def test_renewal_pg_cookie_match_invalid_and_failed_responses_no_cookie(self):
        self.guest(); token=self.client.cookies.get('__Host-mg_session'); now=int(self.app.state.clock())
        self.app.state.clock.tick(86401); response=self.client.get('/api/auth/me')
        self.assertEqual(response.status_code,200)
        self.assertIn('Max-Age=172800',response.headers['set-cookie'])
        self.assertEqual(self.client.cookies.get('__Host-mg_session'),token)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT expires_at FROM sessions').fetchone()['expires_at'],now+86401+172800)
        response=self.client.post('/api/uploads',json={},headers=self.origin)
        self.assertEqual(response.status_code,403); self.assertNotIn('set-cookie',response.headers)
        response=self.client.get('/api/auth/me',headers={'Cookie':'__Host-mg_session=invalid'})
        self.assertEqual(response.status_code,401); self.assertNotIn('set-cookie',response.headers)
