"""Real file and PostgreSQL generation fences, without forged accepted state."""
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from model_generator.web.lock_identity import initialize,generation,valid
from web.helpers import TestClient,make_test_app,settings_for,reset_database,admin_connect


class LockGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_normal_restart_preserves_generation_and_malformed_fails_closed(self):
        path=self.root/'api.lock'
        with open(path,'w+b') as file:
            fcntl.flock(file,fcntl.LOCK_EX)
            first=initialize(file.fileno())
            info=os.fstat(file.fileno())
            self.assertTrue(valid(file.fileno(),path,(info.st_dev,info.st_ino,first)))
        with open(path,'r+b') as file:
            fcntl.flock(file,fcntl.LOCK_EX)
            self.assertEqual(initialize(file.fileno()),first)
            file.seek(0);file.write(b'x');file.flush()
            malformed=path.read_bytes()
            with self.assertRaisesRegex(RuntimeError,'API lock identity changed'):initialize(file.fileno())
            self.assertEqual(path.read_bytes(),malformed)

    def test_changed_generation_same_actual_inode_fails_runtime_and_database(self):
        reset_database()
        settings=settings_for(self.root)
        with TestClient(make_test_app(settings),base_url='https://testserver') as client:
            with admin_connect() as con:
                old=con.execute('SELECT device,inode,generation FROM mg.api_lock_identity').fetchone()
                before=con.execute('SELECT * FROM mg.service_state').fetchall()
            path=self.root/'api.lock'
            fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:
                info=os.fstat(fd)
                self.assertEqual((info.st_dev,info.st_ino),old[:2])
                # A real same-inode content replacement exercises the ABA fence
                # even on kernels that do not deterministically recycle tmpfs IDs.
                replacement=('b' if old[2]!='b'*32 else 'c')*32
                os.pwrite(fd,replacement.encode(),0);os.fsync(fd)
                self.assertFalse(client.app.state.storage.api_lock_valid())
            finally:os.close(fd)
        with self.assertRaisesRegex(RuntimeError,'API lock identity changed'):
            with TestClient(make_test_app(settings),base_url='https://testserver'):pass
        with admin_connect() as con:
            self.assertEqual(con.execute('SELECT device,inode,generation FROM mg.api_lock_identity').fetchone(),old)
            self.assertEqual(con.execute('SELECT * FROM mg.service_state').fetchall(),before)

    def test_hardlink_and_replaced_path_fail_retained_fd(self):
        path=self.root/'api.lock'
        with open(path,'w+b') as file:
            fcntl.flock(file,fcntl.LOCK_EX);nonce=initialize(file.fileno());info=os.fstat(file.fileno())
            expected=(info.st_dev,info.st_ino,nonce)
            os.link(path,self.root/'alias')
            self.assertFalse(valid(file.fileno(),path,expected))
            (self.root/'alias').unlink();path.unlink();path.write_bytes(nonce.encode())
            self.assertFalse(valid(file.fileno(),path,expected))
