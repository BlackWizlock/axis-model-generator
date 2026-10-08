"""Explicit private multipart S3 operations through the configured proxy only."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import errno
import ipaddress
import json
import selectors
import subprocess
import sys
import re
import socket
import threading
import time
import boto3
from botocore.awsrequest import AWSHTTPConnection,AWSHTTPSConnection,AWSHTTPConnectionPool,AWSHTTPSConnectionPool
from botocore.httpsession import URLLib3Session
from botocore.config import Config
from botocore.exceptions import ClientError,BotoCoreError,ReadTimeoutError
from .config import StorageSettings
from .security import ApiError

@dataclass(frozen=True)
class ObjectIntent:
    owner_id: str
    object_id: str
    attempt_epoch: str
    key: str
    state: str
    multipart_id: str | None
    reserved_bytes: int
    deadline: float | None = None  # Process-local monotonic bound, never persisted.

@dataclass(frozen=True)
class ObjectDescriptor:
    key: str
    bytes: int
    sha256: str
    content_type: str

class FilePart(io.RawIOBase):
    """Seekable file window. SDK hashing and socket writes read at most 64 KiB."""
    def __init__(self,file,start,length): self.file=file; self.start=start; self.length=length; self.position=0
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self,offset,whence=0):
        value=offset if whence==0 else self.position+offset if whence==1 else self.length+offset
        if not 0<=value<=self.length: raise ValueError('Invalid part offset')
        self.position=value; return value
    def read(self,size=-1):
        size=min(65536,self.length-self.position,size if size>=0 else 65536)
        self.file.seek(self.start+self.position); chunk=self.file.read(size); self.position+=len(chunk); return chunk

class _SocketBudget:
    """Expire the owned socket, never terminate its Python producer thread."""
    def __init__(self,deadline):
        self.deadline=deadline; self.lock=threading.Lock(); self.sockets=set(); self.connections=set(); self.resolvers=set()
        self.expired=threading.Event()
        self.timer=threading.Timer(max(0,deadline-time.monotonic()),self.abort)
        self.timer.name="mg-s3-deadline-"+str(id(self)); self.timer.daemon=True; self.timer.start()
    def check(self):
        if self.expired.is_set() or time.monotonic()>=self.deadline: raise TimeoutError
    def register(self,connection,sock=None):
        with self.lock:
            self.connections.add(connection)
            if sock is not None: self.sockets.add(sock)
        if self.expired.is_set() or time.monotonic()>=self.deadline:
            self.abort(); raise TimeoutError
    def register_resolver(self,process):
        with self.lock: self.resolvers.add(process)
        if self.expired.is_set():
            try: process.kill()
            except ProcessLookupError: pass
    def abort(self):
        self.expired.set()
        with self.lock:
            resolvers=self.resolvers.copy()
            sockets=self.sockets.copy()
            sockets.update(con.sock for con in self.connections if con.sock is not None)
        for process in resolvers:
            try: process.kill()
            except ProcessLookupError: pass
        for sock in sockets:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: sock.close()
            except OSError: pass
    def close(self):
        self.timer.cancel(); self.timer.join()
        if self.expired.is_set():
            for connection in self.connections: connection.close()

def _resolve(host,port,budget,deadline):
    # libc resolution has no cancellation API. Isolate only that call in an owned,
    # killable child, with no credentials/environment protocol and bounded output.
    try:
        address=ipaddress.ip_address(host)
        return [(socket.AF_INET6 if address.version==6 else socket.AF_INET,socket.SOCK_STREAM,0,(host,port))]
    except ValueError: pass
    program="import json,socket,sys; print(json.dumps(socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),0,socket.SOCK_STREAM)[:16]))"
    budget.check()
    process=subprocess.Popen([sys.executable,'-I','-c',program,host,str(port)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True)
    budget.register_resolver(process)
    try:
        output,_=process.communicate(timeout=max(.001,deadline-time.monotonic()))
        budget.check()
        if process.returncode or len(output)>65536: raise OSError('Private proxy resolution failed')
        return [(family,kind,proto,tuple(address)) for family,kind,proto,_,address in json.loads(output)]
    except subprocess.TimeoutExpired: raise TimeoutError from None
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=.1)  # Physical child exit precedes producer completion.
        process.stdout.close()
        with budget.lock: budget.resolvers.discard(process)

def _connect(connection,budget):
    # One connect budget includes DNS and every address, leaving time to reap DNS.
    deadline=min(budget.deadline-.05,time.monotonic()+float(connection.timeout)-.05)
    for family,kind,proto,address in _resolve(connection._dns_host,connection.port,budget,deadline):
        budget.check()
        if time.monotonic()>=deadline: raise TimeoutError
        sock=socket.socket(family,kind,proto)
        try:
            budget.register(connection,sock)  # Before bind, connect or TLS can block.
            for option in connection.socket_options or (): sock.setsockopt(*option)
            if connection.source_address: sock.bind(connection.source_address)
            sock.setblocking(False)
            result=sock.connect_ex(address)
            if result not in (0,errno.EINPROGRESS,errno.EWOULDBLOCK,errno.EALREADY): raise OSError(result,'Private proxy connection failed')
            if result:
                with selectors.DefaultSelector() as selector:
                    selector.register(sock,selectors.EVENT_WRITE)
                    while True:
                        budget.check(); remaining=deadline-time.monotonic()
                        if remaining<=0: raise TimeoutError
                        if selector.select(min(.02,remaining)): break
                result=sock.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
                if result: raise OSError(result,'Private proxy connection failed')
            sock.settimeout(connection.timeout)
            return sock
        except (OSError,TimeoutError,ValueError):
            sock.close()
            if time.monotonic()>=deadline: raise TimeoutError from None
    raise OSError('Private proxy connection failed')

class _BudgetBody:
    """Keep streaming sockets out of the pool until their timer has been joined."""
    def __init__(self,raw,budget):
        self.raw=raw; self.budget=budget; self.closed=False
        self.release=raw.release_conn
        raw.release_conn=lambda:None
    def __getattr__(self,name): return getattr(self.raw,name)
    def read(self,*args,**kwargs):
        self.budget.check()
        result=self.raw.read(*args,**kwargs)
        self.budget.check()
        return result
    def close(self):
        if self.closed: return
        self.closed=True
        try: self.budget.close()
        finally:
            try: self.raw.close()
            finally: self.release()

class DeadlineTransport(URLLib3Session):
    """Pinned botocore transport with an absolute, owned-socket deadline.

    A pool connection belongs to one send at a time. Per-thread budgets therefore
    stop only that send, including response header/body trickles and proxy traffic.
    Timer cancellation/join completes before a buffered send returns, or before
    the streaming body closes and its connection becomes reusable.
    """
    def __init__(self,**kwargs):
        self.local=threading.local()
        transport=self
        class Tracked:
            def request(self,*args,**kwargs):
                transport.local.budget.register(self,self.sock)
                return super().request(*args,**kwargs)
            def _new_conn(self):
                return _connect(self,transport.local.budget)
            def connect(self):
                transport.local.budget.check()
                super().connect()
                transport.local.budget.register(self,self.sock)
        class HTTPConnection(Tracked,AWSHTTPConnection): pass
        class HTTPSConnection(Tracked,AWSHTTPSConnection): pass
        class HTTPPool(AWSHTTPConnectionPool): ConnectionCls=HTTPConnection
        class HTTPSPool(AWSHTTPSConnectionPool): ConnectionCls=HTTPSConnection
        super().__init__(**kwargs)
        self._pool_classes_by_scheme={'http':HTTPPool,'https':HTTPSPool}
        self._manager.pool_classes_by_scheme=self._pool_classes_by_scheme
    @contextmanager
    def operation(self,deadline):
        previous=getattr(self.local,'deadline',None); self.local.deadline=deadline
        try: yield
        finally: self.local.deadline=previous
    def send(self,request):
        deadline=min(time.monotonic()+30,getattr(self.local,'deadline',None) or float('inf'))
        budget=_SocketBudget(deadline); self.local.budget=budget; streaming=False
        try:
            budget.check(); result=super().send(request); budget.check()
            if request.stream_output:
                result.raw=_BudgetBody(result.raw,budget); streaming=True
            return result
        except TimeoutError:
            raise ReadTimeoutError(endpoint_url=request.url) from None
        finally:
            if not streaming: budget.close()
            self.local.budget=None

class ObjectStore:
    def __init__(self,settings: StorageSettings):
        settings.validate(); self.settings=settings
        self.client=boto3.client('s3',endpoint_url=settings.endpoint,region_name=settings.region,
            aws_access_key_id=settings.access_key_file.read_text().strip(),aws_secret_access_key=settings.secret_key_file.read_text().strip(),
            config=Config(signature_version='s3v4',proxies={'http':settings.proxy_url,'https':settings.proxy_url},
                connect_timeout=1,read_timeout=2,retries={'mode':'standard','total_max_attempts':1},
                max_pool_connections=2,s3={'addressing_style':'path'},request_checksum_calculation='when_required',response_checksum_validation='when_required'))
        # Explicitly pinned internal extension; no fallback to an unbounded transport.
        if boto3.__version__!='1.43.108': raise RuntimeError('Unsupported S3 transport version')
        self.transport=DeadlineTransport(proxies={'http':settings.proxy_url,'https':settings.proxy_url},timeout=(1,2),max_pool_connections=2)
        self.client._endpoint.http_session.close()
        self.client._endpoint.http_session=self.transport
    def _key(self,key):
        if not re.fullmatch(r'owners/[a-f0-9]{32}/(?:uploads|jobs)/[a-f0-9]{32}/[a-f0-9]{32}/(?:input\.zip|report\.json|preview\.json|preview-input\.json|thumbnail\.png|measurements\.json)',key):
            raise ValueError('Invalid private object key')
        return {'Bucket':self.settings.bucket,'Key':key}
    def put_file(self,intent,path,persist_multipart):
        deadline=min(time.monotonic()+600,intent.deadline or float('inf'))
        try:
            with self.transport.operation(deadline):
                return self._put_file(intent,path,persist_multipart)
        except (BotoCoreError,OSError,TimeoutError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None
    def _put_file(self,intent,path,persist_multipart):
        args=self._key(intent.key); started=time.monotonic(); total=0; sha=hashlib.sha256()
        # SHA outside DB and bounded independently of SDK multipart checksums.
        with path.open('rb') as file:
            while chunk:=file.read(65536):
                total+=len(chunk); sha.update(chunk)
                if total>intent.reserved_bytes or time.monotonic()-started>600: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
        if total!=intent.reserved_bytes: raise ApiError('upload_size_mismatch','Upload size does not match.',409)
        digest=sha.hexdigest()
        try:
            content_type='image/png' if intent.key.endswith('/thumbnail.png') else 'application/zip' if intent.key.endswith('/input.zip') else 'application/json'
            multipart=self.client.create_multipart_upload(**args,ContentType=content_type,Metadata={'sha256':digest})['UploadId']
            persist_multipart(multipart)  # Durable ID before any bytes, callback also fences abort.
            parts=[]
            with path.open('rb') as file:
                for index,start in enumerate(range(0,total,8*1024**2),1):
                    if time.monotonic()-started>600: raise TimeoutError
                    persist_multipart(multipart)
                    size=min(8*1024**2,total-start)
                    response=self.client.upload_part(**args,UploadId=multipart,PartNumber=index,Body=FilePart(file,start,size),ContentLength=size)
                    parts.append({'PartNumber':index,'ETag':response['ETag']})
            persist_multipart(multipart)  # Abort wins before CompleteMultipartUpload too.
            self.client.complete_multipart_upload(**args,UploadId=multipart,MultipartUpload={'Parts':parts},IfNoneMatch='*')
            descriptor=self.head(intent)
            if not descriptor or descriptor.bytes!=total or descriptor.sha256!=digest: raise RuntimeError('Private object verification failed')
            return descriptor
        except (BotoCoreError,ClientError,OSError,TimeoutError,RuntimeError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None
    def ensure_multipart(self,intent,sha256):
        """Recover only the stable attempt's MPU identity, never its part ledger."""
        try:
            with self.transport.operation(intent.deadline):
                args=self._key(intent.key)
                result=self.client.list_multipart_uploads(Bucket=self.settings.bucket,Prefix=intent.key,MaxUploads=100)
                if result.get('IsTruncated'): raise RuntimeError
                ids=[item['UploadId'] for item in result.get('Uploads',[]) if item['Key']==intent.key]
                if len(ids)>1: raise RuntimeError
                if ids: return ids[0]
                return self.client.create_multipart_upload(**args,ContentType='application/zip',Metadata={'sha256':sha256})['UploadId']
        except (ClientError,BotoCoreError,OSError,TimeoutError,RuntimeError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None

    def upload_chunk(self,intent,number,path):
        try:
            with self.transport.operation(intent.deadline),path.open('rb') as file:
                size=path.stat().st_size
                if not 1<=size<=8*1024**2: raise RuntimeError
                response=self.client.upload_part(**self._key(intent.key),UploadId=intent.multipart_id,
                    PartNumber=number,Body=FilePart(file,0,size),ContentLength=size)
                etag=response['ETag']
                if not isinstance(etag,str) or not 1<=len(etag.encode())<=256: raise RuntimeError
                return etag
        except (ClientError,BotoCoreError,OSError,TimeoutError,RuntimeError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None

    def complete_chunks(self,intent,parts):
        try:
            with self.transport.operation(intent.deadline):
                # HEAD only selects recovery; it is never the integrity proof.
                if self.head(intent) is not None: return
                self.client.complete_multipart_upload(**self._key(intent.key),UploadId=intent.multipart_id,
                    MultipartUpload={'Parts':parts},IfNoneMatch='*')
        except (ClientError,BotoCoreError,OSError,TimeoutError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None

    def verify_ranges(self,intent,size,expected_sha,check):
        digest=hashlib.sha256(); total=0
        try:
            with self.transport.operation(intent.deadline):
                for start in range(0,size,8*1024**2):
                    check()
                    if time.monotonic()>=intent.deadline: raise TimeoutError
                    end=min(start+8*1024**2,size)-1; stream=None
                    try:
                        response=self.client.get_object(**self._key(intent.key),Range=f'bytes={start}-{end}')
                        stream=response['Body']
                        if (response['ResponseMetadata']['HTTPStatusCode']!=206 or
                                response.get('ContentRange')!=f'bytes {start}-{end}/{size}' or
                                response.get('ContentLength')!=end-start+1):
                            raise ApiError('upload_size_mismatch','Stored input size does not match.',409)
                        count=0
                        while chunk:=stream.read(65536):
                            if time.monotonic()>=intent.deadline: raise TimeoutError
                            count+=len(chunk)
                            if count>end-start+1: raise ApiError('upload_size_mismatch','Stored input size does not match.',409)
                            digest.update(chunk)
                        if count!=end-start+1: raise ApiError('upload_size_mismatch','Stored input size does not match.',409)
                        total+=count
                    finally:
                        if stream is not None: stream.close()
                if total!=size or digest.hexdigest()!=expected_sha:
                    raise ApiError('upload_hash_mismatch','Stored input hash does not match.',409)
                return ObjectDescriptor(intent.key,total,digest.hexdigest(),'application/zip')
        except (ClientError,BotoCoreError,OSError,TimeoutError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None

    def head(self,intent):
        try:
            response=self.client.head_object(**self._key(intent.key))
            checksums=[value for name,value in response.get('Metadata',{}).items() if name.lower()=='sha256']
            sha=checksums[0] if len(checksums)==1 else ''
            if not re.fullmatch('[a-f0-9]{64}',sha): raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503)
            return ObjectDescriptor(intent.key,response['ContentLength'],sha,response.get('ContentType','application/octet-stream'))
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode']==404: return None
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None
        except (BotoCoreError,OSError): raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None
    def abort_multipart(self,intent):
        args=self._key(intent.key)
        try:
            # Includes crash after CreateMultipartUpload before durable ID. No bucket-wide scan.
            uploads=self.client.list_multipart_uploads(Bucket=self.settings.bucket,Prefix=intent.key,MaxUploads=100)
            if uploads.get('IsTruncated'): raise ApiError('storage_unavailable','Private storage cleanup is incomplete.',503)
            ids={item['UploadId'] for item in uploads.get('Uploads',[]) if item['Key']==intent.key}
            if intent.multipart_id: ids.add(intent.multipart_id)
            for id in ids:
                try: self.client.abort_multipart_upload(**args,UploadId=id)
                except ClientError as error:
                    if error.response['Error']['Code']!='NoSuchUpload': raise
            after=self.client.list_multipart_uploads(Bucket=self.settings.bucket,Prefix=intent.key,MaxUploads=100)
            if after.get('IsTruncated') or any(item['Key']==intent.key for item in after.get('Uploads',[])):
                raise ApiError('storage_unavailable','Private storage cleanup is incomplete.',503)
        except (BotoCoreError,ClientError,OSError): raise ApiError('storage_unavailable','Private storage cleanup is incomplete.',503) from None
    def delete(self,descriptor):
        try:
            self.client.delete_object(**self._key(descriptor.key))
            try: self.client.head_object(**self._key(descriptor.key))
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode']==404: return
                raise
            raise ApiError('storage_unavailable','Private storage cleanup is incomplete.',503)
        except (BotoCoreError,ClientError,OSError): raise ApiError('storage_unavailable','Private storage cleanup is incomplete.',503) from None
    @contextmanager
    def open_stream(self,descriptor):
        stream=None
        try:
            response=self.client.get_object(**self._key(descriptor.key)); stream=response['Body']
            yield stream
        except (ClientError,BotoCoreError,OSError,TimeoutError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None
        finally:
            if stream is not None: stream.close()
    def probe(self):
        """A bucket exists and private read permission is usable; no listing."""
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
            intent=ObjectIntent('0'*32,'0'*32,'0'*32,'owners/'+('0'*32)+'/jobs/'+('0'*32)+'/'+('0'*32)+'/report.json','complete',None,0)
            self.head(intent)
        except (ClientError,BotoCoreError,OSError,TimeoutError):
            raise ApiError('storage_unavailable','Private storage is temporarily unavailable.',503) from None

    def presign_get(self,descriptor,expires_seconds):
        if isinstance(expires_seconds,bool) or not 1<=expires_seconds<=300: raise ValueError('Invalid private link lifetime')
        return self.client.generate_presigned_url('get_object',Params=self._key(descriptor.key),ExpiresIn=expires_seconds)
