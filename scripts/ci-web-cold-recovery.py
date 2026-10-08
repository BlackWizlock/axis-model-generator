"""Native isolated cold proof. Never uses production project, secrets or routes."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
release=load('cold_proof_release',ROOT/'scripts/release-web.py')
cold=release.cold_operator

SNAPSHOT="""import json,psycopg
from pathlib import Path
from psycopg import sql
with psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as con:
    result={}
    for (name,) in con.execute("SELECT tablename FROM pg_tables WHERE schemaname='mg' ORDER BY tablename"):
        if name=='api_lock_identity':continue
        result[name]=con.execute(sql.SQL("SELECT COALESCE(jsonb_agg(value ORDER BY value::text),'[]'::jsonb) FROM (SELECT to_jsonb(t) value FROM mg.{} t) q").format(sql.Identifier(name))).fetchone()[0]
    print(json.dumps(result,sort_keys=True))
"""
IDENTITY="""import json,psycopg
from pathlib import Path
with psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as con:
    print(json.dumps(con.execute('SELECT device,inode,generation FROM mg.api_lock_identity WHERE singleton').fetchone()))
"""
S3_SNAPSHOT="""import boto3,hashlib,json
from pathlib import Path
from botocore.config import Config
client=boto3.client('s3',endpoint_url='http://minio:9000',aws_access_key_id=Path('/run/secrets/s3_access').read_text(),aws_secret_access_key=Path('/run/secrets/s3_secret').read_text(),config=Config(proxies={'http':'http://s3-proxy:8080'}))
result={}
for page in client.get_paginator('list_objects_v2').paginate(Bucket='model-generator-test'):
    for row in page.get('Contents',[]):
        body=client.get_object(Bucket='model-generator-test',Key=row['Key'])['Body']
        try:result[row['Key']]=hashlib.sha256(body.read()).hexdigest()
        finally:body.close()
print(json.dumps(result,sort_keys=True))
"""
QUOTAS="""import psycopg
from pathlib import Path
with psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as con:
    for scope,owner,storage,uploads,jobs in con.execute('SELECT scope,owner_id,storage_bytes,active_uploads,active_jobs FROM mg.quota_scopes'):
        condition='TRUE' if owner is None else 'owner_id=%s';params=() if owner is None else (owner,)
        upload_bytes,upload_active=con.execute('SELECT COALESCE(sum(reservation_bytes),0),count(*) FILTER(WHERE active_reserved) FROM mg.uploads WHERE '+condition,params).fetchone()
        job_bytes,job_active=con.execute('SELECT COALESCE(sum(reservation_bytes),0),count(*) FILTER(WHERE active_reserved) FROM mg.jobs WHERE '+condition,params).fetchone()
        assert (storage,uploads,jobs)==(upload_bytes+job_bytes,upload_active,job_active),(scope,storage,uploads,jobs)
        assert 0<=uploads<=(2 if owner is None else 1) and 0<=jobs<=(20 if owner is None else 2)
print('Actual quota counters match durable reservations and unchanged limits')
"""
# The real schedule method verifies the existing durable S3 part ledger under
# the real exclusive API OS lock. No synthetic jobs/checkpoints/SQL state reset.
FINALIZING="""import fcntl,json,os,sys
from model_generator.web.config import Settings
from model_generator.web.db import Database
from model_generator.web.store import Storage
from model_generator.web.lock_identity import generation,valid as lock_valid
settings=Settings.from_env();db=Database(settings);store=Storage(db,settings)
fd=os.open(settings.data_root/'api.lock',os.O_RDWR|os.O_NOFOLLOW)
try:
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    locked=os.fstat(fd);nonce=generation(fd)
    def valid():
        return lock_valid(fd,settings.data_root/'api.lock',(locked.st_dev,locked.st_ino,nonce))
    store.api_lock_valid=valid
    id=json.load(sys.stdin)
    with db.connect() as con:
        identity=con.execute('SELECT device,inode,generation FROM api_lock_identity WHERE singleton').fetchone()
        assert identity=={'device':locked.st_dev,'inode':locked.st_ino,'generation':nonce}
        row=con.execute('SELECT owner_id FROM uploads WHERE id=%s',(id,)).fetchone()
    result=store.chunks.schedule(row['owner_id'],id)
    assert result['state']=='finalizing'
    print('Actual durable chunk finalization scheduled under exclusive OS lock')
