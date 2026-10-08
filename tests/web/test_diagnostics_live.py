"""Real diagnostics contracts: evidence, isolation, bounded progress and journal."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from model_generator.web.security import ApiError, error_response


class DiagnosticContractTests(unittest.TestCase):
    def test_service_error_explains_recovery_without_raw_exception(self):
        response = error_response(ApiError('database_unavailable', 'Service is temporarily unavailable.', 503), 'a' * 32)
        value = json.loads(response.body)['error']
        self.assertIn('nextAction', value)
        self.assertTrue(value['retryable'])
        self.assertIn('повтор', value['nextAction'].lower())
        self.assertEqual(value['request_id'], 'a' * 32)
        self.assertEqual(value['code'], 'database_unavailable')

    def test_observer_and_journal_contracts_exist(self):
        for module in ('model_generator.web.progress', 'model_generator.web.journal', 'model_generator.web.checklist'):
            self.assertIsNotNone(importlib.util.find_spec(module), module)

    def test_inventory_covers_actual_static_and_dynamic_codes(self):
        import ast
        from model_generator.web.diagnostic_catalog import RULE_TITLES,ERROR_CODES
        from model_generator.package_manifest import AREAS
        root=Path(__file__).parents[2]/'src'/'model_generator'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.Constant) and isinstance(node.value,str):
                    code=node.value
                    if code.startswith(('package.','zip.','fbx.','png.','profile.')) and not code.endswith('.') and ' ' not in code and len(code)<90:
                        self.assertIn(code,RULE_TITLES,(path.name,code))
        for area in AREAS: self.assertIn('package.'+area,RULE_TITLES)
        for path in (root/'web').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='ApiError' and node.args and isinstance(node.args[0],ast.Constant):
                    self.assertIn(node.args[0].value,ERROR_CODES,(path.name,node.args[0].value))

    def test_actual_multifile_failure_dominates_and_no_empty_domain_pass(self):
        import hashlib
        from fixtures.builders import zip_bytes,scene_bytes
        from model_generator.web.validation_child import validate_input,ChildSettings
        from model_generator.web.progress import Observer
        from model_generator.web.checklist import presentation,details
        wire=zip_bytes([('good.fbx',scene_bytes()),('bad.fbx',scene_bytes(indices=(0,1,2,-1)))])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'input.zip').write_bytes(wire)
            observed=[]
            class Capture(Observer):
                def _update(self,changes):
                    super()._update(changes); observed.append(json.loads(json.dumps(self.snapshot())))
            observer=Capture(root,hashlib.sha256(wire).hexdigest(),'a'*32)
            report=validate_input(root/'input.zip','zip-fbx',ChildSettings(root),observer).report
            checking=next(value for value in observed if any(row['id']=='input.fbx' and row['state']=='checking' for row in value['checks']))
            self.assertFalse(any(row['id']=='fbx.integrity' and row['state']=='passed' for row in checking['checks']))
            job={'id':'b'*32,'input_sha256':report['input_sha256'],'worker_epoch':'a'*32,'input_kind':'zip-fbx','state':'completed','coverage':report['coverage'],'updated_at':100}
            value=presentation(job,report=report)
            self.assertTrue(value['authoritative'])
            self.assertEqual(next(row['state'] for row in value['checks'] if row['id']=='fbx.triangulation'),'failed')
            self.assertEqual(next(row['state'] for row in value['checks'] if row['id']=='ids_validation'),'not_checked')
            report['report_truncated']=True; report['original_findings_count']=10000
            value=presentation(job,report=report)
            self.assertGreater(value['findings']['omitted'],0)
            self.assertNotIn('passed',[row['state'] for row in value['checks']])
            report['findings'].append({'rule_id':'future.rule','status':'fail','file':'/private/SECRET.rvt','actual':'<script>SECRET</script>','expected':None,'message':'unknown'})
            help=details(job,None,report,'future.rule',0,50)
            self.assertEqual(help['target'],'support')
            self.assertNotIn('SECRET',json.dumps(help))
            self.assertIsNone(help['findings'][0]['elementKey'])

    def test_actual_portable_package_and_empty_zip_keep_coverage_truthful(self):
        import hashlib
        from fixtures.package_builders import make_package
        from fixtures.builders import zip_bytes
        from model_generator.web.validation_child import validate_input,ChildSettings
        from model_generator.web.progress import Observer
        from model_generator.web.checklist import presentation
        cases=[('portable-package',make_package(),True),
               ('portable-package',make_package(manifest_updates={'package_version':{'major':2,'minor':0}}),False),
               ('zip-fbx',zip_bytes([]),False)]
        for kind,wire,valid in cases:
            with self.subTest(kind=kind,valid=valid),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); path=root/'input.zip'; path.write_bytes(wire)
                plain=validate_input(path,kind,ChildSettings(root)).report
                # Preview output is a one-shot artifact; use a fresh child directory.
                (root/'preview-input.json').unlink(missing_ok=True)
                observer=Observer(root,hashlib.sha256(wire).hexdigest(),'a'*32)
                report=validate_input(path,kind,ChildSettings(root),observer).report
                self.assertEqual(report['findings'],plain['findings'])
                self.assertEqual(report['coverage'],plain['coverage'])
                job={'id':'b'*32,'input_sha256':report['input_sha256'],'worker_epoch':'a'*32,'input_kind':kind,'state':'completed','coverage':report['coverage'],'updated_at':100}
                states={row['id']:row['state'] for row in presentation(job,report=report)['checks']}
                self.assertEqual(states['ids_validation'],'not_checked')
                if valid:
                    self.assertEqual(states['input.package'],'passed')
                    self.assertNotIn(states['scene.complete'],('checking','waiting','failed'))
                elif kind=='zip-fbx':
                    self.assertEqual(states['input.fbx'],'not_checked')
                    self.assertFalse(any(state=='passed' for code,state in states.items() if code.startswith('fbx.')))
                else: self.assertEqual(states['package.unsupported'],'failed')

    def test_catalog_gives_condition_specific_recovery(self):
        from model_generator.web.diagnostic_catalog import rule_help,RULE_RECOVERY,KNOWN_CHECKS
        self.assertEqual(set(RULE_RECOVERY),set(KNOWN_CHECKS))
        cases={
            'fbx.unsupported':('export',('7400','бинар'),('экспорт',)),
            'zip.encrypted':('export',('шифр',),('парол',)),
            'zip.duplicate':('export',('регистр','нормализ'),('имен',)),
            'zip.path':('export',('пут',),('..',)),
            'fbx.resource':('export',('PNG','связ'),('встро',)),
            'png.integrity':('export',('CRC','пиксел'),('PNG',)),
            'package.reference':('export',('ссыл','отсутств'),('идентификатор',)),
            'package.unsupported':('export',('ZIP64','верси'),('формат',)),
            'package.uv':('export',('UV',),('координат',)),
            'package.coordinates':('export',('контрольн','высот'),('точ',)),
            'profile.png_dimensions':('profile',('128','256'),('профил',)),
            'profile.png_size':('profile',('3000000','3145728'),('единиц',)),
            'fbx.triangulation':('model',('треуголь',),('триангуляц',)),
        }
        for code,(target,causes,actions) in cases.items():
            with self.subTest(code=code):
                value=rule_help(code); self.assertEqual(value['target'],target)
                for text in causes: self.assertIn(text.lower(),value['why'].lower())
                for text in actions: self.assertIn(text.lower(),value['nextAction'].lower())
        self.assertEqual(rule_help('future.unknown')['target'],'support')
        self.assertEqual(rule_help('package.ifc_semantics')['target'],'external')

    def test_actual_parser_findings_choose_export_recovery(self):
        import struct
        from fixtures.builders import zip_bytes
        from model_generator.validator import validate_bytes
        from model_generator.web.diagnostic_catalog import rule_help
        # Actual normalized duplicates, traversal, encrypted entry and unsupported FBX.
        encrypted=bytearray(zip_bytes([('one.fbx',b'unsupported')]))
        for signature,offset in ((b'PK\x03\x04',6),(b'PK\x01\x02',8)):
            at=encrypted.index(signature)+offset
            struct.pack_into('<H',encrypted,at,struct.unpack_from('<H',encrypted,at)[0]|1)
        cases=[(zip_bytes([('a.fbx',b'ascii')]),'fbx.unsupported'),
               (zip_bytes([('A.fbx',b'a'),('a.fbx',b'b')]),'zip.duplicate'),
               (zip_bytes([('../a.fbx',b'a')]),'zip.path'),(bytes(encrypted),'zip.encrypted')]
        for wire,code in cases:
            with self.subTest(code=code):
                report=validate_bytes(wire)
                self.assertTrue(any(f.rule_id==code and f.status=='fail' for f in report.findings))
                self.assertEqual(rule_help(code)['target'],'export')

    def test_journal_repairs_actual_partial_trailing_record_after_reopen(self):
        from model_generator.web.journal import Journal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'journal'; journal=Journal(root,'worker')
            journal.emit('preview','preview_resource','a'*32); journal.queue.join(); journal.close()
            path=next(root.glob('*.jsonl')); prefix=path.read_bytes()
            with path.open('ab') as stream: stream.write(b'{"requestId":"interrupted')
            journal=Journal(root,'worker')
            journal.emit('preview','preview_runtime_unavailable','b'*32); journal.queue.join(); journal.close()
            self.assertEqual(journal.health()['failed'],0)
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row['requestId'] for row in rows],['a'*32,'b'*32])
            self.assertTrue(path.read_bytes().startswith(prefix))
            self.assertNotIn(b'interrupted',path.read_bytes())

    def test_sidecar_rejects_symlink_oversize_replay_unknown_and_wrong_source(self):
        import os
        from model_generator.web.progress import Observer,ProgressReader,MAX_BYTES
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); observer=Observer(root,'a'*64,'b'*32)
            observer('input.archive','checking')
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:
                reader=ProgressReader(fd,'a'*64,'b'*32)
                value=reader.read(); self.assertEqual(value['sequence'],1)
                self.assertIsNone(reader.read())
                for change in ({'sequence':0},{'inputHash':'c'*64},{'checks':[dict(value['checks'][0],id='bad.id')]},{'checks':[dict(value['checks'][0],count=True)]}):
                    (root/'progress.json').write_text(json.dumps({**value,**change}))
                    with self.assertRaises(ValueError): reader.read()
                (root/'progress.json').write_bytes(b'x'*(MAX_BYTES+1))
                with self.assertRaises(ValueError): reader.read()
                (root/'progress.json').unlink(); (root/'progress.json').symlink_to(root/'outside')
                with self.assertRaises(OSError): reader.read()
            finally: os.close(fd)

    def test_progress_terminal_failure_cannot_regress(self):
        import os
        from model_generator.web.progress import Observer,ProgressReader
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); observer=Observer(root,'a'*64,'b'*32)
            observer('input.archive','failed')
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:
                reader=ProgressReader(fd,'a'*64,'b'*32); value=reader.read()
                for state in ('passed','checking'):
                    row={**value['checks'][0],'state':state,'sequence':2,'completedAt':None if state=='checking' else 1}
                    (root/'progress.json').write_text(json.dumps({**value,'sequence':2,'checks':[row]}))
                    with self.assertRaises(ValueError): reader.read()
            finally: os.close(fd)

    def test_journal_concurrent_writers_and_bounded_queue_are_observable(self):
        import threading
        import time
        from unittest.mock import patch
        from model_generator.web.journal import Journal,QUEUE_RECORDS,FALLBACK_RECORDS,SLOTS
        with tempfile.TemporaryDirectory() as tmp:
            journals=[Journal(Path(tmp)/'shared','api') for _ in range(2)]
            for index in range(60):
                for journal in journals: journal.emit('request','internal_error',f'{index:032x}')
            for journal in journals:
                journal.queue.join(); journal.close()
            rows=[json.loads(line) for path in journals[0].root.glob('*.jsonl') for line in path.read_text().splitlines()]
            self.assertEqual(len(rows),sum(journal.written for journal in journals))
            self.assertEqual(sum(journal.written+journal.failed+journal.dropped for journal in journals),120)
            self.assertLessEqual(len(list(journals[0].root.glob('*.jsonl'))),SLOTS)
            entered=threading.Event(); release=threading.Event()
            def blocked(self,wire): entered.set(); release.wait(3)
            with patch.object(Journal,'_append',blocked):
                journal=Journal(Path(tmp)/'blocked','worker')
                journal.emit('worker','internal_error','a'*32); self.assertTrue(entered.wait(1))
                begin=time.monotonic()
                for _ in range(QUEUE_RECORDS+FALLBACK_RECORDS+10): journal.emit('worker','internal_error','b'*32)
                self.assertLess(time.monotonic()-begin,.2)
                self.assertGreater(journal.health()['dropped'],0)
                self.assertEqual(journal.health()['fallbackRecords'],FALLBACK_RECORDS)
                self.assertFalse(journal.health()['available'])
                release.set(); journal.queue.join(); journal.close()

    def test_journal_durable_rotation_restart_and_no_sensitive_values(self):
        import time
        from model_generator.web.journal import Journal,SLOTS,SLOT_BYTES
        def drain(journal):
            until=time.monotonic()+8
            while journal.queue.unfinished_tasks and time.monotonic()<until: time.sleep(.005)
            self.assertEqual(journal.queue.unfinished_tasks,0)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'journal'; journal=Journal(root,'api')
            error=RuntimeError('SECRET_COOKIE CSRF_TOKEN 192.0.2.1 /private/model.rvt <html>')
            journal.emit('request','database_unavailable','a'*32,error)
            drain(journal); self.assertTrue(journal.health()['available']); journal.close()
            journal=Journal(root,'api')
            journal.emit('stream','storage_unavailable','b'*32,error); drain(journal)
            records=[json.loads(line) for path in root.glob('*.jsonl') for line in path.read_text().splitlines()]
            self.assertEqual({r['requestId'] for r in records},{'a'*32,'b'*32})
            # Drive actual configured byte boundary without waiting on 10k fsyncs.
            for slot in range(SLOTS):
                path=root/f'api.{slot}.jsonl'; path.write_bytes(b'{}\n'*(SLOT_BYTES//3)); path.chmod(0o600)
            for _ in range(SLOTS+1):
                journal.emit('request','internal_error','c'*32,error); drain(journal)
            self.assertLessEqual(len(list(root.glob('*.jsonl'))),SLOTS)
            self.assertTrue(all(path.stat().st_size<=SLOT_BYTES for path in root.glob('*.jsonl')))
            self.assertNotIn('SECRET',b''.join(path.read_bytes() for path in root.glob('*.jsonl')).decode())
            self.assertEqual(journal.health()['failed'],0); journal.close()
            unavailable=Path(tmp)/'blocked'; unavailable.write_text('not a directory')
            journal=Journal(unavailable,'api')
            started=time.monotonic(); journal.emit('request','database_unavailable','d'*32,error)
            self.assertLess(time.monotonic()-started,.1)
            drain(journal); self.assertFalse(journal.health()['available'])
            self.assertEqual(journal.health()['fallbackRecords'],1); journal.close()


class DiagnosticIntegrationTests(unittest.TestCase):
    from web.test_worker import WorkerTests as _Worker
    setUp=_Worker.setUp
    upload=_Worker.upload
    create=_Worker.create
    worker_settings=_Worker.worker_settings
    kill_dedicated_lease=_Worker.kill_dedicated_lease

    def test_real_guarded_bad_report_owner_details_and_immutable_hash(self):
        from contextlib import closing
        import hashlib
        from model_generator.web.worker import worker_lease,run_once
        from model_generator.web.db import Database
        from web.helpers import TestClient,register_login
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'a'*32): self.assertTrue(run_once(db,settings,'a'*32))
            response=self.client.get(f"/api/jobs/{dto['id']}/checks")
            self.assertEqual(response.status_code,200,response.text)
            checks=response.json(); self.assertTrue(checks['authoritative'])
            self.assertEqual(checks['state'],'completed')
            failure=next(row for row in checks['checks'] if row['state']=='failed')
            details=self.client.get(failure['detailsUrl']).json()
            self.assertTrue(details['nextAction']); self.assertTrue(details['findings'])
            item=self.client.get('/api/jobs/'+dto['id']).json()['artifacts'][0]
            before=self.client.get(item['url']).content
            self.client.get(failure['detailsUrl'])
            self.assertEqual(hashlib.sha256(before).hexdigest(),item['sha256'])
            self.assertEqual(before,self.client.get(item['url']).content)
            with closing(TestClient(self.app,base_url='https://testserver')) as other:
                other.portal=self.client.portal
                register_login(other,'diagnostic_neighbour')
                from unittest.mock import patch
                with patch.object(self.app.state.storage.objects,'head',side_effect=AssertionError('S3 before owner gate')):
                    self.assertEqual(other.get(f"/api/jobs/{dto['id']}/checks").status_code,404)
                    self.assertEqual(other.get(failure['detailsUrl']).status_code,404)
        finally: db.close()

    def test_phase_details_enrich_each_actual_finding_from_trusted_catalog(self):
        from fixtures.package_builders import make_package
        from model_generator.web.worker import worker_lease,run_once
        from model_generator.web.db import Database
        from model_generator.web.diagnostic_catalog import rule_help
        dto=self.create(self.upload(make_package(),'portable-package')).json()
        settings=self.worker_settings(); db=Database(settings)
        try:
            with worker_lease(db,settings,'a'*32): self.assertTrue(run_once(db,settings,'a'*32))
            response=self.client.get(f"/api/jobs/{dto['id']}/checks/scene.meshes")
            self.assertEqual(response.status_code,200,response.text)
            findings=response.json()['findings']
            self.assertTrue(findings)
            for finding in findings:
                for field,value in rule_help(finding['ruleId']).items():
                    self.assertEqual(finding[field],value)
            linked=next(finding for finding in findings if finding['elementKey'])
            self.assertEqual(linked['elementKey']['document_id'],'root')
            self.assertEqual(linked['elementKey']['unique_id'],'synthetic-element')
            self.assertEqual(linked['elementKey']['link_instance_path'],[])
            self.assertTrue(linked['message'])
            self.assertIn('observed',linked); self.assertIn('expected',linked)
        finally: db.close()

    def _preview_journal_case(self,mode):
        from dataclasses import replace
        from unittest.mock import patch
        from fixtures.package_builders import make_package
        from model_generator.web import worker
        from model_generator.web.db import Database
        from model_generator.web.journal import Journal
        from model_generator.web.preview import PreviewError
        wire=make_package(scene_updates={'instances':[]}) if mode=='unsupported' else make_package()
        dto=self.create(self.upload(wire,'portable-package')).json()
        settings=self.worker_settings()
        if mode=='budget': settings=replace(settings,preview_max_vertices=2)
        db=Database(settings); db.journal=Journal(Path(self.tmp.name)/'preview-journal','worker')
        original=worker.run_preview; epoch='f'*32
        def render(*args,**kwargs):
            if mode=='resource': return original(*args,**kwargs,probe='oversized')
            if mode=='runtime': raise PreviewError('preview_runtime_unavailable','SECRET_MODEL /private/source.rvt')
            if mode=='cancel':
                self.client.post('/api/jobs/'+dto['id']+'/cancel',headers=self.headers)
                raise PreviewError('preview_cancelled','Cancelled')
            return original(*args,**kwargs)
        try:
            with worker.worker_lease(db,settings,epoch),patch.object(worker,'run_preview',side_effect=render):
                self.assertTrue(worker.run_once(db,settings,epoch))
            result=self.client.get('/api/jobs/'+dto['id']).json()
            db.journal.queue.join()
            records=[json.loads(line) for path in db.journal.root.glob('*.jsonl') for line in path.read_text().splitlines()]
            preview=[row for row in records if row['stage']=='preview']
            if mode=='cancel':
                self.assertEqual(result['state'],'cancelled'); self.assertEqual(preview,[])
            else:
                code={'resource':'preview_resource','runtime':'preview_runtime_unavailable','budget':'preview_budget','unsupported':'preview_unsupported'}[mode]
                self.assertEqual(result['state'],'completed')
                self.assertEqual(result['capabilities']['preview']['reason'],code)
                report=next(item for item in result['artifacts'] if item['kind']=='report')
                self.assertEqual(self.client.get(report['url']).status_code,200)
                self.assertFalse(any(item['kind']=='thumbnail' for item in result['artifacts']))
                if mode=='unsupported': self.assertEqual(preview,[])
                else: self.assertTrue(any(row['code']==code and row['requestId']==result['diagnosticId'] and row['jobId']==dto['id'] and row['attempt']==epoch for row in preview))
                self.assertNotIn('SECRET',json.dumps(records))
        finally: db.journal.close(); db.close()

    def test_actual_preview_child_resource_failure_is_durably_correlated(self): self._preview_journal_case('resource')
    def test_caught_preview_runtime_failure_is_durably_correlated(self): self._preview_journal_case('runtime')
    def test_validation_child_preview_budget_is_durably_correlated(self): self._preview_journal_case('budget')
    def test_preview_cancel_is_not_a_runtime_error(self): self._preview_journal_case('cancel')
    def test_expected_unsupported_preview_is_not_a_runtime_error(self): self._preview_journal_case('unsupported')

    def test_progress_actual_physical_session_loss_and_stale_attempt_fences(self):
        import time
        from model_generator.web.worker import worker_lease
        from model_generator.web.db import Database
        from model_generator.web.jobs import JobRepository
        from model_generator.web.progress import persist_progress,Observer
        from web.helpers import admin_connect
        dto=self.create(self.upload()).json(); settings=self.worker_settings(); db=Database(settings)
        try:
            import psycopg
            with self.assertRaises((RuntimeError,psycopg.Error,OSError,TimeoutError)):
                with worker_lease(db,settings,'b'*32):
                    job=JobRepository(db,settings).claim_next('b'*32,int(time.time()))
                    with tempfile.TemporaryDirectory() as tmp:
                        observer=Observer(Path(tmp),job['input_sha256'],'b'*32)
                        observer('input.archive','checking')
                        persist_progress(db,job,observer.snapshot(),int(time.time()))
                        with admin_connect() as con:
                            pid=con.execute("SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted").fetchone()[0]
                            self.assertTrue(con.execute('SELECT pg_terminate_backend(%s)',(pid,)).fetchone()[0])
                        observer('input.archive','passed')
                        persist_progress(db,job,observer.snapshot(),int(time.time()))
            with admin_connect() as con:
                row=con.execute('SELECT sequence FROM mg.job_progress WHERE job_id=%s',(dto['id'],)).fetchone()
                self.assertEqual(row[0],1)
        finally: db.close()

    def _live_fence(self,mode):
        import os
        import threading
        import time
        from unittest.mock import patch
        import psycopg
        from fixtures.builders import zip_bytes,scene_bytes
        from model_generator.web import worker
        from model_generator.web.db import Database
        from model_generator.web.journal import Journal
        from web.helpers import admin_connect
        mesh=scene_bytes(indices=(0,1,-3)*10000,vertices=(0,0,0,1,0,0,0,1,0)*10000,texture=False)
        wire=zip_bytes([(f'building-{index}.fbx',mesh) for index in range(128)])
        dto=self.create(self.upload(wire)).json(); settings=self.worker_settings(); db=Database(settings)
        db.journal=Journal(Path(self.tmp.name)/'worker-journal','worker')
        original_popen=worker.subprocess.Popen
        failures=[]; epoch='c'*32; children=[]
        def launch(*args,**kwargs):
            process=original_popen(*args,**kwargs)
            if isinstance(args[0],list) and 'model_generator.web.process_guard' in args[0]: children.append(process)
            return process
        def compute():
            try:
                with worker.worker_lease(db,settings,epoch): worker.run_once(db,settings,epoch)
            except (RuntimeError,psycopg.Error,OSError,TimeoutError) as error: failures.append(type(error).__name__)
        thread=None
        try:
            with patch.object(worker.subprocess,'Popen',side_effect=launch):
                thread=threading.Thread(target=compute); thread.start()
                deadline=time.monotonic()+12; current=None
                while time.monotonic()<deadline:
                    response=self.client.get(f"/api/jobs/{dto['id']}/checks")
                    self.assertEqual(response.status_code,200,response.text)
                    current=response.json()
                    states={row['id']:row['state'] for row in current['checks']}
                    if states.get('input.archive')=='passed' and states.get('input.fbx')=='checking': break
                    if not thread.is_alive(): self.fail('Worker stopped before live progress: '+repr(failures))
                    time.sleep(.05)
                else: self.fail('Actual progress not observed')
                self.assertFalse(current['authoritative'])
                self.assertNotEqual(states['fbx.integrity'],'passed')
                scratch=settings.data_root/'jobs'/dto['id']/epoch
                self.assertEqual(len(children),1)
                pid=children[0].pid
                self.assertEqual(os.kill(pid,0),None)
                with admin_connect() as con:
                    sequence=con.execute('SELECT sequence FROM mg.job_progress WHERE job_id=%s',(dto['id'],)).fetchone()[0]
                    if mode=='session':
                        backend=con.execute("SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted").fetchone()[0]
                        self.assertTrue(con.execute('SELECT pg_terminate_backend(%s)',(backend,)).fetchone()[0])
                    elif mode=='expiry': con.execute('UPDATE mg.jobs SET expires_at=created_at+1 WHERE id=%s',(dto['id'],))
                if mode=='cancel':
                    blocked=Path(self.tmp.name)/'unavailable-journal'; blocked.write_text('unavailable')
                    db.journal.root=blocked
                    db.journal.emit('worker','internal_error','f'*32)
                    self.assertEqual(self.client.post(f"/api/jobs/{dto['id']}/cancel",headers=self.headers).status_code,202)
                if mode=='delete': self.assertEqual(self.client.delete(f"/api/jobs/{dto['id']}",headers=self.headers).status_code,202)
                thread.join(12); self.assertFalse(thread.is_alive(),'Guarded child not reaped')
                with self.assertRaises(ProcessLookupError): os.kill(pid,0)
                with admin_connect() as con:
                    self.assertEqual(con.execute('SELECT sequence FROM mg.job_progress WHERE job_id=%s',(dto['id'],)).fetchone()[0],sequence)
                    self.assertEqual(con.execute("SELECT count(*) FROM mg.artifacts WHERE job_id=%s AND state='ready'",(dto['id'],)).fetchone()[0],0)
            if mode=='cancel':
                db.journal.queue.join()
                self.assertFalse(db.journal.health()['available'])
                self.assertEqual(db.journal.health()['failed'],1)
            if mode=='session':
                self.assertTrue(failures)
                end=time.monotonic()+3
                while db.journal.queue.unfinished_tasks and time.monotonic()<end: time.sleep(.01)
                from model_generator.web.journal import correlation
                records=[json.loads(line) for path in db.journal.root.glob('worker.*.jsonl') for line in path.read_text().splitlines()]
                self.assertTrue(any(row['requestId']==correlation('job',dto['id'],epoch) for row in records))
                db.journal.close(); db.close(); db=Database(settings)
                with worker.worker_lease(db,settings,'d'*32):
                    worker.recover(db,settings,'d'*32,int(time.time()))
                    self.assertTrue(worker.run_once(db,settings,'d'*32))
                result=self.client.get(f"/api/jobs/{dto['id']}/checks").json()
                self.assertEqual(result['attempt'],'d'*32)
                self.assertTrue(result['authoritative'])
                self.assertEqual(result['state'],'completed')
            print('Actual guarded child progress fence passed:',mode)
        finally:
            if thread and thread.is_alive():
                db.worker_failed.set(); thread.join(12)
            journal=getattr(db,'journal',None)
            if journal: journal.close()
            db.close()

    def test_live_child_backend_loss_reaps_before_fenced_restart(self): self._live_fence('session')
    def test_live_child_cancel_fences_progress(self): self._live_fence('cancel')
    def test_live_child_delete_fences_progress(self): self._live_fence('delete')
    def test_live_child_expiry_fences_progress(self): self._live_fence('expiry')

    def test_actual_api_exception_and_stream_failure_have_safe_correlation(self):
        import time
        from starlette.responses import StreamingResponse
        from starlette.routing import Route
        async def failure(_request): raise RuntimeError('SECRET_COOKIE csrf sentinel /private/model.rvt 192.0.2.5')
        async def stream(_request):
            async def chunks():
                yield b'first'
                raise RuntimeError('SECRET_STREAM')
            return StreamingResponse(chunks())
        # Explicit test endpoints must precede the installed public-file catchall.
        self.app.router.routes[0:0] = [
            Route('/api/synthetic-error', failure, methods=['GET']),
            Route('/api/synthetic-stream', stream, methods=['GET']),
        ]
        response=self.client.get('/api/synthetic-error')
        self.assertEqual(response.status_code,500)
        public=response.json()['error']; self.assertTrue(public['nextAction'])
        self.assertEqual(public['request_id'],response.headers['x-request-id'])
        self.assertNotIn('SECRET',response.text)
        stream=self.client.get('/api/synthetic-stream')
        journal=self.app.state.journal
        end=time.monotonic()+4
        while journal.queue.unfinished_tasks and time.monotonic()<end: time.sleep(.01)
        records=[json.loads(line) for path in journal.root.glob('api.*.jsonl') for line in path.read_text().splitlines()]
        self.assertTrue(any(row['requestId']==public['request_id'] for row in records))
        self.assertTrue(any(row['requestId']==stream.headers['x-request-id'] and row['stage']=='stream' for row in records))
        self.assertNotIn('SECRET',json.dumps(records))


    def test_actual_database_transport_failure_keeps_journal_durable(self):
        from dataclasses import replace
        from urllib.parse import urlsplit,urlunsplit
        import time
        db=self.app.state.db; original=db.settings
        parts=urlsplit(original.database_url)
        # Actual refused own-database TCP connection, no exception mocking.
        authority=parts.netloc.rsplit(':',1)[0] if parts.port else parts.netloc
        db.settings=replace(original,database_url=urlunsplit((parts.scheme,authority+':1',parts.path,parts.query,parts.fragment)))
        try: response=self.client.get('/api/jobs')
        finally: db.settings=original
        self.assertEqual(response.status_code,503,response.text)
        code=response.json()['error']; self.assertEqual(code['code'],'database_unavailable')
        journal=self.app.state.journal; end=time.monotonic()+3
        while journal.queue.unfinished_tasks and time.monotonic()<end: time.sleep(.01)
        records=[json.loads(line) for path in journal.root.glob('api.*.jsonl') for line in path.read_text().splitlines()]
        self.assertTrue(any(row['requestId']==code['request_id'] and row['code']=='database_unavailable' for row in records))


class DiagnosticChunkJournalTests(unittest.TestCase):
    from web.test_upload_chunks import ChunkHTTPTests as _Chunk
    setUp=_Chunk.setUp
    reserve=_Chunk.reserve
    put=_Chunk.put
    status=_Chunk.status

    def test_actual_async_full_hash_failure_has_owner_correlation(self):
        import hashlib
        import time
        upload=self.reserve(b'x',sha256=hashlib.sha256(b'y').hexdigest())
        public=self.status(upload)['diagnosticId']
        self.assertEqual(self.put(upload,1,b'x').status_code,200)
        self.assertEqual(self.client.post('/api/uploads/'+upload+'/complete',headers=self.headers).status_code,202)
        end=time.monotonic()+8
        while time.monotonic()<end:
            if self.status(upload)['state']=='deleted': break
            time.sleep(.03)
        self.assertEqual(self.status(upload)['state'],'deleted')
        journal=self.app.state.journal; journal.queue.join()
        records=[json.loads(line) for path in journal.root.glob('api.*.jsonl') for line in path.read_text().splitlines()]
        self.assertTrue(any(row['requestId']==public and row['stage']=='finalize' and row['code']=='upload_hash_mismatch' for row in records))


if __name__=='__main__':
    import os
    import sys
    from model_generator.web.journal import Journal
    root=Path(os.environ['MG_JOURNAL_ROOT'])
    if sys.argv[1:] == ['--journal-write']:
        journal=Journal(root,'api')
        journal.emit('startup','internal_error','e'*32)
        journal.queue.join()
        assert journal.health()['failed']==0
        journal.close()
        print('Journal durable container seed passed.')
    elif sys.argv[1:] == ['--journal-read']:
        records=[json.loads(line) for path in root.glob('api.*.jsonl') for line in path.read_text().splitlines()]
        assert any(row['requestId']=='e'*32 for row in records)
        print('Journal separate-container persistent-volume restart readback passed.')
    else: raise SystemExit('Expected journal probe action')
