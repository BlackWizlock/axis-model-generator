"""Single-use raw upload receiver with bounded backpressure and closed-IO cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import os
import stat
import time
import threading
from uuid import uuid4
from fastapi import APIRouter,Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse,Response
from .auth import require_mutation
from .security import ApiError,bounded_json

router=APIRouter(prefix='/api/uploads')

def write_all(fd,chunk):
    view=memoryview(chunk)
    while view:
        written=os.write(fd,view)
        if not written: raise OSError('Short scratch write')
        view=view[written:]

def open_private(path):
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode): os.close(fd); raise OSError('Unsafe scratch')
    return fd

async def owned_io(store,callback,*args,cancel_cleanup=None):
    """Cancellation waits for its one submitted physical IO, then propagates."""
    future=asyncio.get_running_loop().run_in_executor(store.io_executor,callback,*args)
    cancelled=False
    while True:
        try:
            value=await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            cancelled=True
            if future.done():
                # Consume physical worker's result/error, never leave a live write behind.
                try:
                    completed=future.result()
                    if cancel_cleanup: cancel_cleanup(completed)
                except BaseException: pass
                raise
    if cancelled:
        if cancel_cleanup: cancel_cleanup(value)
        raise asyncio.CancelledError
    return value

async def receive_content(request,user,upload_id,store):
    settings=store.settings; db=store.db
    epoch=uuid4().hex; row=None; fd=None; total=0; sha=hashlib.sha256(); success=False; code=None
    # This request cannot produce IO until its result is delivered. The physical
    # callback alone certifies exit even when db.run discards a committed result.
    lifecycle_lock=threading.Lock(); physical_done=False; undelivered=False; claim_result=None; uncertain=False
    def record_closed():
        with store._closed_lock: store.closed_writers.add((store.api_epoch,user.id,upload_id,epoch))
    def claim():
        nonlocal physical_done,claim_result,uncertain
        try:
            claim_result=store.claim_content(user.id,upload_id,epoch)
            return claim_result
        except ApiError:
            # Domain refusals occur before claim writes; never enqueue a losing PUT.
            raise
        except BaseException:
            uncertain=True  # A COMMIT error can still have made the token durable.
            raise
        finally:
            with lifecycle_lock:
                physical_done=True
                if undelivered and (claim_result is not None or uncertain): record_closed()
    started=time.monotonic()
    try:
        row=await db.run(claim)
        store.deadlines[upload_id]=started+settings.upload_wall_seconds
        lengths=request.headers.getlist('content-length')
        if lengths:
            if len(lengths)!=1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0])>20: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
            length=int(lengths[0])
            if length>settings.upload_max_bytes: raise ApiError('upload_too_large','Upload exceeds the size limit.',413)
            if length!=row['declared_bytes']: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
        await db.run(store.check_writer,user.id,upload_id,epoch,int(request.app.state.clock()))
        path=store.private_path('uploads',upload_id,'input.part')
        fd=await owned_io(store,open_private,path,cancel_cleanup=os.close)
        iterator=request.stream().__aiter__()
        while True:
            remaining=settings.upload_wall_seconds-(time.monotonic()-started)
            if remaining<=0: raise ApiError('upload_timeout','Upload timed out.',408)
            await db.run(store.check_writer,user.id,upload_id,epoch,int(request.app.state.clock()))
            try: chunk=await asyncio.wait_for(iterator.__anext__(),timeout=min(settings.upload_idle_seconds,remaining))
            except StopAsyncIteration: break
            except TimeoutError: raise ApiError('upload_timeout','Upload timed out.',408) from None
            if total+len(chunk)>row['declared_bytes']: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
            if total+len(chunk)>settings.upload_max_bytes: raise ApiError('upload_too_large','Upload exceeds the size limit.',413)
            # The ASGI chunk is owned by the server; only a 64 KiB window is written/hash-read.
            for start in range(0,len(chunk),settings.upload_chunk_bytes):
                if time.monotonic()-started>settings.upload_wall_seconds: raise ApiError('upload_timeout','Upload timed out.',408)
                await db.run(store.check_writer,user.id,upload_id,epoch,int(request.app.state.clock()))
                view=memoryview(chunk)[start:start+settings.upload_chunk_bytes]
                await owned_io(store,write_all,fd,view)
                total+=len(view); sha.update(view)
                await db.run(store.check_writer,user.id,upload_id,epoch,int(request.app.state.clock()))
            if lengths and total>int(lengths[0]): raise ApiError('upload_size_mismatch','Upload size does not match.',409)
        if total!=row['declared_bytes']: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
        if sha.hexdigest()!=row['sha256']: raise ApiError('upload_hash_mismatch','Upload hash does not match.',409)
        await owned_io(store,os.fsync,fd)
        os.close(fd); fd=None
        await db.run(store.check_writer,user.id,upload_id,epoch,int(request.app.state.clock()))
        result=await owned_io(store,store.finish_upload,user.id,upload_id,total,sha.hexdigest())
        success=True
        path.unlink(missing_ok=True)
        return result
    except ApiError as error: code=error.code; raise
    except OSError: code='storage_full'; raise ApiError('storage_full','Private scratch capacity is unavailable.',507) from None
    except ClientDisconnect: code='upload_disconnected'; raise ApiError('upload_disconnected','Upload disconnected.',409) from None
    finally:
        if fd is not None: os.close(fd)
        if row is None:
            # No request body/S3 IO was submitted. If the bounded SQL worker is
            # still exiting, it will enqueue its proof only after physical exit.
            with lifecycle_lock:
                undelivered=True
                if physical_done and (claim_result is not None or uncertain): record_closed()
        elif not success:
            # A failed cleanup keeps the durable reserve and is retried by the sweeper.
            try: await owned_io(store,store.acknowledge_closed,user.id,upload_id,epoch,code)
            finally: store.deadlines.pop(upload_id,None)
        else: store.deadlines.pop(upload_id,None)

def require_storage(request):
    store=getattr(request.app.state,'storage',None)
    if store is None or store.objects is None: raise ApiError('storage_not_ready','Private storage is not configured.',503)
    return store

@router.post('')
async def reserve(request: Request):
    user=await require_mutation(request); store=require_storage(request)
    settings=store.settings
    value=await bounded_json(request,settings.json_max_bytes,settings.json_idle_seconds,settings.json_wall_seconds)
    if set(value) not in ({'kind','displayName','bytes','sha256'},
                          {'kind','displayName','bytes','sha256','descriptorVersion'}):
        raise ApiError('invalid_upload','Upload metadata is invalid.',422)
    version=value.get('descriptorVersion',0)
    if type(version) is not int or version not in (0,1):
        raise ApiError('invalid_upload','Upload metadata is invalid.',422)
    result=await store.db.run(partial(store.reserve_upload,descriptor_version=version),user.id,value['kind'],value['displayName'],value['bytes'],value['sha256'],int(request.app.state.clock()))
    return JSONResponse(result,status_code=201)

@router.put('/{upload_id}/content')
async def content(request: Request,upload_id: str):
    user=await require_mutation(request); store=require_storage(request)
    if request.headers.getlist('content-type')!=['application/octet-stream']: raise ApiError('unsupported_media_type','Use application/octet-stream.',415)
    return await receive_content(request,user,upload_id,store)

@router.delete('/{upload_id}')
async def delete(request: Request,upload_id: str):
    user=await require_mutation(request); store=require_storage(request)
    result=await owned_io(store,store.delete_unused,user.id,upload_id,int(request.app.state.clock()))
    return Response(status_code=result['status'])
