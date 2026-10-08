"""Mandatory real PostgreSQL concurrency, permissions and failure checks."""
from web.helpers import enter_fixture_client, close_fixture_client
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import inspect
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import psycopg
from web.helpers import TestClient, PostgreSQLProxy, admin_connect, make_test_app, reset_database, settings_for, secret
from model_generator.web.db import Database
from model_generator.web.auth import consume_limit, hash_password
from model_generator.web.security import ApiError


class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(Database,'check_schema'), 'PostgreSQL schema contract missing')
        reset_database()
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings=settings_for(Path(self.tmp.name))
        self.db=Database(self.settings); self.addCleanup(self.db.close)

    def test_schema_identity_types_and_runtime_permissions(self):
        self.db.check_schema()
        with self.db.connect() as con:
            self.assertEqual(con.execute('SHOW search_path').fetchone()['search_path'].replace(' ',''),'mg,pg_catalog')
            self.assertEqual(con.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='mg' AND table_name='users' AND column_name='disabled'").fetchone()['data_type'],'boolean')
            for sql in ('CREATE TABLE mg.forbidden (id INT)', 'CREATE DATABASE forbidden', 'CREATE ROLE forbidden', 'SELECT * FROM sentinel.private_data'):
                with self.subTest(sql=sql), self.assertRaises(psycopg.Error): con.execute(sql)
        with self.assertRaises(psycopg.Error):
            psycopg.connect(secret('MG_DATABASE_URL').replace('/model_generator?', '/sentinel?'), connect_timeout=1)
        worker=Database(replace(self.settings,db_role='mg_worker',database_url=secret('MG_TEST_WORKER_DATABASE_URL')))
        try: worker.check_schema()
        finally: worker.close()

    def test_invalid_schema_keeps_live_but_blocks_auth_before_body(self):
        with admin_connect() as admin:
            original=admin.execute('SELECT checksum FROM mg.schema_meta WHERE version=1').fetchone()[0]
            try:
                admin.execute('UPDATE mg.schema_meta SET checksum=%s WHERE version=1',('0'*64,))
                with TestClient(make_test_app(self.settings),base_url='https://testserver') as client:
                    self.assertEqual(client.get('/health/live').status_code,200)
                    self.assertEqual(client.get('/health/ready').json()['reason'],'database_not_ready')
                    response=client.post('/api/auth/login',content=b'{',headers={'Origin':'https://testserver','Content-Type':'application/json'})
                    self.assertEqual(response.status_code,503)
            finally: admin.execute('UPDATE mg.schema_meta SET checksum=%s WHERE version=1',(original,))

    def test_thousand_active_users_close_registration(self):
        now=1800000000
        with self.db.transaction() as con:
            con.execute("INSERT INTO users SELECT md5(value::text),'synthetic_'||value,%s,%s,FALSE FROM generate_series(1,1000) AS value",(hash_password(secrets.token_urlsafe(24)),now))
        with TestClient(make_test_app(self.settings),base_url='https://testserver') as client:
            response=client.post('/api/auth/register',json={'username':'over_cap','password':secrets.token_urlsafe(24)},headers={'Origin':'https://testserver'})
            self.assertEqual(response.status_code,429)
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],1000)

    def test_constraints_rollback_and_persistence_new_connections(self):
        record=hash_password(secrets.token_urlsafe(24))
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as con:
                con.execute('INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)',('a'*32,'synthetic',record,1))
                raise RuntimeError('rollback')
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],0)
        with self.db.transaction() as con:
            con.execute('INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)',('a'*32,'synthetic',record,1))
        for sql, params in (("INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)",('invalid','bad',record,1)), ("INSERT INTO sessions VALUES(%s,%s,%s,%s,%s,%s)",('b'*64,'f'*32,'c'*64,1,2,1)), ("INSERT INTO auth_attempts VALUES(%s,%s,%s,%s)",('login','d'*64,1,-1))):
            with self.subTest(sql=sql),self.assertRaises(psycopg.IntegrityError):
                with self.db.transaction() as con: con.execute(sql,params)
        restarted=Database(self.settings)
        with restarted.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],1)
        restarted.close()

    def test_atomic_rate_last_slot_with_two_independent_connections(self):
        key=self.settings.auth_key; now=1800000000
        with self.db.transaction() as con:
            for _ in range(9): consume_limit(con,key,'login-ip','synthetic',now,900,10)
        def consume():
            try:
                with self.db.transaction() as con: consume_limit(con,key,'login-ip','synthetic',now,900,10)
                return 200
            except ApiError as error: return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _:consume(),range(2))),[200,429])
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT count FROM auth_attempts').fetchone()['count'],10)

    def test_concurrent_username_registration_exactly_one_user_event_scope(self):
        app=make_test_app(self.settings)
        lifecycle=enter_fixture_client(self,app)
        clients=[TestClient(app,base_url='https://testserver') for _ in range(2)]
        for client in clients: client.portal=lifecycle.portal
        try:
            payload={'username':'race_user','password':secrets.token_urlsafe(24)}
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(lambda client:client.post('/api/auth/register',json=payload,headers={'Origin':'https://testserver'}).status_code,clients))
            self.assertEqual(sorted(result),[201,409])
            with self.db.connect() as con:
                for table in ('users','usage_events'):
                    self.assertEqual(con.execute('SELECT count(*) AS n FROM '+table).fetchone()['n'],1)
                self.assertEqual(con.execute('SELECT count(*) AS n FROM quota_scopes').fetchone()['n'],2)
        finally:
            for client in clients: client.close()
            close_fixture_client(lifecycle)

    def test_global_last_registration_slot_cannot_exceed_daily_cap(self):
        now=1800000000
        with self.db.transaction() as con:
            con.execute('INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)',('a'*32,'synthetic',hash_password(secrets.token_urlsafe(24)),now))
            for _ in range(99):
                con.execute("INSERT INTO usage_events(action,owner_id,timestamp,bytes) VALUES('register',%s,%s,0)",('a'*32,now))
        app=make_test_app(self.settings)
        lifecycle=enter_fixture_client(self,app)
        clients=[TestClient(app,base_url='https://testserver') for _ in range(2)]
        for client in clients: client.portal=lifecycle.portal
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(lambda pair:pair[1].post('/api/auth/register',json={'username':'last_'+str(pair[0]),'password':secrets.token_urlsafe(24)},headers={'Origin':'https://testserver'}).status_code,enumerate(clients)))
            self.assertEqual(sorted(result),[201,429])
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT count(*) AS n FROM usage_events').fetchone()['n'],100)
        finally:
            for client in clients: client.close()
            close_fixture_client(lifecycle)

    def test_held_quota_lock_is_safe_bounded_503_and_event_loop_live(self):
        app=make_test_app(self.settings)
        with TestClient(app,base_url='https://testserver') as client, self.db.transaction() as held:
            held.execute("SELECT scope FROM quota_scopes WHERE scope='global' FOR UPDATE")
            started=time.monotonic()
            response=client.post('/api/auth/register',json={'username':'locked_user','password':secrets.token_urlsafe(24)},headers={'Origin':'https://testserver'})
            self.assertEqual(response.status_code,503)
            self.assertLess(time.monotonic()-started,2)
            self.assertEqual(client.get('/health/live').status_code,200)
            self.assertEqual(response.headers['retry-after'],'1')
            for forbidden in ('postgres','SELECT','password','model_generator','psycopg'):
                self.assertNotIn(forbidden,response.text)
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],0)

    def test_wrong_schema_versions_checksum_role_and_owner_failclosed(self):
        with admin_connect() as admin:
            originals=admin.execute('SELECT version,checksum,applied_at FROM mg.schema_meta ORDER BY version').fetchall()
            original=originals[0]
            for version,checksum in ((99,original[1]),(1,'0'*64)):
                try:
                    admin.execute('UPDATE mg.schema_meta SET version=%s,checksum=%s WHERE version=1',(version,checksum))
                    with self.assertRaises(RuntimeError): self.db.check_schema()
                finally: admin.execute('UPDATE mg.schema_meta SET version=%s,checksum=%s WHERE version=%s',(original[0],original[1],version))
            try:
                admin.execute('DELETE FROM mg.schema_meta WHERE version=2')
                with self.assertRaises(RuntimeError): self.db.check_schema()
            finally: admin.execute('INSERT INTO mg.schema_meta VALUES(%s,%s,%s)',originals[1])
            try:
                admin.execute('DELETE FROM mg.schema_meta')
                with self.assertRaises(RuntimeError): self.db.check_schema()
            finally:
                for row in originals: admin.execute('INSERT INTO mg.schema_meta VALUES(%s,%s,%s)',row)
            try:
                admin.execute('ALTER SCHEMA mg OWNER TO postgres')
                with self.assertRaises(RuntimeError): self.db.check_schema()
            finally: admin.execute('ALTER SCHEMA mg OWNER TO mg_migrator')
        with self.assertRaises(ValueError): Database(replace(self.settings,database_url=secret('MG_TEST_ADMIN_DATABASE_URL')))

    def test_migration_twice_safe_and_mismatch_refused(self):
        from model_generator.web.migrate import migrate
        migrate(secret('MG_MIGRATION_DATABASE_URL')); migrate(secret('MG_MIGRATION_DATABASE_URL'))
        with admin_connect() as admin:
            original=admin.execute('SELECT checksum FROM mg.schema_meta WHERE version=1').fetchone()[0]
            try:
                admin.execute('UPDATE mg.schema_meta SET checksum=%s WHERE version=1',('0'*64,))
                with self.assertRaises(RuntimeError): migrate(secret('MG_MIGRATION_DATABASE_URL'))
            finally: admin.execute('UPDATE mg.schema_meta SET checksum=%s WHERE version=1',(original,))


class DatabaseDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def _admin_query(self,query,params=None):
        def execute():
            with admin_connect() as admin: return admin.execute(query,params).fetchone()
        return await asyncio.to_thread(execute)

    async def test_deferred_commit_uses_combined_budget_and_cannot_commit_after_503(self):
        def setup():
            reset_database()
            with admin_connect() as admin:
                admin.execute("CREATE FUNCTION mg.test_before_delay() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.85); RETURN NEW; END $$")
                admin.execute("CREATE FUNCTION mg.test_commit_delay() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.85); RETURN NEW; END $$")
                admin.execute('CREATE TRIGGER test_before BEFORE INSERT ON mg.service_state FOR EACH ROW EXECUTE FUNCTION mg.test_before_delay()')
                admin.execute('CREATE CONSTRAINT TRIGGER test_commit AFTER INSERT ON mg.service_state DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mg.test_commit_delay()')
        await asyncio.to_thread(setup)
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(settings_for(Path(tmp)))
            def callback():
                with db.transaction() as con:
                    con.execute("INSERT INTO service_state VALUES('commit-deadline',%s,1)",('a'*32,))
            try:
                start=time.monotonic()
                with self.assertRaises(ApiError) as caught: await db.run(callback)
                self.assertEqual(caught.exception.status,503)
                self.assertLess(time.monotonic()-start,1.7)
                await asyncio.sleep(0.5)
                self.assertIsNone(await self._admin_query("SELECT * FROM mg.service_state WHERE name='commit-deadline'"),'Mutation committed after503')
                self.assertEqual(len(db._running),0)
            finally:
                def cleanup():
                    db.close()
                    with admin_connect() as admin:
                        admin.execute('DROP TRIGGER test_before ON mg.service_state'); admin.execute('DROP TRIGGER test_commit ON mg.service_state')
                        admin.execute('DROP FUNCTION mg.test_before_delay()'); admin.execute('DROP FUNCTION mg.test_commit_delay()')
                await asyncio.to_thread(cleanup)

    async def _transport_stall(self,*,cancel=False,commit=False,preceding_delay=False):
        import threading
        from urllib.parse import urlsplit,urlunsplit
        await asyncio.to_thread(reset_database)
        with tempfile.TemporaryDirectory() as tmp:
            settings=settings_for(Path(tmp)); url=urlsplit(settings.database_url)
            proxy=PostgreSQLProxy(url.hostname,url.port or 5432,pause_on=b'COMMIT\x00' if commit else None)
            netloc=url.netloc.split('@')[0]+'@127.0.0.1:'+str(proxy.address[1])
            db=Database(replace(settings,database_url=urlunsplit((url.scheme,netloc,url.path,url.query,url.fragment))))
            entered=threading.Event(); finished=threading.Event(); pid=[]
            def callback():
                try:
                    with db.transaction() as con:
                        pid.append(con.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
                        if preceding_delay: con.execute('SELECT pg_sleep(0.7)')
                        if commit:
                            con.execute("INSERT INTO service_state VALUES('transport-commit',%s,1)",('a'*32,))
                        else: proxy.paused.set()
                        entered.set()
                        if not commit: con.execute('SELECT 1')
                finally: finished.set()
            task=asyncio.create_task(db.run(callback)); started=time.monotonic()
            try:
                ready=await asyncio.to_thread(entered.wait,1)
                if not ready:
                    # QEMU setup includes connect/refresh commands plus pg_sleep(.7).
                    # Allow only a live callback another .2s to reach the test barrier;
                    # the production1500ms budget and every physical assertion remain.
                    self.assertFalse(finished.is_set(),'Callback already ended before fixture readiness')
                    self.assertFalse(task.done(),'Operation already ended before fixture readiness')
                    ready=await asyncio.to_thread(entered.wait,.2)
                    print(f'Live fixture barrier reached={ready}; elapsed={time.monotonic()-started:.3f}s')
                self.assertTrue(ready)
                self.assertTrue(await asyncio.to_thread(proxy.paused.wait,1))
                if cancel:
                    cancelled=time.monotonic(); task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,0.5)
                    self.assertLess(time.monotonic()-cancelled,0.5)
                else:
                    with self.assertRaises(ApiError) as caught: await task
                    self.assertEqual(caught.exception.status,503)
                    self.assertLess(time.monotonic()-started,1.7)
                self.assertTrue(finished.wait(0.1),'Physical callback still waiting on stalled transport')
                await asyncio.sleep(0.02)
                self.assertEqual(len(db._running),0)
                # Proxy remains paused, including cancellation/EOF: server must release its transaction.
                while time.monotonic()-started<1.8:
                    if not await self._admin_query('SELECT 1 FROM pg_stat_activity WHERE pid=%s',(pid[0],)): break
                    await asyncio.sleep(0.02)
                else: self.fail('Backend transaction survived operation budget')
                self.assertIsNone(await self._admin_query("SELECT * FROM mg.service_state WHERE name='transport-commit'"))
            finally:
                proxy.paused.clear(); await asyncio.to_thread(proxy.close)
                try: await task
                except (ApiError,asyncio.CancelledError): pass
                db.close()

    async def test_stalled_sql_transport_finishes_callback_and_transaction(self):
        await self._transport_stall()

    async def test_stalled_commit_transport_never_commits_after_503(self):
        await self._transport_stall(commit=True)

    async def test_cancelled_stalled_transport_has_bounded_cleanup(self):
        await self._transport_stall(cancel=True)

    async def test_stalled_transport_after_long_sql_releases_backend_by_total_budget(self):
        await self._transport_stall(preceding_delay=True)

    async def test_connection_handshake_has_actual_one_second_deadline(self):
        import socket
        import threading
        from urllib.parse import urlsplit,urlunsplit
        reset_database()
        listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(1)
        finished=threading.Event()
        def stall():
            peer,_=listener.accept()
            try: finished.wait(3)
            finally: peer.close()
        thread=threading.Thread(target=stall); thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            settings=settings_for(Path(tmp)); url=urlsplit(settings.database_url)
            netloc=url.netloc.split('@')[0]+'@127.0.0.1:'+str(listener.getsockname()[1])
            db=Database(replace(settings,database_url=urlunsplit((url.scheme,netloc,url.path,url.query,url.fragment))))
            def connect():
                with db.connect(): self.fail('Non-PostgreSQL listener accepted')
            start=time.monotonic()
            try:
                with self.assertRaises((psycopg.Error,TimeoutError)): await asyncio.to_thread(connect)
                self.assertLess(time.monotonic()-start,1.3)
            finally:
                finished.set(); listener.close(); thread.join(1); db.close()

    async def test_new_connect_refused_when_one_second_budget_is_unavailable(self):
        reset_database()
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(settings_for(Path(tmp)))
            def callback():
                time.sleep(0.6)
                with db.transaction() as con:
                    con.execute("INSERT INTO service_state VALUES('connect-budget',%s,1)",('a'*32,))
            start=time.monotonic()
            try:
                with self.assertRaises(ApiError) as caught: await db.run(callback)
                self.assertEqual(caught.exception.status,503)
                self.assertLess(time.monotonic()-start,1)
                with db.connect() as con:
                    self.assertIsNone(con.execute("SELECT * FROM service_state WHERE name='connect-budget'").fetchone())
            finally: db.close()

    async def test_eight_real_connections_ninth_bounded_rejection(self):
        import threading
        reset_database()
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(settings_for(Path(tmp)))
            entered=threading.Event(); lock=threading.Lock(); count=0
            def callback():
                nonlocal count
                with db.transaction() as con:
                    with lock:
                        count+=1
                        if count==8: entered.set()
                    con.execute('SELECT pg_sleep(0.5)')
            tasks=[asyncio.create_task(db.run(callback)) for _ in range(8)]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,1))
                with admin_connect() as admin:
                    self.assertEqual(admin.execute("SELECT count(*) FROM pg_stat_activity WHERE usename='mg_api'").fetchone()[0],8)
                start=time.monotonic()
                with self.assertRaises(ApiError) as caught: await db.run(lambda:self.fail('Ninth callback was queued'))
                self.assertEqual(caught.exception.status,503)
                self.assertLess(time.monotonic()-start,0.4)
                await asyncio.gather(*tasks)
            finally: db.close()

    async def test_sql_uses_remaining_budget_and_keeps_event_loop_responsive(self):
        reset_database()
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(settings_for(Path(tmp)))
            def callback():
                with db.transaction() as con:
                    con.execute('SELECT pg_sleep(0.8)')
                    con.execute('SELECT pg_sleep(0.8)')
                    con.execute("INSERT INTO service_state VALUES('late-test',%s,1)",('a'*32,))
            start=time.monotonic(); work=asyncio.create_task(db.run(callback))
            ticks=0
            while not work.done():
                await asyncio.sleep(0.02); ticks+=1
            with self.assertRaises(ApiError) as caught: await work
            self.assertEqual(caught.exception.status,503)
            self.assertLess(time.monotonic()-start,1.8)
            self.assertGreater(ticks,20)
            with db.connect() as con:
                self.assertIsNone(con.execute("SELECT * FROM service_state WHERE name='late-test'").fetchone())
            db.close()

    async def test_cancelled_db_callback_never_commits_late(self):
        reset_database()
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(settings_for(Path(tmp)))
            def callback():
                with db.transaction(statement_timeout_ms=1000) as con:
                    con.execute('SELECT pg_sleep(0.5)')
                    con.execute("INSERT INTO service_state VALUES('cancel-test',%s,1)",('a'*32,))
            task=asyncio.create_task(db.run(callback))
            await asyncio.sleep(0.1); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            await asyncio.sleep(0.6)
            with db.connect() as con:
                self.assertIsNone(con.execute("SELECT * FROM service_state WHERE name='cancel-test'").fetchone())
            db.close()
