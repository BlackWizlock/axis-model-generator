import {test} from 'vitest';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {canPoll, statusText} from '../src/jobs.js';
import {uploadFile} from '../src/upload.js';
test('semantic credits, forms, self-hosted scripts, no inline handlers', async()=>{
 const html=await readFile(new URL('../index.html',import.meta.url),'utf8'); const css=await readFile(new URL('../styles.css',import.meta.url),'utf8');
 assert.match(html,/<main/);assert.match(html,/<footer/);assert.match(html,/Разработано <a[^>]+>Axis Consult<\/a> · <a[^>]+>Axis Platform<\/a>/);
 for(const url of ['https://axisconsult.ru','https://axisplatform.ru']) assert.ok(html.includes(`href="${url}" target="_blank" rel="noopener noreferrer"`));
 assert.match(css,/:focus-visible/);assert.doesNotMatch(html,/\son\w+=|<script[^>]+https?:|<style/);assert.doesNotMatch(html,/type="password"|id="auth-form"|id="username"/);
 assert.match(html,/id="workspace"/);assert.match(html,/label for="input-file"/);
});
test('polling ends hidden or terminal, completed is not normative ready',()=>{
 assert.equal(canPoll([{state:'running'}],false),true);assert.equal(canPoll([{state:'running'}],true),false);assert.equal(canPoll([{state:'completed'}],false),false);
 assert.match(statusText('completed'),/Завершено/);assert.equal(canPoll([{state:'deleting'}],false),true);
});
test('cap is enforced before hashing or reserving',async()=>{
 await assert.rejects(uploadFile({size:256*1024*1024+1},'portable-package','token',()=>{},new AbortController().signal),e=>e.code==='upload_too_large');
});
import {hashFile} from '../src/upload.js';
test('hash worker terminates immediately on cancel',async()=>{
 const prior=globalThis.Worker;let terminated=0;
 globalThis.Worker=class{postMessage(){} terminate(){terminated++;}};
 const controller=new AbortController();
 try{const promise=hashFile(new Blob(['test']),()=>{},controller.signal);controller.abort();await assert.rejects(promise,{name:'AbortError'});assert.equal(terminated,1);}finally{globalThis.Worker=prior;}
});
test('poller does not fetch while hidden and removes visibility listener',async()=>{
 const {startPolling}=await import('../src/jobs.js');let calls=0,listener=null;const doc={hidden:true,addEventListener(name,fn){listener=fn;},removeEventListener(){listener=null;}};
 const poller=startPolling(async()=>{calls++;return[{state:'completed'}];},doc);await Promise.resolve();assert.equal(calls,0);doc.hidden=false;listener();await new Promise(resolve=>setTimeout(resolve,5));assert.equal(calls,1);poller.stop();assert.equal(listener,null);
});
test('four axes mapper preserves research and partial statuses',async()=>{
 const {jobAxes}=await import('../src/jobs.js');assert.deepEqual(jobAxes({coverage:{technical:'partial',profile:'research',procedure:'unknown',external:'not_checked'}}).map(n=>n[1]),['partial','research','unknown','not_checked']);
});
test('wire job collection uses backend items and bounds page',async()=>{
 const {jobsPage}=await import('../src/jobs.js');const jobs=[{id:'a'.repeat(32),state:'queued'}];assert.deepEqual(jobsPage({items:jobs,nextCursor:null}),{jobs,nextCursor:null});assert.throws(()=>jobsPage({jobs}));assert.throws(()=>jobsPage({items:Array(51).fill({})}));
});
test('Axis Sites theme, local typography and official brand sign are used',async()=>{
 const html=await readFile(new URL('../index.html',import.meta.url),'utf8');const css=await readFile(new URL('../styles.css',import.meta.url),'utf8');
 assert.match(css,/--background:hsl\(215 17% 12%\)/);assert.match(css,/--primary:hsl\(184 62% 42%\)/);assert.match(css,/font-family:Manrope/);assert.match(css,/font-family:Geologica/);assert.doesNotMatch(css,/Georgia|Avenir|Trebuchet|fonts\.googleapis/);assert.match(html,/src="\/assets\/axis-sign.png"/);assert.match(html,/Регистрация|регистрации/);assert.doesNotMatch(html,/auth-form|username|password/);
});
test('vendored brand assets have pinned SHA256 and font licenses',async()=>{
 const {createHash}=await import('node:crypto');const inventory=JSON.parse(await readFile(new URL('../assets/inventory.json',import.meta.url),'utf8'));
 for(const row of inventory.assets){const bytes=await readFile(new URL(`../assets/${row.path}`,import.meta.url));assert.equal(bytes.length,row.bytes);assert.equal(createHash('sha256').update(bytes).digest('hex'),row.sha256);}
 const license=await readFile(new URL('../assets/fonts/GEOLOGICA-OFL.txt',import.meta.url),'utf8');assert.match(license,/SIL Open Font License/);
});

