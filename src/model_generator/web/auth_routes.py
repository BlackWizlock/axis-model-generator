"""Receive-bounded auth, durable PostgreSQL counters and rotated sessions."""
import hashlib
import secrets
from uuid import uuid4
import psycopg
from fastapi import APIRouter,Request
from starlette.responses import JSONResponse,Response
from .auth import (COOKIE_NAME,SESSION_SECONDS,GUEST_SESSION_SECONDS,USERNAME,DUMMY_RECORD,consume_limit,hash_password,
                   verify_password,validate_password,encode,decode,csrf_token,session_digest,rate_digest,require_user,require_mutation,ensure_database_ready)
from .security import ApiError,bounded_json,exact_origin,client_ip

router=APIRouter(prefix='/api/auth')


async def _ip_limit(request,action,window,limit):
    state=request.app.state
    await ensure_database_ready(state)
    def consume():
        with state.db.transaction() as con:
            consume_limit(con,state.settings.auth_key,action,client_ip(request,state.settings),int(state.clock()),window,limit)
    await state.db.run(consume)


async def _credentials(request):
    settings=request.app.state.settings
    value=await bounded_json(request,settings.json_max_bytes,settings.json_idle_seconds,settings.json_wall_seconds)
    try:
        if set(value)!={'username','password'} or not isinstance(value['username'],str) or not USERNAME.fullmatch(value['username']): raise ValueError
        validate_password(value['password'])
    except (ValueError,UnicodeError,KeyError):
        raise ApiError('invalid_credentials_input','Use a valid username and a password of 12 to 128 characters (at most 512 UTF-8 bytes).',422) from None
    return value['username'],value['password']


