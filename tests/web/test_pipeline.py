"""Container acceptance and a live API probe controlled by the own Docker harness."""
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import unittest
from web.helpers import TestClient, make_test_app, register_login, reset_database, settings_for


class PipelineTests(unittest.TestCase):
    def test_required_linux_amd64_python_runtime(self):
        self.assertEqual(platform.system(),'Linux')
        selected=os.environ.get('MG_TEST_PLATFORM','linux/amd64')
        self.assertIn(selected,{'linux/amd64','linux/arm64'})
        self.assertEqual(platform.machine(),'aarch64' if selected=='linux/arm64' else 'x86_64')
        self.assertEqual(platform.python_version(),'3.14.8')
        self.assertEqual(os.environ.get('MG_TEST_MODE'),'1')


def restart_probe(storage=False):
    """Keep the API and its cookie alive while the host stops/restarts only own PG."""
    reset_database()
    state=Path('/proof/state'); command=Path('/proof/command')
    def signal(value):
        stage=state.with_suffix('.tmp'); stage.write_text(value); stage.replace(state)
    def wait(value):
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if command.exists() and command.read_text()==value: return
            time.sleep(0.1)
        raise AssertionError('Restart harness command deadline exceeded')
    with tempfile.TemporaryDirectory() as tmp:
        app=make_test_app(settings_for(Path(tmp)))
        with TestClient(app,base_url='https://testserver') as client:
            user=register_login(client,'restart_user')
            with app.state.db.connect() as con:
                self_count=con.execute('SELECT count(*) AS n FROM usage_events').fetchone()['n']
                assert self_count==1
            if storage:
                import hashlib
                data=b'synthetic durable input'
                headers={'Origin':'https://testserver','X-CSRF-Token':user['csrf']}
                accepted=client.post('/api/uploads',json={'kind':'zip-fbx','displayName':'synthetic.zip','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},headers=headers)
                assert accepted.status_code==201,accepted.text
                upload=accepted.json()['id']
                completed=client.put('/api/uploads/'+upload+'/content',content=data,headers={**headers,'Content-Type':'application/octet-stream'})
                assert completed.status_code==200,completed.text
                intent=app.state.storage.intent(user['userId'],upload)
                # Every real S3/network operation is outside SQL transactions.
                assert app.state.storage.objects.head(intent).bytes==len(data)
            signal('seeded')
            wait('offline')
            started=time.monotonic()
            response=client.get('/api/auth/me')
            assert response.status_code==503
            assert time.monotonic()-started<2
            assert response.headers['retry-after']=='1'
            assert client.get('/health/live').status_code==200
            assert client.get('/health/ready').json()['reason']=='database_not_ready'
            signal('offline-passed')
            wait('recovered')
            assert client.get('/health/ready').json()['reason']=='worker_not_ready'
            assert client.get('/api/auth/me').json()['id']==user['userId']
            with app.state.db.connect() as con:
                assert con.execute('SELECT count(*) AS n FROM usage_events').fetchone()['n']==(2 if storage else 1)
                assert con.execute("SELECT count FROM auth_attempts WHERE action='login-ip'").fetchone()['count']==1
            assert client.post('/api/auth/logout',headers={'Origin':'https://testserver','X-CSRF-Token':user['csrf']}).status_code==204
            assert client.get('/api/auth/me').status_code==401
            if storage:
                descriptor=app.state.storage.objects.head(intent)
                assert descriptor is not None
                with app.state.storage.objects.open_stream(descriptor) as stream: assert stream.read()==data
                with app.state.db.connect() as con:
                    assert con.execute('SELECT state,object_sha256 FROM uploads WHERE id=%s',(upload,)).fetchone()=={'state':'ready','object_sha256':hashlib.sha256(data).hexdigest()}
            signal('restart-passed')
            wait('finish')
        if storage:
            # Restart the API with the same lock path while deleting only disposable scratch.
            import shutil
            shutil.rmtree(Path(tmp)/'uploads')
            restarted=make_test_app(settings_for(Path(tmp)))
            with TestClient(restarted,base_url='https://testserver'):
                persisted=restarted.state.storage.objects.head(intent)
                assert persisted is not None
                with restarted.state.storage.objects.open_stream(persisted) as stream: assert stream.read()==data
                with restarted.state.db.connect() as con:
                    assert con.execute('SELECT state FROM uploads WHERE id=%s',(upload,)).fetchone()['state']=='ready'
    print('Actual PostgreSQL/API restart, durable session/counters and private S3 restart/scratch loss passed.' if storage else 'Actual PostgreSQL container stop/restart, live API recovery and durable session/counters passed.')


def network_gate():
    import socket
    for host in ('axis-postgres','axis-erp','axis-admin','axis-portal','minio'):
        try: socket.getaddrinfo(host,None)
        except socket.gaierror: continue
        raise AssertionError('Forbidden network neighbour resolved')
    print('Own networks only; API cannot resolve S3 backend or Axis neighbours.')


def crash_worker(root,phase,owner):
    """Actual process death, never an exception pretending to close the OS lease."""
    import hashlib,json
    from dataclasses import replace
    settings=replace(settings_for(Path(root)),min_free_disk_bytes=0)
    app=make_test_app(settings)
    with TestClient(app,base_url='https://testserver'):
        store=app.state.storage
        row=store.reserve_upload(owner,'zip-fbx','synthetic',1,hashlib.sha256(b'x').hexdigest(),int(app.state.clock()))
        store.claim_content(owner,row['id'])
        path=store.private_path('uploads',row['id'],'input.part'); path.write_bytes(b'x')
        (Path(root)/'crash-record.json').write_text(json.dumps({'id':row['id']}))
        intent=store.intent(owner,row['id']); calls=[0]
        def persist(multipart):
            calls[0]+=1
            if phase=='before-id': os._exit(17)
            store._sql(store._persist_multipart,owner,row['id'],intent.attempt_epoch,multipart)
            if phase=='before-complete' and calls[0]==3: os._exit(17)
        store.objects.put_file(intent,path,persist)
        if phase=='after-complete': os._exit(17)
    raise AssertionError('Crash boundary was not reached')


if __name__=='__main__':
    if sys.argv[1:]==['--restart-probe']: restart_probe()
    elif sys.argv[1:]==['--restart-probe','storage']: restart_probe(storage=True)
    elif sys.argv[1:]==['--network-gate']: network_gate()
    elif len(sys.argv)==5 and sys.argv[1]=='--crash-worker': crash_worker(*sys.argv[2:])
    else: unittest.main()
