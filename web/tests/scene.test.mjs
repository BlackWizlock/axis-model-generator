import test from 'node:test';
import assert from 'node:assert/strict';
import {parseNumber,readScene,writeScene,emptyScene,placement,placePlan,placeBuilding,extent,placementNote,MAX_BUILDINGS,SCENE_KEY} from '../src/scene.js';
import {setupScene} from '../src/scene-panel.js';
const close=(got,want)=>{assert.equal(got.length,want.length);got.forEach((value,i)=>assert.ok(Math.abs(value-want[i])<1e-3,`${i}: ${value} != ${want[i]}`));};
const plan=()=>({origin:[8000,1900,137],bounds:{min:[-100,-150,-6],max:[200,150,6]},positions:[0,0,0,10,0,1,0,10,2],indices:[0,1,2]});
const house=()=>({origin:[60,20,5],bounds:{min:[-10,-10,-5],max:[10,10,5]},positions:[-10,-10,-5,10,-10,-5,-10,10,5],indices:[0,1,2]});
test('a number is read the way a designer writes it',()=>{
 assert.equal(parseNumber('138,90'),138.9);assert.equal(parseNumber(' 7 970.425 '),7970.425);assert.equal(parseNumber('-7.789'),-7.789);assert.equal(parseNumber('.5'),0.5);
 for(const bad of ['', '1e3', '12 м', 'abc', '1,2,3', null, undefined])assert.equal(parseNumber(bad),null);
});
test('a saved scene is read back and anything else gives an empty one',()=>{
 const scene={plan:'a'.repeat(32),buildings:[{job:'b'.repeat(32),x:'7970,425',y:'1886.7245',z:'138,90',angle:'7,789',ax:'0',ay:'0',az:'0'}]};
 assert.deepEqual(readScene(writeScene(scene)),scene);
 for(const raw of ['', 'null', '[]', '{"buildings":5}', '{'])assert.deepEqual(readScene(raw),emptyScene());
 const dirty=readScene(JSON.stringify({plan:'../x',buildings:[null,{job:{},x:5,y:'1'.repeat(40),z:'2'},...Array.from({length:30},()=>({job:'c'}))]}));
 assert.equal(dirty.plan,'');assert.equal(dirty.buildings.length,MAX_BUILDINGS-1);assert.deepEqual(dirty.buildings[0],{job:'',x:'',y:'',z:'2',angle:'',ax:'',ay:'',az:''});
 assert.equal(readScene('{"plan":"","buildings":[]}').buildings.length,1);
});
test('a placement needs the point and the level and nothing else',()=>{
 assert.deepEqual(placement({x:'7970,425',y:'1886,7245',z:'138,90',angle:'',ax:'',ay:'',az:''}),{x:7970.425,y:1886.7245,z:138.9,angle:0,anchor:[0,0,0]});
 assert.deepEqual(placement({x:'1',y:'2',z:'3',angle:'7,789',ax:'4',ay:'5',az:'6'}),{x:1,y:2,z:3,angle:7.789,anchor:[4,5,6]});
 assert.equal(placement({x:'1',y:'',z:'3',angle:'0',ax:'0',ay:'0',az:'0'}),null);assert.equal(placement({x:'1',y:'2',z:'3',angle:'north',ax:'0',ay:'0',az:'0'}),null);
});
test('the plan keeps its drawing coordinates and a building lands on its point',()=>{
 const base=[8000,1900,137];
 close(placePlan(plan(),base),[0,0,0,10,0,1,0,10,2]);close(placePlan(plan(),[7990,1900,130]),[10,0,7,20,0,8,10,10,9]);
 // Точка модели (50, 10, 0) встаёт в (8010, 1920, 138.9); без поворота остальное сдвигается вместе с ней.
 const at={x:8010,y:1920,z:138.9,angle:0,anchor:[50,10,0]};
 close(placeBuilding(house(),at,base),[10,20,1.9,30,20,1.9,10,40,11.9]);
 // Четверть оборота против часовой стрелки: ось X модели смотрит вдоль Y чертежа, высота не меняется.
 close(placeBuilding(house(),{...at,angle:90},base),[10,20,1.9,10,40,1.9,-10,20,11.9]);
 close(placeBuilding(house(),{...at,angle:-90},base),[10,20,1.9,10,0,1.9,30,20,11.9]);
});
test('the extent covers every part and an empty scene has none',()=>{
 assert.deepEqual(extent([new Float32Array([0,0,0,1,2,3]),new Float32Array([-4,5,1])]),{min:[-4,0,0],max:[1,5,3]});assert.equal(extent([]),null);
});
test('a point written north first is recognised',()=>{
 assert.equal(placementNote(plan(),{x:7970.425,y:1886.7245}),'');assert.equal(placementNote(plan(),{x:1886.7245,y:7970.425}),'swapped');
 assert.equal(placementNote(plan(),{x:0,y:0}),'outside');assert.equal(placementNote(null,{x:0,y:0}),'');
});
class Node{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.attributes={};this.events={};this.value='';this.disabled=false;this.text='';this.classList={toggle:(name,on)=>{this.error=on;}};}
 set textContent(value){this.text=String(value);}get textContent(){return this.text;}
 append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;}
 setAttribute(k,v){this.attributes[k]=v;}addEventListener(k,fn){this.events[k]=fn;}
 all(){return [this,...this.children.flatMap(child=>child.all())];}
}
function panel({saved='',fail=false}={}){
 const nodes={},stored=new Map(saved?[[SCENE_KEY,saved]]:[]),mounted=[],loads=[];let disposed=0;
 const byId=id=>nodes[id]||=new Node();
 const element=(tag,text,className)=>{const node=new Node(tag);if(text!==undefined)node.textContent=text;node.className=className;return node;};
 const control=setupScene({byId,element,hasPreview:job=>job.preview!==false,storage:{getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)},
  loadPreview:async job=>{loads.push(job.id);if(fail)throw new Error('offline');return job.inputKind==='dwg'?plan():house();},
  mount:(container,parts,markers)=>{mounted.push({parts,markers});return {reset(){},dispose(){disposed++;}};}});
 const input=name=>byId('scene-buildings').all().find(node=>node.id===name);
 return {control,byId,stored,mounted,loads,input,disposed:()=>disposed,message:()=>byId('scene-message').text};
}
const jobs=[{id:'p'.repeat(32),state:'completed',inputKind:'dwg',displayName:'plan.dwg'},{id:'h'.repeat(32),state:'completed',inputKind:'fbx',displayName:'house.fbx'},
 {id:'k'.repeat(32),state:'completed',inputKind:'zip-fbx',displayName:'school.zip'},{id:'r'.repeat(32),state:'running',inputKind:'fbx',displayName:'later.fbx'},
 {id:'n'.repeat(32),state:'completed',inputKind:'fbx',displayName:'no-view.fbx',preview:false},{id:'z'.repeat(32),state:'completed',inputKind:'portable-package',displayName:'pack.zip'}];
