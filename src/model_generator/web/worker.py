"""One managed worker epoch with durable checkpoints and physically owned children."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from uuid import uuid4
import psycopg
from psycopg.types.json import Jsonb
from .config import Settings, read_secret_env
from .db import Database
from .jobs import JobRepository, fingerprint
from .process_guard import identity
from .reporting import sanitize_report
from .s3_store import ObjectStore, ObjectIntent, ObjectDescriptor
from .security import ApiError
from .store import atomic_write
from .preview import PreviewError,PreviewLimits,decode_preview_json
from .blender_runner import _root,_input,THUMBNAIL_MAX_BYTES
from .preview_runner import run_preview,preflight_preview,installed_preview_fingerprint
from ..png_inspection import inspect_png

WORKER_LOCK = 0x4d47574f524b


def terminate_group(process):
    """Signal only this newly created process group, physically wait and reap."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)
    # Startup makes the worker a subreaper; adopted descendants cannot outlive
    # acknowledged cancel. Never wait/kill an identity persisted in PostgreSQL.
    end = time.monotonic() + 10
    while time.monotonic() < end:
        try:
            pid, _ = os.waitpid(-process.pid, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            time.sleep(.02)


def run_child(settings, scratch, kind, cancelled, *, probe=None, progress=None, source_hash=None, attempt=None):
    preview_limits=PreviewLimits(settings.preview_max_instances,settings.preview_max_vertices,
                                 settings.preview_max_triangles,settings.preview_max_bytes)
    ticks, boot = identity(os.getpid())
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'LANG': 'C.UTF-8',
           'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'}
    command = [sys.executable, '-m', 'model_generator.web.validation_child', '--kind', kind, '--scratch', str(scratch)]
    command += ['--preview-max-instances',str(preview_limits.instances),
                '--preview-max-vertices',str(preview_limits.vertices),
                '--preview-max-triangles',str(preview_limits.triangles),
                '--preview-max-bytes',str(preview_limits.wire_bytes)]
    if progress is not None:
        command += ['--input-hash',source_hash,'--attempt',attempt]
    if probe is not None:
        if os.environ.get('MG_TEST_MODE') != '1':
            raise ValueError('Synthetic child probe requires explicit test runtime')
        env['MG_TEST_MODE'] = '1'
        command += ['--probe', probe]
    argv = [sys.executable, '-m', 'model_generator.web.process_guard', '--parent-pid', str(os.getpid()),
            '--parent-start-ticks', str(ticks), '--parent-boot-id', boot, '--scratch', str(scratch),
            '--cpu-seconds', str(settings.validation_cpu_seconds), '--memory-bytes', str(settings.validation_memory_bytes), '--', *command]
    progress_context=None
    progress_reader=None
    if progress is not None:
        from .progress import ProgressReader
        from .blender_runner import _directory,_relative
        from contextlib import ExitStack
        progress_context=ExitStack()
        try:
            root,root_fd=progress_context.enter_context(_root(settings))
            directory=progress_context.enter_context(_directory(root_fd,_relative(root,scratch).parts))
            progress_reader=ProgressReader(directory,source_hash,attempt)
        except BaseException:
            progress_context.close()
            raise
    try:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    except BaseException:
        if progress_context is not None: progress_context.close()
        raise
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    code = None
    deadline = time.monotonic() + settings.validation_wall_seconds
    try:
        with selectors.DefaultSelector() as poll:
            for pipe in buffers:
                os.set_blocking(pipe.fileno(), False)
                poll.register(pipe, selectors.EVENT_READ)
            last_check = 0
            last_progress = 0
            while poll.get_map() or process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    code = 'validation_resource'
                    break
                if now - last_check >= .25:
                    last_check = now
                    if cancelled():
                        code = 'cancelled'
                        break
                if progress_reader is not None and now-last_progress>=.5:
                    last_progress=now
                    try: update=progress_reader.read()
                    except (OSError,ValueError):
                        code='progress_invalid'; break
                    if update is not None: progress(update)
                for key, _ in poll.select(.02):
                    data = os.read(key.fd, 65536)
                    if not data:
                        poll.unregister(key.fileobj)
                        continue
                    if len(buffers[key.fileobj]) + len(data) > 65536:
                        code = 'validation_resource'
                        break
                    buffers[key.fileobj].extend(data)
                if code:
                    break
        if code:
            terminate_group(process)
            return None, code
        process.wait(timeout=1)
        # Also terminate descendants when a leader exits unexpectedly.
        terminate_group(process)
        if process.returncode:
            return None, 'validation_resource' if process.returncode < 0 or process.returncode in {70, 78} else 'validation_failed'
        try:
            message = json.loads(buffers[process.stdout].decode('utf-8', errors='strict'))
            if set(message) != {'schema', 'tool', 'inputHash', 'report', 'previewInput', 'technicalFailure', 'failureCode'} or message['schema'] != 1 or message['technicalFailure'] is not False or message['failureCode'] not in {None,'preview_unsupported','preview_budget','preview_roundtrip_error'}:
                raise ValueError
            preview=message['previewInput']
            if preview is not None:
                if kind!='portable-package' or message['failureCode'] is not None or set(preview)!={'kind','bytes','sha256'} or preview['kind']!='preview' or type(preview['bytes']) is not int or not 1<=preview['bytes']<=settings.preview_max_bytes:
                    raise ValueError
                with _root(settings) as (root,descriptor):
                    _,digest=_input(root,descriptor,scratch/'preview-input.json',preview_limits)
                if digest!=preview['sha256'] or (scratch/'preview-input.json').stat().st_size!=preview['bytes']:
                    raise ValueError
            item = message['report']
            if set(item) != {'kind', 'bytes', 'sha256'} or item['kind'] != 'report' or isinstance(item['bytes'], bool) or not 1 <= item['bytes'] <= settings.report_max_bytes:
                raise ValueError
            path = scratch / 'report.json'
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size != item['bytes']:
                raise ValueError
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError
            report = json.loads(data.decode('utf-8', errors='strict'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if progress is not None:
                from .progress import validate_snapshot
                if report.get('check_evidence') is not None: validate_snapshot(report['check_evidence'],source_hash,attempt)
                elif not report.get('report_truncated'): raise ValueError('progress_invalid')
            sanitized = sanitize_report(report, max_bytes=settings.report_max_bytes, max_findings=settings.report_max_findings)
            if sanitized != report:
                # Sanitizer counts must be validated separately after an already
                # sanitized child report; idempotent check below preserves counters.
                normalized = dict(sanitized)
                normalized['removed_source_values'] = report.get('removed_source_values', 0)
                normalized['original_findings_count'] = report.get('original_findings_count', len(report.get('findings', [])))
                normalized['report_truncated'] = report.get('report_truncated', False)
                if normalized != report:
                    raise ValueError
            return {'message': message, 'report': report, 'data': data}, None
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            return None, 'validation_resource'
    finally:
        if process.poll() is None:
            terminate_group(process)
        for pipe in buffers:
            pipe.close()
        if progress_context is not None: progress_context.close()


def _job(db, job_id):
    with db.connect() as con:
        return con.execute('SELECT * FROM jobs WHERE id=%s', (job_id,)).fetchone()


def _cancelled(db, job, settings):
    if getattr(db, 'worker_failed', threading.Event()).is_set():
        raise RuntimeError('worker_lease_lost')
    row = _job(db, job['id'])
    return not row or row['worker_epoch'] != job['worker_epoch'] or row['state'] != 'running' or row['cancel_requested'] or min(row['expires_at'], row['deadline_at']) <= int(time.time())


def _verified_download(objects, descriptor, path, max_bytes):
    if descriptor.bytes > max_bytes:
        raise ValueError('Input size changed')
    with objects.open_stream(descriptor) as stream:
        def chunks():
            while chunk := stream.read(65536):
                yield chunk
        size, sha = atomic_write(path, chunks(), max_bytes)
    if size != descriptor.bytes or sha != descriptor.sha256:
        path.unlink(missing_ok=True)
        raise ValueError('Input hash changed')


def _artifact_intent(db, job, size, sha, now, kind='report'):
    repo = JobRepository(db, db.settings)
    suffix={'report':'report.json','preview':'preview-input.json','thumbnail':'thumbnail.png'}[kind]
    cap={'report':db.settings.report_max_bytes,'preview':db.settings.preview_max_bytes,'thumbnail':THUMBNAIL_MAX_BYTES}[kind]
    if type(size) is not int or not 1<=size<=cap: raise ValueError('Artifact budget exceeded')
    key = f"owners/{job['owner_id']}/jobs/{job['id']}/{job['worker_epoch']}/{suffix}"
    artifact = uuid4().hex
    with db.transaction() as con:
        repo._locks(con, job['owner_id'])
        row = con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
        if row['state'] != 'running' or row['worker_epoch'] != job['worker_epoch'] or row['cancel_requested'] or min(row['expires_at'],row['deadline_at'])<=now:
            raise ApiError('job_fenced', 'Job publication was cancelled.', 409)
        # Old completed/staging attempts are cleaned and deleted by recovery first.
        con.execute("INSERT INTO artifacts VALUES(%s,%s,%s,%s,%s,'staging',%s,%s,%s)", (artifact,job['id'],kind,size,sha,key,job['worker_epoch'],now))
        con.execute("INSERT INTO job_object_intents VALUES(%s,%s,%s,%s,'writing',NULL,%s)", (artifact, job['owner_id'], job['worker_epoch'], key, size))
    return ObjectIntent(job['owner_id'], artifact, job['worker_epoch'], key, 'writing', None, size,
                        deadline=time.monotonic() + max(.1, min(job['deadline_at'], job['expires_at']) - time.time()))


def _persist_multipart(db, job, intent, multipart):
    with db.transaction() as con:
        row = con.execute('SELECT cancel_requested,state,worker_epoch FROM jobs WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
        if row['cancel_requested'] or row['state'] != 'running' or row['worker_epoch'] != job['worker_epoch']:
            raise ApiError('job_fenced', 'Job publication was cancelled.', 409)
        con.execute('UPDATE job_object_intents SET multipart_id=%s WHERE object_id=%s AND attempt_epoch=%s', (multipart, intent.object_id, intent.attempt_epoch))


def _release_unused_output(db, job):
    # Only called after child/group reap, closed S3 transfer and removed scratch.
    repo=JobRepository(db,db.settings)
    with db.transaction() as con:
        repo._locks(con,job['owner_id'])
        row=con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
        if row['worker_epoch']!=job['worker_epoch'] or row['state']!='running': return
        intents=con.execute("SELECT state FROM job_object_intents WHERE object_id IN (SELECT id FROM artifacts WHERE job_id=%s)",(job['id'],)).fetchall()
        if any(item['state']!='complete' for item in intents): return
        used=con.execute("SELECT COALESCE(sum(a.bytes),0) AS bytes FROM artifacts a JOIN job_object_intents i ON i.object_id=a.id WHERE a.job_id=%s AND a.state IN ('ready','staging') AND i.state='complete'",(job['id'],)).fetchone()['bytes']
        if used>row['reservation_bytes']: raise ValueError('Output reserve exceeded')
        released=row['reservation_bytes']-used
        con.execute("UPDATE quota_scopes SET storage_bytes=storage_bytes-%s WHERE scope IN ('global',%s)",(released,job['owner_id']))
        con.execute('UPDATE jobs SET reservation_bytes=%s WHERE id=%s',(used,job['id']))


def _publish_file(db,objects,job,kind,path,size,sha):
    intent=_artifact_intent(db,job,size,sha,int(time.time()),kind)
    descriptor=objects.put_file(intent,path,lambda upload:_persist_multipart(db,job,intent,upload))
    if descriptor.bytes!=size or descriptor.sha256!=sha: raise ValueError('Artifact hash changed')
    if _cancelled(db,job,db.settings): raise ApiError('job_fenced','Job publication was cancelled.',409)
    return {'id':intent.object_id,'kind':kind,'bytes':descriptor.bytes,'sha256':descriptor.sha256,'key':descriptor.key}


def _restore_checkpoint(db,objects,job,settings,scratch,expected):
    checkpoint=job['checkpoint']
    if (type(checkpoint) is not dict or checkpoint.get('schema')!=1 or checkpoint.get('fingerprint')!=expected
            or checkpoint.get('inputHash')!=job['input_sha256'] or set(checkpoint)-{'schema','fingerprint','inputHash','artifact','previewInput','thumbnail'}):
        raise ValueError('Checkpoint changed')
    for name,kind,filename,cap in [('artifact','report','report.json',settings.report_max_bytes),
                                  ('previewInput','preview','preview-input.json',settings.preview_max_bytes),
                                  ('thumbnail','thumbnail','thumbnail.png',THUMBNAIL_MAX_BYTES)]:
        if name not in checkpoint:
            if name=='artifact' or (name=='previewInput' and 'thumbnail' in checkpoint): raise ValueError('Checkpoint missing')
            continue
        item=checkpoint[name]
        if type(item) is not dict or set(item)!={'id','kind','bytes','sha256','key'} or item['kind']!=kind or type(item['bytes']) is not int or not 1<=item['bytes']<=cap:
            raise ValueError('Checkpoint descriptor changed')
        with db.connect() as con:
            row=con.execute('SELECT a.*,i.state AS intent_state FROM artifacts a JOIN job_object_intents i ON i.object_id=a.id WHERE a.id=%s AND a.job_id=%s',(item['id'],job['id'])).fetchone()
        if (not row or row['state'] not in {'ready','staging'} or row['intent_state']!='complete' or row['kind']!=kind
                or (row['bytes'],row['sha256'],row['object_key'])!=(item['bytes'],item['sha256'],item['key'])
                or item['key']!=f"owners/{job['owner_id']}/jobs/{job['id']}/{row['attempt_epoch']}/{filename}"):
            raise ValueError('Checkpoint intent changed')
        _verified_download(objects,ObjectDescriptor(item['key'],item['bytes'],item['sha256'],'image/png' if kind=='thumbnail' else 'application/json'),scratch/filename,cap)
        if kind=='preview': decode_preview_json((scratch/filename).read_bytes(),PreviewLimits(settings.preview_max_instances,settings.preview_max_vertices,settings.preview_max_triangles,settings.preview_max_bytes))
        if kind=='thumbnail':
            png=inspect_png((scratch/filename).read_bytes())
            if (png['width'],png['height'])!=(512,512): raise ValueError('Checkpoint thumbnail changed')
    return checkpoint


def _journal_preview_failure(db,job,code,error=None):
    # Unsupported input is an expected capability limit; cancellation is not a failure.
    if code not in {'preview_resource','preview_runtime_unavailable','preview_budget','preview_roundtrip_error'}:
        return
    from .journal import emit,correlation
    emit(getattr(db,'journal',None),'preview',code,correlation('job',job['id'],job['worker_epoch']),
         error,job_id=job['id'],attempt=job['worker_epoch'])


def _preview_stage(db,objects,repo,job,settings,scratch,checkpoint):
    if 'previewInput' not in checkpoint or 'thumbnail' in checkpoint: return
    if _cancelled(db,job,settings): return
    staging=scratch/'render'; staging.mkdir(mode=0o700)
    try:
        checked=run_preview(scratch/'preview-input.json',staging,settings,lambda:_cancelled(db,job,settings))
    except PreviewError as error:
        if error.code=='preview_cancelled': return
        _journal_preview_failure(db,job,error.code,error)
        repo.preview_finished(job['id'],job['worker_epoch'],None,error.code,int(time.time()))
        return
    if _cancelled(db,job,settings): return
    image=checked.thumbnail_path.read_bytes()
    thumbnail=_publish_file(db,objects,job,'thumbnail',checked.thumbnail_path,len(image),hashlib.sha256(image).hexdigest())
    repo.preview_finished(job['id'],job['worker_epoch'],thumbnail,None,int(time.time()))


def run_once(db, settings, epoch):
    repo = JobRepository(db, settings)
    job = repo.claim_next(epoch, int(time.time()))
    if not job:
        return False
    scratch = settings.data_root / 'jobs' / job['id'] / epoch
    for parent in (settings.data_root/'jobs',scratch.parent): parent.mkdir(mode=0o700,exist_ok=True)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=False)
    objects = ObjectStore(settings.storage)
    try:
        if _cancelled(db, job, settings):
            repo.finish(job['id'], epoch, 'cancelled', None, int(time.time()))
            return True
        expected = fingerprint(job['input_sha256'], job['region'], job['procedure'], job['submission_date'].isoformat())
        if expected != job['fingerprint']:
            repo.finish(job['id'], epoch, 'failed', 'input_changed', int(time.time()))
            return True
        with db.connect() as con:
            upload = con.execute('SELECT object_key,object_bytes,object_sha256 FROM uploads WHERE id=%s', (job['upload_id'],)).fetchone()
        descriptor = ObjectDescriptor(upload['object_key'], upload['object_bytes'], upload['object_sha256'], 'application/zip')
        if descriptor.sha256 != job['input_sha256']:
            raise ValueError('Input hash changed')
        _verified_download(objects, descriptor, scratch / 'input.zip', settings.upload_max_bytes)
        if job['checkpoint']:
            checkpoint=_restore_checkpoint(db,objects,job,settings,scratch,expected)
            _preview_stage(db,objects,repo,job,settings,scratch,checkpoint)
            # Only hash-checked immutable S3 checkpoints may skip parsing.
            shutil.rmtree(scratch)
            _release_unused_output(db,job)
            repo.finish(job['id'], epoch, 'completed', None, int(time.time()))
            return True
        from .progress import persist_progress
        callback=lambda snapshot:persist_progress(db,job,snapshot,int(time.time()))
        result, code = run_child(settings, scratch, job['input_kind'], lambda: _cancelled(db, job, settings),
                                 progress=callback,source_hash=job['input_sha256'],attempt=epoch)
        if code:
            from .journal import emit,correlation
            if code!='cancelled': emit(getattr(db,'journal',None),'validation',code,correlation('job',job['id'],epoch),job_id=job['id'],attempt=epoch)
            shutil.rmtree(scratch)
            _release_unused_output(db,job)
            repo.finish(job['id'], epoch, 'cancelled' if code == 'cancelled' else 'failed', None if code == 'cancelled' else code, int(time.time()))
            return True
        if result['message']['inputHash'] != descriptor.sha256 or _cancelled(db, job, settings):
            raise ValueError('Input hash changed')
        item = result['message']['report']
        report=_publish_file(db,objects,job,'report',scratch/'report.json',item['bytes'],item['sha256'])
        artifacts=[{**report,'coverage':result['report']['coverage']}]
        preview=result['message']['previewInput']
        if preview is not None:
            artifacts.append(_publish_file(db,objects,job,'preview',scratch/'preview-input.json',preview['bytes'],preview['sha256']))
        _journal_preview_failure(db,job,result['message']['failureCode'])
        repo.checkpoint(job['id'],epoch,'preview' if preview else 'report',expected,artifacts,int(time.time()),result['message']['failureCode'])
        checkpoint=_job(db,job['id'])['checkpoint']
        _preview_stage(db,objects,repo,job,settings,scratch,checkpoint)
        # Close/remove scratch before terminal acknowledgement; durable report is S3.
        shutil.rmtree(scratch)
        _release_unused_output(db,job)
        repo.finish(job['id'], epoch, 'completed', None, int(time.time()))
    except (ValueError, ApiError) as error:
        from .journal import emit,correlation
        emit(getattr(db,'journal',None),'worker',getattr(error,'code','input_changed'),correlation('job',job['id'],epoch),error,job_id=job['id'],attempt=epoch)
        current = _job(db, job['id'])
        if current and current['state'] == 'running' and current['worker_epoch'] == epoch:
            repo.finish(job['id'], epoch, 'failed', 'storage_unavailable' if isinstance(error, ApiError) else 'input_changed', int(time.time()))
    except (RuntimeError,psycopg.Error,OSError,TimeoutError) as error:
        from .journal import emit,correlation
        emit(getattr(db,'journal',None),'worker','worker_interrupted',correlation('job',job['id'],epoch),error,job_id=job['id'],attempt=epoch)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return True


def _clean_artifacts(db, objects, job_id, *, only_staging=False,keep=()):
    with db.connect() as con:
        rows = con.execute("SELECT * FROM job_object_intents WHERE object_id IN (SELECT id FROM artifacts WHERE job_id=%s AND (NOT %s OR state='staging'))", (job_id,only_staging)).fetchall()
    for row in rows:
        if row['object_id'] in keep: continue
        intent = ObjectIntent(**row)
        objects.abort_multipart(intent)
        descriptor = objects.head(intent)
        if descriptor:
            objects.delete(descriptor)
    with db.transaction() as con:
        con.execute("DELETE FROM artifacts WHERE job_id=%s AND (NOT %s OR state='staging') AND NOT(id=ANY(%s))", (job_id,only_staging,list(keep)))


def recover(db, settings, epoch, now):
    """Called after held OS+PG locks prove former worker no longer computes."""
    objects = ObjectStore(settings.storage)
    repo = JobRepository(db, settings)
    with db.connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE state IN ('running','interrupted') AND worker_epoch<>%s ORDER BY created_at", (epoch,)).fetchall()
    for job in rows:
        expected = fingerprint(job['input_sha256'], job['region'], job['procedure'], job['submission_date'].isoformat())
        valid = False
        if job['checkpoint'] and expected == job['fingerprint']:
            scratch = settings.data_root / 'recovery' / job['id']
            scratch.parent.mkdir(mode=0o700,exist_ok=True)
            scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                _restore_checkpoint(db,objects,job,settings,scratch,expected)
                valid=True
            except (ValueError, KeyError, ApiError):
                valid = False
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        if not valid:
            _clean_artifacts(db, objects, job['id'])
        else:
            keep=[item['id'] for name,item in job['checkpoint'].items() if name in {'artifact','previewInput','thumbnail'}]
            _clean_artifacts(db,objects,job['id'],only_staging=True,keep=keep)
        with db.transaction() as con:
            repo._locks(con, job['owner_id'])
            current = con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
            if current['state'] not in {'running', 'interrupted'} or current['worker_epoch'] == epoch:
                continue
            terminal = current['cancel_requested'] or current['expires_at'] <= now or current['deadline_at'] <= now or current['attempts'] >= 2 or expected != current['fingerprint']
            state = 'cancelled' if current['cancel_requested'] or current['expires_at'] <= now else 'failed' if terminal else 'queued'
            con.execute("UPDATE jobs SET state=%s,worker_epoch=NULL,checkpoint=%s,failure_code=%s,updated_at=%s,active_reserved=%s WHERE id=%s", (state, Jsonb(current['checkpoint']) if valid else None, 'worker_interrupted' if state == 'failed' else None, now, current['active_reserved'] and not terminal, job['id']))
            if terminal and current['active_reserved']:
                con.execute("UPDATE quota_scopes SET active_jobs=active_jobs-1 WHERE scope IN ('global',%s)", (job['owner_id'],))
        shutil.rmtree(settings.data_root / 'jobs' / job['id'], ignore_errors=True)


def sweep(db, settings, now):
    objects = ObjectStore(settings.storage)
    repo = JobRepository(db, settings)
    with db.connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE state='deleting' OR (expires_at<=%s AND state<>'deleted') OR (state='queued' AND cancel_requested) OR (state IN ('failed','cancelled') AND updated_at<=%s AND reservation_bytes>0) ORDER BY created_at LIMIT 100", (now,now-900)).fetchall()
    for job in rows:
        # Running jobs are stopped by the owning run_child loop before sweep acknowledgement.
        if job['state'] == 'running':
            continue
        if job['state'] == 'queued' and job['cancel_requested'] and job['expires_at'] > now:
            with db.transaction() as con:
                repo._locks(con, job['owner_id'])
                row = con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
                if row['state'] != 'queued':
                    continue
                con.execute("UPDATE jobs SET state='cancelled',active_reserved=FALSE,updated_at=%s WHERE id=%s", (now, job['id']))
                if row['active_reserved']:
                    con.execute("UPDATE quota_scopes SET active_jobs=active_jobs-1 WHERE scope IN ('global',%s)", (job['owner_id'],))
            continue
        orphan=job['state'] in {'failed','cancelled'} and job['expires_at']>now
        _clean_artifacts(db, objects, job['id'],only_staging=orphan and job['state']=='failed')
        shutil.rmtree(settings.data_root / 'jobs' / job['id'], ignore_errors=True)
        if orphan:
            # Fifteen-minute orphan cleanup preserves the retained source/report
            # metadata. Confirmed closed/removed output alone releases its reserve.
            with db.transaction() as con:
                repo._locks(con,job['owner_id'])
                row=con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
                if row['state'] not in {'failed','cancelled'}: continue
                used=con.execute("SELECT COALESCE(sum(bytes),0) AS bytes FROM artifacts WHERE job_id=%s AND state='ready'",(job['id'],)).fetchone()['bytes']
                if used>row['reservation_bytes']: raise ValueError('Output reserve exceeded')
                con.execute("UPDATE quota_scopes SET storage_bytes=storage_bytes-%s WHERE scope IN ('global',%s)",(row['reservation_bytes']-used,row['owner_id']))
                con.execute('UPDATE jobs SET reservation_bytes=%s WHERE id=%s',(used,job['id']))
            continue
        with db.connect() as con:
            upload = con.execute('SELECT i.* FROM object_intents i WHERE object_id=%s', (job['upload_id'],)).fetchone()
        input_intent = ObjectIntent(**upload)
        objects.abort_multipart(input_intent)
        descriptor = objects.head(input_intent)
        if descriptor:
            objects.delete(descriptor)
        shutil.rmtree(settings.data_root / 'jobs' / job['id'], ignore_errors=True)
        with db.transaction() as con:
            repo._locks(con, job['owner_id'])
            row = con.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
            source = con.execute('SELECT * FROM uploads WHERE id=%s FOR UPDATE', (job['upload_id'],)).fetchone()
            if row['state'] == 'running' or row['state'] == 'deleted':
                continue
            con.execute("UPDATE quota_scopes SET storage_bytes=storage_bytes-%s,active_jobs=active_jobs-%s WHERE scope IN ('global',%s)", (row['reservation_bytes'] + source['reservation_bytes'], int(row['active_reserved']), row['owner_id']))
            con.execute("UPDATE jobs SET state='deleted',reservation_bytes=0,active_reserved=FALSE,checkpoint=NULL,updated_at=%s WHERE id=%s", (now, job['id']))
            con.execute("UPDATE uploads SET state='deleted',reservation_bytes=0 WHERE id=%s", (job['upload_id'],))
            con.execute("UPDATE object_intents SET state='deleted',reserved_bytes=0,multipart_id=NULL WHERE object_id=%s", (job['upload_id'],))
    with db.transaction() as con:
        con.execute("DELETE FROM jobs WHERE state='deleted' AND expires_at<=%s", (now - 7 * 86400,))
    # Unknown scratch has no durable object intent and cannot be published. The
    # held worker lease guarantees that only the current known running job lives.
    root=settings.data_root/'jobs'
    if root.exists():
        for path in list(root.iterdir())[:100]:
            if not path.is_dir() or path.is_symlink() or path.stat().st_mtime>now-900: continue
            with db.connect() as con:
                live=con.execute("SELECT 1 FROM jobs WHERE id=%s AND state IN ('queued','running','interrupted')",(path.name,)).fetchone()
            if not live: shutil.rmtree(path)


@contextmanager
def worker_lease(db, settings, epoch):
    settings.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(settings.data_root / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError('Unsafe worker lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with db.connect() as lease:
            if not lease.execute('SELECT pg_try_advisory_lock(%s) AS locked', (WORKER_LOCK,)).fetchone()['locked']:
                raise RuntimeError('Worker already running')
            db.worker_failed = threading.Event()
            db.worker_guard_verified = threading.Event()
            db.worker_runtime_identity = None
            stop = threading.Event()
            def heartbeat():
                try:
                    while not stop.is_set():
                        with db.worker_heartbeat() as heartbeat_connection:
                            if heartbeat_connection is not None:
                                runtime=db.worker_runtime_identity
                                heartbeat_connection.execute("INSERT INTO worker_state(singleton,epoch,heartbeat,guard_verified,runtime_verified,runtime_version,runtime_fingerprint) VALUES(TRUE,%s,%s,%s,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET epoch=excluded.epoch,heartbeat=excluded.heartbeat,guard_verified=excluded.guard_verified,runtime_verified=excluded.runtime_verified,runtime_version=excluded.runtime_version,runtime_fingerprint=excluded.runtime_fingerprint", (epoch,int(time.time()),db.worker_guard_verified.is_set(),runtime is not None,f"{runtime['engine']}-{runtime['version']}" if runtime else None,Jsonb(runtime) if runtime else None))
                        stop.wait(.25 if heartbeat_connection is None else 5)
                except (RuntimeError, psycopg.Error, OSError, TimeoutError):
                    db.worker_failed.set()
            thread = threading.Thread(target=heartbeat, name='mg-worker-lease')
            with db.worker_session(lease):
                thread.start()
                try:
                    yield
                    if db.worker_failed.is_set():
                        raise RuntimeError('Worker lease lost')
                finally:
                    stop.set()
                    thread.join(2)
                    if thread.is_alive():
                        lease.close()
                        thread.join(2)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--development', action='store_true')
    args = parser.parse_args()
    if not args.development:
        # Managed Docker init must be the direct parent; no old-container loops.
        parent = Path(f'/proc/{os.getppid()}/comm').read_text().strip()
        if parent not in {'docker-init', 'tini'} or os.getppid() != 1:
            raise SystemExit('Managed container init is required.')
    import ctypes
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0):
        raise SystemExit('Worker lifetime guard unavailable.')
    settings = Settings.from_env()
    if settings.db_role != 'mg_worker':
        raise SystemExit('Own worker credential required.')
    db = Database(settings)
    from .journal import Journal,emit
    db.journal=Journal(os.environ.get('MG_JOURNAL_ROOT',str(settings.data_root/'journal')),'worker')
    epoch = uuid4().hex
    try:
        db.check_schema()
        with worker_lease(db, settings, epoch):
            # Even synthetic startup parsing is fenced by both lifetime locks.
            # No verified heartbeat or user claim precedes the actual guard.
            probe=settings.data_root/'startup'/epoch
            probe.mkdir(mode=0o700,parents=True)
            try:
                (probe/'input.zip').write_bytes(b'not-a-zip')
                verified,code=run_child(settings,probe,'zip-fbx',lambda:db.worker_failed.is_set())
                if code or not verified: raise RuntimeError('Worker guard unavailable')
                db.worker_guard_verified.set()
            finally: shutil.rmtree(probe,ignore_errors=True)
            try:
                preflight_preview(settings,lambda:db.worker_failed.is_set())
                db.worker_runtime_identity=installed_preview_fingerprint()
            except PreviewError:
                # Diagnostics remain useful; readiness and preview stay unavailable.
                db.worker_runtime_identity=None
            recover(db, settings, epoch, int(time.time()))
            last_sweep = 0
            while True:
                if db.worker_failed.is_set():
                    raise RuntimeError('Worker lease lost')
                now = int(time.time())
                if now - last_sweep >= settings.sweep_seconds:
                    sweep(db, settings, now)
                    last_sweep = now
                if not run_once(db, settings, epoch):
                    time.sleep(.25)
    except (RuntimeError, psycopg.Error, ApiError, OSError, TimeoutError) as error:
        emit(db.journal,'worker',getattr(error,'code','worker_interrupted'),epoch,error,attempt=epoch)
        raise SystemExit('Worker unavailable.') from None
    finally:
        db.close()
        db.journal.close()


if __name__ == '__main__':
    main()
