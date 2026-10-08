import importlib.util
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('release',Path(__file__).resolve().parents[2]/'scripts/release-web.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.sha='a'*40
        self.images={role:{'id':'sha256:'+letter*64} for role,letter in (('api','b'),('worker','c'),('tests','d'))}
        (self.root/'images.json').write_text(json.dumps({'source_revision':self.sha,'images':self.images}))
        (self.root/'final-native.exit').write_text('0')
        (self.root/'final-native.log').write_text('Final exact native UI, ingress and production active restore acceptance passed')
        (self.root/'diagnostics.log').write_text('Ran 246 tests\nRan 187 tests\ndiagnostics Docker acceptance passed.')
        (self.root/'cold-native.exit').write_text('0')
        (self.root/'cold-native.log').write_text('Final native isolated cold recovery acceptance passed')
        scripts=self.root/'source/scripts';scripts.mkdir(parents=True)
        hashes={}
        for name in ('cold-recovery-web.py','ci-web-cold-recovery.py','legacy-recovery-web.py'):
            data=Path(release.__file__).with_name(name).read_bytes();(scripts/name).write_bytes(data);hashes[name]=hashlib.sha256(data).hexdigest()
        (self.root/'source/public-manifest.json').write_text(json.dumps({'source_revision':self.sha,'files':[{'path':'scripts/'+name,'sha256':value} for name,value in hashes.items()]}))
        (self.root/'cold-native-attestation.json').write_text(json.dumps({'runtime_source':self.sha,'runtime_images':release.manifest_image_ids({'images':self.images}),'helpers':hashes}))
    def tearDown(self):self.tmp.cleanup()
    def test_accepted_source_passes(self):release.evidence_gate(self.root,self.sha)
    def test_cold_failure_missing_marker_and_wrong_source_denied(self):
        for path,value in (('cold-native.exit','1'),('cold-native.log','partial'),('cold-native-attestation.json',json.dumps({'runtime_source':'b'*40,'helpers':{}}))):
            target=self.root/path;previous=target.read_text();target.write_text(value)
            with self.assertRaises(ValueError):release.evidence_gate(self.root,self.sha)
            target.write_text(previous)
    def test_changed_delivered_cold_helper_denied(self):
        (self.root/'source/scripts/cold-recovery-web.py').write_text('changed')
        with self.assertRaises(ValueError):release.evidence_gate(self.root,self.sha)
    def test_new_image_substitution_same_source_denied(self):
        manifest=json.loads((self.root/'images.json').read_text())
        for role in manifest['images']:
            changed=json.loads(json.dumps(manifest));changed['images'][role]['id']='sha256:'+'e'*64
            (self.root/'images.json').write_text(json.dumps(changed))
            with self.assertRaises(ValueError):release.evidence_gate(self.root,self.sha)
        (self.root/'images.json').write_text(json.dumps(manifest))
    def test_previous_image_and_export_substitution_same_source_denied(self):
        previous={'source_revision':'f'*40,'images':self.images,'_export_manifest_sha256':'a'*64}
        target=self.root/'cold-native-attestation.json';attestation=json.loads(target.read_text())
        attestation.update(legacy_source=previous['source_revision'],previous_images=release.manifest_image_ids(previous),previous_export_manifest_sha256=previous['_export_manifest_sha256']);target.write_text(json.dumps(attestation))
        release.previous_binding_gate(self.root,previous)
        for role in previous['images']:
            changed=json.loads(json.dumps(previous));changed['images'][role]['id']='sha256:'+'e'*64
            with self.assertRaises(ValueError):release.previous_binding_gate(self.root,changed)
            with patch.object(release,'configuration_gate'),patch.object(release.subprocess,'run') as mutation,patch.object(release.subprocess,'check_output') as docker:
                with self.assertRaises(ValueError):release._promote_locked(self.root,self.root,self.sha,'unused',previous=changed)
                mutation.assert_not_called();docker.assert_not_called()
        changed={**previous,'_export_manifest_sha256':'b'*64}
        with self.assertRaises(ValueError):release.previous_binding_gate(self.root,changed)
    def keeper(self):
        return {'Image':'sha256:accepted','Config':{'User':'10001:10001','Entrypoint':['python','-I','-c','import time; time.sleep(10**9)'],'Cmd':None,'Env':[]},'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'Memory':64*1024**2,'NanoCpus':50000000,'PidsLimit':8,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']},'Mounts':[{'Destination':'/scratch/'+r,'Type':'volume','Name':'own_'+r+'_scratch','RW':False} for r in ('api','worker')]}
    def test_keeper_correct_scope_passes(self):release.keeper_gate(self.keeper(),'sha256:accepted','own')
    def test_keeper_other_image_denied(self):
        with self.assertRaises(ValueError):release.keeper_gate(self.keeper(),'sha256:other','own')
    def test_keeper_client_or_writable_mount_denied(self):
        for field,value in (('Name','client_pgdata'),('RW',True)):
            with self.subTest(field=field):
                actual=self.keeper();actual['Mounts'][0][field]=value
                with self.assertRaises(ValueError):release.keeper_gate(actual,'sha256:accepted','own')
    def test_client_network_denied(self):
        config={'services': {'api': {'networks': {'client': {}}}}, 'networks': {}}
        with self.assertRaises(ValueError):release.topology_gate(config)
    def test_changed_configuration_denied(self):
        (self.root/'source/public-manifest.json').write_text('{}')
        with self.assertRaises(ValueError):release.configuration_gate(self.root,self.root,self.sha,'b'*64)
    def test_other_source_denied(self):
        with self.assertRaises(ValueError):release.evidence_gate(self.root,'b'*40)
    def test_incomplete_native_denied(self):
        (self.root/'final-native.exit').write_text('1')
        with self.assertRaises(ValueError):release.evidence_gate(self.root,self.sha)
    def test_diagnostic_skip_denied(self):
        with (self.root/'diagnostics.log').open('a') as f:f.write('\nOK (skipped=1)')
        with self.assertRaises(ValueError):release.evidence_gate(self.root,self.sha)
    def test_missing_physical_acceptance_denied(self):
        (self.root/'diagnostics.log').write_text('Ran 246 tests\nRan 187 tests')
        with self.assertRaises(ValueError):release.evidence_gate(self.root,self.sha)