const options=select=>select.children.map(option=>option.text);
test('the panel offers the plan and the buildings from finished jobs only',()=>{
 const app=panel();app.control.jobs(jobs);
 assert.deepEqual(options(app.byId('scene-plan')),['Без генплана','plan.dwg']);assert.deepEqual(options(app.input('scene-0-job')),['Выберите модель FBX','house.fbx','school.zip']);
 // Тот же список при опросе поля не перерисовывает: введённое не теряется.
 const before=app.input('scene-0-x');app.control.jobs(jobs.map(job=>({...job})));assert.equal(app.input('scene-0-x'),before);
 app.control.jobs(jobs.slice(0,2));assert.notEqual(app.input('scene-0-x'),before);
});
test('one plan and several buildings meet in one view and the scene is remembered',async()=>{
 const app=panel();app.control.jobs(jobs);
 await app.byId('scene-show').events.click();assert.match(app.message(),/Выберите генплан или сооружение/);assert.equal(app.mounted.length,0);
 app.byId('scene-plan').value='p'.repeat(32);app.byId('scene-plan').events.change();
 const set=(name,value)=>{const node=app.input(name);node.value=value;node.events.input();};
 app.input('scene-0-job').value='h'.repeat(32);app.input('scene-0-job').events.change();
 set('scene-0-x','8010');set('scene-0-y','1920');set('scene-0-z','138,90');set('scene-0-ax','50');set('scene-0-ay','10');
 app.byId('scene-add').events.click();app.input('scene-1-job').value='k'.repeat(32);app.input('scene-1-job').events.change();
 await app.control.show();
 assert.equal(app.mounted.length,1);assert.equal(app.mounted[0].parts.length,2);assert.equal(app.mounted[0].parts[0].plan,true);
 close(app.mounted[0].parts[1].positions,[10,20,1.9,30,20,1.9,10,40,11.9]);close(app.mounted[0].markers[0],[10,20,1.9]);
 assert.match(app.message(),/Сооружение 2: укажите числами X, Y и отметку нуля/);assert.equal(app.byId('scene-reset').disabled,false);
 // Второе сооружение записано по-геодезически: подсказка и перестановка одной кнопкой.
 set('scene-1-x','1930');set('scene-1-y','8050');set('scene-1-z','139');await app.control.show();
 assert.equal(app.mounted.at(-1).parts.length,3);assert.match(app.message(),/Сооружение 2: точка вставки вне генплана, а с переставленными X и Y/);
 const swap=app.byId('scene-buildings').children[1].all().find(node=>node.text==='Поменять X и Y');swap.events.click();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.input('scene-1-x').value,'8050');assert.match(app.message(),/В сцене: генплан и сооружений 2/);assert.equal(app.disposed(),app.mounted.length-1);
 // Данные просмотра каждого задания запрошены один раз.
 assert.deepEqual(app.loads.sort(),['h'.repeat(32),'k'.repeat(32),'p'.repeat(32)]);
 const again=panel({saved:app.stored.get(SCENE_KEY)});again.control.jobs(jobs);
 assert.equal(again.byId('scene-plan').value,'p'.repeat(32));assert.equal(again.input('scene-1-x').value,'8050');assert.equal(again.input('scene-0-z').value,'138,90');
 again.byId('scene-buildings').children[0].all().find(node=>node.text==='Убрать').events.click();assert.equal(again.byId('scene-buildings').children.length,1);assert.equal(again.input('scene-0-x').value,'8050');
});
test('buildings without a plan stand around the first of them and a failed load is said',async()=>{
 const app=panel();app.control.jobs(jobs);app.input('scene-0-job').value='h'.repeat(32);app.input('scene-0-job').events.change();
 for(const [name,value] of [['x','500'],['y','600'],['z','10']]){const node=app.input(`scene-0-${name}`);node.value=value;node.events.input();}
 await app.control.show();assert.equal(app.mounted[0].parts.length,1);close(app.mounted[0].markers[0],[0,0,0]);assert.match(app.message(),/В сцене: сооружений 1/);
 app.control.clear();assert.equal(app.disposed(),1);assert.equal(app.byId('scene-reset').disabled,true);
 const broken=panel({fail:true});broken.control.jobs(jobs);broken.byId('scene-plan').value='p'.repeat(32);broken.byId('scene-plan').events.change();
 await broken.control.show();assert.match(broken.message(),/Не удалось открыть данные просмотра/);assert.equal(broken.mounted.length,0);
 for(let i=0;i<20;i++)app.byId('scene-add').events.click();assert.equal(app.byId('scene-buildings').children.length,MAX_BUILDINGS);assert.equal(app.byId('scene-add').disabled,true);
});
test('an uploaded file takes its place in the scene by its kind and is shown once processed',async()=>{
 const app=panel();app.control.jobs(jobs);
 assert.equal(app.control.accept('r'.repeat(32),'fbx'),'building');assert.equal(app.control.scene().buildings[0].job,'r'.repeat(32));
 assert.ok(app.byId('scene-buildings').children[0].all().some(node=>/later\.fbx принят и обрабатывается/.test(node.text)));
 assert.equal(app.control.accept('p'.repeat(32),'dwg'),'plan');assert.equal(app.byId('scene-plan').value,'p'.repeat(32));
 assert.equal(app.control.accept('z'.repeat(32),'portable-package'),'');assert.equal(app.mounted.length,0);
 for(const [name,value] of [['x','8010'],['y','1920'],['z','138,90']]){const node=app.input(`scene-0-${name}`);node.value=value;node.events.input();}
 // Задание обработано: сцена собирается сама, без кнопки.
 app.control.jobs(jobs.map(job=>job.id==='r'.repeat(32)?{...job,state:'completed'}:job));await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.mounted.at(-1).parts.length,2);assert.match(app.message(),/В сцене: генплан и сооружений 1/);
 assert.equal(app.input('scene-0-x').value,'8010');assert.equal(app.input('scene-0-job').value,'r'.repeat(32));
 // Следующий файл получает свою строку; тот же файл второй раз строку не занимает.
 assert.equal(app.control.accept('h'.repeat(32),'fbx'),'building');assert.equal(app.control.accept('h'.repeat(32),'fbx'),'building');
 assert.deepEqual(app.control.scene().buildings.map(building=>building.job),['r'.repeat(32),'h'.repeat(32)]);
 for(let i=2;i<MAX_BUILDINGS;i++)assert.equal(app.control.accept(String(i).padStart(32,'0'),'zip-fbx'),'building');
 assert.equal(app.control.accept('f'.repeat(32),'fbx'),'full');assert.equal(app.control.scene().buildings.length,MAX_BUILDINGS);
 const stopped=panel();stopped.control.accept('r'.repeat(32),'fbx');stopped.control.jobs(jobs.map(job=>job.id==='r'.repeat(32)?{...job,state:'failed'}:job));
 assert.ok(stopped.byId('scene-buildings').children[0].all().some(node=>/обработка не завершена/.test(node.text)));
});
test('a reloaded page still shows the scene once its file is processed and a stale row is reused',async()=>{
 const draft=panel(),id='d'.repeat(32),drawing={id,state:'running',inputKind:'dwg',displayName:'site.dwg'};
 draft.control.accept(id,'dwg');
 // Страница перезагружена: в памяти осталась только сохранённая сцена.
 const app=panel({saved:draft.stored.get(SCENE_KEY)});app.control.jobs([drawing]);
 assert.equal(app.message(),'Генплан: site.dwg принят и обрабатывается.');assert.equal(app.mounted.length,0);
 app.control.jobs([{...drawing,state:'completed'}]);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.mounted.length,1);assert.match(app.message(),/В сцене: генплан и сооружений 0/);assert.equal(app.byId('scene-plan').value,id);
 // Постороннее задание показанную сцену не пересобирает.
 app.control.jobs([{...drawing,state:'completed'},{id:'o'.repeat(32),state:'completed',inputKind:'fbx',displayName:'other.fbx'}]);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.mounted.length,1);
 // Отказ обработки снимает ожидание и остаётся в сообщении.
 const failed=panel({saved:draft.stored.get(SCENE_KEY)});failed.control.jobs([drawing]);failed.control.jobs([{...drawing,state:'failed'}]);
 assert.match(failed.message(),/Генплан: site\.dwg: обработка не завершена/);assert.equal(failed.mounted.length,0);
 // Строка с заданием, которого больше нет в списке, отдаётся новому файлу вместе со своими числами.
 const old=panel();old.control.accept('g'.repeat(32),'fbx');old.input('scene-0-x').value='8010';old.input('scene-0-x').events.input();
 const next=panel({saved:old.stored.get(SCENE_KEY)});next.control.jobs(jobs);
 assert.equal(next.control.accept('r'.repeat(32),'fbx'),'building');
 assert.deepEqual(next.control.scene().buildings.map(building=>[building.job,building.x]),[['r'.repeat(32),'8010']]);
});
test('rows are reused with care and the shown scene is not rebuilt without a reason',async()=>{
 const R='r'.repeat(32),P='p'.repeat(32),H='h'.repeat(32),N='n'.repeat(32);
 // Список заданий ещё не получен: занятая строка сохранённой сцены новому файлу не отдаётся.
 const first=panel();first.control.jobs(jobs);first.control.accept(H,'fbx');
 const cold=panel({saved:first.stored.get(SCENE_KEY)});cold.control.accept(N,'fbx');
 assert.deepEqual(cold.control.scene().buildings.map(building=>building.job),[H,N]);
 // Отказ обработки: повторно загруженный файл получает ту же строку с её числами.
 const retry=panel();retry.control.jobs(jobs);retry.control.accept(R,'fbx');
 const x=retry.input('scene-0-x');x.value='77';x.events.input();
 retry.control.jobs(jobs.map(job=>job.id===R?{...job,state:'failed'}:job));
 assert.equal(retry.control.accept(N,'fbx'),'building');assert.deepEqual(retry.control.scene().buildings.map(building=>[building.job,building.x]),[[N,'77']]);
 // Задание удалено во время обработки: строка освобождается без перезагрузки страницы.
 const gone=panel();gone.control.jobs(jobs);gone.control.accept(R,'fbx');gone.control.jobs(jobs);gone.control.jobs(jobs.filter(job=>job.id!==R));
 gone.control.accept(N,'fbx');assert.deepEqual(gone.control.scene().buildings.map(building=>building.job),[N]);
 // Второй чертёж: сайт говорит о замене, под сценой стоит состояние нового чертежа, а не просьба выбрать файл.
 const app=panel();app.control.jobs(jobs);app.byId('scene-plan').value=P;app.byId('scene-plan').events.change();
 await app.control.show();assert.equal(app.mounted.length,1);
 const drawing={id:'d'.repeat(32),state:'running',inputKind:'dwg',displayName:'new.dwg'};
 assert.equal(app.control.accept(drawing.id,'dwg'),'replaced');app.control.jobs([...jobs,drawing]);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.message(),'Генплан: new.dwg принят и обрабатывается.');
 app.control.jobs([...jobs,{...drawing,state:'completed'}]);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.mounted.length,2);assert.match(app.message(),/В сцене: генплан/);
 // Тот же список после показа сцену не пересобирает.
 app.control.jobs([...jobs,{...drawing,state:'completed'}]);app.control.jobs([...jobs,{...drawing,state:'completed'},{id:'o'.repeat(32),state:'completed',inputKind:'fbx',displayName:'other.fbx'}]);
 await new Promise(resolve=>setImmediate(resolve));assert.equal(app.mounted.length,2);
 const manual=panel();manual.control.jobs(jobs);manual.byId('scene-plan').value=P;manual.byId('scene-plan').events.change();await manual.control.show();
 manual.control.jobs([...jobs]);manual.control.jobs(jobs.map(job=>({...job})));await new Promise(resolve=>setImmediate(resolve));assert.equal(manual.mounted.length,1);
});