import {renderChecks,runSourceLabel} from '../src/checks.js';
test('phase findings foreground trusted Russian repair and readable element identity; raw evidence is collapsed text',async()=>{
 const previousDocument=globalThis.document,previousFetch=globalThis.fetch;
 class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.attributes={};this.dataset={};this.events={};this.textContent='';}
  append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;this.textContent='';}
  get childNodes(){return this.children;}querySelectorAll(){return [];}
  setAttribute(k,v){this.attributes[k]=v;}getAttribute(k){return this.attributes[k];}
  addEventListener(k,fn){this.events[k]=fn;}
 }
 const finding={ruleId:'package.normals',title:'Нормали пакета',why:'Нормали не переданы.',nextAction:'Добавьте нормали в экспорт.',target:'export',file:'scene.json',elementKey:{document_id:'root',unique_id:'synthetic-element',link_instance_path:['link-a','link-b']},message:'Instance mesh omits vertex data',observed:{markup:'<script>unsafe</script>'},expected:'normals',locationExplanation:'Связь с Revit отдельно не проверена.'};
 const envelope={jobId:'a'.repeat(32),inputHash:'b'.repeat(64),authoritative:true,findings:{shown:1,original:1,omitted:0},checks:[{id:'scene.meshes',title:'Проверка сеток',state:'warning'}]};
 globalThis.document={createElement:tag=>new Element(tag)};
 globalThis.fetch=async()=>new Response(JSON.stringify({jobId:envelope.jobId,inputHash:envelope.inputHash,title:envelope.checks[0].title,why:finding.why,nextAction:finding.nextAction,findings:[finding],nextOffset:null}),{headers:{'Content-Type':'application/json'}});
 try {
  const container=new Element('div');renderChecks(container,envelope,()=>true);const row=container.children.at(-1);await row.children[0].events.click();const detail=row.children[1],findingNode=detail.children.find(item=>item.className==='finding');
  assert.ok(findingNode,'Each finding has its own repair block');
  const foreground=findingNode.children.filter(item=>item.tagName!=='details').map(item=>item.textContent).join(' ');
  for(const text of ['Нормали пакета','Нормали не переданы.','Добавьте нормали в экспорт.','root','synthetic-element','link-a → link-b'])assert.ok(foreground.includes(text),text);
  assert.doesNotMatch(foreground,/\[object Object\]|Instance mesh|unsafe/);
  const technical=findingNode.children.find(item=>item.tagName==='details');assert.ok(technical);assert.equal(technical.attributes.open,undefined);assert.ok(technical.children.some(item=>item.textContent.includes('Instance mesh')));assert.ok(technical.children.some(item=>item.textContent.includes('<script>unsafe</script>')));
  finding.ruleId='package.unsupported';finding.title='Версия пакета';finding.observed={value:2};finding.expected={value:1};envelope.checks[0]={id:finding.ruleId,title:finding.title,state:'failed'};
  const single=new Element('div');renderChecks(single,envelope,()=>true);const singleRow=single.children.at(-1);await singleRow.children[0].events.click();
  const visibleText=element=>element.tagName==='details'?'':`${element.textContent} ${element.children.map(visibleText).join(' ')}`;
  const text=visibleText(singleRow.children[1]);assert.equal(text.split(finding.title).length-1,1);assert.equal(text.split(finding.why).length-1,1);assert.equal(text.split(finding.nextAction).length-1,1);assert.match(text,/Обнаружено: 2/);assert.match(text,/Требуется: 1/);

 }finally{globalThis.document=previousDocument;globalThis.fetch=previousFetch;}
});

