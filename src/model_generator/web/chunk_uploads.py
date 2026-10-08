"""Sequential durable chunk ledger, physical leases and resumable finalization."""
import asyncio
from dataclasses import replace
import threading
import time
from uuid import uuid4
from .security import ApiError
from .journal import correlation

CHUNK_BYTES=8*1024**2


def conflict():
    return ApiError('upload_conflict','Upload progress changed. Refresh its status.',409)


class ChunkUploads:
    def __init__(self,store):
        self.store=store
        self.closed=set()
        self.lock=threading.Lock()
        self.tasks=set()
        self.wake=asyncio.Event()

    def _valid(self,row,epoch=None,state=None):
        if not self.store.api_lock_valid(): raise ApiError('storage_unavailable','Private upload service is unavailable.',503)
        if row['expires_at']<=int(self.store.clock()):
            raise ApiError('upload_expired','Upload has expired.',410)
        if row['abort_requested'] or row['state'] in ('deleting','deleted'):
            raise ApiError('upload_aborted','Upload was cancelled.',409)
        if state and row['state']!=state: raise conflict()
        if epoch and (row['request_epoch']!=epoch or row['api_epoch']!=self.store.api_epoch or row['writer_closed']):
            raise conflict()
        return row

    @staticmethod
    def dto(row):
        return {'id':row['id'],'protocol':row['protocol'],'state':row['state'],
                'acknowledgedBytes':row['received_bytes'],'totalBytes':row['declared_bytes'],
                'chunkBytes':CHUNK_BYTES,'nextPart':row['received_bytes']//CHUNK_BYTES+1 if row['received_bytes']<row['declared_bytes'] else None,
                'expiresAt':row['expires_at'],'kind':row['input_kind'],'sha256':row['sha256'],
                'reason':row['failure_code'],'descriptorVersion':row['descriptor_version'],
                'diagnosticId':correlation('upload',row['id'])}

    def status(self,owner,id):
        with self.store.db.connect() as con: row=self.store._row(con,owner,id)
        if row['expires_at']<=int(self.store.clock()): raise ApiError('upload_expired','Upload has expired.',410)
        return self.dto(row)

    def pending(self,owner):
        with self.store.db.connect() as con:
            rows=con.execute("SELECT * FROM uploads WHERE owner_id=%s AND state IN ('receiving','finalizing','ready') AND expires_at>%s ORDER BY created_at DESC LIMIT 20",(owner,int(self.store.clock()))).fetchall()
        return {'uploads':[self.dto(row) for row in rows]}

    def claim(self,owner,id,part,offset,size,sha,epoch):
        with self.store.db.transaction() as con:
            row=self._valid(self.store._row(con,owner,id,True))
            if row['protocol'] not in (None,'chunks-v1'): raise conflict()
            expected=min(CHUNK_BYTES,row['declared_bytes']-offset)
            if part<1 or part>32 or offset!=(part-1)*CHUNK_BYTES or expected<1 or size not in (None,expected): raise conflict()
            saved=con.execute('SELECT * FROM upload_parts WHERE upload_id=%s AND part_number=%s',(id,part)).fetchone()
            if saved and (saved['sha256']!=sha or saved['bytes']!=expected): raise conflict()
            if saved and saved['etag'] is not None: return self.dto(row),True
            self._valid(row,state='receiving')
            if row['received_bytes']!=offset or row['request_epoch'] is not None: raise conflict()
            if not saved:
                con.execute('INSERT INTO upload_parts(upload_id,part_number,byte_offset,bytes,sha256) VALUES(%s,%s,%s,%s,%s)',(id,part,offset,expected,sha))
            con.execute("UPDATE uploads SET protocol='chunks-v1',request_epoch=%s,api_epoch=%s,content_claimed=TRUE,writer_closed=FALSE,writer_started_at=%s WHERE id=%s",(epoch,self.store.api_epoch,int(time.time()),id))
            con.execute("UPDATE object_intents SET state='writing' WHERE object_id=%s",(id,))
            row.update(protocol='chunks-v1',request_epoch=epoch,api_epoch=self.store.api_epoch,writer_closed=False)
            return row,False

    def check(self,owner,id,epoch,state='receiving'):
        with self.store.db.connect() as con: row=self.store._row(con,owner,id)
        return self._valid(row,epoch,state)

    def verified(self,owner,id,epoch,part):
        with self.store.db.transaction() as con:
            self._valid(self.store._row(con,owner,id,True),epoch,'receiving')
            con.execute('UPDATE upload_parts SET verified=TRUE WHERE upload_id=%s AND part_number=%s AND etag IS NULL',(id,part))

    def multipart(self,owner,id,epoch,mp):
        with self.store.db.transaction() as con:
            self._valid(self.store._row(con,owner,id,True),epoch,'receiving')
            con.execute('UPDATE object_intents SET multipart_id=%s WHERE object_id=%s AND multipart_id IS NULL',(mp,id))

    def acknowledge(self,owner,id,epoch,part,etag,deadline=None):
        if deadline is not None and time.monotonic()>=deadline: raise ApiError('upload_timeout','Chunk timed out.',408)
        with self.store.db.transaction() as con:
            row=self._valid(self.store._row(con,owner,id,True),epoch,'receiving')
            saved=con.execute('SELECT * FROM upload_parts WHERE upload_id=%s AND part_number=%s',(id,part)).fetchone()
            if not saved or not saved['verified'] or saved['etag'] is not None or saved['byte_offset']!=row['received_bytes']: raise conflict()
            con.execute('UPDATE upload_parts SET etag=%s WHERE upload_id=%s AND part_number=%s',(etag,id,part))
            con.execute('UPDATE uploads SET received_bytes=received_bytes+%s WHERE id=%s',(saved['bytes'],id))
            row['received_bytes']+=saved['bytes']
            if deadline is not None and time.monotonic()>=deadline: raise ApiError('upload_timeout','Chunk timed out.',408)
            return self.dto(row)

    def upload(self,owner,id,epoch,part,path,deadline):
        store=self.store
        row=store._sql(self.check,owner,id,epoch)
        intent=replace(store._sql(store.intent,owner,id),deadline=deadline)
        store._sql(self.verified,owner,id,epoch,part)
        if not intent.multipart_id:
            # Any earlier ambiguous Create completed before its request lease closed.
            # Only this stable key is inspected, never acknowledged part promotion.
            mp=store.objects.ensure_multipart(intent,row['sha256'])
            store._sql(self.multipart,owner,id,epoch,mp)
            intent=replace(intent,multipart_id=mp)
        store._sql(self.check,owner,id,epoch)
        etag=store.objects.upload_chunk(intent,part,path)
        store._sql(self.check,owner,id,epoch)
        return store._sql(self.acknowledge,owner,id,epoch,part,etag,deadline)

    def record_closed(self,owner,id,epoch):
        with self.lock: self.closed.add((owner,id,epoch))

    def close_lease(self,owner,id,epoch):
        """Only callers that have physically joined all their I/O may enter here."""
        with self.store.db.transaction() as con:
            row=self.store._row(con,owner,id,True)
            if row['request_epoch']==epoch and row['api_epoch']==self.store.api_epoch:
                con.execute('UPDATE uploads SET request_epoch=NULL,content_claimed=FALSE,writer_closed=TRUE WHERE id=%s',(id,))
        with self.lock: self.closed.discard((owner,id,epoch))

    def schedule(self,owner,id):
        with self.store.db.transaction() as con:
            row=self._valid(self.store._row(con,owner,id,True))
            if row['protocol']!='chunks-v1': raise conflict()
            if row['state'] in ('finalizing','ready'): return self.dto(row)
            if row['state']!='receiving' or row['request_epoch'] or row['received_bytes']!=row['declared_bytes']: raise conflict()
            self._parts(con,row)
            con.execute("UPDATE uploads SET state='finalizing' WHERE id=%s",(id,))
            row['state']='finalizing'; return self.dto(row)

    def _parts(self,con,row):
        parts=con.execute('SELECT * FROM upload_parts WHERE upload_id=%s ORDER BY part_number',(row['id'],)).fetchall()
        total=0
        for number,part in enumerate(parts,1):
            if part['part_number']!=number or part['byte_offset']!=total or not part['etag'] or not part['verified'] or part['bytes']!=min(CHUNK_BYTES,row['declared_bytes']-total): raise conflict()
            total+=part['bytes']
        if total!=row['declared_bytes']: raise conflict()
        return [{'PartNumber':part['part_number'],'ETag':part['etag']} for part in parts]

    def claim_final(self,owner,id,epoch):
        with self.store.db.transaction() as con:
            row=self._valid(self.store._row(con,owner,id,True),state='finalizing')
            if row['request_epoch']: raise conflict()
            parts=self._parts(con,row)
            con.execute('UPDATE uploads SET request_epoch=%s,api_epoch=%s,content_claimed=TRUE,writer_closed=FALSE WHERE id=%s',(epoch,self.store.api_epoch,id))
        return row,parts

    def ready(self,owner,id,epoch,intent,descriptor):
        with self.store.db.transaction() as con:
            self.store._locks(con,owner)
            row=self._valid(self.store._row(con,owner,id,True),epoch,'finalizing')
            if row['writer_epoch']!=intent.attempt_epoch or descriptor.key!=intent.key or descriptor.bytes!=row['declared_bytes'] or descriptor.sha256!=row['sha256']: raise conflict()
            con.execute("UPDATE uploads SET state='ready',object_key=%s,object_bytes=%s,object_sha256=%s,active_reserved=FALSE WHERE id=%s",(descriptor.key,descriptor.bytes,descriptor.sha256,id))
            con.execute("UPDATE object_intents SET state='complete',multipart_id=NULL WHERE object_id=%s",(id,))
            con.execute("UPDATE quota_scopes SET active_uploads=active_uploads-1 WHERE scope IN ('global',%s)",(owner,))

    def fail(self,owner,id,epoch,reason):
        with self.store.db.transaction() as con:
            row=self.store._row(con,owner,id,True)
            if row['request_epoch']!=epoch or row['api_epoch']!=self.store.api_epoch or row['state']!='finalizing': return
            con.execute("UPDATE uploads SET state='deleting',abort_requested=TRUE,failure_code=%s WHERE id=%s",(reason,id))
            con.execute("UPDATE object_intents SET state='deleting' WHERE object_id=%s",(id,))

    def finalize(self,owner,id):
        store=self.store; epoch=uuid4().hex
        try:
            row,parts=store._sql(self.claim_final,owner,id,epoch)
            deadline=time.monotonic()+store.settings.upload_finalize_seconds
            intent=replace(store._sql(store.intent,owner,id),deadline=deadline)
            check=lambda:store._sql(self.check,owner,id,epoch,'finalizing')
            check()
            store.objects.complete_chunks(intent,parts)
            check()
            descriptor=store.objects.verify_ranges(intent,row['declared_bytes'],row['sha256'],check)
            check(); store._sql(self.ready,owner,id,epoch,intent,descriptor)
        except ApiError as error:
            from .journal import emit,correlation
            emit(getattr(store,'journal',None),'finalize',error.code,correlation('upload',id),error)
            if error.code in ('upload_hash_mismatch','upload_size_mismatch','upload_expired'):
                store._sql(self.fail,owner,id,epoch,error.code)
        finally:
            self.record_closed(owner,id,epoch)
            store._sql(self.close_lease,owner,id,epoch)

    def reconcile(self,api_lock_owned=False):
        store=self.store
        if not store.api_lock_valid(): raise ApiError('storage_unavailable','Private upload service is unavailable.',503)
        with self.lock: closed=list(self.closed)
        for owner,id,epoch in closed: store._sql(self.close_lease,owner,id,epoch)
        def recover():
            with store.db.transaction() as con:
                if api_lock_owned:
                    con.execute("UPDATE uploads SET request_epoch=NULL,content_claimed=FALSE,writer_closed=TRUE WHERE protocol='chunks-v1' AND request_epoch IS NOT NULL AND api_epoch<>%s",(store.api_epoch,))
                return con.execute("SELECT id,owner_id,request_epoch,state,expires_at FROM uploads WHERE protocol='chunks-v1' AND state IN ('receiving','finalizing','deleting','ready') AND (state<>'ready' OR expires_at<=%s) ORDER BY created_at LIMIT 100",(int(store.clock()),)).fetchall()
        rows=store._sql(recover)
        for row in rows:
            if row['expires_at']<=int(store.clock()) or row['state']=='deleting':
                result=store._sql(store.request_abort,row['owner_id'],row['id'],int(store.clock()))
                if result['status']==204:
                    intent=store._sql(store.intent,row['owner_id'],row['id'])
                    store.acknowledge_closed(row['owner_id'],row['id'],intent.attempt_epoch)
        return [(r['owner_id'],r['id']) for r in rows if r['state']=='finalizing' and not r['request_epoch'] and r['expires_at']>int(store.clock())]

    async def run(self):
        from .uploads import owned_io
        async def produce(owner,id):
            try: await owned_io(self.store,self.finalize,owner,id)
            except Exception as error:
                from .journal import emit,correlation
                emit(getattr(self.store,'journal',None),'finalize',getattr(error,'code','internal_error'),correlation('upload',id),error)
        while True:
            self.wake.clear()
            try:
                pending=await owned_io(self.store,self.reconcile,True)
                active={getattr(task,'upload_id',None) for task in self.tasks}
                for owner,id in pending:
                    if len(self.tasks)>=2: break
                    if id in active: continue
                    task=asyncio.create_task(produce(owner,id)); task.upload_id=id
                    self.tasks.add(task); task.add_done_callback(self.tasks.discard)
            except (ApiError,RuntimeError) as error:
                from .journal import emit
                emit(getattr(self.store,'journal',None),'reconcile',getattr(error,'code','internal_error'),uuid4().hex,error)
            try: await asyncio.wait_for(self.wake.wait(),timeout=1)
            except TimeoutError: pass

    async def stop(self):
        for task in self.tasks.copy(): task.cancel()
        if self.tasks: await asyncio.gather(*self.tasks,return_exceptions=True)
