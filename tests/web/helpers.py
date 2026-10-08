"""Own PostgreSQL only; scratch temporary directories never create a DB."""
from contextlib import ExitStack
from pathlib import Path
import os
import secrets
import selectors
import socket
import threading
import warnings
import psycopg
with warnings.catch_warnings():
    warnings.filterwarnings('ignore', message='Using `httpx` with `starlette.testclient` is deprecated.*')
    from fastapi.testclient import TestClient
from model_generator.web.app import create_app
from model_generator.web.config import Settings,StorageSettings


def secret(name):
    return Path(os.environ[name + '_FILE']).read_text().strip()


def admin_connect():
    return psycopg.connect(secret('MG_TEST_ADMIN_DATABASE_URL'), autocommit=True)


def reset_database():
    with admin_connect() as con:
        con.execute('TRUNCATE mg.sessions, mg.usage_events, mg.auth_attempts, mg.quota_scopes, mg.users, mg.service_state, mg.worker_state RESTART IDENTITY CASCADE')
        con.execute("INSERT INTO mg.quota_scopes(scope) VALUES ('global')")


def settings_for(root):
    return Settings(data_root=Path(root), public_origin='https://testserver', auth_key=bytes.fromhex(secret('MG_AUTH_KEY')), database_url=secret('MG_DATABASE_URL'), storage=StorageSettings.from_env())


class Clock:
    def __init__(self): self.now = 1800000000
    def __call__(self): return self.now
    def tick(self, seconds): self.now += seconds


def make_test_app(settings: Settings):
    if not isinstance(settings, Settings) or os.environ.get('MG_TEST_MODE') != '1':
        raise ValueError('Explicit ephemeral PostgreSQL test configuration required')
    app = create_app(settings)
    app.state.clock = Clock()
    return app


def register_login(client, username):
    password = secrets.token_urlsafe(24)
    headers = {'Origin': 'https://testserver'}
    response = client.post('/api/auth/register', json={'username':username, 'password':password}, headers=headers)
    assert response.status_code == 201, response.text
    response = client.post('/api/auth/login', json={'username':username, 'password':password}, headers=headers)
    assert response.status_code == 200, response.text
    me = client.get('/api/auth/me').json()
    return {'userId':me['id'], 'csrf':me['csrfToken']}


class PostgreSQLProxy:
    """Real TCP forwarding; pause also blackholes new cancellation connections."""
    def __init__(self,host,port=5432,pause_on=None):
        self.host=host; self.port=port; self.pause_on=pause_on
        self.paused=threading.Event(); self.stop=threading.Event()
        self.listener=socket.socket(); self.listener.bind(('127.0.0.1',0)); self.listener.listen()
        self.listener.settimeout(0.05); self.address=self.listener.getsockname()
        self.peers=[]; self.threads=[]
        self.thread=threading.Thread(target=self._accept); self.thread.start()

    def _accept(self):
        while not self.stop.is_set():
            try: client,_=self.listener.accept()
            except socket.timeout: continue
            except OSError: break
            server=socket.create_connection((self.host,self.port),timeout=1)
            self.peers.extend((client,server))
            worker=threading.Thread(target=self._forward,args=(client,server)); self.threads.append(worker); worker.start()

    def _forward(self,client,server):
        pending=[]
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(client,selectors.EVENT_READ,server)
                selector.register(server,selectors.EVENT_READ,client)
                while not self.stop.is_set():
                    if self.paused.wait(0.01):
                        self.stop.wait(0.02); continue
                    for target,data in pending: target.sendall(data)
                    pending=[]
                    for key,_ in selector.select(0.02):
                        data=key.fileobj.recv(65536)
                        if not data: return
                        if key.fileobj is client and self.pause_on and self.pause_on in data:
                            self.paused.set(); pending.append((key.data,data)); break
                        key.data.sendall(data)
        except OSError: pass
        finally:
            client.close(); server.close()

    def close(self):
        self.stop.set(); self.listener.close()
        for peer in self.peers: peer.close()
        self.thread.join(2)
        for thread in self.threads: thread.join(2)


def enter_fixture_client(test_case, app, *, cookies=None):
    """Register cleanup before entry, including unsuccessful setUp and restarts."""
    stack = ExitStack()
    test_case.addCleanup(stack.close)
    context = TestClient(app, base_url='https://testserver')
    if cookies is not None:
        context.cookies = cookies
    client = stack.enter_context(context)
    client.fixture_lifespan = stack
    return client


def close_fixture_client(client):
    """ExitStack closes once; its later unittest cleanup is harmless."""
    client.fixture_lifespan.close()
