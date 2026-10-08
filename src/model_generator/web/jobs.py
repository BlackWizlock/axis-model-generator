"""Short owner-scoped PostgreSQL transactions for durable single-use jobs."""
import base64
from datetime import date
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4
from psycopg.types.json import Jsonb
from model_generator import __version__
from .security import ApiError
from .store import ID
from .preview_runner import installed_preview_fingerprint

HASH = re.compile(r'[a-f0-9]{64}\Z')
TERMINAL = {'completed', 'failed', 'cancelled', 'deleted'}
AXES = {'technical': 'partial', 'profile': 'research', 'procedure': 'unknown', 'external': 'not_checked'}


def fingerprint(input_hash, region, procedure, submission_date):
    root = Path(__file__).resolve().parents[3]
    locks = {}
    for name in ('requirements-web.lock', 'requirements-web-test.lock'):
        locks[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    # Tool source hashes protect recovery across same-version implementation edits.
    source = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parents[1].rglob('*.py')):
        source.update(str(path.relative_to(Path(__file__).resolve().parents[1])).encode())
        source.update(hashlib.sha256(path.read_bytes()).digest())
    value = {'tool': __version__, 'source': source.hexdigest(), 'locks': locks,
             'profile': {'region': region, 'status': 'research', 'version': 1},
             'inputHash': input_hash, 'params': {'region': region, 'procedure': procedure, 'submissionDate': submission_date},
             'previewRuntime':installed_preview_fingerprint()}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class _PreparedJob:
    owner_id: str
    upload_id: str
    source_sha256: str
    region: str
    procedure: str
    submission_date: str
    day: date


def job_dto(row,artifacts,now):
    """An explicit truthful public envelope, independent of SQL/S3 values."""
    fields = {'id': 'id', 'displayName': 'display_name', 'inputKind': 'input_kind', 'region': 'region',
              'procedure': 'procedure', 'state': 'state', 'stage': 'stage', 'createdAt': 'created_at',
              'expiresAt': 'expires_at', 'failureCode': 'failure_code', 'cancelRequested': 'cancel_requested'}
    result = {public: row[private] for public, private in fields.items()}
    from .journal import correlation
    from .diagnostic_catalog import error_help
    result['inputHash']=row['input_sha256']
    result['attempt']=row['worker_epoch']
    result['checksUrl']=f"/api/jobs/{row['id']}/checks"
    result['diagnosticId']=correlation('job',row['id'],row['worker_epoch'] or '')
    result['failure']=error_help(row['failure_code']) if row['failure_code'] else None
    result['submissionDate'] = row['submission_date'].isoformat()
    result['coverage'] = {key: row['coverage'].get(key, default) for key, default in AXES.items()}
    if result['coverage']['technical'] not in {'partial','failed','not_checked'}: result['coverage']['technical']='partial'
    result['coverage']['profile']='research'
    if result['coverage']['procedure'] not in {'partial','unknown'}: result['coverage']['procedure']='unknown'
    result['coverage']['external']='not_checked'
    result['capabilities'] = {kind: {'availability': 'unavailable', 'reason': 'generation_not_implemented'} for kind in ('npm', 'vpm', 'ifc')}
    result['capabilities']['preview'] = {'availability': 'unavailable', 'reason': 'zip_fbx_preview_not_verified' if row['input_kind'] == 'zip-fbx' else row.get('preview_failure_code') or 'preview_pending'}
    result['artifacts'] = []
    if row['state'] in {'completed', 'failed'} and not row['cancel_requested'] and row['expires_at']>now:
        for item in artifacts:
            result['artifacts'].append({**{key:item[key] for key in ('id','kind','bytes','sha256')}, 'url': f"/api/jobs/{row['id']}/artifacts/{item['id']}"})
    if row['input_kind']=='portable-package' and {'preview','thumbnail'} <= {item['kind'] for item in result['artifacts']}:
        result['capabilities']['preview']={'availability':'available','reason':None}
    return result


