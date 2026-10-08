"""Synthetic host-controlled managed worker crash/restart probes, not production hooks."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def serve():
    if os.environ.get('MG_TEST_MODE')!='1': raise SystemExit('Explicit test runtime required.')
    from model_generator.web import worker
    mode=os.environ.get('MG_TEST_PROCESS_MODE','normal')
    original=worker.run_child
    def child(settings,scratch,kind,cancelled,**kwargs):
        if 'startup' in scratch.parts:
            marker=scratch.parents[1]/'startup-preflight-count'
            marker.write_text(str(int(marker.read_text())+1 if marker.exists() else 1))
        if mode=='descendant' and 'jobs' in scratch.parts:
            kwargs['probe']='descendant'
        return original(settings,scratch,kind,cancelled,**kwargs)
    worker.run_child=child
    original_checkpoint=worker.JobRepository.checkpoint
    def checkpoint(self,*args,**kwargs):
        if mode=='before-checkpoint': os._exit(17)
        result=original_checkpoint(self,*args,**kwargs)
        if mode=='after-checkpoint': os._exit(17)
        return result
    worker.JobRepository.checkpoint=checkpoint
    if mode in {'lease-multipart','lease-terminal'}:
        from model_generator.web.db import Database,_BoundConnection
        from model_generator.web.config import Settings
        settings=Settings.from_env()
        def terminate_lease():
            # A separate bounded synthetic connection kills ONLY the managed
            # worker's dedicated session; PostgreSQL/API/S3 remain available.
            killer=Database(settings)
            try:
                with killer.connect() as con:
                    rows=con.execute("SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted").fetchall()
                    assert len(rows)==1
                    assert con.execute('SELECT pg_terminate_backend(%s) AS killed',(rows[0]['pid'],)).fetchone()['killed']
            finally: killer.close()
            print('Synthetic dedicated backend terminated at '+mode,flush=True)
        if mode=='lease-multipart':
            original_transfer=worker.ObjectStore._put_file
            def transfer(objects,intent,path,persist):
                calls=0
                def callback(multipart):
                    nonlocal calls
                    calls+=1
                    # Small bounded reports have one real uploaded part. This
                    # callback is immediately before CompleteMultipartUpload.
                    if calls==3: terminate_lease()
                    persist(multipart)
                return original_transfer(objects,intent,path,callback)
            worker.ObjectStore._put_file=transfer
        else:
            original_commit=_BoundConnection.commit
            def commit(connection):
                row=connection.execute("SELECT id FROM jobs WHERE state='completed' AND worker_epoch=(SELECT epoch FROM worker_state WHERE singleton)").fetchone()
                if row: terminate_lease()
                return original_commit(connection)
            _BoundConnection.commit=commit
    # Synthetic diagnostics identify the failing boundary without printing any
    # exception values, credentials, private input bodies or configuration.
    for name in ('recover','run_once'):
        original_operation=getattr(worker,name)
        def diagnostic(*args,_operation=original_operation,_name=name,**kwargs):
            try: return _operation(*args,**kwargs)
            except Exception as error:
                import traceback
                print('Synthetic '+_name+' exception type='+type(error).__name__,file=sys.stderr,flush=True)
                print(''.join(traceback.format_tb(error.__traceback__)),file=sys.stderr,flush=True)
                raise
        setattr(worker,name,diagnostic)
    sys.argv=['model_generator.web.worker']
    worker.main()


def seed():
    from web.helpers import reset_database,TestClient,make_test_app,settings_for,register_login
    reset_database()
    # The own proof volume preserves the API lock through sequential lifespans.
    with nullcontext('/proof/api') as tmp:
        app=make_test_app(settings_for(Path(tmp))); app.state.clock.now=int(time.time())
        with TestClient(app,base_url='https://testserver') as client:
            owner=register_login(client,'process_probe')
            headers={'Origin':'https://testserver','X-CSRF-Token':owner['csrf']}
            data=b'not-a-zip'; sha=hashlib.sha256(data).hexdigest()
            response=client.post('/api/uploads',json={'kind':'zip-fbx','displayName':'synthetic.zip','bytes':len(data),'sha256':sha},headers=headers)
            assert response.status_code==201
            upload=response.json()['id']
            assert client.put('/api/uploads/'+upload+'/content',content=data,headers={**headers,'Content-Type':'application/octet-stream'}).status_code==200
            response=client.post('/api/jobs',json={'uploadId':upload,'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-06'},headers=headers)
            assert response.status_code==201
            Path('/proof/worker-job').write_text(json.dumps({'id':response.json()['id'],'owner':owner['userId']}))
    print('Synthetic durable job seeded.')


def status():
    records=list(Path('/scratch/jobs').glob('*/*/descendant.pid')) if Path('/scratch/jobs').exists() else []
    result={'descendant':False}
    if records:
        record=records[0]; pid=int(record.read_text())
        result={'descendant':True,'pid':pid,'writes':(record.parent/'descendant.writes').stat().st_size}
    print(json.dumps(result))
    raise SystemExit(0 if result['descendant'] else 1)


def verify(stage):
    from web.helpers import admin_connect
    record=json.loads(Path('/proof/worker-job').read_text())
    with admin_connect() as con:
        job=con.execute('SELECT state,attempts,checkpoint,worker_epoch FROM mg.jobs WHERE id=%s',(record['id'],)).fetchone()
        running=con.execute("SELECT count(*) FROM mg.jobs WHERE state='running'").fetchone()[0]
        assert running<=1
        if stage=='running': assert job[0]=='running' and job[1]==1
        if stage=='interrupted': assert job[0]=='running' and job[1]==1
        if stage=='completed':
            assert job[0]=='completed' and job[1]==2,job[:2]
            assert job[2] and job[2]['schema']==1
            item=job[2]['artifact']
            assert con.execute('SELECT state,sha256 FROM mg.artifacts WHERE job_id=%s',(record['id'],)).fetchone()==('ready',item['sha256'])
        if stage=='unavailable':
            assert job[0]=='running'
            assert con.execute("SELECT count(*) FROM mg.artifacts WHERE job_id=%s AND state='ready'",(record['id'],)).fetchone()[0]==0
            assert job[2] is None
            assert con.execute('SELECT reservation_bytes,active_reserved FROM mg.jobs WHERE id=%s',(record['id'],)).fetchone()==(64*1024**2,True)
        if stage=='terminal-fenced':
            assert job[0]=='running' and job[1]==1 and job[2] is not None
            assert con.execute('SELECT active_reserved FROM mg.jobs WHERE id=%s',(record['id'],)).fetchone()[0]
        if stage=='cancelled':
            assert job[0]=='cancelled' and job[1]==1
    if stage=='completed':
        from model_generator.web.s3_store import ObjectStore,ObjectDescriptor
        from web.helpers import settings_for
        with tempfile.TemporaryDirectory() as tmp:
            objects=ObjectStore(settings_for(Path(tmp)).storage)
            with objects.open_stream(ObjectDescriptor(item['key'],item['bytes'],item['sha256'],'application/json')) as stream:
                data=stream.read(item['bytes']+1)
            assert len(data)==item['bytes'] and hashlib.sha256(data).hexdigest()==item['sha256']
            report=json.loads(data)
            assert report['coverage']['profile']=='research'
            assert any(f['status']=='fail' for f in report['findings'])
    print('Durable worker '+stage+' proof passed.')


def cancel():
    import secrets
    from web.helpers import admin_connect,settings_for,TestClient,make_test_app
    from model_generator.web.auth import COOKIE_NAME,encode,csrf_token,session_digest
    record=json.loads(Path('/proof/worker-job').read_text()); now=int(time.time())
    # The own proof volume preserves the API lock through sequential lifespans.
    with nullcontext('/proof/api') as tmp:
        settings=settings_for(Path(tmp)); raw=secrets.token_bytes(32); csrf=csrf_token(settings.auth_key,raw)
        with admin_connect() as con:
            con.execute('INSERT INTO mg.sessions VALUES(%s,%s,%s,%s,%s,%s)',(session_digest(raw),record['owner'],hashlib.sha256(csrf.encode()).hexdigest(),now,now+86400,now))
        app=make_test_app(settings); app.state.clock.now=now
        with TestClient(app,base_url='https://testserver') as client:
            client.cookies.set(COOKIE_NAME,encode(raw))
            start=time.monotonic()
            response=client.post('/api/jobs/'+record['id']+'/cancel',headers={'Origin':'https://testserver','X-CSRF-Token':csrf})
            elapsed=time.monotonic()-start
            assert response.status_code==202 and elapsed<2
            print('Actual running-child API cancel acknowledgement seconds='+format(elapsed,'.3f'))


if __name__=='__main__':
    if sys.argv[1:]==['serve']: serve()
    elif sys.argv[1:]==['seed']: seed()
    elif sys.argv[1:]==['status']: status()
    elif sys.argv[1:]==['cancel']: cancel()
    elif len(sys.argv)==3 and sys.argv[1]=='verify': verify(sys.argv[2])
    else: raise SystemExit('Invalid synthetic probe command.')
