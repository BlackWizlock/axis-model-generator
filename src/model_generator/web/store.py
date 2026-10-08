"""Durable quota/intent state with private scratch and acknowledged S3 cleanup."""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import time
import threading
import unicodedata
from dataclasses import replace
from uuid import uuid4
from .security import ApiError
from .s3_store import ObjectStore,ObjectIntent

ID=re.compile(r'[a-f0-9]{32}\Z')
SUFFIXES={'input.zip','input.part','report.json','preview.json','preview-input.json','thumbnail.png','measurements.json'}

def atomic_write(path: Path,chunks,max_bytes):
    if path.is_symlink(): raise OSError('Unsafe scratch')
    tmp=path.with_name(path.name+'.'+uuid4().hex+'.tmp'); fd=None; total=0; sha=hashlib.sha256()
    try:
        fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        for chunk in chunks:
            if total+len(chunk)>max_bytes: raise OSError('Scratch budget exceeded')
            view=memoryview(chunk)
            while view:
                written=os.write(fd,view[:65536])
                if not written: raise OSError('Short scratch write')
                view=view[written:]
            total+=len(chunk); sha.update(chunk)
        os.fsync(fd); os.close(fd); fd=None
        if path.is_symlink(): raise OSError('Unsafe scratch')
        os.replace(tmp,path); return total,sha.hexdigest()
    finally:
        if fd is not None: os.close(fd)
        tmp.unlink(missing_ok=True)

