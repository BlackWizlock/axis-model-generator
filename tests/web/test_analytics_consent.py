"""Consent receipts persist independently of guests and fail closed."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from web.helpers import (admin_connect,close_fixture_client,enter_fixture_client,
                         make_test_app,reset_database,settings_for)


class AnalyticsConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); reset_database()
        with admin_connect() as con:
            con.execute('TRUNCATE mg.analytics_consents,mg.analytics_consent_versions')
        self.document=root/'analytics-consent.html'; self.document.write_text('<html><!-- analytics-consent-document-v1:start -->Consent version one<!-- analytics-consent-document-v1:end --><script src="old.js"></script></html>',encoding='utf-8')
        self.sha=hashlib.sha256(b'Consent version one').hexdigest()
        self.patch=patch('model_generator.web.analytics_routes.DOCUMENT_PATH',self.document)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.app=make_test_app(settings_for(root/'data'))
        self.client=enter_fixture_client(self,self.app)
        self.headers={'Origin':'https://testserver'}

    def post(self,value,**kwargs):
        return self.client.post('/api/analytics/consent',json=value,headers=self.headers,**kwargs)

    def grant(self):
        response=self.post({'action':'grant','version':1,'documentSha256':self.sha})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_grant_hash_only_exact_document_no_guest_or_cookie(self):
        now=int(self.app.state.clock()); result=self.grant()
        self.assertTrue(result['allowed']); self.assertRegex(result['receipt'],r'^[a-f0-9]{64}$')
        self.assertEqual(result['expiresAt'],now+30*86400)
        self.assertEqual(len(self.client.cookies),0)
        with self.app.state.db.connect() as con:
            row=con.execute('SELECT * FROM analytics_consents').fetchone()
            version=con.execute('SELECT * FROM analytics_consent_versions').fetchone()
            self.assertEqual(con.execute('SELECT count(*) AS n FROM users').fetchone()['n'],0)
            self.assertEqual(con.execute('SELECT count(*) AS n FROM sessions').fetchone()['n'],0)
        self.assertNotIn(result['receipt'],tuple(row.values()))
        self.assertEqual(row['receipt_hash'],hashlib.sha256(result['receipt'].encode()).hexdigest())
        self.assertEqual(row['purge_at'],now+3*365*86400)
        self.assertEqual(version['content_text'],'Consent version one')
        self.assertEqual(version['document_sha256'],self.sha)

    def test_check_withdraw_unknown_expiry_restart(self):
        receipt=self.grant()['receipt']
        close_fixture_client(self.client)
        self.client=enter_fixture_client(self,self.app)
        self.assertTrue(self.post({'action':'check','receipt':receipt}).json()['allowed'])
        self.assertEqual(self.post({'action':'withdraw','receipt':receipt}).json(),{'allowed':False})
        self.assertEqual(self.post({'action':'withdraw','receipt':receipt}).json(),{'allowed':False})
        unknown='0'*64
        for value in (receipt,unknown):
            self.assertEqual(self.post({'action':'check','receipt':value}).json(),{'allowed':False,'expiresAt':None})
        second=self.grant()['receipt']; self.app.state.clock.tick(30*86400)
        self.assertEqual(self.post({'action':'check','receipt':second}).json(),{'allowed':False,'expiresAt':None})
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM analytics_consents').fetchone()['n'],2)

    def test_current_document_change_invalidates_and_same_version_rejected(self):
        receipt=self.grant()['receipt']; self.document.write_text('<html><!-- analytics-consent-document-v1:start -->Changed<!-- analytics-consent-document-v1:end --></html>')
        self.assertFalse(self.post({'action':'check','receipt':receipt}).json()['allowed'])
        sha=hashlib.sha256(b'Changed').hexdigest()
        self.assertEqual(self.post({'action':'grant','version':1,'documentSha256':sha}).status_code,503)
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT content_text FROM analytics_consent_versions').fetchone()['content_text'],'Consent version one')

    def test_origin_strict_inputs_limits_and_no_query_receipts(self):
        valid={'action':'grant','version':1,'documentSha256':self.sha}
        self.assertEqual(self.client.post('/api/analytics/consent',json=valid).status_code,403)
        self.assertEqual(self.client.post('/api/analytics/consent',json=valid,headers={'Origin':'https://evil.invalid'}).status_code,403)
        for value in ({**valid,'version':True},{**valid,'documentSha256':'0'*64},{**valid,'extra':1},
                      {'action':'check','receipt':123},{'action':'check','receipt':'short'},{'action':'other'}):
            self.assertEqual(self.post(value).status_code,422)
        self.assertEqual(self.client.post('/api/analytics/consent?receipt=secret',json=valid,headers=self.headers).status_code,422)
        self.assertEqual(self.client.post('/api/analytics/consent',content='{"action":"check","action":"grant"}',headers={**self.headers,'Content-Type':'application/json'}).status_code,400)
        self.assertEqual(self.post({'action':'check','receipt':'x'*5000}).status_code,413)
        for _ in range(10): self.grant()
        self.assertEqual(self.post(valid).status_code,429)
        with self.app.state.db.connect() as con:
            rows=con.execute("SELECT digest FROM auth_attempts WHERE action LIKE 'analytics-%'").fetchall()
            self.assertNotIn('testclient',str(rows))

    def test_purge_removes_receipts_retains_immutable_document(self):
        from model_generator.web.analytics_routes import cleanup_consents
        self.grant(); self.app.state.clock.tick(3*365*86400)
        cleanup_consents(self.app.state.db,int(self.app.state.clock()))
        with self.app.state.db.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) AS n FROM analytics_consents').fetchone()['n'],0)
            self.assertEqual(con.execute('SELECT count(*) AS n FROM analytics_consent_versions').fetchone()['n'],1)

    def test_unavailable_document_and_database_never_allow(self):
        self.document.unlink()
        self.assertEqual(self.post({'action':'grant','version':1,'documentSha256':self.sha}).status_code,503)
        self.document.write_text('Consent version one')
        from model_generator.web.security import ApiError
        with patch.object(self.app.state.db,'run',side_effect=ApiError('database_unavailable','Unavailable',503)):
            self.assertEqual(self.post({'action':'check','receipt':'0'*64}).status_code,503)

    def test_global_admission_and_worker_privilege(self):
        from model_generator.web.auth import rate_digest
        now=int(self.app.state.clock())
        with self.app.state.db.transaction() as con:
            con.execute('INSERT INTO auth_attempts(action,digest,window_start,count) VALUES(%s,%s,%s,1000)',
                        ('analytics-global',rate_digest(self.app.state.settings.auth_key,'analytics-global','global'),now//86400*86400))
            row=con.execute("SELECT has_table_privilege('mg_worker','mg.analytics_consents','SELECT') AS allowed").fetchone()
            self.assertFalse(row['allowed'])
        self.assertEqual(self.post({'action':'grant','version':1,'documentSha256':self.sha}).status_code,429)

    def test_bundle_change_keeps_exact_document_and_existing_receipt(self):
        receipt=self.grant()['receipt']
        self.document.write_text(self.document.read_text().replace('old.js','new-bundle.js'),encoding='utf-8')
        self.assertTrue(self.post({'action':'check','receipt':receipt}).json()['allowed'])
        self.assertEqual(self.post({'action':'grant','version':1,'documentSha256':self.sha}).status_code,200)

    def test_cold_new_path_cannot_enable_legacy_consent_exception(self):
        import importlib.util
        script=Path('/app/scripts/ci-web-cold-recovery.py')
        spec=importlib.util.spec_from_file_location('consent_cold_review',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        # The guard precedes all subprocess use: exercise its exact method with an inert receiver.
        import types
        method=next(value.api for value in vars(module).values() if isinstance(value,type) and hasattr(value,'api'))
        with self.assertRaises(ValueError):method(types.SimpleNamespace(phase='bootstrap'),Path('unused'),{'legacyWithoutConsent':True})
