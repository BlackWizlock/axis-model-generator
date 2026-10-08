"""Crash fault occurs after a real repository call, without synthesizing state."""
import ast
import base64
import io
import json
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
class CrashBoundaryTests(unittest.TestCase):
    def test_crash_only_after_claim_or_committed_checkpoint_returns(self):
        code=(ROOT/'tests/fixtures/cold_recovery_crash.py').read_text()
        for mode in ('claim','checkpoint'):
            with self.subTest(mode=mode):
                events=[]
                class Repository:
                    def claim_next(self):
                        events.append('claim');return {'actual':'claim result'} if len(events)>1 else None
                    def checkpoint(self):events.append('durable checkpoint');return None
                class ControlledExit(Exception):pass
                def exit(code):events.append(('exit',code));raise ControlledExit
                def main():
                    repo=Repository()
                    if mode=='claim':repo.claim_next();repo.claim_next()
                    else:repo.checkpoint()
                modules={'model_generator.web.jobs':SimpleNamespace(JobRepository=Repository),'model_generator.web.worker':SimpleNamespace(main=main)}
                with patch.dict(sys.modules,modules),patch.object(sys,'argv',['fixture',mode]),patch('os._exit',side_effect=exit):
                    with self.assertRaises(ControlledExit):exec(compile(code,'cold fixture','exec'),{})
                self.assertEqual(events,['claim','claim',('exit',137)] if mode=='claim' else ['durable checkpoint',('exit',137)])
    def test_host_and_embedded_payloads_compile_without_native_execution(self):
        spec=importlib.util.spec_from_file_location('cold_proof',ROOT/'scripts/ci-web-cold-recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for name in ('SNAPSHOT','IDENTITY','S3_SNAPSHOT','QUOTAS','FINALIZING'):
            ast.parse(getattr(module,name))
        ast.parse((ROOT/'tests/fixtures/cold_recovery_http.py').read_text())
        for node in ast.walk(ast.parse((ROOT/'scripts/ci-web-cold-recovery.py').read_text())):
            if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value.startswith(('import ','from ')):
                ast.parse(node.value)
    def test_probe_cleanup_denies_foreign_container_before_mutation(self):
        import json
        spec=importlib.util.spec_from_file_location('cold_proof_cleanup',ROOT/'scripts/ci-web-cold-recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        proof=object.__new__(module.Proof);proof.project='mg-cold-proof-12345678'
        for label in ('client',proof.project):
            commands=[]
            def run(args,**kwargs):
                commands.append(args)
                return SimpleNamespace(returncode=0,stdout=json.dumps([{'Config':{'Labels':{'com.axis.model-generator.cold-proof':label}}}]))
            proof.run=run
            if label=='client':
                with self.assertRaises(ValueError):proof.remove_probe()
                self.assertEqual(len(commands),1)
            else:
                proof.remove_probe()
                self.assertEqual(commands[-1],['docker','rm','-f',proof.project+'-db-negative'])

    def test_startup_negative_requires_exact_identity_runtime_error(self):
        from contextlib import asynccontextmanager,redirect_stdout
        import io
        spec=importlib.util.spec_from_file_location('cold_proof_negative',ROOT/'scripts/ci-web-cold-recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for error in (None,RuntimeError('API lock identity changed'),
                      RuntimeError('Own PostgreSQL schema unavailable'),ValueError('API lock identity changed')):
            with self.subTest(error=type(error).__name__ if error else 'startup success'):
                events=[]
                @asynccontextmanager
                async def lifespan(app):
                    events.append('entered')
                    try:
                        if error:raise error
                        yield
                    finally:events.append('exited')
                app=SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))
                with patch.dict(sys.modules,{'model_generator.web.app':SimpleNamespace(create_default_app=lambda:app)}),redirect_stdout(io.StringIO()):
                    if isinstance(error,RuntimeError) and str(error)=='API lock identity changed':
                        exec(compile(module.COLD_STARTUP_DENY,'cold actual startup negative','exec'),{})
                    else:
                        with self.assertRaises((AssertionError,ValueError)):
                            exec(compile(module.COLD_STARTUP_DENY,'cold actual startup negative','exec'),{})
                self.assertEqual(events,['entered','exited'])
    def test_image_attestation_denies_missing_or_wrong_revision_and_identity(self):
        from copy import deepcopy
        spec=importlib.util.spec_from_file_location('cold_proof_attest',ROOT/'scripts/ci-web-cold-recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        source='a'*40
        roles=('api','worker','proxy','keeper','emulator')
        manifest={'source_revision':source,'images':{role:{'id':'sha256:'+str(n)*64} for n,role in enumerate(roles)}}
        images={value['id']:{'Id':value['id'],'Architecture':'amd64','Os':'linux',
            'Config':{'Labels':{'org.opencontainers.image.revision':source}}} for value in manifest['images'].values()}
        with patch.object(module.cold,'inspect',side_effect=lambda id:images[id]):
            module.attest_images(manifest,source)
            for role in ('api','worker'):
                image=images[manifest['images'][role]['id']]
                for labels in (None,{}, {'org.opencontainers.image.revision':'b'*40}):
                    with self.subTest(role=role,labels=labels):
                        image['Config']['Labels']=labels
                        with self.assertRaises(SystemExit):module.attest_images(manifest,source)
                image['Config']['Labels']={'org.opencontainers.image.revision':source}
            for field,value in (('Id','sha256:'+'f'*64),('Architecture','arm64'),('Os','windows')):
                original=deepcopy(images)
                images[manifest['images']['api']['id']][field]=value
                with self.assertRaises(SystemExit):module.attest_images(manifest,source)
                images=original
        for candidate in ('8e42b74','A'*40,'g'*40,'b'*40):
            with patch.object(module.cold,'inspect') as inspect:
                with self.assertRaises(SystemExit):module.attest_images(manifest,candidate)
                inspect.assert_not_called()

    def exercise_http_fixture(self,mode,me_status=200,with_cookie=True,ids=None,upload_status=201):
        from urllib import request as http_request
        code=(ROOT/'tests/fixtures/cold_recovery_http.py').read_text()
        calls=[];sent=[];ids=[] if ids is None else ids
        payload={'mode':mode,'fixture':base64.b64encode(b'fixture').decode()}
        cookie='__Host-mg_session=existing-session'
        if with_cookie:payload['cookie']=cookie
        class Response:
            def __init__(inner,status,body):inner.status=status;inner.headers={};inner.body=body
            def __enter__(inner):return inner
            def __exit__(inner,*args):return False
            def read(inner):return json.dumps(inner.body).encode()
        def urlopen(req,**kwargs):
            path=req.full_url.removeprefix('http://localhost:8000');calls.append((req.method,path))
            self.assertEqual(req.get_header('Cookie'),cookie)
            if path=='/api/auth/me':return Response(me_status,{'csrfToken':'existing-csrf'})
            self.assertNotEqual(path,'/api/auth/guest','Fixture must never mint a guest')
            self.assertEqual(req.get_header('X-csrf-token'),'existing-csrf')
            if path=='/api/uploads':
                if upload_status!=201:return Response(upload_status,{'code':'csrf_invalid'})
                ids.append('upload-'+str(len(ids)+1));return Response(201,{'id':ids[-1]})
            if path.endswith('/content'):return Response(200,{})
            if path=='/api/jobs':
                self.assertEqual(json.loads(req.data)['uploadId'],ids[-1]);return Response(201,{'id':'job-'+str(len(ids))})
            raise AssertionError('Unexpected fixture HTTP request')
        class InterruptedStop(RuntimeError):pass
        output=io.StringIO()
        with patch.object(http_request,'urlopen',side_effect=urlopen),patch.object(sys,'stdin',io.StringIO(json.dumps(payload))),patch.object(sys,'stdout',output),patch('socket.create_connection',return_value=SimpleNamespace(sendall=lambda data:sent.append(data))),patch('time.sleep',side_effect=InterruptedStop):
            if mode=='interrupt' and me_status==200 and with_cookie and upload_status==201:
                with self.assertRaises(InterruptedStop):exec(code,{})
            elif me_status!=200 or upload_status!=201:
                with self.assertRaises(AssertionError):exec(code,{})
            elif not with_cookie:
                with self.assertRaises(KeyError):exec(code,{})
            else:exec(code,{})
        return calls,sent,output.getvalue(),ids
    def test_job_and_interrupt_reuse_session_csrf_and_fresh_uploads(self):
        ids=[]
        for mode in ('job','job','interrupt'):
            calls,sent,wire,ids=self.exercise_http_fixture(mode,ids=ids)
            self.assertEqual(calls[0],('GET','/api/auth/me'))
            self.assertNotIn(('POST','/api/auth/guest'),calls)
            self.assertEqual(json.loads(wire)['cookie'],'__Host-mg_session=existing-session')
            if mode=='interrupt':
                self.assertEqual(json.loads(wire)['id'],ids[-1])
                self.assertEqual(len(sent),1)
                self.assertTrue(sent[0].startswith(('PUT /api/uploads/'+ids[-1]+'/content HTTP/1.1').encode()))
                self.assertTrue(sent[0].endswith(b'\r\n\r\nf'))
        self.assertEqual(len(set(ids)),3)
    def test_denied_auth_or_missing_cookie_has_no_guest_fallback_or_reservation(self):
        for mode in ('job','interrupt'):
            calls,sent,wire,ids=self.exercise_http_fixture(mode,me_status=401)
            self.assertEqual(calls,[('GET','/api/auth/me')]);self.assertEqual((sent,wire,ids),([],'',[]))
            calls,sent,wire,ids=self.exercise_http_fixture(mode,with_cookie=False)
            self.assertEqual((calls,sent,wire,ids),([],[],'',[]))
            calls,sent,wire,ids=self.exercise_http_fixture(mode,upload_status=403)
            self.assertEqual(calls,[('GET','/api/auth/me'),('POST','/api/uploads')]);self.assertEqual((sent,wire,ids),([],'',[]))
    def test_interrupted_eof_reports_primary_exit_instead_of_json_error(self):
        spec=importlib.util.spec_from_file_location('cold_eof',ROOT/'scripts/ci-web-cold-recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        proof=object.__new__(module.Proof);proof.phase='seed-interrupted-single-put'
        proof.interrupt=SimpleNamespace(stdout=io.StringIO(''),stderr=io.StringIO('AssertionError: HTTP authentication denied'),wait=lambda **kwargs:1)
        with self.assertRaisesRegex(RuntimeError,'Interrupted HTTP fixture exited before ready signal .exit 1.'):
            proof.interrupted_ready()
        self.assertIn('exit=1',proof.interrupt_failure);self.assertIn('HTTP authentication denied',proof.interrupt_failure)
