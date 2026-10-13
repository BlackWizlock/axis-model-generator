import {SCENE_KEY,MAX_BUILDINGS,emptyBuilding,readScene,writeScene,placement,placePlan,placeBuilding,placementNote} from './scene.js';
const FIELDS=[['x','X в чертеже, м'],['y','Y в чертеже, м'],['z','Отметка нуля, м'],['angle','Поворот, градусы'],['ax','Точка модели X, м'],['ay','Точка модели Y, м'],['az','Точка модели Z, м']];
const BUILDING_KINDS=new Set(['fbx','zip-fbx']);
const STOPPED=new Set(['failed','cancelled','interrupted','deleted']);
// Панель сцены участка: загруженный файл сам встаёт в сцену, сооружению задаётся точка вставки, вид совмещённый.
// deps: byId, element, loadPreview(job), mount(container, parts, markers), storage, hasPreview(job).
export function setupScene(deps){
  const {byId,element,loadPreview,mount,storage,hasPreview,focused}=deps;
  const scene=readScene(read());let jobs=[],listed='',viewer=null,epoch=0,shown=false,planSaid='',stateSaid='',readyUsed='',loaded=false;
  // Файлы сцены, которые на этой странице ещё обрабатывались: их готовность собирает сцену.
  const pending=new Set(),seen=new Set();
  const previews=new Map();
  function read(){try{return storage?.getItem(SCENE_KEY)??'';}catch{return'';}}
  function save(){try{storage?.setItem(SCENE_KEY,writeScene(scene));}catch{ /* Сцена тогда живёт до закрытия страницы. */ }}
  function say(text,error=false){const node=byId('scene-message');node.textContent=text;node.classList.toggle('error',error);}
  const ready=kinds=>jobs.filter(job=>job.state==='completed'&&kinds(job.inputKind)&&hasPreview(job));
  const find=id=>jobs.find(job=>job.id===id&&job.state==='completed'&&hasPreview(job));
  // Файл из сцены, которого ещё нет среди готовых: что с ним сейчас.
  function waiting(id,plan=false){
    const job=id&&!find(id)?jobs.find(item=>item.id===id):null;
    if(!job)return'';
    const name=job.displayName||'Файл';
    if(STOPPED.has(job.state))return`${name}: обработка не завершена. Загрузите файл ещё раз.`;
    return job.state==='completed'?`${name}: просмотр для этого файла недоступен.`:`${name} принят и обрабатывается.${plan?'':' Точку вставки можно задать уже сейчас.'}`;
  }
  // Состояния файлов сцены, которые ещё не готовы: строка под сценой, пока показывать нечего.
  function sayStates(asked=true){
    const states=[waiting(scene.plan,true)&&`Генплан: ${waiting(scene.plan,true)}`,...scene.buildings.map(building=>waiting(building.job))].filter(Boolean);
    stateSaid=states.join(' ');
    // Просьба выбрать файл уместна в ответ на кнопку; при опросе списка строка просто гаснет.
    say(stateSaid||(asked?'Выберите генплан или сооружение из своих моделей. Сначала загрузите файлы в разделе выше.':''),asked&&!states.length);
  }
  const statesShown=()=>Boolean(stateSaid)&&byId('scene-message').textContent===stateSaid;
  function fill(select,rows,value,placeholder){
    const first=element('option',placeholder);first.value='';
    select.replaceChildren(first,...rows.map(job=>{const option=element('option',job.displayName||'Без имени');option.value=job.id;return option;}));
    select.value=rows.some(job=>job.id===value)?value:'';
  }
  // Сцену уже просили показать: выбор и числа сразу идут в неё, в том числе пока под сценой стоят состояния файлов.
  function settle(asked){save();if(shown||asked)void show();}
  // Слушатель события: аргументов не принимает, объект события сюда попасть не должен.
  function changed(){settle(statesShown());}
  // Перерисовка строк может погасить строку состояний: была ли она, запоминаем до перерисовки.
  function redrawn(){const asked=statesShown();render();settle(asked);}
  function field(building,index,[name,label]){
    const box=element('div'),id=`scene-${index}-${name}`,caption=element('label',label),input=element('input');
    caption.setAttribute('for',id);input.id=id;input.type='text';input.setAttribute('inputmode','decimal');input.setAttribute('autocomplete','off');input.value=building[name];
    input.addEventListener('input',()=>{building[name]=input.value.slice(0,32);save();});input.addEventListener('change',changed);
    box.append(caption,input);return box;
  }
  function row(building,index){
    const node=element('article',undefined,'scene-row'),head=element('div',undefined,'scene-row-head'),choice=element('div',undefined,'select-field'),select=element('select');
    const id=`scene-${index}-job`,caption=element('label',`Сооружение ${index+1}`);caption.setAttribute('for',id);select.id=id;
    fill(select,ready(kind=>BUILDING_KINDS.has(kind)),building.job,'Выберите модель FBX');
    select.addEventListener('change',()=>{building.job=select.value;changed();});choice.append(select);
    const swap=element('button','Поменять X и Y','button quiet compact');swap.type='button';
    swap.addEventListener('click',()=>{[building.x,building.y]=[building.y,building.x];redrawn();});
    const remove=element('button','Убрать','button quiet compact');remove.type='button';
    remove.addEventListener('click',()=>{scene.buildings.splice(index,1);if(!scene.buildings.length)scene.buildings.push(emptyBuilding());redrawn();});
    const title=element('div');title.append(caption,choice);head.append(title,swap,remove);
    const fields=element('div',undefined,'scene-fields');fields.append(...FIELDS.map(item=>field(building,index,item)));
    const state=waiting(building.job);
    node.append(head,fields,...(state?[element('p',state,'small')]:[]));return node;
  }
  const usedReady=()=>[scene.plan,...scene.buildings.map(building=>building.job)].filter(id=>id&&find(id)).join();
  // Строка о генплане не затирает чужое сообщение, пока её не просят явно.
  function render(force=false){
    const active=focused?.(),id=active?.id||'',caret=id.startsWith('scene-')?[active.selectionStart,active.selectionEnd]:null;
    fill(byId('scene-plan'),ready(kind=>kind==='dwg'),scene.plan,'Без генплана');
    byId('scene-buildings').replaceChildren(...scene.buildings.map(row));
    byId('scene-add').disabled=scene.buildings.length>=MAX_BUILDINGS;
    // Поле, в которое пользователь вводит числа, остаётся под курсором после перерисовки строк.
    if(caret){const node=byId(id);node?.focus?.({preventScroll:true});try{node?.setSelectionRange?.(...caret);}catch{ /* У списка выбора каретки нет. */ }}
    const state=waiting(scene.plan,true),text=state?`Генплан: ${state}`:'',said=byId('scene-message').textContent;
    if(text&&(force||!said||said===planSaid))say(text);else if(!text&&planSaid&&said===planSaid)say('');
    planSaid=text;
  }
  async function preview(job){
    if(!previews.has(job.id)){if(previews.size>=MAX_BUILDINGS+1)previews.clear();previews.set(job.id,loadPreview(job));}
    try{return await previews.get(job.id);}catch(error){previews.delete(job.id);throw error;}
  }
  function note(index,plan,at){
    const kind=placementNote(plan,at);
    if(kind==='swapped')return`Сооружение ${index+1}: точка вставки вне генплана, а с переставленными X и Y попадает на участок. Нажмите «Поменять X и Y».`;
    return kind==='outside'?`Сооружение ${index+1}: точка вставки вне границ генплана.`:'';
  }
  async function show(){
    const turn=++epoch,planJob=find(scene.plan);
    const chosen=scene.buildings.map((building,index)=>({index,job:find(building.job),at:placement(building)})).filter(item=>item.job);
    readyUsed=usedReady();
    if(!planJob&&!chosen.length){sayStates();return;}
    say('Собираем сцену…');
    try{
      const [plan,...models]=await Promise.all([planJob?preview(planJob):null,...chosen.map(item=>preview(item.job))]);
      if(turn!==epoch)return;
      const placed=chosen.map((item,order)=>({...item,model:models[order]})),set=placed.filter(item=>item.at);
      const planState=waiting(scene.plan,true),notes=[...(planState?[`Генплан: ${planState}`]:[]),...placed.filter(item=>!item.at).map(item=>`Сооружение ${item.index+1}: укажите числами X, Y и отметку нуля.`)];
      if(!plan&&!set.length){say(notes.join(' '),true);return;}
      const base=plan?plan.origin:[set[0].at.x,set[0].at.y,set[0].at.z],parts=[];
      if(plan)parts.push({plan:true,positions:placePlan(plan,base),indices:plan.indices});
      for(const item of set){parts.push({positions:placeBuilding(item.model,item.at,base),indices:item.model.indices});notes.push(note(item.index,plan,item.at));}
      viewer?.dispose();viewer=mount(byId('scene-viewer'),parts,set.map(item=>[item.at.x-base[0],item.at.y-base[1],item.at.z-base[2]]));shown=true;byId('scene-reset').disabled=false;
      const text=notes.filter(Boolean).join(' ');
      say(text||`В сцене: ${plan?'генплан и ':''}сооружений ${set.length}. Красная веха отмечает точку вставки. Меняйте числа, вид пересобирается.`,Boolean(text));
    }catch{if(turn===epoch)say('Не удалось открыть данные просмотра. Проверьте, что модели обработаны, и повторите.',true);}
  }
  function clear(){epoch++;viewer?.dispose();viewer=null;shown=false;previews.clear();byId('scene-reset').disabled=true;}
  byId('scene-plan').addEventListener('change',()=>{scene.plan=byId('scene-plan').value;redrawn();});
  byId('scene-add').addEventListener('click',()=>{if(scene.buildings.length<MAX_BUILDINGS){scene.buildings.push(emptyBuilding());save();render();}});
  byId('scene-show').addEventListener('click',()=>{void show();});
  byId('scene-reset').addEventListener('click',()=>viewer?.reset());
  render();
  return {
    // Список заданий меняется при опросе; поля перерисовываются только когда изменился сам выбор.
    jobs(list){
      jobs=Array.isArray(list)?list:[];
      const used=new Set([scene.plan,...scene.buildings.map(building=>building.job)]);
      const key=JSON.stringify([jobs.filter(job=>job.state==='completed'&&hasPreview(job)).map(job=>[job.id,job.inputKind,job.displayName]),jobs.filter(job=>used.has(job.id)).map(job=>[job.id,job.state])]);
      // Загруженный файл показывается в сцене сам, как только обработан, в том числе после перезагрузки страницы.
      let arrived=false;loaded=true;
      // Задание, которое было в списке и пропало, удалено: его строка больше не ждёт.
      const present=new Set(jobs.map(job=>job.id));
      // Файл, вытесненный из сцены, её больше не собирает.
      for(const id of pending)if(!used.has(id)||(seen.has(id)&&!present.has(id)))pending.delete(id);
      for(const job of jobs)if(used.has(job.id)){
        seen.add(job.id);
        if(find(job.id))arrived=pending.delete(job.id)||arrived;
        else if(STOPPED.has(job.state)||job.state==='completed')pending.delete(job.id);
        else pending.add(job.id);
      }
      const fresh=arrived||(shown&&usedReady()!==readyUsed);
      // Строка состояний, сказанная по кнопке, идёт за заданиями: отказ обработки в ней виден.
      const stale=key!==listed&&statesShown();
      if(key!==listed){listed=key;render(!fresh);}
      if(fresh)void show();else if(stale)sayStates(false);
    },
    // Только что загруженный файл встаёт в сцену по своему виду: чертёж генпланом, модель сооружением.
    accept(id,kind){
      let placed='building';
      if(kind==='dwg'){placed=scene.plan&&scene.plan!==id&&jobs.some(job=>job.id===scene.plan)?'replaced':'plan';scene.plan=id;}
      else if(BUILDING_KINDS.has(kind)){
        if(!scene.buildings.some(building=>building.job===id)){
          // Строку отдаём по порядку: с отказом обработки (её числа нужны повторной загрузке), пустую, с пропавшим заданием.
          const state=building=>jobs.find(job=>job.id===building.job)?.state;
          const gone=building=>loaded&&!pending.has(building.job)&&!jobs.some(job=>job.id===building.job);
          const free=scene.buildings.find(building=>STOPPED.has(state(building)))||scene.buildings.find(building=>!building.job)||scene.buildings.find(gone)
            ||(scene.buildings.length<MAX_BUILDINGS?scene.buildings[scene.buildings.push(emptyBuilding())-1]:null);
          if(!free)return'full';
          free.job=id;
        }
      }else return'';
      pending.add(id);listed='';save();render(true);
      return placed;
    },
    clear,show,
    scene:()=>scene,
  };
}
