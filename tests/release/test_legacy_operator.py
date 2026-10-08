"""Strict old attestation and negative transition invariants."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('legacy',Path(__file__).resolve().parents[2]/'scripts/legacy-recovery-web.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)


class LegacyOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'source').mkdir()
        self.source='a'*40;self.pin='sha256:'+'b'*64
        self.export=json.dumps({'source_revision':self.source,'files':[]}).encode()
        (self.root/'source/public-manifest.json').write_bytes(self.export)
        self.digest=hashlib.sha256(self.export).hexdigest()
        (self.root/'images.json').write_text(json.dumps({'source_revision':self.source,'images':{role:{'id':self.pin} for role in ('api','worker')}}))
        self.image={'Id':self.pin,'Os':'linux','Architecture':'amd64','Config':{'Labels':None}}
    def test_unlabelled_old_exact_image_allowed_but_wrong_labels_platform_and_id_denied(self):
        with patch.object(legacy.subprocess,'check_output',return_value=json.dumps([self.image])):
            self.assertEqual(legacy.previous_gate(self.root,self.source,self.digest)['source_revision'],self.source)
        for key,value in (('Id','sha256:'+'c'*64),('Architecture','arm64'),('Os','windows'),('Config',{'Labels':{'org.opencontainers.image.revision':'c'*40}})):
            image=dict(self.image);image[key]=value
            with patch.object(legacy.subprocess,'check_output',return_value=json.dumps([image])):
                with self.assertRaises(ValueError):legacy.previous_gate(self.root,self.source,self.digest)
    def test_previous_source_and_export_hash_mismatch_denied_before_docker(self):
        with patch.object(legacy.subprocess,'check_output') as docker:
            for source,digest in ((self.source,'c'*64),('d'*40,self.digest),('main',self.digest)):
                with self.assertRaises(ValueError):legacy.previous_gate(self.root,source,digest)
            docker.assert_not_called()
    def test_embedded_protocol_payloads_compile(self):
        for name in ('OFFLINE','SNAPSHOT','S3_SNAPSHOT','STARTUP_DENY'):
            compile(getattr(legacy,name),name,'exec')
    def test_unsafe_old_writer_hardening_denied(self):
        value={'Config':{'Labels':{'com.docker.compose.service':'api'},'User':'10001:10001'},'HostConfig':{'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'PidsLimit':64,'NanoCpus':1000000000,'Memory':512*1024**2}}
        legacy.writer_gate({'api':value})
        for key,changed in (('ReadonlyRootfs',False),('Memory',1024),('Privileged',True),('PortBindings',{'8000/tcp':[{}]})):
            host=dict(value['HostConfig']);host[key]=changed
            with self.assertRaises(ValueError):legacy.writer_gate({'api':{**value,'HostConfig':host}})

    def exercise_replacement(self,live=False):
        secrets=self.root/'secrets/runtime';secrets.mkdir(parents=True);(secrets/'migrator_dsn').write_text('synthetic')
        images={role:{'id':'sha256:'+letter*64} for role,letter in (('api','b'),('worker','c'),('keeper','d'))}
        inventory={}
        for role in ('api','worker','scratch-keeper'):
            inventory[role]={'Config':{'Labels':{'com.docker.compose.service':role},'User':'10001:10001'},'State':{'Running':False,'Pid':0},'HostConfig':{'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'PidsLimit':64,'NanoCpus':1000000000,'Memory':(512 if role=='api' else 2048)*1024**2,'RestartPolicy':{'Name':'unless-stopped'}}}
        commands=[]
        def run(args,**kwargs):
            commands.append(args)
            if args[:2]==['compose','create']:
                raise subprocess.CalledProcessError(1,args,stderr='unknown flag: --no-deps')
            if args[:2]==['compose','up']:
                self.assertEqual(args,['compose','up','--no-start','--no-deps','--force-recreate','api','worker'])
                if live:inventory['api']['State']={'Running':False,'Pid':7}
            if args[:2]==['docker','update']:
                inventory[args[-1]]['HostConfig']['RestartPolicy']['Name']='no'
            output='[1,2]' if args[-1]!=legacy.OFFLINE and args[:2]==['docker','run'] else ''
            return SimpleNamespace(stdout=output)
        cold=SimpleNamespace(holders=lambda *args,**kwargs:inventory,prepare=lambda *args:None,stopped=lambda value: not value['State']['Running'] and value['State']['Pid']==0 and value['HostConfig']['RestartPolicy']['Name']=='no')
        def migrate():raise RuntimeError('Reached migration after stopped-only fence')
        with patch.object(legacy.subprocess,'run',side_effect=run):
            legacy.transition(['compose'],self.root,'mg-cold-proof-12345678',images,{'images':images},(1,2,None),cold,lambda *args:None,migrate)
    def test_supported_stopped_only_replacement_reaches_migration(self):
        with self.assertRaisesRegex(RuntimeError,'Reached migration after stopped-only fence'):
            self.exercise_replacement()
    def test_replacement_pid_nonzero_denies_before_migration(self):
        with self.assertRaisesRegex(ValueError,'New stopped-only writer replacement failed'):
            self.exercise_replacement(live=True)
