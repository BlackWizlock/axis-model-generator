"""Credential-free bounded observations, independently checked by the worker."""
import json
import os
import re
import stat
import time
from .diagnostic_catalog import KNOWN_CHECKS, PHASE_TITLES

MAX_BYTES=131072
MAX_ROWS=256
MAX_SEQUENCE=10000
HASH=re.compile('[a-f0-9]{64}\\Z')
ID=re.compile('[a-f0-9]{32}\\Z')
STATES={'waiting','checking','passed','failed','warning','not_checked'}
STATUS={'pass':'passed','fail':'failed','warn':'warning','not_checked':'not_checked'}
RANK={'passed':0,'not_checked':1,'warning':2,'failed':3}


def aggregate(findings):
    result={}
    for finding in findings:
        code=finding.rule_id if hasattr(finding,'rule_id') else finding.get('rule_id')
        status=finding.status if hasattr(finding,'status') else finding.get('status')
        if code not in KNOWN_CHECKS or status not in STATUS: continue
        row=result.setdefault(code,{'state':'passed','count':0})
        row['state']=max(row['state'],STATUS[status],key=RANK.get)
        row['count']=min(10000000,row['count']+1)
    return result


def validate_snapshot(value, source_hash, attempt):
    if type(value) is not dict or set(value)!={'schema','inputHash','attempt','sequence','checks'}:
        raise ValueError('progress_invalid')
    if value['schema']!=1 or type(value['schema']) is not int or value['inputHash']!=source_hash or value['attempt']!=attempt:
        raise ValueError('progress_invalid')
    if not HASH.fullmatch(source_hash) or not ID.fullmatch(attempt): raise ValueError('progress_invalid')
    sequence=value['sequence']; rows=value['checks']
    if type(sequence) is not int or not 1<=sequence<=MAX_SEQUENCE or type(rows) is not list or not 1<=len(rows)<=MAX_ROWS:
        raise ValueError('progress_invalid')
    seen=set()
    for row in rows:
        if type(row) is not dict or set(row)!={'id','state','count','sequence','completedAt'}: raise ValueError('progress_invalid')
        if type(row['id']) is not str or row['id'] not in KNOWN_CHECKS or row['id'] in seen: raise ValueError('progress_invalid')
        seen.add(row['id'])
        if type(row['state']) is not str or row['state'] not in STATES: raise ValueError('progress_invalid')
        if type(row['sequence']) is not int or not 1<=row['sequence']<=sequence: raise ValueError('progress_invalid')
        if type(row['count']) is not int or not 0<=row['count']<=10000000: raise ValueError('progress_invalid')
        stamp=row['completedAt']
        if row['state'] in {'checking','waiting'}:
            if stamp is not None: raise ValueError('progress_invalid')
        elif type(stamp) is not int or not 0<=stamp<=4102444800: raise ValueError('progress_invalid')
    return value


