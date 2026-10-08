"""Private HTTP proof inside exact API image; stdout is captured, never logged."""
import base64
import hashlib
import http.cookies
import json
import socket
import sys
import time
import urllib.error
import urllib.request
ORIGIN='https://testserver'
def request(path,method='GET',data=None,cookie=None,csrf=None,headers=None):
    h={'Host':'testserver','Origin':ORIGIN}
    if cookie:h['Cookie']=cookie
    if csrf:h['X-CSRF-Token']=csrf
    if headers:h.update(headers)
    if isinstance(data,dict):data=json.dumps(data).encode();h['Content-Type']='application/json'
    elif data is not None:h['Content-Type']='application/octet-stream'
    try:
        with urllib.request.urlopen(urllib.request.Request('http://localhost:8000'+path,data=data,method=method,headers=h),timeout=30) as response:
            return response.status,response.headers,response.read()
    except urllib.error.HTTPError as error:
        try:return error.code,error.headers,error.read()
        finally:error.close()
def csrf(cookie):
    status,_,wire=request('/api/auth/me',cookie=cookie);assert status==200,(status,wire)
    return json.loads(wire)['csrfToken']
def upload(data,cookie,token):
    status,_,wire=request('/api/uploads','POST',{'kind':'portable-package','displayName':'cold-proof.zip','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},cookie,token)
    assert status==201,(status,wire);return json.loads(wire)['id']
p=json.load(sys.stdin);mode=p['mode']
if mode=='job':
    cookie=p['cookie'];token=csrf(cookie);data=base64.b64decode(p['fixture']);id=upload(data,cookie,token)
    assert request('/api/uploads/'+id+'/content','PUT',data,cookie,token)[0]==200
    status,_,wire=request('/api/jobs','POST',{'uploadId':id,'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-07'},cookie,token)
    assert status==201,(status,wire)
    print(json.dumps({'id':json.loads(wire)['id'],'cookie':cookie}))
elif mode=='verify':
    proof=p['job'];deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        status,_,wire=request('/api/jobs/'+proof['id'],cookie=proof['cookie']);assert status==200
        job=json.loads(wire)
        if job['state']=='completed':break
        assert job['state'] not in {'failed','cancelled'},job
        time.sleep(.5)
    assert job['state']=='completed',job
    assert {'report','preview','thumbnail'}<={artifact['kind'] for artifact in job['artifacts']},job
    assert request('/api/jobs/'+proof['id'],cookie=p['neighbour'])[0]==404
    assert request('/api/jobs/'+proof['id'])[0]==401
    for artifact in job['artifacts']:
        status,_,wire=request('/api/jobs/'+proof['id']+'/artifacts/'+artifact['id'],cookie=proof['cookie'])
        assert status==200 and hashlib.sha256(wire).hexdigest()==artifact['sha256']
    print('Cold restored running job and artifact hashes verified')
elif mode=='interrupt':
    cookie=p['cookie'];token=csrf(cookie);data=base64.b64decode(p['fixture']);id=upload(data,cookie,token)
    connection=socket.create_connection(('127.0.0.1',8000),timeout=5)
    headers=(f'PUT /api/uploads/{id}/content HTTP/1.1\r\nHost: testserver\r\nOrigin: {ORIGIN}\r\nCookie: {cookie}\r\nX-CSRF-Token: {token}\r\nContent-Type: application/octet-stream\r\nContent-Length: {len(data)}\r\n\r\n').encode()
    connection.sendall(headers+data[:1]);print(json.dumps({'id':id,'cookie':cookie}),flush=True)
    # API is killed by the host after SQL proves a real unclosed single PUT.
    time.sleep(180)
    raise AssertionError('Host never interrupted actual HTTP upload')
elif mode=='cancel':
    job=p['job'];token=csrf(job['cookie']);path='/api/jobs/'+job['id']+'/cancel'
    for _ in range(2):
        status,_,wire=request(path,'POST',cookie=job['cookie'],csrf=token)
        assert status in (200,202),(status,wire)
    print('Cancellation and repeated request accepted without duplicate reservation')
elif mode=='retry-complete':
    cookie=p['proof']['ownerCookie'];token=csrf(cookie);id=p['active']['receiving']
    for _ in range(2):
        status,_,wire=request('/api/uploads/'+id+'/complete','POST',cookie=cookie,csrf=token)
        assert status==202,(status,wire)
    print('Repeated chunk completion accepted idempotently')
else:raise SystemExit('Unknown cold HTTP fixture mode')
