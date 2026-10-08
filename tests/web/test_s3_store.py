"""Real private S3 emulator checks; no cloud resources."""
import unittest
try:
    from model_generator.web.s3_store import ObjectStore,ObjectIntent
except ModuleNotFoundError:
    ObjectStore=ObjectIntent=None

class S3ContractTests(unittest.TestCase):
    def test_only_fixed_input_transport_names_are_allowed(self):
        from types import SimpleNamespace
        store=object.__new__(ObjectStore); store.settings=SimpleNamespace(bucket='model-generator-test')
        prefix='owners/'+('1'*32)+'/uploads/'+('2'*32)+'/'+('3'*32)+'/'
        for basename in ('input.zip','input.bin'):
            self.assertEqual(store._key(prefix+basename)['Key'],prefix+basename)
        for basename in ('model.rvt','../input.bin','input.bin/other','input.BIN'):
            with self.subTest(basename=basename),self.assertRaises(ValueError):
                store._key(prefix+basename)

    def test_pinned_explicit_multipart(self):
        self.assertIsNotNone(ObjectStore,'Private ObjectStore missing')
        import boto3
        self.assertEqual(boto3.__version__,'1.43.108')
        self.assertTrue(callable(ObjectStore.put_file))
        from types import SimpleNamespace
        from model_generator.web.security import ApiError
        store=object.__new__(ObjectStore); store.settings=SimpleNamespace(bucket='model-generator-test')
        intent=SimpleNamespace(key='owners/'+('1'*32)+'/uploads/'+('2'*32)+'/'+('3'*32)+'/input.zip')
        for metadata in ({'sha256':'a'*64},{'Sha256':'a'*64},{'SHA256':'a'*64}):
            with self.subTest(metadata_name=next(iter(metadata))):
                store.client=SimpleNamespace(head_object=lambda **kw:{'Metadata':metadata,'ContentLength':1})
                self.assertEqual(store.head(intent).sha256,'a'*64)
        for metadata in ({},{'Sha256':'invalid'},{'sha256':'a'*64,'Sha256':'b'*64}):
            with self.subTest(invalid_metadata=tuple(metadata)):
                store.client=SimpleNamespace(head_object=lambda **kw:{'Metadata':metadata,'ContentLength':1})
                with self.assertRaises(ApiError):store.head(intent)


import hashlib
from pathlib import Path
import tempfile
from dataclasses import replace
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx
from model_generator.web.config import StorageSettings