class Observer:
    def __init__(self, scratch, source_hash, attempt):
        self.scratch=scratch; self.source_hash=source_hash; self.attempt=attempt
        self.rows={}; self.sequence=0

    def __call__(self, code, state, findings=()):
        if code not in PHASE_TITLES or state not in {'checking','passed','failed','not_checked'}: raise ValueError('progress_invalid')
        data=aggregate(findings)
        if state!='checking' and data:
            state=max([state,*[row['state'] for row in data.values()]],key=RANK.get)
        self._update({code:{'state':state,'count':sum(row['count'] for row in data.values())}})

    def finish(self, findings, incomplete=False):
        values=aggregate(findings)
        if incomplete:
            for code,row in values.items():
                if code.startswith(('fbx.','png.','profile.')) and row['state']=='passed': row['state']='not_checked'
        self._update(values)

    def _update(self, changes):
        if not changes: return
        self.sequence+=1
        for code,row in changes.items():
            prior=self.rows.get(code)
            if prior and prior['state']=='failed': row={**row,'state':'failed'}
            self.rows[code]={'id':code,**row,'sequence':self.sequence,
                            'completedAt':None if row['state']=='checking' else int(time.time())}
        value=self.snapshot()
        wire=json.dumps(value,separators=(',',':'),sort_keys=True).encode()
        if len(wire)>MAX_BYTES: raise ValueError('progress_invalid')
        fd=os.open(self.scratch/'progress.tmp',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb') as stream: stream.write(wire)
            os.replace(self.scratch/'progress.tmp',self.scratch/'progress.json')
        finally: (self.scratch/'progress.tmp').unlink(missing_ok=True)

    def snapshot(self):
        return validate_snapshot({'schema':1,'inputHash':self.source_hash,'attempt':self.attempt,
                                  'sequence':self.sequence,'checks':list(self.rows.values())},self.source_hash,self.attempt)


class ProgressReader:
    def __init__(self, directory_fd, source_hash, attempt):
        self.directory_fd=directory_fd; self.source_hash=source_hash; self.attempt=attempt
        self.sequence=0; self.last=None

    def read(self):
        try: fd=os.open('progress.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.directory_fd)
        except FileNotFoundError: return None
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid() or info.st_nlink!=1 or not 1<=info.st_size<=MAX_BYTES:
                raise ValueError('progress_invalid')
            wire=os.read(fd,MAX_BYTES+1)
            if len(wire)!=info.st_size or len(wire)>MAX_BYTES: raise ValueError('progress_invalid')
            def unique(pairs):
                result={}
                for key,value in pairs:
                    if key in result: raise ValueError('progress_invalid')
                    result[key]=value
                return result
            value=json.loads(wire,object_pairs_hook=unique)
            validate_snapshot(value,self.source_hash,self.attempt)
            if value['sequence']<self.sequence or (value['sequence']==self.sequence and value!=self.last): raise ValueError('progress_invalid')
            if value['sequence']==self.sequence: return None
            if self.last is not None:
                previous={row['id']:row for row in self.last['checks']}
                current={row['id']:row for row in value['checks']}
                if not previous.keys()<=current.keys(): raise ValueError('progress_invalid')
                for code,old in previous.items():
                    new=current[code]
                    if new['sequence']<old['sequence'] or (new['sequence']==old['sequence'] and new!=old): raise ValueError('progress_invalid')
                    if old['state']=='failed' and new['state']!='failed': raise ValueError('progress_invalid')
                    if old['state'] not in {'waiting','checking'} and new['state'] in {'waiting','checking'}: raise ValueError('progress_invalid')
            self.sequence=value['sequence']; self.last=value
            return value
        except (RecursionError,UnicodeError,TypeError): raise ValueError('progress_invalid') from None
        finally: os.close(fd)


def persist_progress(db, job, value, now):
    """Must use the exact physical worker lease session for SQL and COMMIT."""
    from psycopg.types.json import Jsonb
    if getattr(db,'_worker_connection',None) is None: raise RuntimeError('worker_lease_lost')
    validate_snapshot(value,job['input_sha256'],job['worker_epoch'])
    with db.transaction() as con:
        row=con.execute("SELECT id FROM jobs WHERE id=%s AND worker_epoch=%s AND state='running' AND NOT cancel_requested AND expires_at>%s AND deadline_at>%s FOR UPDATE",(job['id'],job['worker_epoch'],now,now)).fetchone()
        if not row: raise RuntimeError('progress_fenced')
        con.execute("INSERT INTO job_progress(job_id,attempt,source_sha256,sequence,snapshot,observed_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET attempt=excluded.attempt,source_sha256=excluded.source_sha256,sequence=excluded.sequence,snapshot=excluded.snapshot,observed_at=excluded.observed_at WHERE job_progress.attempt<>excluded.attempt OR job_progress.sequence<excluded.sequence",
                    (job['id'],job['worker_epoch'],job['input_sha256'],value['sequence'],Jsonb(value),now))
