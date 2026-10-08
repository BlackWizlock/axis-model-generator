"""Own PostgreSQL only, short transactions and bounded off-thread callbacks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
import ipaddress
import selectors
import socket
from urllib.parse import urlsplit
import psycopg
from psycopg import pq
from psycopg import waiting
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from .config import Settings
from .migrate import EXPECTED_SCHEMA_VERSION, migrations
from .security import ApiError


@dataclass
class _Operation:
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)

    def remaining_ms(self):
        remaining=int((self.deadline-time.monotonic())*1000)
        if self.cancelled.is_set() or remaining<=0: raise TimeoutError
        return remaining

    def query_ms(self):
        # Reserve bounded cancellation/socket cleanup within the 1500ms budget.
        remaining=self.remaining_ms()-100
        if remaining<=0: raise TimeoutError
        return remaining


_local=threading.local()


class _BoundConnection(psycopg.Connection):
    """Bound physical client I/O, server execution and cleanup by one deadline."""
    def _abort(self):
        if self.closed: return
        operation=getattr(self,'_operation',None)
        remaining=max(0,(operation.deadline-time.monotonic())) if operation else 0.075
        try:
            # Never fall back to the blocking cancellation API on older libpq.
            if remaining>0 and pq.version()>=170000:
                self.cancel_safe(timeout=min(0.075,remaining))
        except (psycopg.Error,OSError): pass
        finally: self.close()

    def wait(self,gen,interval=0.02,timeout=None):
        operation=getattr(self,'_operation',None)
        def guarded():
            value=None; initial=True
            while True:
                if operation: operation.query_ms()
                try:
                    state=next(gen) if initial else gen.send(value)
                except StopIteration as done: return done.value
                initial=False
                value=yield state
        try:
            limit=operation.query_ms()/1000 if operation else 1.0
            if timeout is not None: limit=min(limit,timeout)
            return waiting.wait(guarded(),self.pgconn.socket,interval=0.02,timeout=limit)
        except (TimeoutError,psycopg.errors._WaitTimeout):
            self._abort()
            raise TimeoutError from None

    def _refresh_timeouts(self):
        operation=getattr(self,'_operation',None)
        remaining=operation.query_ms() if operation else 1000
        # PG16 has no total transaction timeout. Leave 200ms for server idle
        # expiry after even the last slow statement when cancel/EOF are lost.
        idle=min(200,remaining) if operation else 1000
        statement=remaining-idle if operation else remaining
        if statement<=0: raise TimeoutError
        super().execute("SELECT set_config('statement_timeout',%s,false),set_config('lock_timeout',%s,false),set_config('idle_in_transaction_session_timeout',%s,false)",
                        (str(min(self._statement_ms,statement)),str(min(self._lock_ms,statement)),str(idle)))
        if operation: operation.query_ms()

    def execute(self,query,params=None,*,prepare=None,binary=False):
        self._refresh_timeouts()
        return super().execute(query,params,prepare=prepare,binary=binary)

    def commit(self):
        # COMMIT is a command too: deferred triggers cannot inherit a stale budget.
        self._refresh_timeouts()
        return super().commit()

    def rollback(self):
        if self.closed: return
        operation=getattr(self,'_operation',None)
        if operation and (operation.cancelled.is_set() or operation.deadline-time.monotonic()<=0.1):
            self._abort(); return
        try: return super().rollback()
        except (psycopg.Error,TimeoutError,OSError):
            self._abort()


class Database:
    def __init__(self,settings: Settings):
        settings.validate()
        self.settings=settings
        self._slots=threading.BoundedSemaphore(8)
        self._async_slots=asyncio.Semaphore(8)
        self._executor=ThreadPoolExecutor(max_workers=8,thread_name_prefix='mg-db')
        self._dns_executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='mg-db-dns')
        self._dns_slots=threading.BoundedSemaphore(2)
        self._running=set()
        self._closed=False

    @contextmanager
    def worker_session(self, connection):
        """Bind worker SQL/COMMIT to the physical session holding its lease."""
        if self.settings.db_role != 'mg_worker' or getattr(self,'_worker_connection',None) is not None:
            raise RuntimeError('Unsafe worker session binding')
        self._worker_connection=connection
        self._worker_lock=threading.Lock()
        try:
            yield
        finally:
            self._worker_connection=None

    @contextmanager
    def worker_heartbeat(self):
        connection=getattr(self,'_worker_connection',None)
        if connection is None:
            raise RuntimeError('Worker session is not bound')
        with self._worker_connect(connection,heartbeat=True) as con:
            yield con

    @contextmanager
    def _worker_connect(self, connection, *, heartbeat=False):
        # Heartbeat and transaction never overlap on one libpq connection. The
        # same physical 1500ms budget bounds SQL, COMMIT and cancellation; API
        # connections keep their existing admission/deadline path below.
        operation=_Operation(time.monotonic()+1.5,cancelled=self.worker_failed)
        # A healthy bounded transaction can hold this mutex while waiting for
        # an allowed quota row lock. Defer heartbeat; busy is not lease loss.
        # Foreground admission includes serialization in its existing budget.
        acquire=.2 if heartbeat else operation.remaining_ms()/1000
        if not self._worker_lock.acquire(timeout=acquire):
            if heartbeat:
                yield None
                return
            raise TimeoutError
        try:
            if self._closed or self.worker_failed.is_set() or connection.closed:
                raise RuntimeError('worker_lease_lost')
            connection._operation=operation
            connection._statement_ms=1000; connection._lock_ms=500
            yield connection
        except (psycopg.Error,OSError,TimeoutError):
            self.worker_failed.set()
            connection._abort()
            raise
        finally:
            try:
                if not connection.closed and not connection.autocommit:
                    connection.autocommit=True
            except (psycopg.Error,OSError,TimeoutError):
                self.worker_failed.set()
                connection._abort()
                raise
            finally:
                connection._operation=None
                self._worker_lock.release()

    def _connect(self,operation):
        """Public libpq polling enforces 1s; Psycopg connect() otherwise floors it at 2s."""
        deadline=min(time.monotonic()+1,operation.deadline) if operation else time.monotonic()+1
        def remaining():
            if operation: operation.remaining_ms()
            value=deadline-time.monotonic()
            if value<=0: raise TimeoutError
            return value
        url=urlsplit(self.settings.database_url)
        try: address=str(ipaddress.ip_address(url.hostname))
        except ValueError:
            if not self._dns_slots.acquire(timeout=min(0.2,remaining())): raise TimeoutError
            try:
                future=self._dns_executor.submit(socket.getaddrinfo,url.hostname,url.port or 5432,type=socket.SOCK_STREAM)
            except BaseException:
                self._dns_slots.release(); raise
            future.add_done_callback(lambda _:self._dns_slots.release())
            answers=future.result(timeout=remaining())
            remaining()
            address=answers[0][4][0]
        remaining()
        conninfo=make_conninfo(self.settings.database_url,hostaddr=address,connect_timeout=1,gssencmode='disable')
        pgconn=pq.PGconn.connect_start(conninfo.encode('utf-8'))
        try:
            with selectors.DefaultSelector() as selector:
                status=pq.PollingStatus.WRITING
                while status!=pq.PollingStatus.OK:
                    if status==pq.PollingStatus.FAILED or pgconn.socket<0:
                        raise psycopg.OperationalError('Own PostgreSQL unavailable')
                    event=selectors.EVENT_READ if status==pq.PollingStatus.READING else selectors.EVENT_WRITE
                    selector.register(pgconn.socket,event)
                    try:
                        if not selector.select(remaining()): raise TimeoutError
                    finally: selector.unregister(pgconn.socket)
                    remaining()
                    status=pgconn.connect_poll()
            remaining()
            pgconn.nonblocking=1
            connection=_BoundConnection(pgconn,row_factory=dict_row)
            connection.autocommit=True
            return connection
        except BaseException:
            pgconn.finish()
            raise

    @contextmanager
    def connect(self):
        if getattr(self,'worker_failed',threading.Event()).is_set():
            raise RuntimeError('worker_lease_lost')
        worker_connection=getattr(self,'_worker_connection',None)
        if worker_connection is not None:
            with self._worker_connect(worker_connection) as con:
                yield con
            return
        operation=getattr(_local,'operation',None)
        acquire=min(0.2,operation.remaining_ms()/1000) if operation else 0.2
        if self._closed or not self._slots.acquire(timeout=acquire): raise TimeoutError
        con=None
        try:
            if operation and operation.remaining_ms()<1000:
                # Do not admit a fresh connect without its full declared budget.
                raise TimeoutError
            con=self._connect(operation)
            con._operation=operation
            con._statement_ms=1000; con._lock_ms=500
            if operation: operation.remaining_ms()
            yield con
        finally:
            if con is not None: con.close()
            self._slots.release()

    @contextmanager
    def transaction(self,*,lock_timeout_ms: int=500,statement_timeout_ms: int=1000):
        if not 0 < lock_timeout_ms <= 500 or not 0 < statement_timeout_ms <= 1000:
            raise ValueError('Invalid PostgreSQL transaction budget')
        with self.connect() as con:
            con._lock_ms=lock_timeout_ms; con._statement_ms=statement_timeout_ms
            # Arm idle-transaction expiry before BEGIN, including an undelivered next query.
            con._refresh_timeouts()
            con.autocommit=False
            try:
                con.execute("SELECT set_config('lock_timeout',%s,true),set_config('statement_timeout',%s,true)",
                            (str(lock_timeout_ms),str(statement_timeout_ms)))
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    async def run(self,callback,*args):
        """No callback queue beyond eight slots; cancellation fences every later SQL/commit."""
        operation=_Operation(time.monotonic()+1.5)
        try:
            await asyncio.wait_for(self._async_slots.acquire(),timeout=0.2)
        except TimeoutError:
            raise ApiError('database_unavailable','Service is temporarily unavailable.',503) from None
        if self._closed:
            self._async_slots.release()
            raise ApiError('database_unavailable','Service is temporarily unavailable.',503)
        def worker():
            _local.operation=operation
            try:
                try: return True, callback(*args)
                except Exception as error: return False, error
            finally: del _local.operation
        future=asyncio.get_running_loop().run_in_executor(self._executor,worker)
        self._running.add(future)
        def done(completed):
            self._running.discard(completed); self._async_slots.release()
            if not completed.cancelled(): completed.exception()
        future.add_done_callback(done)
        try:
            success,result=await asyncio.wait_for(asyncio.shield(future),timeout=operation.remaining_ms()/1000)
            if not success: raise result
            return result
        except asyncio.CancelledError:
            operation.cancelled.set()
            # Physical wait polls cancellation; cancellation and close have a reserved bound.
            try: await asyncio.wait_for(asyncio.shield(future),timeout=0.2)
            except Exception: pass
            raise
        except (psycopg.Error,TimeoutError,OSError):
            operation.cancelled.set()
            try: await asyncio.wait_for(asyncio.shield(future),timeout=0.2)
            except Exception: pass
            raise ApiError('database_unavailable','Service is temporarily unavailable.',503) from None

    def check_schema(self):
        try:
            with self.connect() as con:
                identity=con.execute("SELECT current_database() AS db,current_user AS role,current_setting('search_path') AS path").fetchone()
                if identity != {'db':'model_generator','role':self.settings.db_role,'path':identity['path']} or identity['path'].replace(' ','')!='mg,pg_catalog':
                    raise RuntimeError('Own PostgreSQL identity mismatch')
                role=con.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
                if not role or any(role.values()): raise RuntimeError('Unsafe runtime role')
                elevated=con.execute("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname<>current_user AND pg_has_role(current_user,oid,'MEMBER')) AS elevated").fetchone()['elevated']
                if elevated: raise RuntimeError('Unsafe runtime membership')
                owner=con.execute("SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname='mg'").fetchone()
                if not owner or owner['owner']!='mg_migrator': raise RuntimeError('Unsafe own schema owner')
                ddl=con.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE') OR has_database_privilege(current_user,current_database(),'TEMP') OR has_schema_privilege(current_user,'mg','CREATE') OR has_schema_privilege(current_user,'public','CREATE') AS ddl").fetchone()['ddl']
                if ddl: raise RuntimeError('Runtime DDL is forbidden')
                unsafe_tables=con.execute("SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='mg' AND pg_get_userbyid(c.relowner)<>'mg_migrator') AS unsafe").fetchone()['unsafe']
                if unsafe_tables: raise RuntimeError('Unsafe table owner')
                rows=con.execute('SELECT version,checksum FROM mg.schema_meta ORDER BY version').fetchall()
                expected=[{'version':v,'checksum':c} for v,c,_ in migrations()]
                if rows!=expected or len(rows)!=EXPECTED_SCHEMA_VERSION: raise RuntimeError('Unsupported own PostgreSQL schema')
        except (psycopg.Error,TimeoutError,OSError):
            raise RuntimeError('Own PostgreSQL schema unavailable') from None

    def close(self):
        self._closed=True
        self._executor.shutdown(wait=True,cancel_futures=True)
        # A timed-out resolver has no socket/SQL capability and cannot make a late connection.
        self._dns_executor.shutdown(wait=False,cancel_futures=True)