class RealS3Tests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ObjectStore,'Private ObjectStore missing')
        self.settings=StorageSettings.from_env(); self.store=ObjectStore(self.settings)
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.path=Path(self.tmp.name)/'input.zip'
        self.path.write_bytes(b'x'*(8*1024**2+31))
        self.intent=ObjectIntent('1'*32,__import__('uuid').uuid4().hex,__import__('uuid').uuid4().hex,'','writing',None,self.path.stat().st_size)
        self.intent=replace(self.intent,key=f'owners/{self.intent.owner_id}/uploads/{self.intent.object_id}/{self.intent.attempt_epoch}/input.zip')
    def tearDown(self):
        self.store.abort_multipart(self.intent)
        descriptor=self.store.head(self.intent)
        if descriptor: self.store.delete(descriptor)
    def test_version_one_full_and_chunk_upload_content_type(self):
        import time
        self.path.write_bytes(b'x')
        self.intent=replace(self.intent,key=self.intent.key.removesuffix('input.zip')+'input.bin',reserved_bytes=1,deadline=time.monotonic()+30)
        sha=hashlib.sha256(b'x').hexdigest()
        descriptor=self.store.put_file(self.intent,self.path,lambda id:None)
        self.assertEqual(descriptor.content_type,'application/octet-stream')
        self.store.delete(descriptor)
        mp=self.store.ensure_multipart(self.intent,sha)
        self.intent=replace(self.intent,multipart_id=mp)
        etag=self.store.upload_chunk(self.intent,1,self.path)
        self.store.complete_chunks(self.intent,[{'PartNumber':1,'ETag':etag}])
        verified=self.store.verify_ranges(self.intent,1,sha,lambda:None)
        self.assertEqual(verified.content_type,'application/octet-stream')
        self.assertEqual(self.store.head(self.intent).content_type,'application/octet-stream')

    def test_explicit_multipart_head_hash_private_anonymous_refusal(self):
        ids=[]
        descriptor=self.store.put_file(self.intent,self.path,ids.append)
        self.assertTrue(ids)
        self.assertEqual(descriptor.bytes,self.path.stat().st_size)
        self.assertEqual(descriptor.sha256,hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.store.head(self.intent),descriptor)
        with self.store.open_stream(descriptor) as file:
            self.assertEqual(hashlib.file_digest(file,'sha256').hexdigest(),descriptor.sha256)
        with httpx.Client(proxy=self.settings.proxy_url,trust_env=False) as anonymous:
            self.assertEqual(anonymous.get(self.settings.endpoint+'/'+self.settings.bucket+'/'+descriptor.key).status_code,403)
            self.assertEqual(anonymous.get(self.settings.endpoint+'/'+self.settings.bucket+'?list-type=2').status_code,403)
            self.assertEqual(anonymous.get('http://example.com/').status_code,403)
            self.assertEqual(anonymous.get(self.store.presign_get(descriptor,30)).status_code,200)
        self.store.delete(descriptor); self.assertIsNone(self.store.head(self.intent))
    def test_valid_other_owner_sts_policy_denies_get_and_list(self):
        descriptor=self.store.put_file(self.intent,self.path,lambda id: None)
        policy=__import__('json').dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetObject'],'Resource':[f'arn:aws:s3:::{self.settings.bucket}/owners/{"2"*32}/*']}]})
        credentials=boto3.client('sts',endpoint_url=self.settings.endpoint,region_name=self.settings.region,
            aws_access_key_id=self.settings.access_key_file.read_text().strip(),aws_secret_access_key=self.settings.secret_key_file.read_text().strip(),
            config=Config(proxies={'http':self.settings.proxy_url},retries={'total_max_attempts':1})).assume_role(
                RoleArn='arn:aws:iam::123456789012:role/nonowner',RoleSessionName='nonowner',Policy=policy)['Credentials']
        other=boto3.client('s3',endpoint_url=self.settings.endpoint,region_name=self.settings.region,
            aws_access_key_id=credentials['AccessKeyId'],aws_secret_access_key=credentials['SecretAccessKey'],aws_session_token=credentials['SessionToken'],
            config=Config(proxies={'http':self.settings.proxy_url},s3={'addressing_style':'path'},retries={'total_max_attempts':1}))
        for fn,kw in ((other.get_object,{'Key':descriptor.key}),(other.list_objects_v2,{})):
            with self.assertRaises(ClientError) as denied: fn(Bucket=self.settings.bucket,**kw)
            self.assertEqual(denied.exception.response['ResponseMetadata']['HTTPStatusCode'],403)
    def test_crash_before_multipart_persistence_sweeps_exact_key(self):
        def crashed(id): raise SystemExit('Synthetic process boundary')
        with self.assertRaises(SystemExit): self.store.put_file(self.intent,self.path,crashed)
        listed=self.store.client.list_multipart_uploads(Bucket=self.settings.bucket,Prefix=self.intent.key)
        self.assertTrue(listed.get('Uploads'))
        self.store.abort_multipart(self.intent); self.store.abort_multipart(self.intent)
        self.assertFalse(self.store.client.list_multipart_uploads(Bucket=self.settings.bucket,Prefix=self.intent.key).get('Uploads'))
    def test_completed_key_is_immutable(self):
        descriptor=self.store.put_file(self.intent,self.path,lambda id:None)
        self.path.write_bytes(b'y'*self.path.stat().st_size)
        from model_generator.web.security import ApiError
        with self.assertRaises(ApiError): self.store.put_file(self.intent,self.path,lambda id:None)
        self.assertEqual(self.store.head(self.intent),descriptor)

    def test_actual_s3_response_trickle_stops_at_absolute_deadline(self):
        import time
        from model_generator.web.security import ApiError
        server,thread=real_s3_trickle_proxy()
        store=ObjectStore(replace(self.settings,proxy_url=f'http://127.0.0.1:{server.server_port}'))
        self.path.write_bytes(b'x'); intent=replace(self.intent,reserved_bytes=1)
        object.__setattr__(intent,'deadline',time.monotonic()+.2)
        started=time.monotonic()
        try:
            with self.assertRaises(ApiError): store.put_file(intent,self.path,lambda id:None)
            self.assertLess(time.monotonic()-started,1.5)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_streamed_get_trickle_keeps_absolute_deadline_until_body_close(self):
        import time
        from model_generator.web.security import ApiError
        from botocore.exceptions import BotoCoreError
        self.path.write_bytes(b'x'*512)
        descriptor=self.store.put_file(replace(self.intent,reserved_bytes=512),self.path,lambda id:None)
        server,thread=real_s3_trickle_proxy(slow_methods=('GET',))
        store=ObjectStore(replace(self.settings,proxy_url=f'http://127.0.0.1:{server.server_port}'))
        started=time.monotonic(); stream=None
        try:
            with self.assertRaises((ApiError,BotoCoreError,OSError,TimeoutError)):
                with store.transport.operation(started+.2),store.open_stream(descriptor) as stream:
                    stream.read()
            self.assertLess(time.monotonic()-started,1.0)
            self.assertTrue(stream._raw_stream.closed)
            self.assertFalse(any(t.name.startswith('mg-s3-deadline-') for t in __import__('threading').enumerate()))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


def real_s3_trickle_proxy(slow_methods=('POST',)):
    import http.client,threading,time
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Trickle(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def forward(self):
            connection=http.client.HTTPConnection('s3-proxy',8080,timeout=2)
            try:
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                connection.request(self.command,self.path,body,dict(self.headers))
                response=connection.getresponse(); data=response.read(8192)
                assert len(data)<8192
                self.send_response(response.status)
                for name,value in response.getheaders():
                    if name.lower() not in {'connection','transfer-encoding'}: self.send_header(name,value)
                self.send_header('Connection','close'); self.end_headers()
                if self.command in slow_methods:
                    for byte in data:
                        self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.01)
                else: self.wfile.write(data)
            except OSError: pass
            finally: connection.close(); self.close_connection=True
        do_POST=do_GET=do_HEAD=do_DELETE=forward
    server=ThreadingHTTPServer(('127.0.0.1',0),Trickle)
    thread=threading.Thread(target=server.serve_forever); thread.start()
    return server,thread

from contextlib import contextmanager
@contextmanager
def real_dns_fault(mode):
    """Native libc DNS against own UDP fixture; real TCP backlog drops SYNs."""
    import socket,threading,struct
    from types import SimpleNamespace
    hosts={name:socket.gethostbyname(name) for name in ('postgres','s3-proxy')}
    original=Path('/etc/resolv.conf').read_bytes()
    dns=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); dns.bind(('127.0.0.1',53)); dns.settimeout(.05)
    listener=socket.socket(); listener.bind(('0.0.0.0',0)); listener.listen(1)
    fillers=[]; queried=threading.Event(); stop=threading.Event()
    if mode=='addresses':
        for _ in range(2): fillers.append(socket.create_connection(('127.0.0.1',listener.getsockname()[1]),timeout=.3))
    name='stalled-dns.invalid' if mode=='dns' else 'many-addresses.invalid'
    def serve():
        while not stop.is_set():
            try: packet,peer=dns.recvfrom(4096)
            except socket.timeout: continue
            except OSError: break
            pos=12; labels=[]
            while packet[pos]:
                length=packet[pos]; labels.append(packet[pos+1:pos+1+length].decode()); pos+=length+1
            pos+=1; qtype=struct.unpack('!H',packet[pos:pos+2])[0]; end=pos+4; host='.'.join(labels)
            if host==name:
                queried.set()
                if mode=='dns': continue
                addresses=['127.0.0.'+str(i) for i in range(1,17)]
            else: addresses=[hosts[host]] if host in hosts else []
            if qtype!=1: addresses=[]
            header=packet[:2]+struct.pack('!HHHHH',0x8180,1,len(addresses),0,0)
            records=b''.join(b'\xc0\x0c'+struct.pack('!HHIH',1,1,0,4)+socket.inet_aton(ip) for ip in addresses)
            dns.sendto(header+packet[12:end]+records,peer)
    thread=threading.Thread(target=serve); thread.start()
    try:
        Path('/etc/resolv.conf').write_text('nameserver 127.0.0.1\noptions timeout:2 attempts:2\n')
        yield SimpleNamespace(name=name,port=listener.getsockname()[1],queried=queried)
    finally:
        Path('/etc/resolv.conf').write_bytes(original)
        stop.set(); dns.close(); thread.join(2)
        for peer in fillers: peer.close()
        listener.close()