finally:os.close(fd)
"""

COLD_STARTUP_DENY="""import asyncio
from model_generator.web.app import create_default_app
async def probe():
    app=create_default_app()
    try:
        async with app.router.lifespan_context(app):pass
    except RuntimeError as error:
        if str(error)!='API lock identity changed':
            raise AssertionError('Expected persisted API lock identity denial') from None
    else:
        raise AssertionError('Fresh scratch API startup unexpectedly accepted')
    print('Persisted old API identity denied actual fresh scratch startup')
asyncio.run(probe())
"""

def attest_images(manifest,source):
    if not re.fullmatch(r'[0-9a-f]{40}',source) or manifest.get('source_revision')!=source:
        raise SystemExit('Exact final runtime source required')
    for role in ('api','worker','proxy','keeper','emulator'):
        actual=cold.inspect(manifest['images'][role]['id'])
        if actual['Id']!=manifest['images'][role]['id'] or actual['Architecture']!='amd64' or actual['Os']!='linux':
            raise SystemExit('Exact native image identity required')
        labels=actual['Config'].get('Labels') or {}
        if role in ('api','worker') and labels.get('org.opencontainers.image.revision')!=source:
            raise SystemExit('Runtime image source attestation mismatch')


class Proof:
    def __init__(self,manifest,source):
        self.project='mg-cold-proof-'+uuid.uuid4().hex[:10]
        self.runtime=ROOT/'_scratch'/self.project;self.runtime.mkdir(mode=0o700,parents=True)
        self.images=manifest['images'];self.source=source;self.phase='bootstrap';self.private=[]
        self.compose=[];self.interrupt=None;self.interrupt_failure=''
    def run(self,args,*,data=None,check=True):
        value=subprocess.run(args,input=data,text=True,capture_output=True,timeout=180)
        if check and value.returncode:raise RuntimeError('Own cold phase command failed')
        return value
    def interrupted_ready(self):
        line=self.interrupt.stdout.readline()
        if not line:
            status=self.interrupt.wait(timeout=10)
            stderr=self.interrupt.stderr.read(65536)
            self.interrupt_failure='\nInterrupted HTTP fixture exited before ready signal; phase='+self.phase+'; exit='+str(status)+'\n'+stderr
            raise RuntimeError('Interrupted HTTP fixture exited before ready signal (exit '+str(status)+')')
        return json.loads(line)
    def oneoff(self,code,data=None):
        return self.run(self.compose+['run','--rm','-T','--entrypoint','python','api','-c',code],data=None if data is None else json.dumps(data)).stdout
    def api(self,path,data):
        if data.get('legacyWithoutConsent') is True and self.phase not in ('legacy-bootstrap','legacy-transition'):
            raise ValueError('Legacy consent exception outside actual legacy proof')
        return self.run(self.compose+['exec','-T','api','python','-c',path.read_text()],data=json.dumps(data)).stdout
    def identity(self):return json.loads(self.oneoff(IDENTITY))
    def configure(self,previous=None):
        secrets=self.runtime/'secrets/runtime'
        self.run([sys.executable,str(ROOT/'scripts/web-test-env.py'),str(secrets)])
        import secrets as random_secrets
        (secrets/'backup').write_text(random_secrets.token_hex(32));(secrets/'backup').chmod(0o444)
        startup_images=self.images if previous is None else {**self.images,**{role:previous['images'][role] for role in ('api','worker')}}
        os.environ.update(MG_PUBLIC_ORIGIN='https://testserver',MG_TRUSTED_PROXY_IPS='127.0.0.1',MG_SECRET_ROOT=str(secrets),MG_S3_REGION='us-east-1',MG_S3_BUCKET='model-generator-test',**{'MG_'+role.upper()+'_IMAGE':startup_images[role]['id'] for role in ('api','worker','proxy','keeper')})
        overlay=self.runtime/'proof.yaml'
        overlay.write_text('''services:
  api:
    environment:
      MG_TEST_MODE: "1"
      MG_S3_PROFILE: ephemeral-emulator
      MG_S3_ENDPOINT: http://minio:9000
      MG_MIGRATION_DATABASE_URL_FILE: /run/secrets/migrator_dsn
    secrets: [migrator_dsn]
  worker:
    environment:
      MG_TEST_MODE: "1"
      MG_S3_PROFILE: ephemeral-emulator
      MG_S3_ENDPOINT: http://minio:9000
  s3-proxy:
    entrypoint: [python, /proxy.py]
    volumes: ["'''+str(ROOT/'deploy/web/s3-test-proxy.py')+''':/proxy.py:ro"]
  minio:
    image: '''+self.images['emulator']['id']+'''
    environment:
      MINIO_ROOT_USER_FILE: /run/secrets/s3_access
      MINIO_ROOT_PASSWORD_FILE: /run/secrets/s3_secret
    secrets: [s3_access, s3_secret]
    networks: [mg_egress]
secrets:
  migrator_dsn:
    file: '''+str(secrets/'migrator_dsn')+'''
networks:
  mg_edge:
    ipam:
      config: [{subnet: 10.251.51.0/24}]
  mg_db:
    ipam:
      config: [{subnet: 10.251.52.0/24}]
  mg_files:
    ipam:
      config: [{subnet: 10.251.53.0/24}]
  mg_egress:
    ipam:
      config: [{subnet: 10.251.54.0/24}]
''')
        self.compose=['docker','compose','-p',self.project,'-f',str(ROOT/'deploy/web/compose.yaml'),'-f',str(overlay)]
        self.run(self.compose+['up','-d','--wait','postgres','minio','s3-proxy']+(['scratch-keeper'] if previous is None else []))
        self.oneoff('from model_generator.web.migrate import main;main()')
        self.oneoff(S3_SNAPSHOT.split('result={}')[0]+"client.create_bucket(Bucket='model-generator-test')")
        if previous is None:self.start()
        else:
            self.run(self.compose+['up','-d','--wait','api','worker']);self.ready()
    def start(self,worker=True):
        self.run(self.compose+['up','-d','--wait','--no-recreate','scratch-keeper'])
        self.run(self.compose+['up','-d','--wait','--force-recreate','api']+(['worker'] if worker else []))
        self.run(self.compose+['exec','-T','api','python','scripts/web-healthcheck.py','--url','http://localhost:8000/health/'+('ready' if worker else 'live')])
    def ready(self):
        self.run(self.compose+['exec','-T','api','python','scripts/web-healthcheck.py','--url','http://localhost:8000/health/ready'])
    def crash(self,mode):
        overlay=self.runtime/'crash.yaml'
        code=(ROOT/'tests/fixtures/cold_recovery_crash.py').read_text()
        overlay.write_text('services:\n  worker:\n    restart: "no"\n    entrypoint: '+json.dumps(['python','-c',code,mode])+'\n')
        self.run(self.compose+['-f',str(overlay),'up','-d','--force-recreate','worker'])
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            id=self.run(self.compose+['ps','-a','-q','worker']).stdout.strip()
            state=cold.inspect(id)['State']
            if not state['Running']:
                assert state['ExitCode']==137 and state['Pid']==0,state
                break
            time.sleep(.5)
        else:raise RuntimeError('Actual worker never reached test crash boundary')
    def reset(self,expected):
        cold.reset(self.compose,self.runtime,self.project,self.images,expected,release.keeper_gate)
    def remove_probe(self):
        name=self.project+'-db-negative'
        result=self.run(['docker','inspect',name],check=False)
        if result.returncode==0:
            value=json.loads(result.stdout)[0]
            if value['Config']['Labels'].get('com.axis.model-generator.cold-proof')!=self.project:
                raise ValueError('Foreign DB probe container denied')
            self.run(['docker','rm','-f',name])
    def db_negative(self,kind,expected):
        dsn=self.runtime/'secrets/runtime'/('worker_dsn' if kind=='runtime' else 'migrator_dsn')
        lock_code="con.execute('SELECT pg_advisory_lock(736421)');" if kind=='advisory' else "con.execute('SELECT singleton FROM mg.api_lock_identity FOR UPDATE');" if kind=='row' else ''
        code="import sys,psycopg;from pathlib import Path;con=psycopg.connect(Path('/run/secrets/database').read_text(),connect_timeout=2);"+lock_code+"print('ready',flush=True);sys.stdin.readline();con.close()"
        args=['docker','run','--rm','-i','--name',self.project+'-db-negative','--label','com.axis.model-generator.cold-proof='+self.project,'--network',self.project+'_mg_db','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--cpus','.5','--pids-limit','32','-v',str(dsn)+':/run/secrets/database:ro','--entrypoint','python',self.images['api']['id'],'-c',code]
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            assert process.stdout.readline().strip()=='ready'
            try:self.reset(expected)
            except subprocess.CalledProcessError:pass
            else:raise AssertionError('Actual PostgreSQL offline deny gate failed')
            assert self.identity()==list(expected)
        finally:
            try:process.communicate('\n',timeout=10)
            except subprocess.TimeoutExpired:
                self.remove_probe();process.kill();process.wait(timeout=10)
            finally:
                self.remove_probe()
                for stream in (process.stdin,process.stdout,process.stderr):stream.close()
    def cold_cycle(self,mode,finalizing=None,api_live=True):
        self.phase='cold-'+mode;expected=tuple(self.identity())
        if api_live:
            try:self.reset(expected)
            except ValueError:pass
            else:raise AssertionError('Live API reset unexpectedly allowed')
            assert self.identity()==list(expected)
        if finalizing:
            self.run(self.compose+['stop','api'])
            self.oneoff(FINALIZING,finalizing)
        cold.prepare(self.compose,self.project,self.images,release.keeper_gate)
        inventory=cold.holders(self.project,self.images,release.keeper_gate)
        assert all(cold.stopped(v) for v in inventory.values() if v['Config']['Labels']['com.docker.compose.service']!='scratch-keeper')
        before=json.loads(self.oneoff(SNAPSHOT));s3_before=json.loads(self.oneoff(S3_SNAPSHOT))
        assert self.identity()==list(expected)
        fresh_code="import json,os;from model_generator.web.lock_identity import generation;\ntry:fd=os.open('/scratch/api.lock',os.O_RDONLY|os.O_NOFOLLOW)\nexcept FileNotFoundError:print('null')\nelse:s=os.fstat(fd);print(json.dumps([s.st_dev,s.st_ino,generation(fd)]));os.close(fd)"
        # Actual managed API image, normal UID/nets/scratch, no binds or migration.
        # --rm returns only after the one-off lifespan and physical process exited.
        self.run(self.compose+['run','--rm','--no-deps','-T','--entrypoint','python','api','-c',COLD_STARTUP_DENY])
        fresh=json.loads(self.run(self.compose+['run','--rm','--no-deps','-T','--entrypoint','python','api','-c',fresh_code]).stdout)
        assert fresh[2]!=expected[2], 'Fresh scratch generation reused'
        print(json.dumps({'phase':self.phase,'oldLock':list(expected),'freshLock':fresh,'kernelPairReused':fresh[:2]==list(expected[:2])}),flush=True)
        inventory=cold.holders(self.project,self.images,release.keeper_gate)
        keepers=[v for v in inventory.values() if v['Config']['Labels']['com.docker.compose.service']=='scratch-keeper']
        assert len(keepers)==1 and keepers[0]['State']['Running']
        assert all(cold.stopped(v) for v in inventory.values() if v not in keepers)
        assert self.identity()==list(expected)
        assert json.loads(self.oneoff(SNAPSHOT))==before
        assert json.loads(self.oneoff(S3_SNAPSHOT))==s3_before
        self.db_negative('runtime',expected);self.db_negative('advisory',expected);self.db_negative('row',expected)
        try:self.reset((expected[0],expected[1]+1,expected[2]))
        except subprocess.CalledProcessError:pass
        else:raise AssertionError('Wrong old DB identity unexpectedly accepted')
        assert self.identity()==list(expected)
        self.reset(expected);self.reset(expected)
        assert self.identity() is None
        assert json.loads(self.oneoff(SNAPSHOT))==before
        assert json.loads(self.oneoff(S3_SNAPSHOT))==s3_before
        self.start(worker=False)
        new=self.identity();assert new!=list(expected) and new is not None
        assert new==json.loads(self.run(self.compose+['exec','-T','api','python','-c',"import json,os;from model_generator.web.lock_identity import generation;fd=os.open('/scratch/api.lock',os.O_RDONLY|os.O_NOFOLLOW);s=os.fstat(fd);print(json.dumps([s.st_dev,s.st_ino,generation(fd)]));os.close(fd)"]).stdout)
        keeper=self.run(self.compose+['ps','-q','scratch-keeper']).stdout.strip()
        self.run(['docker','update','--restart=unless-stopped',keeper])
        print('Cold '+mode+': physical holder exit, fresh generation startup denial, DB deny gates, idempotent identity exception and unchanged durable/S3 snapshot passed',flush=True)
    def legacy_proof(self,previous):
        self.phase='legacy-bootstrap';self.configure(previous)
        old=json.loads(self.oneoff(IDENTITY.replace(',generation','')))
        assert len(old)==2
        old_file=json.loads(self.run(self.compose+['exec','-T','api','python','-c',"import os,json;s=os.stat('/scratch/api.lock');print(json.dumps([s.st_dev,s.st_ino,s.st_size]))"]).stdout)
        assert old_file==old+[0], 'Actual legacy lock must be the old empty file'
        fixture=self.run(self.compose+['exec','-T','api','python','-c',(ROOT/'tests/fixtures/package_builders.py').read_text()+"\nimport base64;print(base64.b64encode(make_package()).decode())"]).stdout.strip()
        http=ROOT/'tests/web/production_http_proof.py'
        ready=json.loads(self.api(http,{'mode':'create','fixture':fixture,'legacyWithoutConsent':True}));self.private.extend((ready['ownerCookie'],ready['neighbourCookie']))
        os.environ.update(**{'MG_'+role.upper()+'_IMAGE':self.images[role]['id'] for role in ('api','worker')})
        self.phase='legacy-transition'
        with cold.operator_lock(self.runtime):
            release.legacy_operator.transition(self.compose,self.runtime,self.project,self.images,previous,tuple(old)+(None,),cold,release.keeper_gate,lambda:self.oneoff('from model_generator.web.migrate import main;main()'),lambda:self.run(self.compose+['up','-d','--wait','postgres','s3-proxy']))
        self.start();new=self.identity();assert new[2] is not None
        self.api(http,{'mode':'verify','proof':ready,'legacyWithoutConsent':True});self.oneoff(QUOTAS);self.ready()
        print(json.dumps({'phase':'legacy-transition','oldLock':old+[None],'newLock':new}),flush=True)
        print('Final native isolated legacy generation transition acceptance passed',flush=True)
    def run_proof(self):
        self.configure()
        fixture=self.run(self.compose+['exec','-T','api','python','-c',(ROOT/'tests/fixtures/package_builders.py').read_text()+"\nimport base64;print(base64.b64encode(make_package()).decode())"]).stdout.strip()
        http=ROOT/'tests/web/production_http_proof.py';active_http=ROOT/'tests/web/production_active_restore_proof.py';own_http=ROOT/'tests/fixtures/cold_recovery_http.py'
        ready=json.loads(self.api(http,{'mode':'create','fixture':fixture}));self.private.extend((ready['ownerCookie'],ready['neighbourCookie']))
        self.run(self.compose+['stop','worker'])
        active=json.loads(self.api(active_http,{'mode':'seed','fixture':fixture,'proof':ready}))
        with cold.operator_lock(self.runtime):
            try:
                with cold.operator_lock(self.runtime):pass
            except ValueError:pass
            else:raise AssertionError('Concurrent operator was accepted')
            self.cold_cycle('queued-chunks',active['finalizing'])
        self.api(own_http,{'mode':'retry-complete','proof':ready,'active':active})
        self.run(self.compose+['up','-d','--wait','--force-recreate','worker'])
        self.api(active_http,{'mode':'verify','proof':ready,'active':active})
        self.api(http,{'mode':'verify','proof':ready})
        completed_chunks=json.loads(self.oneoff("import json,psycopg,sys;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as c:print(json.dumps(c.execute('SELECT state,object_key,object_bytes,object_sha256 FROM mg.uploads WHERE id=ANY(%s)',(json.load(sys.stdin),)).fetchall()))",[active['receiving'],active['finalizing']]))
        objects=json.loads(self.oneoff(S3_SNAPSHOT));assert len(completed_chunks)==2
        for state,key,size,digest in completed_chunks:
            assert state=='ready' and size==active['bytes'] and digest==active['sha256']
            assert objects[key]==active['sha256']
        self.oneoff(QUOTAS);self.ready()
        for mode in ('claim','checkpoint'):
            self.phase='seed-'+mode
            self.run(self.compose+['stop','worker'])
            job=json.loads(self.api(own_http,{'mode':'job','fixture':fixture,'cookie':ready['ownerCookie']}));self.private.append(job['cookie'])
            self.crash(mode)
            code="import json,psycopg,sys;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as c:print(json.dumps(c.execute('SELECT state,checkpoint,worker_epoch,attempts FROM mg.jobs WHERE id=%s',(json.load(sys.stdin),)).fetchone()))"
            row=json.loads(self.oneoff(code,job['id']));assert row[0]=='running' and row[2] and row[3]==1
            assert (row[1] is not None)==(mode=='checkpoint')
            if mode=='checkpoint':
                objects=json.loads(self.oneoff(S3_SNAPSHOT))
                for name in ('artifact','previewInput','thumbnail'):
                    if name in row[1]:
                        descriptor=row[1][name]
                        assert objects[descriptor['key']]==descriptor['sha256']
            interrupted=None
            if mode=='claim':
                # stdout is private; the sender remains inside the API's process
                # namespace until the host physically kills that exact container.
                self.phase='seed-interrupted-single-put'
                args=self.compose+['exec','-T','api','python','-c',own_http.read_text()]
                self.interrupt=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                self.interrupt.stdin.write(json.dumps({'mode':'interrupt','fixture':fixture,'cookie':ready['ownerCookie']}));self.interrupt.stdin.close()
                interrupted=self.interrupted_ready();self.private.append(interrupted['cookie'])
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    status=json.loads(self.oneoff("import json,psycopg,sys;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as c:print(json.dumps(c.execute('SELECT protocol,content_claimed,writer_closed FROM mg.uploads WHERE id=%s',(json.load(sys.stdin),)).fetchone()))",interrupted['id']))
                    if status==['single-put',True,False]:break
                    time.sleep(.05)
                assert status==['single-put',True,False],status
                # Live negative before SIGKILL, with exact inventory validation.
                expected=tuple(self.identity())
                try:self.reset(expected)
                except ValueError:pass
                else:raise AssertionError('Live interrupted API reset accepted')
                inventory=cold.holders(self.project,self.images,release.keeper_gate)
                api=next(id for id,value in inventory.items() if value['Config']['Labels']['com.docker.compose.service']=='api')
                self.run(['docker','update','--restart=no',api]);self.run(['docker','kill','--signal','KILL',api])
                self.interrupt.wait(timeout=10)
            with cold.operator_lock(self.runtime):self.cold_cycle(mode,api_live=interrupted is None)
            self.run(self.compose+['up','-d','--wait','--force-recreate','worker'])
            self.api(own_http,{'mode':'verify','job':job,'neighbour':ready['neighbourCookie']})
            recovered=json.loads(self.oneoff(code,job['id']))
            assert recovered[0]=='completed' and recovered[2]!=row[2] and recovered[3]==2
            if mode=='checkpoint':assert recovered[1]['artifact']==row[1]['artifact']
            self.api(http,{'mode':'verify','proof':ready})
            if interrupted:
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    row=json.loads(self.oneoff("import json,psycopg,sys;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as c:print(json.dumps(c.execute('SELECT state,reservation_bytes,writer_closed,active_reserved FROM mg.uploads WHERE id=%s',(json.load(sys.stdin),)).fetchone()))",interrupted['id']))
                    if row==['deleted',0,True,False]:break
                    time.sleep(.5)
                assert row==['deleted',0,True,False],row
            self.oneoff(QUOTAS);self.ready()
        self.run(self.compose+['stop','worker'])
        cancelled=json.loads(self.api(own_http,{'mode':'job','fixture':fixture,'cookie':ready['ownerCookie']}));self.private.append(cancelled['cookie'])
        self.api(own_http,{'mode':'cancel','job':cancelled})
        self.run(self.compose+['up','-d','--wait','--force-recreate','worker'])
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            row=json.loads(self.oneoff("import json,psycopg,sys;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/migrator_dsn').read_text()) as c:print(json.dumps(c.execute('SELECT state,active_reserved FROM mg.jobs WHERE id=%s',(json.load(sys.stdin),)).fetchone()))",cancelled['id']))
            if row==['cancelled',False]:break
            time.sleep(.5)
        assert row==['cancelled',False],row
        self.api(own_http,{'mode':'cancel','job':cancelled});self.oneoff(QUOTAS);self.ready()
        self.api(http,{'mode':'verify','proof':ready,'withdrawConsent':True})
        print('Final native isolated cold recovery acceptance passed',flush=True)
    def close(self,failed):
        if failed and self.compose:
            logs=self.run(self.compose+['logs','--no-color','--tail','500','api','worker','postgres'],check=False)
            text='Cold failed phase: '+self.phase+'\n'+logs.stdout+logs.stderr+getattr(self,'interrupt_failure','')
            secrets=self.runtime/'secrets/runtime'
            import http.cookies
            values=set(self.private)
            for cookie in self.private:
                parsed=http.cookies.SimpleCookie(cookie)
                values.update(item.value for item in parsed.values())
            if secrets.exists():
                for path in secrets.iterdir():
                    value=path.read_text().strip();values.add(value)
                    if value.startswith('postgresql://') and urlsplit(value).password:values.add(urlsplit(value).password)
            for value in sorted(values,key=len,reverse=True):
                if value:text=text.replace(value,'[REDACTED]')
            path=self.runtime/'failure-redacted.log';path.write_text(text[-256*1024:]);path.chmod(0o600)
        if self.compose:
            self.remove_probe()
            cleanup=self.run(self.compose+['down','--volumes','--remove-orphans'],check=False)
            if cleanup.returncode:raise RuntimeError('Own cold project cleanup failed')
        if self.interrupt:
            self.interrupt.stdin=None
            try:self.interrupt.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.interrupt.terminate();self.interrupt.wait(timeout=10)
            for stream in (self.interrupt.stdout,self.interrupt.stderr):stream.close()
        secrets=self.runtime/'secrets/runtime'
        if secrets.exists():
            for path in secrets.iterdir():path.unlink()
            secrets.rmdir();secrets.parent.rmdir()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--images',type=Path,required=True);parser.add_argument('--runtime-source',required=True);parser.add_argument('--native-authorized',action='store_true');parser.add_argument('--previous-evidence',type=Path);parser.add_argument('--previous-source');parser.add_argument('--previous-export-manifest-sha256');args=parser.parse_args()
    if not args.native_authorized or platform.system()!='Linux' or platform.machine() not in ('x86_64','amd64'):
        raise SystemExit('Explicit native isolated proof authorization and Linux amd64 required')
    manifest=json.loads(args.images.read_text())
    attest_images(manifest,args.runtime_source)
    previous=None
    if any((args.previous_evidence,args.previous_source,args.previous_export_manifest_sha256)):
        if not all((args.previous_evidence,args.previous_source,args.previous_export_manifest_sha256)):raise SystemExit('Complete explicit previous attestation required')
        previous=release.legacy_operator.previous_gate(args.previous_evidence,args.previous_source,args.previous_export_manifest_sha256)
    proof=Proof(manifest,args.runtime_source);failed=True
    print('Runtime source '+args.runtime_source+'; reviewed operator helper SHA256 '+hashlib.sha256((ROOT/'scripts/cold-recovery-web.py').read_bytes()).hexdigest()+'; host proof SHA256 '+hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),flush=True)
    try:
        if previous is not None:
            proof.legacy_proof(previous);proof.close(False)
            proof=Proof(manifest,args.runtime_source)
        proof.run_proof();failed=False
        attestation={'runtime_source':args.runtime_source,'runtime_images':release.manifest_image_ids(manifest),'helpers':{name:hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest() for name in ('cold-recovery-web.py','ci-web-cold-recovery.py','legacy-recovery-web.py')}}
        if previous is not None:
            attestation.update(legacy_source=previous['source_revision'],previous_images=release.manifest_image_ids(previous),previous_export_manifest_sha256=previous['_export_manifest_sha256'])
        path=args.images.parent/'cold-native-attestation.json';path.write_text(json.dumps(attestation,sort_keys=True)+'\n');path.chmod(0o600)
    except Exception as error:raise SystemExit('Isolated cold proof failed in '+proof.phase+' ('+type(error).__name__+'); see private redacted log') from None
    finally:proof.close(failed)
if __name__=='__main__':main()
