"""Single API process, explicit schema validation and no startup migrations."""
from contextlib import asynccontextmanager
import asyncio
import fcntl
import os
import stat
import shutil
import time
from uuid import uuid4
from fastapi import FastAPI,Request
from starlette.exceptions import HTTPException
from .lock_identity import initialize,valid
from .config import Settings
from .db import Database
from .auth import KdfPool,cleanup_guests,consume_limit
from .auth_routes import router as auth_router
from .uploads import router as uploads_router,owned_io
from .store import Storage
from .upload_chunks import router as chunks_router
from .jobs import JobRepository
from .job_routes import router as jobs_router
from concurrent.futures import ThreadPoolExecutor
from .security import ApiError,SecurityMiddleware,error_response


def create_app(settings: Settings) -> FastAPI:
    settings.validate()
    @asynccontextmanager
    async def lifespan(app):
        settings.data_root.mkdir(mode=0o700,parents=True,exist_ok=True)
        settings.data_root.chmod(0o700)
        descriptor=os.open(settings.data_root/'api.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        from .journal import Journal,emit
        journal=Journal(os.environ.get('MG_JOURNAL_ROOT',str(settings.data_root/'journal')),'api')
        app.state.journal=journal
        app.state.db=Database(settings)
        app.state.jobs=JobRepository(app.state.db,settings)
        app.state.job_fingerprint_active=0
        app.state.kdf=KdfPool(settings.scrypt_active,settings.scrypt_queued,settings.scrypt_acquire_seconds)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode): raise RuntimeError('Unsafe API lock')
            try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('An API process already owns this scratch') from None
            locked=os.fstat(descriptor)
            current=(settings.data_root/'api.lock').stat(follow_symlinks=False)
            if locked.st_nlink!=1 or (locked.st_dev,locked.st_ino)!=(current.st_dev,current.st_ino): raise RuntimeError('API lock identity changed')
            lock_generation=initialize(descriptor)
            expected_lock=(locked.st_dev,locked.st_ino,lock_generation)
            app.state.api_epoch=uuid4().hex
            app.state.storage=Storage(app.state.db,settings,app.state.api_epoch)
            app.state.storage.journal=journal
            app.state.storage.loop=asyncio.get_running_loop()
            app.state.storage.clock=app.state.clock
            app.state.storage.io_executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='mg-private-io')
            app.state.lock_identity_verified=False
            def lock_valid():
                return app.state.lock_identity_verified and valid(descriptor,settings.data_root/'api.lock',expected_lock)
            app.state.storage.api_lock_valid=lock_valid
            def startup():
                if not valid(descriptor,settings.data_root/'api.lock',expected_lock): raise RuntimeError('API lock identity changed')
                app.state.db.check_schema()
                with app.state.db.transaction() as con:
                    con.execute("INSERT INTO service_state VALUES('api',%s,%s) ON CONFLICT(name) DO UPDATE SET epoch=excluded.epoch,started_at=excluded.started_at",(app.state.api_epoch,int(app.state.clock())))
                    con.execute('INSERT INTO api_lock_identity(singleton,device,inode,generation) VALUES(TRUE,%s,%s,%s) ON CONFLICT DO NOTHING',expected_lock)
                    identity=con.execute('SELECT device,inode,generation FROM api_lock_identity WHERE singleton').fetchone()
                    if identity!={'device':locked.st_dev,'inode':locked.st_ino,'generation':lock_generation} or not valid(descriptor,settings.data_root/'api.lock',expected_lock): raise RuntimeError('API lock identity changed')
                app.state.lock_identity_verified=True
            app.state.database_probe=startup
            try:
                await app.state.db.run(startup)
                app.state.database_ready=True
            except RuntimeError as error:
                emit(journal,'startup','database_unavailable',uuid4().hex,error)
                if str(error)=='API lock identity changed': raise
                app.state.database_ready=False
            except ApiError as error:
                emit(journal,'startup',error.code,uuid4().hex,error)
                app.state.database_ready=False
            # Holding api.lock proves every previous API process has exited, not heartbeat expiry.
            async def sweep():
                if not lock_valid(): raise RuntimeError('API lock identity unverified')
                await owned_io(app.state.storage,lambda:app.state.storage.sweep(int(app.state.clock()),api_lock_owned=True))
                await app.state.db.run(cleanup_guests,app.state.db,int(app.state.clock()))
            try: await sweep()
            except (ApiError,RuntimeError) as error:
                emit(journal,'cleanup',getattr(error,'code','internal_error'),uuid4().hex,error)
            async def cleanup_loop():
                while True:
                    await asyncio.sleep(settings.sweep_seconds)
                    try: await sweep()
                    except (ApiError,RuntimeError) as error:
                        emit(journal,'cleanup',getattr(error,'code','internal_error'),uuid4().hex,error)
            cleanup=asyncio.create_task(cleanup_loop())
            chunks=asyncio.create_task(app.state.storage.chunks.run())
            try: yield
            finally:
                chunks.cancel()
                try: await chunks
                except asyncio.CancelledError: pass
                await app.state.storage.chunks.stop()
                cleanup.cancel()
                try: await cleanup
                except asyncio.CancelledError: pass
                await asyncio.to_thread(app.state.storage.io_executor.shutdown,wait=True,cancel_futures=True)
        finally:
            await app.state.kdf.close()
            await asyncio.to_thread(app.state.db.close)
            os.close(descriptor)
            journal.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None,debug=False)
    app.state.settings=settings
    app.state.clock=time.time
    app.include_router(auth_router)
    app.include_router(uploads_router)
    app.include_router(chunks_router)
    app.include_router(jobs_router)
    @app.exception_handler(ApiError)
    async def api_error(request: Request,error: ApiError):
        from .journal import emit
        emit(getattr(app.state,'journal',None),'request',error.code,request.state.request_id,error)
        return error_response(error,request.state.request_id)
    @app.exception_handler(HTTPException)
    async def http_error(request: Request,error: HTTPException):
        code='not_found' if error.status_code==404 else 'request_rejected'
        from .journal import emit
        emit(getattr(app.state,'journal',None),'request',code,request.state.request_id,error)
        return error_response(ApiError(code,'Resource is unavailable.' if error.status_code==404 else 'Request was rejected.',error.status_code),request.state.request_id)
    @app.get('/health/live')
    async def live(): return {'status':'ok'}
    async def public_limit(request):
        from .auth import ensure_database_ready
        from .security import client_ip
        await ensure_database_ready(app.state)
        def consume():
            with app.state.db.transaction() as con:
                consume_limit(con,settings.auth_key,'public-ip',client_ip(request,settings),int(app.state.clock()),60,120)
        await app.state.db.run(consume)

    @app.get('/api/config')
    async def config(request: Request):
        await public_limit(request)
        from .jobs import AXES
        return {'inputKinds':['zip-fbx','portable-package'],'profileStatus':'research',
                'coverage':dict(AXES),'sourceLink':settings.source_url,
                'capabilities':{kind:{'availability':'unavailable','reason':'generation_not_implemented'} for kind in ('npm','vpm','ifc')},
                'plugin':{'available':False},
                'limits':{'uploadBytes':settings.upload_max_bytes,'storageBytes':settings.storage_per_user_bytes,
                          'jobsActive':settings.jobs_per_user,'acceptedPerDay':settings.accepted_per_user_day,
                          'retentionSeconds':settings.retention_seconds,'unusedUploadSeconds':settings.unused_upload_seconds,
                          'previewInstances':settings.preview_max_instances,'previewVertices':settings.preview_max_vertices,
                          'previewTriangles':settings.preview_max_triangles,'previewBytes':settings.preview_max_bytes,
                          'reportBytes':settings.report_max_bytes}}

    @app.get('/api/plugin/releases')
    async def releases(request: Request):
        await public_limit(request)
        return {'available':False,'releases':[]}

    @app.get('/api/demo/preview')
    async def demo(request: Request):
        await public_limit(request)
        from .preview import build_synthetic_demo,validate_preview,PreviewLimits
        result=build_synthetic_demo()
        limits=PreviewLimits(settings.preview_max_instances,settings.preview_max_vertices,settings.preview_max_triangles,settings.preview_max_bytes)
        return validate_preview(result,limits)

    @app.get('/health/ready')
    async def ready():
        def unavailable(reason):
            from starlette.responses import JSONResponse
            return JSONResponse({'status':'unavailable','reason':reason},status_code=503,headers={'Retry-After':'1'})
        try: await app.state.db.run(app.state.database_probe)
        except (RuntimeError,ApiError):
            app.state.database_ready=False
            return unavailable('database_not_ready')
        app.state.database_ready=True
        if not app.state.journal.health()['available']: return unavailable('journal_unavailable')
        if shutil.disk_usage(settings.data_root).free<settings.min_free_disk_bytes:
            return unavailable('scratch_not_ready')
        def worker_status():
            with app.state.db.connect() as con:
                worker=con.execute('SELECT * FROM worker_state WHERE singleton').fetchone()
                quota=con.execute("SELECT storage_bytes,active_jobs,active_uploads FROM quota_scopes WHERE scope='global'").fetchone()
                return worker,quota
        try: worker,quota=await app.state.db.run(worker_status)
        except ApiError: return unavailable('database_not_ready')
        if not quota or quota['storage_bytes']>=settings.storage_global_bytes or quota['active_jobs']>=settings.jobs_global or quota['active_uploads']>=settings.uploads_global:
            return unavailable('quota_not_ready')
        if not worker or not int(app.state.clock())-15<=worker['heartbeat']<=int(app.state.clock()):
            return unavailable('worker_not_ready')
        if not worker['guard_verified']: return unavailable('guard_not_verified')
        from .preview_runner import installed_preview_fingerprint
        from .preview import PreviewError
        try: runtime=installed_preview_fingerprint()
        except PreviewError: return unavailable('runtime_not_verified')
        if not worker['runtime_verified'] or worker['runtime_version']!='python-cpu-1' or worker['runtime_fingerprint']!=runtime:
            return unavailable('runtime_not_verified')
        if app.state.storage.objects is None: return unavailable('storage_not_ready')
        try: await owned_io(app.state.storage,app.state.storage.objects.probe)
        except ApiError: return unavailable('storage_not_ready')
        return {'status':'ok'}
    from .static import install_static
    install_static(app)
    app.add_middleware(SecurityMiddleware,settings=settings)
    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.from_env())
