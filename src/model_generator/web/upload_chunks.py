"""Owner-only bounded HTTP chunk receiver and durable progress routes."""
import asyncio
import hashlib
import os
import re
import threading
import time
from uuid import uuid4
from fastapi import APIRouter,Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from .auth import require_mutation,require_user
from .security import ApiError
from .uploads import owned_io,open_private,write_all,require_storage
from .chunk_uploads import CHUNK_BYTES

router=APIRouter(prefix='/api/uploads')


def header(request,name,pattern):
    values=request.headers.getlist(name)
    if len(values)!=1 or not re.fullmatch(pattern,values[0]):
        raise ApiError('invalid_chunk','Chunk headers are invalid.',422)
    return values[0]


async def receive_chunk(request,user,id,number,store):
    service=store.chunks
    # Ownership/TTL always precedes even syntax/media checks, body or S3.
    await store.db.run(service.status,user.id,id)
    if request.headers.getlist('content-type')!=['application/octet-stream']:
        raise ApiError('unsupported_media_type','Use application/octet-stream.',415)
    if not re.fullmatch('[1-9][0-9]?',number): raise ApiError('invalid_chunk','Chunk number is invalid.',422)
    part=int(number); offset=int(header(request,'upload-offset','0|[1-9][0-9]{0,8}'))
    sha=header(request,'upload-chunk-sha256','[a-f0-9]{64}')
    lengths=request.headers.getlist('content-length')
    size=None
    if lengths:
        if len(lengths)!=1 or not re.fullmatch('[0-9]{1,10}',lengths[0]): raise ApiError('invalid_chunk','Chunk length is invalid.',422)
        size=int(lengths[0])
        if size>CHUNK_BYTES: raise ApiError('upload_too_large','Chunk exceeds the size limit.',413)
    epoch=uuid4().hex; row=None; fd=None; path=None
    lifecycle=threading.Lock(); done=False; undelivered=False; uncertain=False; claimed=False
    def claim():
        nonlocal done,uncertain,claimed
        try:
            value=service.claim(user.id,id,part,offset,size,sha,epoch)
            claimed=not value[1]
            return value
        except ApiError: raise
        except BaseException:
            uncertain=True; raise
        finally:
            with lifecycle:
                done=True
                if undelivered and (claimed or uncertain): service.record_closed(user.id,id,epoch)
    started=time.monotonic(); deadline=started+store.settings.upload_part_wall_seconds
    try:
        row,replay=await store.db.run(claim)
        if replay: row=None; return await store.db.run(service.status,user.id,id)
        expected=min(CHUNK_BYTES,row['declared_bytes']-offset)
        path=store.private_path('uploads',id,'input.part')
        # Previous request's physical exit is proven before this fresh lease.
        path.unlink(missing_ok=True)
        fd=await owned_io(store,open_private,path,cancel_cleanup=os.close)
        total=0; digest=hashlib.sha256(); iterator=request.stream().__aiter__()
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise ApiError('upload_timeout','Chunk timed out.',408)
            await store.db.run(service.check,user.id,id,epoch)
            try: chunk=await asyncio.wait_for(iterator.__anext__(),min(store.settings.upload_idle_seconds,remaining))
            except StopAsyncIteration: break
            except TimeoutError: raise ApiError('upload_timeout','Chunk timed out.',408) from None
            if total+len(chunk)>expected: raise ApiError('upload_size_mismatch','Chunk size does not match.',409)
            for start in range(0,len(chunk),65536):
                if time.monotonic()>=deadline: raise ApiError('upload_timeout','Chunk timed out.',408)
                view=memoryview(chunk)[start:start+65536]
                await owned_io(store,write_all,fd,view)
                digest.update(view); total+=len(view)
        if total!=expected: raise ApiError('upload_size_mismatch','Chunk size does not match.',409)
        if digest.hexdigest()!=sha: raise ApiError('upload_hash_mismatch','Chunk hash does not match.',409)
        await owned_io(store,os.fsync,fd)
        os.close(fd); fd=None
        return await owned_io(store,service.upload,user.id,id,epoch,part,path,deadline)
    except ClientDisconnect:
        raise ApiError('upload_disconnected','Chunk disconnected. Resume from acknowledged progress.',409) from None
    except OSError:
        raise ApiError('storage_full','Private scratch capacity is unavailable.',507) from None
    finally:
        if fd is not None: os.close(fd)
        if row is not None:
            # owned_io joins SDK/disk workers even when this coroutine is cancelled.
            if path is not None: path.unlink(missing_ok=True)
            service.record_closed(user.id,id,epoch)
            try: await store.db.run(service.close_lease,user.id,id,epoch)
            finally: service.wake.set()
        else:
            with lifecycle:
                undelivered=True
                if done and (claimed or uncertain): service.record_closed(user.id,id,epoch)


@router.get('')
async def pending(request:Request):
    user=await require_user(request); store=require_storage(request)
    return await store.db.run(store.chunks.pending,user.id)

@router.get('/{upload_id}')
async def status(request:Request,upload_id:str):
    user=await require_user(request); store=require_storage(request)
    return await store.db.run(store.chunks.status,user.id,upload_id)

@router.put('/{upload_id}/chunks/{part_number}')
async def chunk(request:Request,upload_id:str,part_number:str):
    user=await require_mutation(request); store=require_storage(request)
    return await receive_chunk(request,user,upload_id,part_number,store)

@router.post('/{upload_id}/complete')
async def complete(request:Request,upload_id:str):
    user=await require_mutation(request); store=require_storage(request)
    row=await store.db.run(store.chunks.schedule,user.id,upload_id)
    store.chunks.wake.set()
    return JSONResponse(row,status_code=202)
