import {test} from 'vitest';
import assert from 'node:assert/strict';
import {api, setCsrfToken, ApiError, artifactPath} from '../src/api.js';
test('API same origin credentials and CSRF only on mutations', async () => {
  const calls = []; const prior = globalThis.fetch;
  globalThis.fetch = async (path, options) => {calls.push({path, options}); return new Response('{}', {headers: {'Content-Type': 'application/json'}});};
  try {setCsrfToken('test-token'); await api('/api/jobs'); await api('/api/jobs', {method:'POST', body:{uploadId:'abc'}});
    assert.equal(calls[0].options.credentials,'same-origin'); assert.equal(calls[0].options.headers.get('X-CSRF-Token'),null);
    assert.equal(calls[1].options.headers.get('X-CSRF-Token'),'test-token');
    await assert.rejects(api('https://evil.example/api'), ApiError);
  } finally {globalThis.fetch=prior; setCsrfToken(null);}
});
for (const status of [401,403,429,507]) test(`safe ${status} error hides sender text`, async () => {
  const prior=globalThis.fetch; globalThis.fetch=async()=>new Response(JSON.stringify({error:{code:'bad',message:'<script>/private/path secret</script>'}}),{status,headers:{'Content-Type':'application/json'}});
  try {await assert.rejects(api('/api/jobs'), error => error.status===status && !error.message.includes('private'));} finally {globalThis.fetch=prior;}
});
test('unexpected content type and bounded JSON fail closed', async () => {
  const prior=globalThis.fetch;
  try {globalThis.fetch=async()=>new Response('<html/>',{headers:{'Content-Type':'text/html'}}); await assert.rejects(api('/api/jobs'),ApiError);
    globalThis.fetch=async()=>new Response(' '.repeat(257),{headers:{'Content-Type':'application/json'}}); await assert.rejects(api('/api/jobs',{maxBytes:256}),ApiError);
  } finally {globalThis.fetch=prior;}
});
test('artifact links accept opaque IDs only',()=>{
  assert.equal(artifactPath('a'.repeat(32),'b'.repeat(32)),`/api/jobs/${'a'.repeat(32)}/artifacts/${'b'.repeat(32)}`);
  assert.throws(()=>artifactPath('../secret','b'.repeat(32)),ApiError);
});
test('opaque pagination cursor query stays same-origin',async()=>{
 const prior=globalThis.fetch;let called;
 globalThis.fetch=async path=>{called=path;return new Response('{}',{headers:{'Content-Type':'application/json'}});};
 try{await api('/api/jobs?limit=50&cursor=abc%3D');assert.equal(called,'/api/jobs?limit=50&cursor=abc%3D');}finally{globalThis.fetch=prior;}
});
test('guest bootstrap requires no prior CSRF and keeps returned token only in memory',async()=>{
 const prior=globalThis.fetch;let headers;setCsrfToken(null);
 globalThis.fetch=async(path,options)=>{headers=options.headers;return new Response(JSON.stringify({id:'a'.repeat(32),csrfToken:'guest-csrf'}),{headers:{'Content-Type':'application/json'}});};
 try{const {guestSession}=await import('../src/auth.js');const user=await guestSession();assert.equal(user.csrfToken,'guest-csrf');assert.equal(headers.get('X-CSRF-Token'),null);assert.equal(headers.get('Accept'),'application/json');}finally{globalThis.fetch=prior;setCsrfToken(null);}
});

test('transport interruption and timeout explain connectivity and uncertain server acceptance',async()=>{
 const priorFetch=globalThis.fetch,priorTimeout=AbortSignal.timeout;
 try{
  globalThis.fetch=async()=>{throw new TypeError('network disconnected');};
  await assert.rejects(api('/api/jobs'),error=>error.code==='request_network'&&/сеть/.test(error.message)&&/Обновите/.test(error.message));
  const controller=new AbortController();controller.abort();AbortSignal.timeout=()=>controller.signal;
  await assert.rejects(api('/api/jobs'),error=>error.code==='request_timeout'&&error.status===408&&/ожидания/.test(error.message)&&/проверьте/.test(error.message));
  assert.match(new ApiError('upload_network').message,/сеть/);
 }finally{globalThis.fetch=priorFetch;AbortSignal.timeout=priorTimeout;}
});
