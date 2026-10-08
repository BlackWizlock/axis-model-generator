"""Bounded asynchronous local error journal, independent of database health.

Callers enqueue safe structures only; disk locks, writes and fsync never run in
cancellation/lease/reap loops. A failed durable write is explicitly observable.
"""
from collections import deque
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time
from .diagnostic_catalog import ERROR_CODES

SLOTS=4
SLOT_BYTES=262144
RECORD_BYTES=4096
QUEUE_RECORDS=128
FALLBACK_RECORDS=64
SAFE_ID=re.compile('[a-f0-9]{32}\\Z')
SAFE_NAME=re.compile('[A-Za-z_][A-Za-z0-9_]{0,79}\\Z')
STAGES={'request','stream','startup','cleanup','finalize','reconcile','validation','preview','worker','progress'}
EXCEPTIONS={'ApiError','RuntimeError','ValueError','OSError','TimeoutError','MemoryError','ConnectionError',
            'OperationalError','InterfaceError','DatabaseError','PermissionError','FileNotFoundError','ExceptionGroup','CancelledError','ClientDisconnect'}


def correlation(kind, identifier, attempt=''):
    if kind not in {'job','upload'} or not SAFE_ID.fullmatch(identifier): raise ValueError('Invalid correlation input')
    if attempt and not SAFE_ID.fullmatch(attempt): raise ValueError('Invalid correlation input')
    return hashlib.sha256((kind+':'+identifier+':'+attempt).encode()).hexdigest()[:32]


def safe_record(component, stage, code, request_id, error=None, job_id=None, attempt=None):
    if component not in {'api','worker'} or stage not in STAGES or not SAFE_ID.fullmatch(request_id): raise ValueError('Invalid journal event')
    record={'timestamp':int(time.time()),'severity':'error','component':component,'stage':stage,
            'code':code if code in ERROR_CODES else 'internal_error','requestId':request_id}
    for name,value in [('jobId',job_id),('attempt',attempt)]:
        if value is not None and SAFE_ID.fullmatch(value): record[name]=value
    if error is not None:
        name=type(error).__name__
        record['exceptionType']=name if name in EXCEPTIONS else 'Exception'
        frames=[]; frame=error.__traceback__
        # No exception messages, chain, source text, filenames, values or locals.
        for _ in range(32):
            if frame is None: break
            module=frame.tb_frame.f_globals.get('__name__','')
            function=frame.tb_frame.f_code.co_name
            if module.startswith('model_generator.') and len(module)<=100 and all(SAFE_NAME.fullmatch(part) for part in module.split('.')) and SAFE_NAME.fullmatch(function):
                frames.append({'module':module,'function':function,'line':min(frame.tb_lineno,1000000)})
            frame=frame.tb_next
        record['frames']=frames[-8:]
    wire=json.dumps(record,separators=(',',':')).encode()+b'\n'
    if len(wire)>RECORD_BYTES: raise ValueError('Journal record exceeds budget')
    return wire


class Journal:
    def __init__(self, root, component):
        if component not in {'api','worker'}: raise ValueError('Unknown component')
        self.root=Path(root); self.component=component
        self.queue=queue.Queue(QUEUE_RECORDS); self.fallback=deque(maxlen=FALLBACK_RECORDS)
        self.dropped=0; self.failed=0; self.written=0; self.available=True
        self.stopping=threading.Event(); self.closed=False
        self.thread=threading.Thread(target=self._run,name='mg-journal-'+component,daemon=True)
        self.thread.start()

    def emit(self, stage, code, request_id, error=None, job_id=None, attempt=None):
        wire=safe_record(self.component,stage,code,request_id,error,job_id,attempt)
        try:
            if self.closed: raise queue.Full
            self.queue.put_nowait(wire)
            return True
        except queue.Full:
            self.dropped+=1; self.available=False; self.fallback.append(wire)
            return False

    def health(self):
        return {'available':self.available,'pending':self.queue.qsize(),'written':self.written,
                'failed':self.failed,'dropped':self.dropped,'fallbackRecords':len(self.fallback)}

    def _run(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try: wire=self.queue.get(timeout=.05)
            except queue.Empty: continue
            try:
                self._append(wire); self.written+=1; self.available=True
            except (OSError,ValueError):
                self.failed+=1; self.available=False; self.fallback.append(wire)
            finally: self.queue.task_done()

    def _append(self,wire):
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        lock=None
        try:
            info=os.fstat(directory)
            if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700: raise ValueError('Unsafe journal directory')
            lock=os.open(self.component+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
            self._regular(lock)
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            candidates=[]
            for slot in range(SLOTS):
                name=f'{self.component}.{slot}.jsonl'
                try: item=os.stat(name,dir_fd=directory,follow_symlinks=False)
                except FileNotFoundError: continue
                if not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or item.st_uid!=os.geteuid() or stat.S_IMODE(item.st_mode)!=0o600 or item.st_size>SLOT_BYTES: raise ValueError('Unsafe journal slot')
                candidates.append((item.st_mtime_ns,slot,item.st_size))
            _,slot,size=max(candidates,default=(0,0,0))
            rotate=size+len(wire)>SLOT_BYTES
            if rotate: slot=(slot+1)%SLOTS
            fd=os.open(f'{self.component}.{slot}.jsonl',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
            try:
                self._regular(fd)
                if rotate: os.ftruncate(fd,0)
                end=os.lseek(fd,0,os.SEEK_END)
                if end:
                    os.lseek(fd,max(0,end-RECORD_BYTES),os.SEEK_SET)
                    tail=os.read(fd,RECORD_BYTES)
                    if not tail.endswith(b'\n'):
                        last=tail.rfind(b'\n')
                        os.ftruncate(fd,max(0,end-len(tail)+last+1))
                end=os.lseek(fd,0,os.SEEK_END)
                try:
                    if os.write(fd,wire)!=len(wire): raise OSError('Short journal write')
                except OSError:
                    os.ftruncate(fd,end)
                    raise
                os.fsync(fd)
            finally: os.close(fd)
            os.fsync(directory)
        finally:
            if lock is not None: os.close(lock)
            os.close(directory)

    @staticmethod
    def _regular(fd):
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600: raise ValueError('Unsafe journal file')

    def close(self):
        self.closed=True; self.stopping.set()
        # No durability promise on shutdown if storage hangs. Daemon cannot hold
        # process lifetime or the physical worker lease; health reports pending.
        self.thread.join(.2)


def emit(journal, stage, code, request_id, error=None, **identifiers):
    if journal is not None:
        return journal.emit(stage,code,request_id,error,**identifiers)
    return False
