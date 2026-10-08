import base64,hashlib,http.cookies,json,sys,time,urllib.error,urllib.request
ORIGIN='https://testserver';BASE='http://localhost:8000'
def request(path,method='GET',data=None,cookie=None,csrf=None,headers=None):
 h={'Host':'testserver','Origin':ORIGIN}
 if cookie:h['Cookie']=cookie
 if csrf:h['X-CSRF-Token']=csrf
 if headers:h.update(headers)
 if isinstance(data,dict):data=json.dumps(data).encode();h['Content-Type']='application/json'
 elif data is not None:h['Content-Type']='application/octet-stream'
 try:
  with urllib.request.urlopen(urllib.request.Request(BASE+path,data=data,method=method,headers=h),timeout=30) as r:return r.status,r.read()
 except urllib.error.HTTPError as e:return e.code,e.read()
p=json.load(sys.stdin);cookie=p['proof']['ownerCookie']; neighbour=p['proof']['neighbourCookie']
s,b=request('/api/auth/me',cookie=cookie);assert s==200;csrf=json.loads(b)['csrfToken']
if p['mode']=='seed':
 data=base64.b64decode(p['fixture']);uploads=[]
 for i in range(3):
  active_cookie=neighbour if i==2 else cookie
  s,b=request('/api/auth/me',cookie=active_cookie);assert s==200;active_csrf=json.loads(b)['csrfToken']
  s,b=request('/api/uploads','POST',{'kind':'portable-package','displayName':'active-recovery.zip','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},active_cookie,active_csrf);assert s==201,(s,b)
  id=json.loads(b)['id']
  if i==0:
   s,b=request('/api/uploads/'+id+'/content','PUT',data,active_cookie,active_csrf);assert s==200,(s,b)
   s,b=request('/api/jobs','POST',{'uploadId':id,'region':'moscow','procedure':'diagnostic','submissionDate':'2026-10-07'},active_cookie,active_csrf);assert s==201,(s,b);job=json.loads(b)['id']
  else:
   s,b=request('/api/uploads/'+id+'/chunks/1','PUT',data,active_cookie,active_csrf,{'Upload-Offset':'0','Upload-Chunk-Sha256':hashlib.sha256(data).hexdigest()});assert s==200,(s,b);uploads.append(id)
 print(json.dumps({'job':job,'receiving':uploads[0],'finalizing':uploads[1],'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}))
else:
 a=p['active'];deadline=time.monotonic()+90
 while time.monotonic()<deadline:
  s,b=request('/api/jobs/'+a['job'],cookie=cookie);assert s==200;(job:=json.loads(b))
  s,b=request('/api/uploads/'+a['finalizing'],cookie=neighbour);assert s==200;(upload:=json.loads(b))
  if job['state']=='completed' and upload['state']=='ready':break
  assert job['state'] not in ('failed','cancelled'),job;time.sleep(.5)
 assert job['state']=='completed' and upload['state']=='ready',(job,upload)
 s,b=request('/api/uploads/'+a['receiving'],cookie=cookie);assert s==200 and json.loads(b)['acknowledgedBytes']==a['bytes'];assert request('/api/uploads/'+a['receiving'],cookie=neighbour)[0]==404
 s,b=request('/api/uploads/'+a['receiving']+'/complete','POST',cookie=cookie,csrf=csrf);assert s==202,(s,b)
 deadline=time.monotonic()+30
 while time.monotonic()<deadline:
  s,b=request('/api/uploads/'+a['receiving'],cookie=cookie);row=json.loads(b)
  if row['state']=='ready':break
  time.sleep(.5)
 assert row['state']=='ready' and row['sha256']==a['sha256'],row
 print('Active queued job, finalizing upload and acknowledged resumable upload restored; owner isolation passed')
