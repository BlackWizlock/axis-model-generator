"""Anonymous opt-in receipts, separate from private owners and model journals."""
import hashlib
import re
import secrets
from fastapi import APIRouter,Request
from .auth import consume_limit,ensure_database_ready
from .security import ApiError,bounded_json,client_ip,exact_origin
from .static import ROOT

router=APIRouter(prefix='/api/analytics')
DOCUMENT_PATH=ROOT/'analytics-consent.html'
DOCUMENT_VERSION=1
CONSENT_SECONDS=30*86400
PROOF_SECONDS=3*365*86400
GRANT_LOCK=0x4d47434f4e53
HEX64=re.compile(r'^[a-f0-9]{64}$')


def _document():
    try:
        if DOCUMENT_PATH.is_symlink(): raise ValueError
        with DOCUMENT_PATH.open('rb') as stream: data=stream.read(65537)
        if not data or len(data)>65536: raise ValueError
        text=data.decode('utf-8')
        marker=re.fullmatch(r'.*?<!-- analytics-consent-document-v1:start -->(.*?)<!-- analytics-consent-document-v1:end -->.*',text,re.DOTALL)
        if marker is None or text.count('<!-- analytics-consent-document-v1:start -->')!=1 or text.count('<!-- analytics-consent-document-v1:end -->')!=1: raise ValueError
        document=marker.group(1)
        if not document: raise ValueError
        return hashlib.sha256(document.encode('utf-8')).hexdigest(),document
    except (OSError,ValueError,UnicodeError):
        raise ApiError('analytics_unavailable','Analytics preference is temporarily unavailable.',503) from None


def cleanup_consents(db,now):
    with db.transaction() as con:
        con.execute('DELETE FROM analytics_consents WHERE purge_at<=%s',(now,))


@router.post('/consent')
async def consent(request: Request):
    exact_origin(request)
    if request.url.query: raise ApiError('invalid_analytics_input','Analytics preference is invalid.',422)
    state=request.app.state
    await ensure_database_ready(state)
    now=int(state.clock()); ip=client_ip(request,state.settings)
    def limit():
        with state.db.transaction() as con:
            consume_limit(con,state.settings.auth_key,'analytics-ip',ip,now,60,60)
    await state.db.run(limit)
    value=await bounded_json(request,min(4096,state.settings.json_max_bytes),state.settings.json_idle_seconds,state.settings.json_wall_seconds)
    action=value.get('action')
    valid=False
    if action=='grant':
        valid=(set(value)=={'action','version','documentSha256'} and type(value['version']) is int
               and value['version']==DOCUMENT_VERSION and isinstance(value['documentSha256'],str)
               and HEX64.fullmatch(value['documentSha256']) is not None)
    elif action in ('check','withdraw'):
        valid=set(value)=={'action','receipt'} and isinstance(value['receipt'],str) and HEX64.fullmatch(value['receipt']) is not None
    if not valid: raise ApiError('invalid_analytics_input','Analytics preference is invalid.',422)
    if action=='withdraw':
        digest=hashlib.sha256(value['receipt'].encode()).hexdigest()
        def withdraw():
            with state.db.transaction() as con:
                con.execute('UPDATE analytics_consents SET revoked_at=%s WHERE receipt_hash=%s AND revoked_at IS NULL',(now,digest))
        await state.db.run(withdraw)
        return {'allowed':False}
    document_sha,text=_document()
    if action=='grant':
        if value['documentSha256']!=document_sha:
            raise ApiError('invalid_analytics_input','Analytics preference is invalid.',422)
        receipt=secrets.token_hex(32); digest=hashlib.sha256(receipt.encode()).hexdigest()
        def grant():
            with state.db.transaction() as con:
                con.execute('SELECT pg_advisory_xact_lock(%s)',(GRANT_LOCK,))
                consume_limit(con,state.settings.auth_key,'analytics-grant',ip,now,3600,10)
                consume_limit(con,state.settings.auth_key,'analytics-global','global',now,86400,1000)
                con.execute('INSERT INTO analytics_consent_versions(version,document_sha256,content_text) VALUES(%s,%s,%s) ON CONFLICT(version) DO NOTHING',(DOCUMENT_VERSION,document_sha,text))
                archived=con.execute('SELECT document_sha256,content_text FROM analytics_consent_versions WHERE version=%s',(DOCUMENT_VERSION,)).fetchone()
                if archived!={'document_sha256':document_sha,'content_text':text}:
                    raise ApiError('analytics_unavailable','Analytics preference is temporarily unavailable.',503)
                con.execute('INSERT INTO analytics_consents(receipt_hash,version,granted_at,expires_at,purge_at) VALUES(%s,%s,%s,%s,%s)',(digest,DOCUMENT_VERSION,now,now+CONSENT_SECONDS,now+PROOF_SECONDS))
        await state.db.run(grant)
        return {'allowed':True,'receipt':receipt,'expiresAt':now+CONSENT_SECONDS}
    digest=hashlib.sha256(value['receipt'].encode()).hexdigest()
    def check():
        with state.db.connect() as con:
            return con.execute('''SELECT c.expires_at FROM analytics_consents c
                JOIN analytics_consent_versions v ON v.version=c.version
                WHERE c.receipt_hash=%s AND c.version=%s AND v.document_sha256=%s
                AND c.revoked_at IS NULL AND c.granted_at<=%s AND c.expires_at>%s AND c.purge_at>%s''',
                (digest,DOCUMENT_VERSION,document_sha,now,now,now)).fetchone()
    row=await state.db.run(check)
    return {'allowed':bool(row),'expiresAt':row['expires_at'] if row else None}
