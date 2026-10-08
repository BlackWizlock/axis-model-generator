import {test} from 'vitest';
import assert from 'node:assert/strict';
import {uploadFile,CHUNK_BYTES,validateUpload} from '../src/upload.js';
import {completionText,validateChecks} from '../src/checks.js';
const id='a'.repeat(32),hash='b'.repeat(64);
const row=(size,ack=0,state='receiving')=>({id,descriptorVersion:1,state,sha256:hash,kind:'portable-package',totalBytes:size,acknowledgedBytes:ack,chunkBytes:CHUNK_BYTES,nextPart:ack<size?ack/CHUNK_BYTES+1:null});
function mocks({size=CHUNK_BYTES+3,cancel=false,network=false,pending=false,candidate={},reservedVersion=1}={}) {
 const old={fetch:globalThis.fetch,Worker:globalThis.Worker,XMLHttpRequest:globalThis.XMLHttpRequest};
 const calls=[],progress=[];let ack=pending?CHUNK_BYTES:0;const controller=new AbortController();
 globalThis.Worker=class{postMessage(){queueMicrotask(()=>this.onmessage({data:{type:'done',digest:hash}}));}terminate(){}};
 globalThis.fetch=async(path,options)=>{
  calls.push([options.method,path,options.body]);
  let value={};if(path==='/api/uploads')value=options.method==='GET'?{uploads:pending?[{...row(size,ack),...candidate}]:[]}:{id,state:'receiving',descriptorVersion:reservedVersion};
  else if(path.endsWith('/complete'))value=row(size,size,'ready');
  else value=row(size,ack);
  return new Response(options.method==='DELETE'?null:JSON.stringify(value),{status:options.method==='DELETE'?204:200,headers:{'Content-Type':'application/json'}});
 };
 globalThis.XMLHttpRequest=class{
  constructor(){this.upload={};this.headers={};}open(method,path){calls.push([method,path]);}setRequestHeader(key,value){this.headers[key]=value;}getResponseHeader(){return'application/json';}
  send(blob){assert.equal(this.withCredentials,true);assert.equal(this.headers['X-CSRF-Token'],'csrf');assert.match(this.headers['Upload-Chunk-Sha256'],/^[a-f0-9]{64}$/);assert.ok(blob.size<=CHUNK_BYTES);
   if(cancel){controller.abort();return;}if(network){this.onerror();return;}
   this.upload.onprogress({loaded:blob.size});ack+=blob.size;this.status=200;this.responseText=JSON.stringify(row(size,ack));this.onload();}
  abort(){this.onabort?.();}
 };
 const file=new Blob([new Uint8Array(size)]);file.name='synthetic.zip';
 return {calls,progress,controller,file,restore:()=>Object.assign(globalThis,old)};
}
test('actual distinct HTTP chunks acknowledged before complete',async()=>{
 const m=mocks();try{const result=await uploadFile(m.file,'portable-package','csrf',p=>m.progress.push(p),m.controller.signal);assert.equal(result.state,'ready');
 assert.deepEqual(m.calls.filter(c=>c[0]==='PUT').map(c=>c[1]),[`/api/uploads/${id}/chunks/1`,`/api/uploads/${id}/chunks/2`]);assert.ok(m.progress.some(p=>p.phase==='transport'&&p.acknowledged===0));assert.ok(m.progress.some(p=>p.phase==='ack'&&p.done===CHUNK_BYTES));assert.ok(m.calls.at(-1)[1].endsWith('/complete'));}finally{m.restore();}
});
test('resume only identical hash size and kind without new reservation',async()=>{
 const m=mocks({pending:true});try{await uploadFile(m.file,'portable-package','csrf',()=>{},m.controller.signal);assert.equal(m.calls.filter(c=>c[0]==='PUT').length,1);assert.equal(m.calls.filter(c=>c[1]==='/api/uploads'&&c[0]==='POST').length,0);}finally{m.restore();}
});
test('cancel deletes only reserved ID and network failure preserves parts',async()=>{
 for(const mode of ['cancel','network']){const m=mocks({size:3,[mode]:true});try{await assert.rejects(uploadFile(m.file,'portable-package','csrf',()=>{},m.controller.signal));assert.equal(m.calls.some(c=>c[0]==='DELETE'&&c[1]===`/api/uploads/${id}`),mode==='cancel');}finally{m.restore();}}
});
test('different identity and impossible acknowledgement rejected',()=>{
 assert.throws(()=>validateUpload(row(3),{sha256:'c'.repeat(64),kind:'portable-package',bytes:3,descriptorVersion:1}));assert.throws(()=>validateUpload(row(CHUNK_BYTES+3,3),{sha256:hash,kind:'portable-package',bytes:CHUNK_BYTES+3,descriptorVersion:1}));
});
test('checklist fenced by source job and attempt, completion keeps defect visible',()=>{
 const job={id,inputHash:hash,attempt:'attempt1',state:'completed'};const checks={jobId:id,inputHash:hash,attempt:'attempt1',checks:[{id:'package.geometry',title:'Geometry',state:'failed'}],findings:{shown:1,original:1,omitted:0}};
 assert.equal(validateChecks(checks,job),checks);assert.throws(()=>validateChecks({...checks,attempt:'old'},job));assert.throws(()=>validateChecks({...checks,inputHash:'other'},job));assert.match(completionText(job,checks),/найдены ошибки/);assert.doesNotMatch(completionText(job,checks),/Все проверки пройдены/);
});