class JobRepository:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def _locks(self, con, owner):
        global_row = con.execute("SELECT * FROM quota_scopes WHERE scope='global' FOR UPDATE").fetchone()
        owner_row = con.execute('SELECT * FROM quota_scopes WHERE scope=%s FOR UPDATE', (owner,)).fetchone()
        if not global_row or not owner_row:
            raise RuntimeError('Quota unavailable')
        return global_row, owner_row

    def _row(self, con, owner, job_id, now, lock=False):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise ApiError('not_found', 'Resource is unavailable.', 404)
        row = con.execute('SELECT j.*,u.display_name FROM jobs j JOIN uploads u ON u.id=j.upload_id WHERE j.id=%s AND j.owner_id=%s' + (' FOR UPDATE OF j' if lock else ''), (job_id, owner)).fetchone()
        if not row or row['state'] in {'deleting', 'deleted'}:
            raise ApiError('not_found', 'Resource is unavailable.', 404)
        if row['expires_at'] <= now:
            raise ApiError('job_expired', 'Job has expired.', 410)
        return row

    def _dto(self, con, row, now):
        artifacts=con.execute("SELECT id,kind,bytes,sha256 FROM artifacts WHERE job_id=%s AND state='ready' ORDER BY kind",(row['id'],)).fetchall()
        return job_dto(row,artifacts,now)

    def prepare_create(self, owner_id, upload_id, region, procedure, submission_date):
        try:
            if region not in {'moscow', 'moscow-oblast'} or procedure != 'diagnostic' or not isinstance(submission_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', submission_date):
                raise ValueError
            day = date.fromisoformat(submission_date)
        except (ValueError, TypeError):
            raise ApiError('invalid_job', 'Job parameters are invalid.', 422) from None
        if not isinstance(upload_id, str) or not ID.fullmatch(upload_id):
            raise ApiError('not_found', 'Resource is unavailable.', 404)
        # Hash source/lock data outside database locks; no filesystem/S3 under transaction.
        with self.db.connect() as con:
            source = con.execute('SELECT u.sha256 FROM uploads u JOIN users owner ON owner.id=u.owner_id WHERE u.id=%s AND u.owner_id=%s AND NOT owner.disabled', (upload_id, owner_id)).fetchone()
        if not source:
            raise ApiError('not_found', 'Resource is unavailable.', 404)
        return _PreparedJob(owner_id,upload_id,source['sha256'],region,procedure,submission_date,day)

    def fingerprint_prepared(self,prepared):
        if not isinstance(prepared,_PreparedJob): raise ValueError('Invalid prepared job')
        return fingerprint(prepared.source_sha256,prepared.region,prepared.procedure,prepared.submission_date)

    def create(self, owner_id, upload_id, region, procedure, submission_date, now):
        # Internal synchronous compatibility; HTTP uses separate bounded phases.
        prepared=self.prepare_create(owner_id,upload_id,region,procedure,submission_date)
        return self.create_prepared(prepared,self.fingerprint_prepared(prepared),now)

    def create_prepared(self,prepared,digest,now):
        if not isinstance(prepared,_PreparedJob) or not isinstance(digest,str) or not HASH.fullmatch(digest):
            raise ValueError('Invalid prepared job')
        owner_id,upload_id=prepared.owner_id,prepared.upload_id
        region,procedure,day=prepared.region,prepared.procedure,prepared.day
        job_id = uuid4().hex
        reserve = self.settings.output_reserve_bytes
        with self.db.transaction() as con:
            global_row, owner = self._locks(con, owner_id)
            upload = con.execute('SELECT u.*,owner.disabled AS owner_disabled FROM uploads u JOIN users owner ON owner.id=u.owner_id WHERE u.id=%s AND u.owner_id=%s FOR UPDATE OF u', (upload_id, owner_id)).fetchone()
            if not upload or upload['owner_disabled'] or upload['state'] in {'deleting', 'deleted'}:
                raise ApiError('not_found', 'Resource is unavailable.', 404)
            if upload['expires_at'] <= now:
                raise ApiError('upload_expired', 'Upload has expired.', 410)
            if upload['state'] != 'ready' or upload['abort_requested'] or not upload['writer_closed'] or upload['sha256'] != prepared.source_sha256 or upload['object_sha256'] != prepared.source_sha256:
                raise ApiError('upload_conflict', 'Upload is not ready for a job.', 409)
            daily = con.execute("SELECT count(*) AS total,count(*) FILTER(WHERE owner_id=%s) AS own FROM usage_events WHERE action='job' AND timestamp>=%s", (owner_id, now // 86400 * 86400)).fetchone()
            if global_row['active_jobs'] >= self.settings.jobs_global or owner['active_jobs'] >= self.settings.jobs_per_user or daily['total'] >= self.settings.accepted_global_day or daily['own'] >= self.settings.accepted_per_user_day:
                raise ApiError('job_limited', 'Job limit reached.', 429)
            if global_row['storage_bytes'] + reserve > self.settings.storage_global_bytes or owner['storage_bytes'] + reserve > self.settings.storage_per_user_bytes:
                raise ApiError('storage_full', 'Private storage capacity is unavailable.', 507)
            from .auth import consume_guest_acceptance
            consume_guest_acceptance(con,self.settings,owner_id,'job',now)
            con.execute("INSERT INTO jobs(id,owner_id,upload_id,input_kind,region,procedure,submission_date,profile,fingerprint,input_sha256,state,stage,created_at,updated_at,deadline_at,expires_at,reservation_bytes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued','input_check',%s,%s,%s,%s,%s)",
                        (job_id, owner_id, upload_id, upload['input_kind'], region, procedure, day, Jsonb({'region': region, 'status': 'research', 'version': 1}), digest, prepared.source_sha256, now, now, now + self.settings.job_wall_seconds, now + self.settings.retention_seconds, reserve))
            con.execute("UPDATE uploads SET state='consumed',expires_at=%s WHERE id=%s", (now + self.settings.retention_seconds, upload_id))
            con.execute("UPDATE quota_scopes SET storage_bytes=storage_bytes+%s,active_jobs=active_jobs+1 WHERE scope IN ('global',%s)", (reserve, owner_id))
            con.execute("INSERT INTO usage_events(action,owner_id,timestamp,bytes) VALUES('job',%s,%s,%s)", (owner_id, now, reserve))
            return self._dto(con, self._row(con, owner_id, job_id, now),now)

    def get_owned(self, owner_id, job_id, now):
        with self.db.connect() as con:
            return self._dto(con, self._row(con, owner_id, job_id, now),now)

    def list_owned(self, owner_id, limit, cursor, now):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ApiError('invalid_cursor', 'List parameters are invalid.', 422)
        stamp, job_id = 0, '0' * 32
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 128:
                    raise ValueError
                stamp_text, job_id = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode('ascii').split(':')
                stamp = int(stamp_text)
                if not 0 <= stamp <= 9223372036854775807 or stamp_text!=str(stamp) or not ID.fullmatch(job_id) or base64.urlsafe_b64encode(f'{stamp}:{job_id}'.encode()).decode().rstrip('=')!=cursor:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise ApiError('invalid_cursor', 'List parameters are invalid.', 422) from None
        with self.db.connect() as con:
            rows = con.execute("SELECT j.*,u.display_name FROM jobs j JOIN uploads u ON u.id=j.upload_id WHERE j.owner_id=%s AND j.expires_at>%s AND j.state NOT IN ('deleting','deleted') AND (j.created_at,j.id)>(%s,%s) ORDER BY j.created_at,j.id LIMIT %s", (owner_id, now, stamp, job_id, limit + 1)).fetchall()
            next_cursor = None
            if len(rows) > limit:
                row = rows[limit - 1]
                next_cursor = base64.urlsafe_b64encode(f"{row['created_at']}:{row['id']}".encode()).decode().rstrip('=')
            return {'items': [self._dto(con, row,now) for row in rows[:limit]], 'nextCursor': next_cursor}

    def claim_next(self, epoch, now):
        if not ID.fullmatch(epoch):
            raise ValueError('Invalid epoch')
        with self.db.transaction() as con:
            # Idle polling needs no tuple lock; every nonempty claim retains
            # the global/owner locks and rechecks its candidate below.
            if not con.execute("SELECT EXISTS (SELECT 1 FROM jobs WHERE state='queued') AS queued").fetchone()['queued']:
                return None
            con.execute("SELECT scope FROM quota_scopes WHERE scope='global' FOR UPDATE")
            if con.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            candidate = con.execute("SELECT id,owner_id FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not candidate:
                return None
            con.execute('SELECT scope FROM quota_scopes WHERE scope=%s FOR UPDATE', (candidate['owner_id'],))
            candidate=con.execute("SELECT id FROM jobs WHERE id=%s AND state='queued' FOR UPDATE SKIP LOCKED",(candidate['id'],)).fetchone()
            if not candidate: return None
            return con.execute("UPDATE jobs SET state='running',attempts=attempts+1,worker_epoch=%s,updated_at=%s WHERE id=%s AND state='queued' AND attempts<2 RETURNING *", (epoch, now, candidate['id'])).fetchone()

    def checkpoint(self, job_id, epoch, stage, fingerprint, artifacts, now, preview_reason=None):
        if stage not in {'report','preview'} or not HASH.fullmatch(fingerprint) or len(artifacts) not in {1,2} or (stage=='preview') != (len(artifacts)==2):
            raise ValueError('Invalid checkpoint')
        with self.db.transaction() as con:
            row = con.execute('SELECT owner_id FROM jobs WHERE id=%s', (job_id,)).fetchone()
            if not row:
                raise ValueError('Unknown job')
            self._locks(con, row['owner_id'])
            job = con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if job['state'] != 'running' or job['worker_epoch'] != epoch or job['fingerprint'] != fingerprint or job['cancel_requested'] or job['expires_at'] <= now or job['deadline_at'] <= now:
                raise ApiError('job_fenced', 'Job publication was cancelled.', 409)
            item = artifacts[0]
            if set(item) != {'id', 'kind', 'bytes', 'sha256', 'key', 'coverage'} or item['kind'] != 'report' or not ID.fullmatch(item['id']) or not HASH.fullmatch(item['sha256']) or not 1 <= item['bytes'] <= self.settings.report_max_bytes or item['key'] != f"owners/{job['owner_id']}/jobs/{job_id}/{epoch}/report.json":
                raise ValueError('Invalid artifact checkpoint')
            existing = con.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s AND attempt_epoch=%s AND state='staging' FOR UPDATE", (item['id'], job_id, epoch)).fetchone()
            if not existing or existing['object_key'] != item['key'] or existing['sha256'] != item['sha256'] or existing['bytes'] != item['bytes']:
                raise ValueError('Artifact intent mismatch')
            coverage = {key: item['coverage'].get(key, default) for key, default in AXES.items()}
            if coverage['profile'] != 'research' or coverage['external'] != 'not_checked' or coverage['technical'] != 'partial' or coverage['procedure'] not in {'partial', 'unknown'}:
                raise ValueError('Invalid coverage axes')
            con.execute("UPDATE artifacts SET state='ready' WHERE id=%s", (item['id'],))
            con.execute("UPDATE job_object_intents SET state='complete',multipart_id=NULL WHERE object_id=%s", (item['id'],))
            checkpoint={'schema':1,'fingerprint':fingerprint,'inputHash':job['input_sha256'],'artifact':{key:item[key] for key in ('id','kind','bytes','sha256','key')}}
            if len(artifacts)==2:
                preview=artifacts[1]
                self._checked_intent(con,job,preview,'preview',epoch,self.settings.preview_max_bytes)
                con.execute("UPDATE job_object_intents SET state='complete',multipart_id=NULL WHERE object_id=%s",(preview['id'],))
                checkpoint['previewInput']=preview
            self._preview_reason(preview_reason)
            con.execute('UPDATE jobs SET checkpoint=%s,stage=%s,coverage=%s,updated_at=%s,preview_failure_code=%s WHERE id=%s', (Jsonb(checkpoint),stage,Jsonb(coverage),now,preview_reason,job_id))

    @staticmethod
    def _preview_reason(reason):
        if reason not in {None,'preview_unsupported','preview_budget','preview_roundtrip_error','preview_resource','preview_runtime_unavailable'}:
            raise ValueError('Invalid preview reason')

    def _checked_intent(self,con,job,item,kind,epoch,cap):
        suffix={'preview':'preview-input.json','thumbnail':'thumbnail.png'}[kind]
        if (set(item)!={'id','kind','bytes','sha256','key'} or item['kind']!=kind or not ID.fullmatch(item['id']) or not HASH.fullmatch(item['sha256'])
                or type(item['bytes']) is not int or not 1<=item['bytes']<=cap or item['key']!=f"owners/{job['owner_id']}/jobs/{job['id']}/{epoch}/{suffix}"):
            raise ValueError('Invalid preview artifact checkpoint')
        row=con.execute('SELECT * FROM artifacts WHERE id=%s AND job_id=%s FOR UPDATE',(item['id'],job['id'])).fetchone()
        if not row or row['state']!='staging' or row['attempt_epoch']!=epoch or row['object_key']!=item['key'] or row['bytes']!=item['bytes'] or row['sha256']!=item['sha256']:
            raise ValueError('Preview artifact intent mismatch')

    def preview_finished(self,job_id,epoch,thumbnail,reason,now):
        self._preview_reason(reason)
        with self.db.transaction() as con:
            row=con.execute('SELECT owner_id FROM jobs WHERE id=%s',(job_id,)).fetchone()
            if not row: raise ValueError('Unknown job')
            self._locks(con,row['owner_id'])
            job=con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE',(job_id,)).fetchone()
            if job['state']!='running' or job['worker_epoch']!=epoch or job['cancel_requested'] or min(job['expires_at'],job['deadline_at'])<=now:
                raise ApiError('job_fenced','Job publication was cancelled.',409)
            checkpoint=job['checkpoint']
            if not checkpoint or not checkpoint.get('previewInput'): raise ValueError('Missing checked preview checkpoint')
            if thumbnail is not None:
                if reason is not None: raise ValueError('Conflicting preview result')
                self._checked_intent(con,job,thumbnail,'thumbnail',epoch,4*1024**2)
                preview=checkpoint['previewInput']
                stored=con.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s AND kind='preview' AND state='staging' FOR UPDATE",(preview['id'],job_id)).fetchone()
                if not stored or (stored['object_key'],stored['bytes'],stored['sha256'])!=(preview['key'],preview['bytes'],preview['sha256']):
                    raise ValueError('Preview checkpoint changed')
                checkpoint={**checkpoint,'thumbnail':thumbnail}
                con.execute("UPDATE artifacts SET state='ready' WHERE id IN (%s,%s)",(preview['id'],thumbnail['id']))
                con.execute("UPDATE job_object_intents SET state='complete',multipart_id=NULL WHERE object_id=%s",(thumbnail['id'],))
            elif reason is None: raise ValueError('Missing preview result')
            con.execute("UPDATE jobs SET checkpoint=%s,preview_failure_code=%s,stage='report',updated_at=%s WHERE id=%s",(Jsonb(checkpoint),reason,now,job_id))

    def finish(self, job_id, epoch, state, failure_code, now):
        if state not in {'completed', 'failed', 'cancelled'} or failure_code not in {None, 'validation_resource', 'validation_failed', 'worker_interrupted', 'input_changed', 'job_expired', 'storage_unavailable', 'progress_invalid'}:
            raise ValueError('Invalid terminal state')
        with self.db.transaction() as con:
            row = con.execute('SELECT owner_id FROM jobs WHERE id=%s', (job_id,)).fetchone()
            if not row:
                return
            self._locks(con, row['owner_id'])
            job = con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if job['worker_epoch'] != epoch or job['state'] != 'running':
                raise ApiError('job_fenced', 'Job publication was cancelled.', 409)
            if job['cancel_requested'] or job['expires_at'] <= now:
                state, failure_code = 'cancelled', None
            if state == 'completed' and not job['checkpoint']:
                raise ValueError('Missing checkpoint')
            con.execute("UPDATE jobs SET state=%s,stage='done',failure_code=%s,updated_at=%s,active_reserved=FALSE WHERE id=%s", (state, failure_code, now, job_id))
            if job['active_reserved']:
                con.execute("UPDATE quota_scopes SET active_jobs=active_jobs-1 WHERE scope IN ('global',%s)", (job['owner_id'],))

    def request_cancel(self,owner,job_id,now):
        return self.cancel(owner,job_id,now)

    def request_delete(self,owner,job_id,now):
        return self.cancel(owner,job_id,now,True)

    def cancel(self, owner, job_id, now, delete=False):
        if not isinstance(job_id,str) or not ID.fullmatch(job_id):
            raise ApiError('not_found','Resource is unavailable.',404)
        with self.db.transaction() as con:
            self._locks(con,owner)
            # DELETE retries must observe durable deleting/deleted even after
            # access has closed. GET and cancellation still use _row's gate.
            if delete:
                row=con.execute('SELECT j.*,u.display_name FROM jobs j JOIN uploads u ON u.id=j.upload_id WHERE j.id=%s AND j.owner_id=%s FOR UPDATE OF j',(job_id,owner)).fetchone()
                if not row: raise ApiError('not_found','Resource is unavailable.',404)
                if row['state']=='deleted': return 204,None
                if row['state']=='deleting': return 202,{'id':job_id,'state':'deleting'}
                if row['expires_at']<=now: raise ApiError('job_expired','Job has expired.',410)
            else:
                row=self._row(con,owner,job_id,now,True)
                if row['state'] in TERMINAL: return 200,self._dto(con,row,now)
            con.execute('UPDATE jobs SET cancel_requested=TRUE,state=%s,updated_at=%s WHERE id=%s',('deleting' if delete else row['state'],now,job_id))
            if delete: return 202,{'id':job_id,'state':'deleting'}
            row.update(cancel_requested=True)
            return 202,self._dto(con,row,now)