class Storage:
    def __init__(self,db,settings,api_epoch=None):
        self.db=db; self.settings=settings; self.api_epoch=api_epoch or uuid4().hex
        self.loop=None
        self.clock=time.time
        self.deadlines={}
        self.closed_writers=set()
        self._closed_lock=threading.Lock()
        from .chunk_uploads import ChunkUploads
        self.chunks=ChunkUploads(self)
        self.objects=ObjectStore(settings.storage) if settings.storage else None
    def _sql(self,callback,*args):
        if self.loop is None: return callback(*args)
        return asyncio.run_coroutine_threadsafe(self.db.run(callback,*args),self.loop).result(timeout=2)
    def private_path(self,kind,object_id,suffix):
        if kind not in {'uploads','jobs'} or not ID.fullmatch(object_id) or suffix not in SUFFIXES: raise ValueError('Invalid scratch identifier')
        root=self.settings.data_root
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        for directory in (root/kind,root/kind/object_id):
            if directory.is_symlink(): raise OSError('Unsafe scratch')
            directory.mkdir(mode=0o700,exist_ok=True)
            if not stat.S_ISDIR(directory.lstat().st_mode): raise OSError('Unsafe scratch')
            directory.chmod(0o700)
        return root/kind/object_id/suffix
    def _locks(self,con,owner):
        global_row=con.execute("SELECT * FROM quota_scopes WHERE scope='global' FOR UPDATE").fetchone()
        con.execute('INSERT INTO quota_scopes(scope,owner_id) VALUES(%s,%s) ON CONFLICT(scope) DO NOTHING',(owner,owner))
        owner_row=con.execute('SELECT * FROM quota_scopes WHERE scope=%s FOR UPDATE',(owner,)).fetchone()
        if not global_row or not owner_row: raise RuntimeError('Quota unavailable')
        return global_row,owner_row
    def _row(self,con,owner,id,lock=False):
        if not isinstance(id,str) or not ID.fullmatch(id): raise ApiError('not_found','Resource is unavailable.',404)
        row=con.execute('SELECT * FROM uploads WHERE id=%s AND owner_id=%s'+(' FOR UPDATE' if lock else ''),(id,owner)).fetchone()
        if not row: raise ApiError('not_found','Resource is unavailable.',404)
        return row
    def reserve_upload(self,owner_id,kind,display_name,size,sha256,now):
        try:
            if kind not in {'zip-fbx','portable-package'} or not isinstance(display_name,str): raise ValueError
            name=unicodedata.normalize('NFC',display_name)
            if not 1<=len(name.encode('utf-8'))<=160 or any(unicodedata.category(c).startswith('C') for c in name): raise ValueError
            if isinstance(size,bool) or not isinstance(size,int) or size<1 or not isinstance(sha256,str) or not re.fullmatch('[a-f0-9]{64}',sha256): raise ValueError
        except (ValueError,TypeError,UnicodeError): raise ApiError('invalid_upload','Upload metadata is invalid.',422) from None
        if size>self.settings.upload_max_bytes: raise ApiError('upload_too_large','Upload exceeds the size limit.',413)
        self.settings.data_root.mkdir(mode=0o700,parents=True,exist_ok=True)
        free=shutil.disk_usage(self.settings.data_root).free  # CPU/OS outside transaction.
        id=uuid4().hex; epoch=uuid4().hex; expires=now+self.settings.unused_upload_seconds
        key=f'owners/{owner_id}/uploads/{id}/{epoch}/input.zip'
        with self.db.transaction() as con:
            global_row,owner=self._locks(con,owner_id)
            daily=con.execute("SELECT count(*) AS global_count,count(*) FILTER(WHERE owner_id=%s) AS owner_count FROM usage_events WHERE action='upload' AND timestamp>=%s",(owner_id,now//86400*86400)).fetchone()
            if daily['global_count']>=self.settings.accepted_global_day or daily['owner_count']>=self.settings.accepted_per_user_day:
                raise ApiError('upload_limited','Upload frequency limit reached.',429)
            if global_row['active_uploads']>=self.settings.uploads_global or owner['active_uploads']>=self.settings.uploads_per_user:
                raise ApiError('upload_busy','Another upload is still active.',429)
            scratch=con.execute('SELECT COALESCE(sum(reservation_bytes),0) AS bytes FROM uploads WHERE active_reserved').fetchone()['bytes']
            if (global_row['storage_bytes']+size>self.settings.storage_global_bytes or owner['storage_bytes']+size>self.settings.storage_per_user_bytes
                    or scratch+size>2*1024**3 or free-scratch-size<self.settings.min_free_disk_bytes):
                raise ApiError('storage_full','Private storage capacity is unavailable.',507)
            from .auth import consume_guest_acceptance
            consume_guest_acceptance(con,self.settings,owner_id,'upload',now)
            con.execute('INSERT INTO uploads(id,owner_id,input_kind,display_name,declared_bytes,sha256,state,reservation_bytes,writer_epoch,created_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s,\'receiving\',%s,%s,%s,%s)',(id,owner_id,kind,name,size,sha256,size,epoch,now,expires))
            con.execute("INSERT INTO object_intents VALUES(%s,%s,%s,%s,'reserved',NULL,%s)",(id,owner_id,epoch,key,size))
            con.execute('UPDATE quota_scopes SET storage_bytes=storage_bytes+%s,active_uploads=active_uploads+1 WHERE scope IN (\'global\',%s)',(size,owner_id))
            con.execute("INSERT INTO usage_events(action,owner_id,timestamp,bytes) VALUES('upload',%s,%s,%s)",(owner_id,now,size))
        return {'id':id,'state':'receiving','expiresAt':expires}
    def claim_content(self,owner_id,upload_id,writer_epoch=None):
        writer_epoch=writer_epoch or uuid4().hex
        if not ID.fullmatch(writer_epoch): raise ValueError("Invalid writer epoch")
        with self.db.transaction() as con:
            row=self._row(con,owner_id,upload_id,True)
            if row['state']!='receiving' or row['abort_requested'] or row['content_claimed'] or row['protocol'] not in (None,'single-put'): raise ApiError('upload_conflict','Upload cannot be received again.',409)
            if row['expires_at']<=int(self.clock()): raise ApiError('upload_expired','Upload has expired.',410)
            con.execute("UPDATE uploads SET protocol='single-put',content_claimed=TRUE,api_epoch=%s,writer_epoch=%s,writer_started_at=%s WHERE id=%s",(self.api_epoch,writer_epoch,int(time.time()),upload_id))
            # No object producer exists before this single-use claim. Bind the durable
            # intent and immutable key to this request's proof in the same COMMIT.
            key=f'owners/{owner_id}/uploads/{upload_id}/{writer_epoch}/input.zip'
            con.execute("UPDATE object_intents SET state='writing',attempt_epoch=%s,key=%s WHERE object_id=%s",(writer_epoch,key,upload_id))
            row['api_epoch']=self.api_epoch; row['writer_epoch']=writer_epoch
            return row
    def check_writer(self,owner,id,epoch,now=None):
        with self.db.connect() as con:
            row=self._row(con,owner,id)
        if row['state']!='receiving' or row['abort_requested'] or row['writer_closed'] or row['writer_epoch']!=epoch or row['api_epoch']!=self.api_epoch:
            raise ApiError('upload_aborted','Upload was cancelled.',409)
        if now is not None and row['expires_at']<=now: raise ApiError('upload_expired','Upload has expired.',410)
        return row
    def intent(self,owner,id):
        with self.db.connect() as con:
            self._row(con,owner,id)
            row=con.execute('SELECT * FROM object_intents WHERE object_id=%s AND owner_id=%s',(id,owner)).fetchone()
        return ObjectIntent(**row)
    def _persist_multipart(self,owner,id,epoch,multipart):
        self._deadline(id)
        with self.db.transaction() as con:
            row=self._row(con,owner,id,True)
            if row['state']!='receiving' or row['abort_requested'] or row['writer_epoch']!=epoch or row['api_epoch']!=self.api_epoch:
                raise ApiError('upload_aborted','Upload was cancelled.',409)
            con.execute('UPDATE object_intents SET multipart_id=%s WHERE object_id=%s AND attempt_epoch=%s',(multipart,id,epoch))
    def _deadline(self,id):
        if id in self.deadlines and time.monotonic()>=self.deadlines[id]:
            raise ApiError('upload_timeout','Upload timed out.',408)
    def finish_upload(self,owner_id,upload_id,received_bytes,sha256):
        intent=replace(self._sql(self.intent,owner_id,upload_id),deadline=self.deadlines.get(upload_id))
        row=self._sql(self.check_writer,owner_id,upload_id,intent.attempt_epoch)
        if received_bytes!=row['declared_bytes']: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
        if sha256!=row['sha256']: raise ApiError('upload_hash_mismatch','Upload hash does not match.',409)
        if self.objects is None: raise ApiError('storage_not_ready','Private storage is not configured.',503)
        descriptor=self.objects.put_file(intent,self.private_path('uploads',upload_id,'input.part'),lambda mp:self._sql(self._persist_multipart,owner_id,upload_id,intent.attempt_epoch,mp))
        return self._sql(self._commit_ready,owner_id,upload_id,intent,descriptor,received_bytes,sha256)
    def _commit_ready(self,owner_id,upload_id,intent,descriptor,received_bytes,sha256):
        self._deadline(upload_id)
        if descriptor.bytes!=received_bytes or descriptor.sha256!=sha256:
            raise ApiError('upload_hash_mismatch','Private upload verification failed.',409)
        with self.db.transaction() as con:
            self._locks(con,owner_id); row=self._row(con,owner_id,upload_id,True)
            if row['state']!='receiving' or row['abort_requested'] or row['writer_epoch']!=intent.attempt_epoch or row['api_epoch']!=self.api_epoch:
                raise ApiError('upload_aborted','Upload was cancelled.',409)
            if row['expires_at']<=int(self.clock()): raise ApiError('upload_expired','Upload has expired.',410)
            con.execute("UPDATE uploads SET state='ready',received_bytes=%s,object_key=%s,object_bytes=%s,object_sha256=%s,writer_closed=TRUE,active_reserved=FALSE WHERE id=%s",(received_bytes,descriptor.key,descriptor.bytes,descriptor.sha256,upload_id))
            con.execute("UPDATE object_intents SET state='complete',multipart_id=NULL WHERE object_id=%s",(upload_id,))
            con.execute('UPDATE quota_scopes SET active_uploads=active_uploads-1 WHERE scope IN (\'global\',%s)',(owner_id,))
        return {'id':upload_id,'state':'ready','bytes':received_bytes,'sha256':sha256}
    def request_abort(self,owner_id,upload_id,now):
        with self.db.transaction() as con:
            self._locks(con,owner_id); row=self._row(con,owner_id,upload_id,True)
            if row['state']=='consumed': raise ApiError('upload_consumed','Upload is already in use.',409)
            if row['state']=='deleted': return {'status':204,'writer_epoch':row['writer_epoch']}
            con.execute("UPDATE uploads SET state='deleting',abort_requested=TRUE WHERE id=%s",(upload_id,))
            con.execute("UPDATE object_intents SET state='deleting' WHERE object_id=%s",(upload_id,))
            active=row['content_claimed'] and not row['writer_closed']
        return {'status':202 if active else 204,'writer_epoch':row['writer_epoch']}
    def acknowledge_closed(self,owner_id,upload_id,writer_epoch,code=None):
        # Called only after the receiver has waited for ALL disk/S3 IO and closed its FD.
        proof=(self.api_epoch,owner_id,upload_id,writer_epoch)
        with self._closed_lock: self.closed_writers.add(proof)
        row=self._sql(self._mark_closed,owner_id,upload_id,writer_epoch,code)
        if row is None:
            with self._closed_lock: self.closed_writers.discard(proof)
            return
        intent=self._sql(self.intent,owner_id,upload_id)
        if self.objects is None: raise ApiError('storage_not_ready','Private storage is not configured.',503)
        if self.objects:
            self.objects.abort_multipart(intent)
            descriptor=self.objects.head(intent)
            if descriptor: self.objects.delete(descriptor)
        self.private_path('uploads',upload_id,'input.part').unlink(missing_ok=True)
        self._sql(self._release,owner_id,upload_id,writer_epoch)
        with self._closed_lock: self.closed_writers.discard(proof)
    def _mark_closed(self,owner_id,upload_id,writer_epoch,code):
        with self.db.transaction() as con:
            row=self._row(con,owner_id,upload_id,True)
            if row['state']=='deleted': return
            if row['state']=='consumed': raise ApiError('upload_consumed','Upload is already in use.',409)
            if row['writer_epoch']!=writer_epoch: raise ApiError('upload_conflict','Upload writer changed.',409)
            con.execute("UPDATE uploads SET state='deleting',abort_requested=TRUE,writer_closed=TRUE,failure_code=COALESCE(%s,failure_code) WHERE id=%s",(code,upload_id))
            con.execute("UPDATE object_intents SET state='deleting' WHERE object_id=%s",(upload_id,))
        return row
    def _release(self,owner_id,upload_id,writer_epoch):
        with self.db.transaction() as con:
            self._locks(con,owner_id); row=self._row(con,owner_id,upload_id,True)
            if row['state']=='deleted': return
            if row['writer_epoch']!=writer_epoch or not row['writer_closed']: raise ApiError('upload_conflict','Upload writer changed.',409)
            con.execute('UPDATE quota_scopes SET storage_bytes=storage_bytes-%s,active_uploads=active_uploads-%s WHERE scope IN (\'global\',%s)',(row['reservation_bytes'],int(row['active_reserved']),owner_id))
            con.execute("UPDATE uploads SET state='deleted',reservation_bytes=0,active_reserved=FALSE WHERE id=%s",(upload_id,))
            con.execute("UPDATE object_intents SET state='deleted',multipart_id=NULL,reserved_bytes=0 WHERE object_id=%s",(upload_id,))
    def delete_unused(self,owner_id,upload_id,now):
        result=self._sql(self.request_abort,owner_id,upload_id,now)
        if result['status']==204: self.acknowledge_closed(owner_id,upload_id,result['writer_epoch'])
        return result
    def sweep(self,now,*,api_lock_owned=False):
        # Only lifespan's held OS api.lock authorizes reclaim of previous-process writers.
        with self._closed_lock: closed_proofs=self.closed_writers.copy()
        closed_ids=[proof[2] for proof in closed_proofs]
        def inventory():
            with self.db.connect() as con:
                return con.execute("""SELECT id,owner_id,writer_epoch,api_epoch,writer_closed,content_claimed,state,expires_at
                    FROM uploads WHERE protocol IS DISTINCT FROM 'chunks-v1' AND state IN ('receiving','ready','deleting') AND
                    (state='deleting' OR expires_at<=%s OR id=ANY(%s) OR
                     (%s AND content_claimed AND NOT writer_closed AND api_epoch<>%s))
                    ORDER BY (state='deleting') DESC,created_at LIMIT 100""",(now,closed_ids,api_lock_owned,self.api_epoch)).fetchall()
        rows=self._sql(inventory)
        for proof in closed_proofs:
            def matches(proof=proof):
                with self.db.connect() as con:
                    return con.execute('SELECT 1 FROM uploads WHERE id=%s AND owner_id=%s AND writer_epoch=%s AND api_epoch=%s AND state<>\'deleted\'',(proof[2],proof[1],proof[3],proof[0])).fetchone()
            if not self._sql(matches):
                with self._closed_lock: self.closed_writers.discard(proof)
        for row in rows:
            abandoned=api_lock_owned and row['content_claimed'] and not row['writer_closed'] and row['api_epoch']!=self.api_epoch
            closed=(self.api_epoch,row['owner_id'],row['id'],row['writer_epoch']) in closed_proofs
            if abandoned or closed or row['state']=='deleting' or row['expires_at']<=now:
                self._sql(self.request_abort,row['owner_id'],row['id'],now)
                if abandoned or closed or row['writer_closed'] or not row['content_claimed']:
                    self.acknowledge_closed(row['owner_id'],row['id'],row['writer_epoch'],'upload_interrupted' if abandoned else None)
