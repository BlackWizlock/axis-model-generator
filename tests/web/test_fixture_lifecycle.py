"""Failed fixture startup must release the actual client lifespan and file lease."""
import fcntl
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from web import test_upload_chunks


class FixtureLifecycleTests(unittest.TestCase):
    def test_registration_failure_closes_lifespan_before_next_fixture(self):
        events=[]
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory)/'api.lock'
            @asynccontextmanager
            async def lifespan(app):
                with lock.open('a') as stream:
                    fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
                    events.append('entered')
                    try: yield
                    finally:
                        events.append('closed')
                        fcntl.flock(stream,fcntl.LOCK_UN)
            app=FastAPI(lifespan=lifespan)
            app.state.storage=object()
            from dataclasses import make_dataclass
            settings=make_dataclass('Settings',[('min_free_disk_bytes',int)])(0)
            failed=test_upload_chunks.ChunkHTTPTests('test_actual_chunk_hash_failure_does_not_acknowledge')
            with (patch('web.helpers.reset_database'),patch('web.helpers.settings_for',return_value=settings),
                  patch('web.helpers.make_test_app',return_value=app),
                  patch('web.helpers.register_login',side_effect=RuntimeError('Synthetic registration failure'))):
                result=unittest.TestResult()
                failed.run(result)
            self.assertEqual(len(result.errors),1)
            self.assertIn('Synthetic registration failure',result.errors[0][1])
            self.assertEqual(events,['entered','closed'])
            self.assertFalse(Path(failed.tmp.name).exists())
            identity=(lock.stat().st_dev,lock.stat().st_ino)
            next_fixture=test_upload_chunks.ChunkHTTPTests('test_actual_chunk_hash_failure_does_not_acknowledge')
            try:
                with (patch('web.helpers.reset_database'),patch('web.helpers.settings_for',return_value=settings),
                      patch('web.helpers.make_test_app',return_value=app),
                      patch('web.helpers.register_login',return_value={'csrf':'synthetic'})):
                    next_fixture.setUp()
                self.assertEqual((lock.stat().st_dev,lock.stat().st_ino),identity)
                self.assertEqual(events,['entered','closed','entered'])
            finally: next_fixture.doCleanups()
            self.assertEqual(events,['entered','closed','entered','closed'])
