"""Host gate negatives: no database mutation without physical offline ownership."""
import importlib.util
import io
import sys
from types import SimpleNamespace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('release',Path(__file__).resolve().parents[2]/'scripts/release-web.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
cold=release.cold_operator


class ColdOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.project='mg-cold-proof-12345678'
        self.images={role:{'id':'sha256:'+role} for role in ('api','worker','keeper')}
        self.inventory={}
        for role in ('api','worker','scratch-keeper'):
            self.inventory[role]={'Image':self.images['keeper' if role=='scratch-keeper' else role]['id'],
                'Config':{'Labels':{'com.docker.compose.project':self.project,'com.docker.compose.service':role}},
                'State':{'Running':role=='scratch-keeper','Pid':20 if role=='scratch-keeper' else 0},
                'HostConfig':{'RestartPolicy':{'Name':'no'}},
                'NetworkSettings':{'Networks':{self.project+'_'+name:{} for name in (('mg_edge','mg_db','mg_files') if role=='api' else ('mg_db','mg_files'))}},
                'Mounts':[{'Type':'volume','Name':self.project+'_'+role+'_scratch','Destination':'/scratch','RW':True}] if role!='scratch-keeper' else []}
        (self.root/'secrets/runtime').mkdir(parents=True)
        (self.root/'secrets/runtime/migrator_dsn').write_text('synthetic never read by host')
    def output(self,args,**kwargs):
        if args[:2]==['docker','inspect']:return json.dumps([self.inventory[args[2]]]).encode()
        if args[:2]==['docker','exec']:return 'null'
        return '\n'.join(self.inventory)
    def reset(self):cold.reset(['compose'],self.root,self.project,self.images,(1,2,'a'*32),lambda *args:None)
    def test_operator_lock_excludes_concurrent_release(self):
        with cold.operator_lock(self.root):
            with self.assertRaises(ValueError):
                with cold.operator_lock(self.root):pass
        with cold.operator_lock(self.root):pass
        self.assertEqual((self.root/'.release-operator.lock').stat().st_mode&0o777,0o600)
    def test_operator_symlink_and_unsafe_modes_denied(self):
        path=self.root/'.release-operator.lock';path.symlink_to(self.root/'other')
        with self.assertRaises(OSError):
            with cold.operator_lock(self.root):pass
        path.unlink();self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            with cold.operator_lock(self.root):pass
    def test_no_reset_with_live_paused_or_restartable_writer(self):
        for field,value in (('Running',True),('Pid',1),('Restarting',True),('Paused',True)):
            with self.subTest(field=field):
                original=self.inventory['api']['State'].copy();self.inventory['api']['State'][field]=value
                with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
                    with self.assertRaises(ValueError):self.reset()
                    mutation.assert_not_called()
                self.inventory['api']['State']=original
        self.inventory['worker']['HostConfig']['RestartPolicy']['Name']='unless-stopped'
        with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
            with self.assertRaises(ValueError):self.reset()
            mutation.assert_not_called()
    def test_no_reset_with_foreign_image_label_or_volume(self):
        for key,value in (('Image','sha256:foreign'),('Mounts',[{'Type':'volume','Name':'client_pgdata'}])):
            original=self.inventory['api'][key];self.inventory['api'][key]=value
            with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
                with self.assertRaises(ValueError):self.reset()
                mutation.assert_not_called()
            self.inventory['api'][key]=original
        self.inventory['api']['Config']['Labels']['com.docker.compose.project']='client'
        with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
            with self.assertRaises(ValueError):self.reset()
            mutation.assert_not_called()
    def test_reset_preserved_inode_denied(self):
        def output(args,**kwargs):return json.dumps([1,2,'a'*32]) if args[:2]==['docker','exec'] else self.output(args,**kwargs)
        with patch.object(cold.subprocess,'check_output',side_effect=output),patch.object(cold.subprocess,'run') as mutation:
            with self.assertRaises(ValueError):self.reset()
            mutation.assert_not_called()
    def test_reset_only_offline_own_file_and_database_network(self):
        with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
            self.reset()
        args=mutation.call_args.args[0]
        self.assertEqual(args[args.index('--network')+1],self.project+'_mg_db')
        self.assertIn(str(self.root/'secrets/runtime/migrator_dsn')+':/run/secrets/database:ro',args)
        self.assertNotIn('synthetic never read by host',' '.join(args))
        self.assertEqual(json.loads(mutation.call_args.kwargs['input']),[1,2,'a'*32])
    def test_preparation_inventories_before_mutation(self):
        self.inventory['worker']['Config']['Labels']['com.docker.compose.project']='client'
        with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
            with self.assertRaises(ValueError):cold.prepare(['compose'],self.project,self.images,lambda *args:None)
            mutation.assert_not_called()
    def test_keeper_missing_or_stopped_denied(self):
        self.inventory['scratch-keeper']['State']={'Running':False,'Pid':0}
        with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
            with self.assertRaises(ValueError):self.reset()
            mutation.assert_not_called()

    def test_preparation_stops_all_holders_before_reopening_keeper(self):
        for value in self.inventory.values():
            value['State']={'Running':True,'Pid':20}
            value['HostConfig']['RestartPolicy']['Name']='unless-stopped'
        commands=[]
        def mutation(args,**kwargs):
            commands.append(args)
            if args[:2]==['docker','update']:
                self.inventory[args[-1]]['HostConfig']['RestartPolicy']['Name']='no'
            elif args[:2]==['docker','stop']:
                self.inventory[args[-1]]['State']={'Running':False,'Pid':0}
            else:
                self.assertTrue(all(cold.stopped(value) for value in self.inventory.values()))
                self.inventory['scratch-keeper']['State']={'Running':True,'Pid':99}
        with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run',side_effect=mutation):
            cold.prepare(['compose'],self.project,self.images,lambda *args:None)
        self.assertEqual(len(commands),7)
        self.assertEqual(commands[-1],['compose','up','-d','--wait','--no-recreate','scratch-keeper'])
        self.assertTrue(cold.stopped(self.inventory['api']))
        self.assertTrue(cold.stopped(self.inventory['worker']))

    def test_transaction_policy_denies_live_connections_locks_and_mismatch(self):
        # Execute the actual one-off script with a controlled connection. This
        # verifies fail-before-DELETE policy; actual PG semantics need native proof.
        for identity,runtime,advisory,row,allowed in (
            (('model_generator','mg_migrator'),0,0,(1,2,'a'*32),True),
            (('model_generator','mg_migrator'),0,0,None,True),
            (('client','mg_migrator'),0,0,(1,2,'a'*32),False),
            (('model_generator','mg_api'),0,0,(1,2,'a'*32),False),
            (('model_generator','mg_migrator'),1,0,(1,2,'a'*32),False),
            (('model_generator','mg_migrator'),0,1,(1,2,'a'*32),False),
            (('model_generator','mg_migrator'),0,0,(3,4,'b'*32),False)):
            with self.subTest(identity=identity,runtime=runtime,advisory=advisory,row=row):
                statements=[]
                class Connection:
                    def __enter__(inner):return inner
                    def __exit__(inner,*args):return False
                    def execute(inner,sql,params=None):
                        statements.append((sql,params))
                        result=None
                        if sql.startswith('SELECT current_database'):result=identity
                        elif 'pg_stat_activity' in sql:result=(runtime,)
                        elif 'pg_locks' in sql:result=(advisory,)
                        elif sql.startswith('SELECT device'):result=row
                        elif sql.startswith('DELETE'):result=row
                        return SimpleNamespace(fetchone=lambda:result)
                fake=SimpleNamespace(connect=lambda dsn:Connection())
                with patch.dict(sys.modules,{'psycopg':fake}),patch('pathlib.Path.read_text',return_value='synthetic'),patch.object(sys,'stdin',io.StringIO(json.dumps([1,2,'a'*32]))),patch.object(sys,'stdout',io.StringIO()):
                    if allowed:exec(cold.RESET_CODE,{})
                    else:
                        with self.assertRaises(RuntimeError):exec(cold.RESET_CODE,{})
                deletes=[entry for entry in statements if entry[0].startswith('DELETE')]
                self.assertEqual(len(deletes),int(allowed and row is not None))
                if deletes:self.assertEqual(deletes[0][1],(1,2,'a'*32))

    def test_foreign_writer_network_or_daemon_mount_denied(self):
        for key,value in (
            ('NetworkSettings',{'Networks':{'client_default':{}}}),
            ('Mounts',self.inventory['api']['Mounts']+[{'Type':'bind','Source':'/var/run/docker.sock','Destination':'/var/run/docker.sock','RW':True}])):
            with self.subTest(key=key):
                original=self.inventory['api'][key];self.inventory['api'][key]=value
                with patch.object(cold.subprocess,'check_output',side_effect=self.output),patch.object(cold.subprocess,'run') as mutation:
                    with self.assertRaises(ValueError):self.reset()
                    mutation.assert_not_called()
                self.inventory['api'][key]=original
    def test_public_503_requires_explicit_flag_and_both_paths(self):
        for origin,confirmed in (('https://model.axisconsult.ru',False),('https://client.invalid',True)):
            with patch.object(cold.urllib.request,'urlopen') as request:
                with self.assertRaises(ValueError):cold.maintenance_gate(origin,confirmed)
                request.assert_not_called()
        def unavailable(url,**kwargs):raise cold.urllib.error.HTTPError(url,503,'synthetic',None,None)
        with patch.object(cold.urllib.request,'urlopen',side_effect=unavailable) as request:
            cold.maintenance_gate('https://model.axisconsult.ru',True)
            self.assertEqual([c.args[0] for c in request.call_args_list],['https://model.axisconsult.ru/','https://model.axisconsult.ru/health/live'])
