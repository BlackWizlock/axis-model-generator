"""Fixed-cost password verification, bounded executor and durable sessions."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
from .security import ApiError,exact_origin

COOKIE_NAME='__Host-mg_session'
SESSION_SECONDS=43200
GUEST_SESSION_SECONDS=172800
USERNAME=re.compile(r'[a-z0-9][a-z0-9_.-]{2,31}\Z',re.ASCII)


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def decode(value: str,length: int) -> bytes:
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',value): raise ValueError('Invalid encoding')
    result=base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)
    if len(result)!=length or encode(result)!=value: raise ValueError('Invalid length')
    return result


def validate_password(password: str) -> None:
    if not isinstance(password,str) or not 12<=len(password)<=128:
        raise ValueError('Invalid password length')
    if len(password.encode('utf-8'))>512: raise ValueError('Invalid password bytes')


def _derive(password: str,salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode('utf-8'),salt=salt,n=32768,r=8,p=1,dklen=32,maxmem=64*1024**2)


def hash_password(password: str) -> str:
    validate_password(password); salt=secrets.token_bytes(16)
    return 'scrypt-v1$32768$8$1$'+encode(salt)+'$'+encode(_derive(password,salt))


# A fixed, non-credential record still executes the identical scrypt path.
DUMMY_RECORD='scrypt-v1$32768$8$1$'+encode(bytes(16))+'$'+encode(bytes(32))


def verify_password(password: str,record: str) -> bool:
    try:
        validate_password(password)
        parts=record.split('$')
        if len(parts)!=6 or parts[:4]!=['scrypt-v1','32768','8','1']: return False
        salt,digest=decode(parts[4],16),decode(parts[5],32)
        return hmac.compare_digest(_derive(password,salt),digest)
    except (ValueError,TypeError,UnicodeError): return False


class KdfPool:
    """At most two threads and two admitted waiters; cancellation keeps live work owned."""
    def __init__(self,active=2,queued=2,acquire_seconds=0.2):
        self.limit=active+queued; self.admitted=0; self.acquire_seconds=acquire_seconds
        self.semaphore=asyncio.Semaphore(active)
        self.executor=ThreadPoolExecutor(max_workers=active,thread_name_prefix='mg-kdf')
        self.running=set()

    async def run(self,function,*args):
        if self.admitted>=self.limit:
            raise ApiError('service_busy','Service is busy. Try again shortly.',503)
        self.admitted+=1
        acquired=False; submitted=False
        try:
            try: await asyncio.wait_for(self.semaphore.acquire(),timeout=self.acquire_seconds)
            except TimeoutError:
                raise ApiError('service_busy','Service is busy. Try again shortly.',503) from None
            acquired=True
            future=asyncio.get_running_loop().run_in_executor(self.executor,function,*args)
            submitted=True; self.running.add(future)
            def done(completed):
                self.running.discard(completed); self.admitted-=1; self.semaphore.release()
                # Consume errors even when the requesting task was cancelled.
                if not completed.cancelled(): completed.exception()
            future.add_done_callback(done)
            return await asyncio.shield(future)
        finally:
            if not submitted:
                self.admitted-=1
                if acquired: self.semaphore.release()

    async def close(self):
        if self.running: await asyncio.gather(*self.running,return_exceptions=True)
        self.executor.shutdown(wait=True,cancel_futures=True)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    username: str
    session_hash: str
    csrf_token: str


def session_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def csrf_token(key: bytes,raw: bytes) -> str:
    return encode(hmac.new(key,b'csrf-v1\0'+raw,hashlib.sha256).digest())


def rate_digest(key: bytes,domain: str,value: str) -> str:
    return hmac.new(key,domain.encode()+b'\0'+value.encode('utf-8'),hashlib.sha256).hexdigest()


def consume_limit(con,key: bytes,action: str,subject: str,now: int,window: int,limit: int,auth: bool=False):
    start=now//window*window
    digest=rate_digest(key,action,subject)
    row=con.execute('''INSERT INTO auth_attempts(action,digest,window_start,count) VALUES(%s,%s,%s,1)
        ON CONFLICT(action,digest,window_start) DO UPDATE SET count=auth_attempts.count+1
        WHERE auth_attempts.count < %s RETURNING count''',(action,digest,start,limit)).fetchone()
    if not row:
        error=ApiError('rate_limited','Too many requests. Try again later.',429)
        error.retry_after=start+window-now
        raise error
    con.execute('DELETE FROM auth_attempts WHERE window_start<%s',(now-86400,))


async def ensure_database_ready(state) -> None:
    if state.database_ready: return
    try: await state.db.run(state.database_probe)
    except (RuntimeError,ApiError):
        raise ApiError('database_not_ready','Service is temporarily unavailable.',503) from None
    state.database_ready=True


async def require_user(request) -> AuthenticatedUser:
    await ensure_database_ready(request.app.state)
    cached=getattr(request.state,'authenticated_user',None)
    if cached: return cached
    token=request.cookies.get(COOKIE_NAME,'')
    try: raw=decode(token,32)
    except ValueError: raise ApiError('authentication_required','Establish a private session to continue.',401) from None
    digest=session_digest(raw); now=int(request.app.state.clock())
    state=request.app.state
    csrf=csrf_token(state.settings.auth_key,raw)
    def authenticate():
        with state.db.transaction() as con:
            row=con.execute('SELECT s.*,u.username,u.disabled,g.owner_id AS guest FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN guest_owners g ON g.owner_id=u.id WHERE s.hash=%s',(digest,)).fetchone()
            if not row or row['disabled'] or row['expires_at']<=now or not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(),row['csrf_hash']):
                raise ApiError('authentication_required','Establish a private session to continue.',401)
            consume_limit(con,state.settings.auth_key,'api-user',row['user_id'],now,60,120)
            if row['guest']:
                # Same ordering as mint/cleanup/quotas. Recheck after admission locks.
                con.execute("SELECT scope FROM quota_scopes WHERE scope='global' FOR UPDATE")
                con.execute('SELECT scope FROM quota_scopes WHERE scope=%s FOR UPDATE',(row['user_id'],))
                current=con.execute('SELECT expires_at FROM sessions WHERE hash=%s FOR UPDATE',(digest,)).fetchone()
                if not current or current['expires_at']<=now:
                    raise ApiError('authentication_required','Establish a private session to continue.',401)
                con.execute('UPDATE sessions SET last_seen=%s,expires_at=%s WHERE hash=%s',(now,now+GUEST_SESSION_SECONDS,digest))
                request.state.guest_cookie=(token,GUEST_SESSION_SECONDS)
            else:
                con.execute('UPDATE sessions SET last_seen=%s WHERE hash=%s',(now,digest))
            return AuthenticatedUser(row['user_id'],row['username'],digest,csrf)
    user=await state.db.run(authenticate)
    request.state.authenticated_user=user
    return user


async def require_mutation(request) -> AuthenticatedUser:
    exact_origin(request)
    user=await require_user(request)
    values=request.headers.getlist('x-csrf-token')
    if len(values)!=1 or len(values[0])!=43 or not hmac.compare_digest(values[0],user.csrf_token):
        raise ApiError('csrf_forbidden','Request verification failed.',403)
    return user


def consume_guest_acceptance(con,settings,owner_id,action,now):
    row=con.execute('SELECT ip_digest FROM guest_owners WHERE owner_id=%s',(owner_id,)).fetchone()
    if row:
        consume_limit(con,settings.auth_key,'guest-'+action,row['ip_digest'],now,86400,settings.accepted_per_user_day)


def cleanup_guests(db,now):
    """Only reclaim fully retired owners under the same global/owner lock order.

    Session expiry is not proof of closed IO. Retained metadata, live writers,
    reservations or unacknowledged intents each block reclamation independently.
    Daily keyed counters survive owner deletion through the current day.
    """
    with db.transaction() as con:
        work=con.execute("""SELECT
            EXISTS(SELECT 1 FROM guest_owners g WHERE NOT EXISTS(
                SELECT 1 FROM sessions s WHERE s.user_id=g.owner_id AND s.expires_at>%s)) AS expired_guest,
            EXISTS(SELECT 1 FROM auth_attempts WHERE window_start<%s) AS old_auth""",(now,now-86400)).fetchone()
        if not work['expired_guest'] and not work['old_auth']:
            return
        con.execute("SELECT scope FROM quota_scopes WHERE scope='global' FOR UPDATE")
        owners=con.execute("SELECT g.owner_id FROM guest_owners g WHERE NOT EXISTS(SELECT 1 FROM sessions s WHERE s.user_id=g.owner_id AND s.expires_at>%s) ORDER BY g.owner_id LIMIT 100",(now,)).fetchall()
        for item in owners:
            owner=item['owner_id']
            quota=con.execute('SELECT * FROM quota_scopes WHERE scope=%s FOR UPDATE',(owner,)).fetchone()
            if not quota or any(quota[field] for field in ('storage_bytes','active_jobs','active_uploads')): continue
            if con.execute('SELECT 1 FROM sessions WHERE user_id=%s AND expires_at>%s',(owner,now)).fetchone(): continue
            if con.execute("SELECT 1 FROM uploads WHERE owner_id=%s AND (expires_at>%s OR state<>'deleted' OR reservation_bytes>0 OR active_reserved OR (content_claimed AND NOT writer_closed)) LIMIT 1",(owner,now)).fetchone(): continue
            if con.execute("SELECT 1 FROM jobs WHERE owner_id=%s AND (expires_at>%s OR state<>'deleted' OR reservation_bytes>0 OR active_reserved) LIMIT 1",(owner,now)).fetchone(): continue
            if con.execute("SELECT 1 FROM object_intents WHERE owner_id=%s AND (state<>'deleted' OR reserved_bytes>0 OR multipart_id IS NOT NULL) LIMIT 1",(owner,)).fetchone(): continue
            if con.execute("SELECT 1 FROM job_object_intents WHERE owner_id=%s AND (state<>'deleted' OR reserved_bytes>0 OR multipart_id IS NOT NULL) LIMIT 1",(owner,)).fetchone(): continue
            con.execute('DELETE FROM jobs WHERE owner_id=%s',(owner,))
            con.execute('DELETE FROM object_intents WHERE owner_id=%s',(owner,))
            con.execute('DELETE FROM uploads WHERE owner_id=%s',(owner,))
            con.execute('DELETE FROM usage_events WHERE owner_id=%s',(owner,))
            con.execute('DELETE FROM users WHERE id=%s',(owner,))
        con.execute('DELETE FROM auth_attempts WHERE window_start<%s',(now-86400,))