for(const change of [{descriptorVersion:0},{descriptorVersion:undefined},{sha256:'c'.repeat(64)},{totalBytes:CHUNK_BYTES+4},{kind:'zip-fbx'}]) test(`new ZIP is v1 and never resumes mismatched identity ${JSON.stringify(change)}`,async()=>{
 const oldId='d'.repeat(32);
  const m=mocks({pending:true,candidate:{id:oldId,...change}});
  try{
   await uploadFile(m.file,'portable-package','csrf',()=>{},m.controller.signal);
   const reserve=m.calls.find(call=>call[0]==='POST'&&call[1]==='/api/uploads');
   assert.equal(JSON.parse(reserve[2]).descriptorVersion,1);
   assert.equal(m.calls.some(call=>call[0]==='PUT'&&call[1].includes(oldId)),false);
  }finally{m.restore();}
});
test('upload DTO accepts absent legacy version only against v0 and rejects invalid versions',()=>{
 const source={sha256:hash,kind:'portable-package',bytes:3,descriptorVersion:1};
 for(const descriptorVersion of [undefined,0,2,-1,'1',null,true,1.5]){
  assert.throws(()=>validateUpload({...row(3),descriptorVersion},source),error=>error.code==='invalid_response');
 }
 assert.equal(validateUpload(row(3),source).descriptorVersion,1);
 assert.ok(validateUpload({...row(3),descriptorVersion:undefined},{...source,descriptorVersion:0}));
});
test('invalid pending descriptor version fails closed before reservation or transfer',async()=>{
 const m=mocks({pending:true,candidate:{descriptorVersion:2}});
 try{await assert.rejects(uploadFile(m.file,'portable-package','csrf',()=>{},m.controller.signal),error=>error.code==='invalid_response');assert.equal(m.calls.some(c=>c[0]==='POST'||c[0]==='PUT'),false);}finally{m.restore();}
});
test('preserved cancellation keeps confirmed parts for versioned resume',async()=>{
 const m=mocks({pending:true,cancel:true});
 try{await assert.rejects(uploadFile(m.file,'portable-package','csrf',()=>{},m.controller.signal,{preserveOnAbort:true}),{name:'AbortError'});assert.equal(m.calls.some(c=>c[0]==='DELETE'),false);}finally{m.restore();}
});

for(const reservedVersion of [0,2,null,'1']) test(`invalid reservation version rejects before chunk transfer ${String(reservedVersion)}`,async()=>{
 const m=mocks({reservedVersion});
 try{await assert.rejects(uploadFile(m.file,'portable-package','csrf',()=>{},m.controller.signal),error=>error.code==='invalid_response');assert.equal(m.calls.some(call=>call[0]==='PUT'),false);}finally{m.restore();}
});
