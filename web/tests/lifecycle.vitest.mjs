import {test} from 'vitest';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const settle=()=>new Promise(resolve=>setImmediate(resolve));
import {renderChecks} from '../src/checks.js';
import {trackGoal,watchDiagnostic,observeDiagnostic} from '../src/analytics.js';
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.attributes={};this.dataset={};this.events={};this.files=[];this.hidden=false;this.disabled=false;this.value='';this.classList={toggle(){},add(){},remove(){}};}
 set textContent(value){this.text=value;this.children=value?[{textContent:value}]:[];}
 get textContent(){return this.text||'';}
 append(...children){this.children.push(...children);}replaceChildren(...children){this.text='';this.children=children;}
 get childNodes(){return this.children;}querySelectorAll(){return [];}
 setAttribute(k,v){this.attributes[k]=v;}getAttribute(k){return this.attributes[k];}removeAttribute(k){delete this.attributes[k];}
 addEventListener(k,fn){this.events[k]=fn;}click(){return this.events.click?.();}scrollIntoView(){}
}
async function appHarness(overrides={}) {
 const nodes={},events={},pollers=[];let disposed=0;
 const get=id=>nodes[id]||=new Element();
 const context={console,Date,JSON,Set,Math,AbortController,trackGoal,watchDiagnostic,observeDiagnostic,document:{hidden:false,getElementById:get,createElement:tag=>new Element(tag)},window:{addEventListener:(name,fn)=>events[name]=fn},setInterval:()=>1,clearInterval(){},
 api:async path=>{if(path==='/api/config')return {sourceLink:'https://github.com/BlackWizlock/axis-model-generator'};if(path==='/api/auth/me')return {csrfToken:'token'};return {provenance:'synthetic'};},
 ApiError:class extends Error{},artifactPath:()=>'/artifact',setCsrfToken(){},guestSession:async()=>{},UPLOAD_CAP:1000,uploadStageText:x=>x,
 getJobs:async()=>({jobs:[],nextCursor:null}),getJob:async id=>({id,state:'completed',inputHash:'b'.repeat(64)}),createJob:async()=>({id:'a'.repeat(32)}),cancelJob(){},deleteJob(){},
 startPolling:()=>{const poller={stopped:false,stop(){this.stopped=true;},refresh:async()=>{}};pollers.push(poller);return poller;},statusText:x=>x,terminal:new Set(['completed']),jobAxes:()=>[],jobArtifacts:()=>[],
 getChecks:async()=>({attempt:1,checks:[],coverage:{}}),renderChecks(){},completionText:()=>'',runSourceLabel:()=> 'Источник',mountPreview:()=>({dispose(){disposed++;},reset(){}}),previewReason:()=>'',uploadFile:async()=>({id:'upload'}),...overrides};
 vm.createContext(context);const source=await readFile(new URL('../src/app.js',import.meta.url),'utf8');vm.runInContext(source.replace(/^import .*;$/gm,''),context);await settle();
 return {nodes,get,events,pollers,context,disposed:()=>disposed};
}
test('BFCache restores polling and the disposed demonstration viewer',async()=>{
 const app=await appHarness();await app.get('demo-button').click();assert.equal(app.get('reset-view').disabled,false);
 app.events.pagehide();assert.equal(app.pollers[0].stopped,true);assert.equal(app.disposed(),1);
 await app.events.pageshow?.({persisted:true});await settle();
 assert.equal(app.pollers.length,2);assert.equal(app.pollers[1].stopped,false);assert.equal(app.get('reset-view').disabled,false);
 assert.equal(vm.runInContext('state.limits.preserveOnAbort',app.context),false);
});
test('new upload clears the previous source and hides cancellation during job creation',async()=>{
 let resolveJob;const jobPending=new Promise(resolve=>resolveJob=resolve);const app=await appHarness({createJob:()=>jobPending});
 await app.get('demo-button').click();app.get('report-panel').hidden=false;app.get('report-download').href='/old-report';app.get('report-download').setAttribute('href','/old-report');
 app.get('input-file').files=[{name:'model.zip',size:10}];const pending=app.get('upload-form').events.submit({preventDefault(){}});await settle();
 try{assert.equal(app.disposed(),1);assert.equal(app.get('report-panel').hidden,true);assert.equal(app.get('report-download').getAttribute('href'),undefined);assert.doesNotMatch(app.get('preview-provenance').textContent,/ДЕМОНСТРАЦИЯ/);assert.match(app.get('viewer').textContent,/Новый пакет/);assert.equal(app.get('upload-cancel').hidden,true);assert.match(app.get('upload-message').textContent,/Создаём задание/);}
 finally{resolveJob({id:'a'.repeat(32)});await pending;}
});
test('failed check detail fetch retries when reopened and caches successful detail',async()=>{
 const priorDocument=globalThis.document,priorFetch=globalThis.fetch;let calls=0;
 const envelope={jobId:'a'.repeat(32),inputHash:'b'.repeat(64),authoritative:true,findings:{shown:0,original:0,omitted:0},checks:[{id:'scene.meshes',title:'Сетки',state:'warning'}]};
 globalThis.document={createElement:tag=>new Element(tag)};globalThis.fetch=async()=>{calls++;if(calls===1)throw new TypeError('offline');return new Response(JSON.stringify({...envelope,title:'Сетки',why:'Пояснение',nextAction:'Исправление',findings:[],nextOffset:null}),{headers:{'Content-Type':'application/json'}});};
 try{const container=new Element();renderChecks(container,envelope,()=>true);const [button,detail]=container.children.at(-1).children;await button.click();assert.match(detail.textContent,/соедин|сеть|Сеть|Попробуйте/);await button.click();await button.click();assert.equal(calls,2);assert.ok(detail.children.some(child=>child.textContent==='Пояснение'));await button.click();await button.click();assert.equal(calls,2);}finally{globalThis.document=priorDocument;globalThis.fetch=priorFetch;}
});

test('interrupted upload retains last server acknowledgement until next attempt',async()=>{
 const app=await appHarness({uploadFile:async(file,kind,csrf,progress)=>{progress({phase:'ack',done:5,total:10,acknowledged:5});throw new Error('offline');}});
 app.get('input-file').files=[{name:'model.zip',size:10}];await app.get('upload-form').events.submit({preventDefault(){}});
 assert.match(app.get('upload-heartbeat').textContent,/последний ответ сервера/);assert.match(app.get('upload-heartbeat').textContent,/с назад/);
});

test('BFCache reopens the selected private job without duplicating initial polling',async()=>{
 let reads=0;const app=await appHarness({getJob:async id=>{reads++;return {id,state:'completed',inputHash:'b'.repeat(64)};}});
 await app.events.pageshow({persisted:false});assert.equal(app.pollers.length,1);
 await vm.runInContext("openJob('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',{scroll:false})",app.context);assert.equal(reads,1);
 app.events.pagehide();await app.events.pageshow({persisted:true});assert.equal(reads,2);assert.equal(app.pollers.length,2);assert.equal(app.get('report-panel').hidden,false);assert.match(app.get('preview-provenance').textContent,/ПРИВАТНЫЙ ПАКЕТ/);
});
