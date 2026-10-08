"""Truthful public DTOs and safe readiness."""
from web.helpers import enter_fixture_client, close_fixture_client
from pathlib import Path
import tempfile
import unittest
from web.helpers import TestClient,make_test_app,settings_for,reset_database
from model_generator.web.preview import validate_preview,PreviewLimits

class ConfigTests(unittest.TestCase):
    def setUp(self):
        reset_database(); self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app=make_test_app(settings_for(Path(self.tmp.name)))
        self.client=enter_fixture_client(self,self.app)
    def test_truthful_public_contract_demo_and_plugin(self):
        response=self.client.get('/api/config'); self.assertEqual(response.status_code,200,response.text)
        dto=response.json(); self.assertEqual(dto['sourceLink'],'https://github.com/BlackWizlock/axis-model-generator')
        self.assertEqual(dto['inputKinds'],['zip-fbx','portable-package'])
        rows={row['id']:row for row in dto['inputFormats']}
        self.assertEqual(set(rows),{'zip-fbx','portable-package','fbx','ifc','glb','gltf','obj','rvt','dwg','skp','3dm'})
        self.assertEqual([row['id'] for row in dto['inputFormats'] if row['upload']],dto['inputKinds'])
        self.assertEqual([row['id'] for row in dto['inputFormats'] if row['diagnostics']],['zip-fbx','portable-package'])
        self.assertEqual([row['id'] for row in dto['inputFormats'] if row['preview']],['portable-package'])
        self.assertFalse(any(row['generation'] for row in dto['inputFormats']))
        self.assertEqual(dto['capabilities'],{kind:{'availability':'unavailable','reason':'generation_not_implemented'} for kind in ('npm','vpm','ifc')})
        for kind in ('fbx','ifc','glb','gltf','obj','rvt','dwg','skp','3dm'):
            self.assertEqual(rows[kind]['reason'],'engine_unavailable')
        self.assertEqual(dto['limits']['retentionSeconds'],86400)
        self.assertFalse(dto['plugin']['available'])
        self.assertEqual(dto['profileStatus'],'research')
        self.assertEqual(set(dto),{'inputFormats','inputKinds','limits','profileStatus','coverage','capabilities','sourceLink','plugin'})
        self.assertNotIn('owner',str(dto)); self.assertNotIn('object_key',str(dto))
        self.assertEqual(self.client.get('/api/plugin/releases').json(),{'available':False,'releases':[]})
        demo=self.client.get('/api/demo/preview'); self.assertEqual(demo.status_code,200,demo.text)
        self.assertEqual(demo.json(),self.client.get('/api/demo/preview').json())
        validate_preview(demo.json(),PreviewLimits()); self.assertEqual(demo.json()['provenance'],'synthetic')
        self.assertEqual(demo.headers['cache-control'],'no-store')
    def test_missing_worker_readiness_safe_dto(self):
        response=self.client.get('/health/ready'); self.assertEqual(response.status_code,503)
        self.assertEqual(response.json(),{'status':'unavailable','reason':'worker_not_ready'})
    def test_stale_future_runtime_storage_gates_keep_exact_safe_reason(self):
        from unittest.mock import patch
        from psycopg.types.json import Jsonb
        from model_generator.web.preview_runner import installed_preview_fingerprint
        from web.helpers import admin_connect
        from model_generator.web.security import ApiError
        now=int(self.app.state.clock())
        with admin_connect() as con:
            con.execute("INSERT INTO mg.worker_state(singleton,epoch,heartbeat,guard_verified,runtime_verified,runtime_version,runtime_fingerprint) VALUES(TRUE,%s,%s,TRUE,TRUE,'python-cpu-1',%s)",('a'*32,now,Jsonb(installed_preview_fingerprint())))
        for stamp in (now-16,now+1):
            with admin_connect() as con: con.execute('UPDATE mg.worker_state SET heartbeat=%s',(stamp,))
            response=self.client.get('/health/ready')
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json(),{'status':'unavailable','reason':'worker_not_ready'})
        with admin_connect() as con: con.execute('UPDATE mg.worker_state SET heartbeat=%s',(now,))
        self.assertEqual(self.client.get('/health/ready').status_code,200)
        with patch.object(self.app.state.storage.objects,'probe',side_effect=ApiError('storage_unavailable','Safe unavailable',503)):
            response=self.client.get('/health/ready'); self.assertEqual(response.status_code,503)
            self.assertEqual(response.json(),{'status':'unavailable','reason':'storage_not_ready'})