@router.post('/register')
async def register(request: Request):
    exact_origin(request)
    await _ip_limit(request,'register-ip',3600,3)
    username,password=await _credentials(request)
    state=request.app.state; now=int(state.clock())
    record=await state.kdf.run(hash_password,password)
    user_id=uuid4().hex
    def create():
        try:
            with state.db.transaction() as con:
                scope=con.execute("SELECT scope FROM quota_scopes WHERE scope='global' FOR UPDATE").fetchone()
                if not scope: raise RuntimeError('Global quota unavailable')
                active=con.execute('SELECT COUNT(*) AS n FROM users WHERE NOT disabled').fetchone()['n']
                daily=con.execute("SELECT COUNT(*) AS n FROM usage_events WHERE action='register' AND timestamp>=%s",(now//86400*86400,)).fetchone()['n']
                if active>=1000 or daily>=100: raise ApiError('registration_limited','Registration is temporarily limited.',429)
                con.execute('INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)',(user_id,username,record,now))
                con.execute('INSERT INTO quota_scopes(scope,owner_id) VALUES(%s,%s)',(user_id,user_id))
                con.execute("UPDATE quota_scopes SET registrations=registrations+1 WHERE scope='global'")
                con.execute("INSERT INTO usage_events(action,owner_id,timestamp,bytes) VALUES('register',%s,%s,0)",(user_id,now))
        except psycopg.errors.UniqueViolation:
            raise ApiError('username_unavailable','This username is unavailable.',409) from None
    await state.db.run(create)
    return JSONResponse({'id':user_id},status_code=201)


@router.post('/login')
async def login(request: Request):
    exact_origin(request)
    await _ip_limit(request,'login-ip',900,10)
    username,password=await _credentials(request)
    state=request.app.state; now=int(state.clock())
    def lookup():
        with state.db.transaction() as con:
            consume_limit(con,state.settings.auth_key,'login-username',username,now,900,10)
            return con.execute('SELECT u.* FROM users u WHERE username=%s AND NOT EXISTS(SELECT 1 FROM guest_owners g WHERE g.owner_id=u.id)',(username,)).fetchone()
    row=await state.db.run(lookup)
    valid=await state.kdf.run(verify_password,password,row['password_record'] if row else DUMMY_RECORD)
    if not valid or not row or row['disabled']: raise ApiError('invalid_credentials','Username or password is incorrect.',401)
    raw=secrets.token_bytes(32); digest=session_digest(raw); csrf=csrf_token(state.settings.auth_key,raw)
    try: old_digest=session_digest(decode(request.cookies.get(COOKIE_NAME,''),32))
    except ValueError: old_digest=None
    def persist():
        with state.db.transaction() as con:
            current=con.execute('SELECT disabled,password_record FROM users WHERE id=%s FOR UPDATE',(row['id'],)).fetchone()
            if not current or current['disabled'] or current['password_record']!=row['password_record']:
                raise ApiError('invalid_credentials','Username or password is incorrect.',401)
            if old_digest: con.execute('DELETE FROM sessions WHERE hash=%s',(old_digest,))
            con.execute('DELETE FROM sessions WHERE expires_at<=%s',(now,))
            con.execute('INSERT INTO sessions VALUES(%s,%s,%s,%s,%s,%s)',(digest,row['id'],hashlib.sha256(csrf.encode()).hexdigest(),now,now+SESSION_SECONDS,now))
    await state.db.run(persist)
    response=JSONResponse({'id':row['id'],'username':row['username']})
    response.set_cookie(COOKIE_NAME,encode(raw),max_age=SESSION_SECONDS,secure=True,httponly=True,samesite='lax',path='/')
    return response


@router.get('/me')
async def me(request: Request):
    user=await require_user(request); settings=request.app.state.settings
    return {'id':user.id,'username':user.username,'csrfToken':user.csrf_token,
            'limits':{'uploadBytes':settings.upload_max_bytes,'storageBytes':settings.storage_per_user_bytes,'jobsActive':settings.jobs_per_user}}


@router.post('/logout')
async def logout(request: Request):
    user=await require_mutation(request)
    request.state.guest_cookie=None
    def delete():
        with request.app.state.db.transaction() as con: con.execute('DELETE FROM sessions WHERE hash=%s',(user.session_hash,))
    await request.app.state.db.run(delete)
    response=Response(status_code=204)
    response.delete_cookie(COOKIE_NAME,path='/',secure=True,httponly=True,samesite='lax')
    return response


@router.post('/guest')
async def guest(request: Request):
    exact_origin(request)
    state=request.app.state; await ensure_database_ready(state)
    # A valid session keeps its owner, token, CSRF and every reservation.
    try:
        user=await require_user(request)
        return JSONResponse({'id':user.id,'username':user.username,'csrfToken':user.csrf_token})
    except ApiError as error:
        if error.status!=401: raise
    await _ip_limit(request,'guest-ip',3600,3)
    now=int(state.clock()); owner=uuid4().hex; raw=secrets.token_bytes(32)
    digest=session_digest(raw); csrf=csrf_token(state.settings.auth_key,raw)
    def mint():
        with state.db.transaction() as con:
            con.execute("SELECT scope FROM quota_scopes WHERE scope='global' FOR UPDATE")
            consume_limit(con,state.settings.auth_key,'guest-day',client_ip(request,state.settings),now,86400,10)
            consume_limit(con,state.settings.auth_key,'guest-global','global',now,86400,100)
            if con.execute('SELECT count(*) AS n FROM users').fetchone()['n']>=1000:
                raise ApiError('guest_limited','Private session creation is temporarily limited.',429)
            # Guest credentials are not a login path; no password is supplied.
            con.execute('INSERT INTO users VALUES(%s,%s,%s,%s,FALSE)',(owner,'guest_'+owner[:26],DUMMY_RECORD,now))
            con.execute('INSERT INTO guest_owners VALUES(%s,%s)',(owner,rate_digest(state.settings.auth_key,'guest-source-ip',client_ip(request,state.settings))))
            con.execute('INSERT INTO quota_scopes(scope,owner_id) VALUES(%s,%s)',(owner,owner))
            con.execute('INSERT INTO sessions VALUES(%s,%s,%s,%s,%s,%s)',(digest,owner,hashlib.sha256(csrf.encode()).hexdigest(),now,now+GUEST_SESSION_SECONDS,now))
    await state.db.run(mint)
    response=JSONResponse({'id':owner,'username':'guest_'+owner[:26],'csrfToken':csrf})
    response.set_cookie(COOKIE_NAME,encode(raw),max_age=GUEST_SESSION_SECONDS,secure=True,httponly=True,samesite='lax',path='/')
    return response