if __name__=='__main__':unittest.main()


class PromotedFinalizationTests(unittest.TestCase):
    def inventory(self):
        return {n:{'Id':n} for n in release.FINAL_ROLES}
    def test_finalization_only_mutation_is_last_keeper_policy(self):
        config={'services':{'scratch-keeper':{'restart':'unless-stopped'}}};inventory=self.inventory()
        with patch.object(release,'final_runtime_gate',return_value=inventory),patch.object(release.cold_operator,'holders',return_value={n:inventory[n] for n in ('api','worker','scratch-keeper')}),patch.object(release.subprocess,'run') as run:
            release.finalize_promoted(['compose'],{'images':{}},config,'a'*40)
        self.assertEqual(len(run.call_args_list),3)
        self.assertEqual(run.call_args_list[-1].args[0],['docker','update','--restart=unless-stopped','scratch-keeper'])
        self.assertIn(release.READONLY_PROMOTED_PROBE,run.call_args_list[0].args[0])
        self.assertIn('/health/ready',run.call_args_list[1].args[0][-1])
    def test_partial_inventory_and_probe_readiness_failure_cannot_mutate(self):
        inventory=self.inventory();config={'services':{'scratch-keeper':{'restart':'unless-stopped'}}}
        for stage in ('inventory','generation','readiness','changed'):
            with self.subTest(stage=stage),patch.object(release,'final_runtime_gate') as gate,patch.object(release.cold_operator,'holders',return_value={n:inventory[n] for n in ('api','worker','scratch-keeper')}),patch.object(release.subprocess,'run') as run:
                gate.side_effect=ValueError('Partial or wrong image') if stage=='inventory' else [inventory,{**inventory,'api':{'Id':'replacement'}}] if stage=='changed' else [inventory,inventory]
                if stage in ('generation','readiness'):
                    run.side_effect=[release.subprocess.CalledProcessError(1,['readonly'])] if stage=='generation' else [None,release.subprocess.CalledProcessError(1,['readiness'])]
                with self.assertRaises((ValueError,release.subprocess.CalledProcessError)):release.finalize_promoted(['compose'],{'images':{}},config,'a'*40)
                self.assertFalse(any(c.args[0][:2]==['docker','update'] for c in run.call_args_list))
    def test_container_gate_wrong_image_state_generation_source_and_hardening(self):
        configured={'pids_limit':64,'mem_limit':512,'cpus':1,'user':'10001:10001','restart':'unless-stopped','networks':{'db':{}},'volumes':[],'secrets':[],'healthcheck':{'test':['CMD','probe']}}
        config={'networks':{'db':{'name':'own_db'}}}
        actual={'Image':'expected','Config':{'User':'10001:10001','Labels':{'com.docker.compose.project':'model-generator-mvp','com.docker.compose.service':'api','org.opencontainers.image.revision':'a'*40}},'State':{'Running':True,'Pid':123,'Health':{'Status':'healthy'}},'HostConfig':{'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'PidsLimit':64,'Memory':512,'NanoCpus':10**9,'RestartPolicy':{'Name':'unless-stopped'}},'NetworkSettings':{'Networks':{'own_db':{}}},'Mounts':[]}
        release.promoted_container_gate(actual,'api',configured,'expected',config,'a'*40)
        for part,key,value in ((None,'Image','wrong'),('State','Running',False),('State','Pid',0),('HostConfig','ReadonlyRootfs',False),('NetworkSettings','Networks',{'foreign':{}}),('Config','Labels',{'com.docker.compose.project':'wrong'})):
            bad=json.loads(json.dumps(actual));(bad[part] if part else bad)[key]=value
            with self.assertRaises(ValueError):release.promoted_container_gate(bad,'api',configured,'expected',config,'a'*40)
        self.assertEqual(release.FINAL_ROLES['backup-proxy'],'proxy')
    def test_readonly_probe_actual_schema_and_lock_generation(self):
        import os,sys,types
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);lock=root/'api.lock';lock.write_text('a'*32);st=lock.stat()
            config=types.ModuleType('model_generator.web.config');config.Settings=types.SimpleNamespace(from_env=lambda:types.SimpleNamespace(data_root=root,database_url='private'))
            migrate=types.ModuleType('model_generator.web.migrate');migrate.migrations=lambda:[(i,str(i),'') for i in range(1,9)]
            identity=types.ModuleType('model_generator.web.lock_identity')
            identity.generation=lambda fd:os.pread(fd,33,0).decode()
            for wrong in ('none','schema','generation'):
                queries=[]
                class Connection:
                    def execute(self,query):
                        queries.append(query)
                        return types.SimpleNamespace(fetchall=lambda:[(i,str(i)) for i in range(1,8 if wrong=='schema' else 9)] if 'schema_meta' in query else [(st.st_dev,st.st_ino,'b'*32 if wrong=='generation' else 'a'*32)])
                @contextmanager
                def connect(url,**kwargs):
                    self.assertTrue(kwargs['autocommit']);self.assertIn('default_transaction_read_only=on',kwargs['options']);yield Connection()
                psycopg=types.ModuleType('psycopg');psycopg.connect=connect
                with patch.dict(sys.modules,{'psycopg':psycopg,'model_generator.web.config':config,'model_generator.web.migrate':migrate,'model_generator.web.lock_identity':identity}),patch('signal.alarm'),patch('signal.signal'):
                    if wrong=='none':exec(release.READONLY_PROMOTED_PROBE,{})
                    else:
                        with self.assertRaises(AssertionError):exec(release.READONLY_PROMOTED_PROBE,{})
                self.assertTrue(all(q.startswith('SELECT ') for q in queries));self.assertEqual(lock.read_text(),'a'*32)

    def test_finalize_cli_denies_missing_authorization_and_mixed_phases(self):
        import runpy,sys
        base=['release-web.py','--evidence','unused','--accepted-source','a'*40,'--deployment-root','unused','--export-manifest-sha256','b'*64,'--finalize-promoted']
        for flags in ([],['--promote-authorized'],['--promote-authorized','--public-maintenance-confirmed','--check-only'],['--promote-authorized','--public-maintenance-confirmed','--prepare-cold-recovery'],['--promote-authorized','--public-maintenance-confirmed','--previous-source','c'*40],['--promote-authorized','--public-maintenance-confirmed','--cold-recovery-identity','1:2:legacy']):
            with patch.object(sys,'argv',base+flags),patch.object(release.subprocess,'run') as mutation,patch.object(release.subprocess,'check_output') as docker:
                with self.assertRaises(SystemExit) as error:runpy.run_path(release.__file__,run_name='__main__')
                self.assertEqual(error.exception.code,2);mutation.assert_not_called();docker.assert_not_called()

    def test_compose_null_inherits_but_explicit_empty_clears_exact_argv(self):
        image={'Entrypoint':['docker-entrypoint.sh'],'Cmd':['postgres']}
        for configured in ({},{'entrypoint':None,'command':None}):
            self.assertEqual(release.effective_command(configured,image),(['docker-entrypoint.sh'],['postgres']))
        for empty in ([], ''):
            self.assertEqual(release.effective_command({'entrypoint':empty},image),(None,None))
            self.assertEqual(release.effective_command({'command':empty},image),(['docker-entrypoint.sh'],None))
        ingress=['sh','-c','umask 077; exec caddy run --config /etc/model-ingress.json']
        self.assertEqual(release.effective_command({'entrypoint':ingress,'command':None},{'Entrypoint':None,'Cmd':['caddy','run']}),(ingress,None))
        safe=['postgres','-c','max_connections=32','-c','fsync=on','-c','synchronous_commit=on']
        expected=release.effective_command({'entrypoint':None,'command':safe},image)
        self.assertEqual(expected,(['docker-entrypoint.sh'],safe))
        for unsafe in (['postgres'],[v.replace('fsync=on','fsync=off') for v in safe],list(reversed(safe))):
            self.assertNotEqual((['docker-entrypoint.sh'],release.command_argv(unsafe)),expected)
        for unsupported in ('sh -c anything',[1],{'argv':['anything']}):
            with self.assertRaises(ValueError):release.command_argv(unsupported)

    def test_compose_decimal_memory_serialization_preserves_exact_limit(self):
        self.assertEqual(release.memory_bytes('536870912'),536870912)
        self.assertEqual(release.memory_bytes(536870912),536870912)
        self.assertNotEqual(release.memory_bytes('536870913'),536870912)
        for invalid in (True,False,0,-1,536870912.0,'512m','536870912.0','+536870912',' 536870912',None):
            with self.assertRaises(ValueError):release.memory_bytes(invalid)

    def test_compose_secret_relative_and_absolute_targets_preserve_scope(self):
        self.assertEqual(release.secret_destination({'source':'api_dsn','target':'database'}),'/run/secrets/database')
        self.assertEqual(release.secret_destination({'source':'auth_key','target':'/run/secrets/auth_key'}),'/run/secrets/auth_key')
        self.assertEqual(release.secret_destination({'source':'auth_key'}),'/run/secrets/auth_key')
        for target in ('/etc/passwd','../auth_key','/run/secrets/../auth_key','/run/secrets//auth_key','',None):
            with self.assertRaises(ValueError):release.secret_destination({'source':'auth_key','target':target})
