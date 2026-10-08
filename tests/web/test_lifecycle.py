"""Owner durable lifecycle acknowledgement, locks and cleanup."""
import time
import unittest
from web import test_jobs as job_helpers
from web.helpers import admin_connect
from model_generator.web.worker import sweep

class LifecycleTests(unittest.TestCase):
    setUp=job_helpers.JobTests.setUp
    upload=job_helpers.JobTests.upload
    create=job_helpers.JobTests.create
    def test_delete_idempotent_immediate_access_gate_and_exact_release(self):
        dto=self.create(self.upload()).json(); url='/api/jobs/'+dto['id']
        response=self.client.delete(url,headers=self.headers); self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.client.delete(url,headers=self.headers).status_code,202)
        self.assertEqual(self.client.get(url).status_code,404)
        now=int(self.app.state.clock()); sweep(self.app.state.db,self.settings,now); sweep(self.app.state.db,self.settings,now)
        self.assertEqual(self.client.delete(url,headers=self.headers).status_code,204)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT storage_bytes,active_jobs FROM quota_scopes WHERE scope=%s',(self.owner['userId'],)).fetchone(),{'storage_bytes':0,'active_jobs':0})
    def test_cancel_lock_contention_503_under_two_seconds(self):
        dto=self.create(self.upload()).json()
        with admin_connect() as con:
            con.autocommit=False; con.execute("SELECT scope FROM mg.quota_scopes WHERE scope='global' FOR UPDATE")
            start=time.monotonic(); response=self.client.post('/api/jobs/'+dto['id']+'/cancel',headers=self.headers)
            self.assertEqual(response.status_code,503,response.text); self.assertLess(time.monotonic()-start,2)
            con.rollback()
        self.assertFalse(self.client.get('/api/jobs/'+dto['id']).json()['cancelRequested'])