import {uploadStageText} from '../src/upload.js';
test('real upload heartbeat phases use Russian user terms including integrity, queue and processing',async()=>{
 for(const phase of ['preparation','hash','transport','ack','verify','finalizing','ready','queue','queued','processing','running']){const label=uploadStageText(phase);assert.match(label,/[А-Яа-я]/);assert.equal(label.includes(phase),false);}
 assert.match(uploadStageText('hash'),/контрольная сумма/);assert.match(uploadStageText('transport'),/Передача/);assert.match(uploadStageText('ack'),/Подтверждение/);assert.match(uploadStageText('finalizing'),/целостности/);
 const app=await readFile(new URL('../src/app.js',import.meta.url),'utf8');assert.match(app,/uploadStageText\(state.uploadPhase\)/);assert.doesNotMatch(app,/state.uploadPhase\|\|'Подготовка'/);
});

test('retained immutable report and private preview label their actual run and input hash',async()=>{
 const first={id:'a'.repeat(32),inputHash:'b'.repeat(64)},next={id:'c'.repeat(32),inputHash:'d'.repeat(64)};
 assert.match(runSourceLabel(first),/aaaaaaaa.*bbbbbbbbbbbb/);assert.notEqual(runSourceLabel(first),runSourceLabel(next));assert.throws(()=>runSourceLabel({...first,inputHash:'filename.zip'}));
 const app=await readFile(new URL('../src/app.js',import.meta.url),'utf8');assert.match(app,/const source=runSourceLabel\(job\)/);assert.match(app,/preview-provenance.*\$\{source\}/);assert.match(app,/report-summary.*\$\{source\}/);
});

test('guest bootstrap quota error preserves trusted repair and request ID instead of reload advice',async()=>{
 const previous=globalThis.fetch;const help={code:'rate_limited',title:'Достигнут предел сервиса',why:'Временный предел запросов исчерпан.',nextAction:'Дождитесь освобождения квоты и повторите позднее.',request_id:'f'.repeat(32)};
 globalThis.fetch=async()=>new Response(JSON.stringify({error:help}),{status:429,headers:{'Content-Type':'application/json'}});
 try{const {guestSession}=await import('../src/auth.js');await assert.rejects(guestSession(),error=>error.status===429&&error.message.includes(help.title)&&error.message.includes(help.nextAction)&&error.message.includes(help.request_id));}finally{globalThis.fetch=previous;}
 const app=await readFile(new URL('../src/app.js',import.meta.url),'utf8');assert.match(app,/catch\(error\)\{setUser\(null\);message\('session-message',safeError\(error\),true\)/);assert.match(app,/setAttribute\('role',error\?'alert':'status'\)/);assert.doesNotMatch(app,/Загрузка станет доступна после подключения сервиса\.|Сеанс недоступен\. Откройте страницу заново\./);
});

test('cancelled and failed diagnostics are final user-facing states',async()=>{
 const {completionText}=await import('../src/checks.js');
 for(const[state,text]of [['cancelled','Обработка отменена'],['interrupted','Обработка прервана'],['failed','Обработку не удалось завершить'],['deleted','Задание удалено'],['deleting','Задание удаляется']]) assert.equal(completionText({state},{checks:[]}),text);
});
