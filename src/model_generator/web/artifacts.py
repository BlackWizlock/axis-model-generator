"""Owner-first immutable S3 descriptors and bounded complete-body integrity."""
import hashlib
from starlette.responses import StreamingResponse
from .security import ApiError
from .store import ID
from .s3_store import ObjectDescriptor,ObjectIntent
from .uploads import owned_io

KINDS={
    'report':('application/json','model-generator-report.json','report.json','report_max_bytes'),
    'preview':('application/json','model-generator-preview.json','preview-input.json','preview_max_bytes'),
    'thumbnail':('image/png','model-generator-thumbnail.png','thumbnail.png',None),
}


def _unavailable():
    return ApiError('storage_unavailable','Private storage is temporarily unavailable.',503)


class OwnedStream:
    """Own the SDK context even when ASGI stops before iteration starts."""
    def __init__(self,context):
        self.context=context; self.raw=context.__enter__(); self.closed=False
    def read(self,size): return self.raw.read(size)
    def close(self):
        if self.closed: return
        self.closed=True
        self.context.__exit__(None,None,None)


def owned_artifact_descriptor(db,store,owner_id,job_id,artifact_id,now):
    from .jobs import JobRepository
    with db.connect() as con:
        job=JobRepository(db,store.settings)._row(con,owner_id,job_id,now)
        if not isinstance(artifact_id,str) or not ID.fullmatch(artifact_id) or job['state'] not in {'completed','failed'} or job['cancel_requested']:
            raise ApiError('not_found','Resource is unavailable.',404)
        item=con.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s AND state='ready'",(artifact_id,job_id)).fetchone()
        if not item or item['kind'] not in KINDS: raise ApiError('not_found','Resource is unavailable.',404)
        mime,filename,suffix,cap_name=KINDS[item['kind']]
        cap=getattr(store.settings,cap_name) if cap_name else 4*1024**2
        if (not 1<=item['bytes']<=cap or item['object_key']!=f"owners/{owner_id}/jobs/{job_id}/{item['attempt_epoch']}/{suffix}"):
            raise ApiError('not_found','Resource is unavailable.',404)
        return {**item,'owner_id':owner_id,'contentType':mime,'filename':filename,'expiresAt':job['expires_at']}


def open_owned_artifact(db,store,owner_id,job_id,artifact_id,now):
    item=owned_artifact_descriptor(db,store,owner_id,job_id,artifact_id,now)
    return open_descriptor(store,item)


def open_descriptor(store,item):
    if store.objects is None: raise ApiError('storage_not_ready','Private storage is not configured.',503)
    descriptor=ObjectDescriptor(item['object_key'],item['bytes'],item['sha256'],item['contentType'])
    head=store.objects.head(ObjectIntent(item['owner_id'],item['id'],item['attempt_epoch'],item['object_key'],'complete',None,item['bytes']))
    if not head or (head.bytes,head.sha256,head.content_type)!=(descriptor.bytes,descriptor.sha256,descriptor.content_type):
        raise _unavailable()
    return OwnedStream(store.objects.open_stream(descriptor)),item


def verified_chunks(stream,item):
    """Hold at most one 64 KiB block until EOF, length and SHA all match.

    A corrupt stream can send a prefix, but cannot send all declared bytes or
    the terminating ASGI body. Never claim successful completion after a hash
    mismatch. No whole object buffer or local durable file is used.
    """
    total=0; sha=hashlib.sha256(); pending=None
    try:
        while True:
            chunk=stream.read(65536)
            if not chunk: break
            total+=len(chunk)
            if total>item['bytes']: raise _unavailable()
            sha.update(chunk)
            if pending is not None: yield pending
            pending=chunk
        if total!=item['bytes'] or sha.hexdigest()!=item['sha256']: raise _unavailable()
        if pending is not None: yield pending
    finally:
        stream.close()


def _next(iterator):
    return next(iterator,None)


class ArtifactResponse(StreamingResponse):
    def __init__(self,store,stream,item):
        self.store=store; self.stream=stream
        self.chunks=verified_chunks(stream,item)
        async def body():
            while True:
                chunk=await owned_io(store,_next,self.chunks)
                if chunk is None: break
                yield chunk
        super().__init__(body(),media_type=item['contentType']+('; charset=utf-8' if item['kind']=='report' else ''),headers={
            'Content-Length':str(item['bytes']),
            'Content-Disposition':f'attachment; filename="{item["filename"]}"',
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        })
    async def __call__(self,scope,receive,send):
        try: await super().__call__(scope,receive,send)
        finally:
            # owned_io waits for any physically running read even on disconnect.
            def close():
                try: self.chunks.close()
                finally: self.stream.close()
            await owned_io(self.store,close)
