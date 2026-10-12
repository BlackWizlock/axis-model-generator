import {SCENE_KEY,MAX_BUILDINGS,emptyBuilding,readScene,writeScene,placement,placePlan,placeBuilding,placementNote} from './scene.js';
const FIELDS=[['x','X в чертеже, м'],['y','Y в чертеже, м'],['z','Отметка нуля, м'],['angle','Поворот, градусы'],['ax','Точка модели X, м'],['ay','Точка модели Y, м'],['az','Точка модели Z, м']];
const BUILDING_KINDS=new Set(['fbx','zip-fbx']);
// Панель сцены участка: выбор генплана и сооружений из своих заданий, точки вставки, совмещённый вид.
// deps: byId, element, loadPreview(job), mount(container, parts, markers), storage, hasPreview(job).
export function setupScene(deps){
  const {byId,element,loadPreview,mount,storage,hasPreview}=deps;
  const scene=readScene(read());let jobs=[],listed='',viewer=null,epoch=0,shown=false;
  const previews=new Map();
  function read(){try{return storage?.getItem(SCENE_KEY)??'';}catch{return'';}}
  function save(){try{storage?.setItem(SCENE_KEY,writeScene(scene));}catch{ /* Сцена тогда живёт до закрытия страницы. */ }}
  function say(text,error=false){const node=byId('scene-message');node.textContent=text;node.classList.toggle('error',error);}
  const ready=kinds=>jobs.filter(job=>job.state==='completed'&&kinds(job.inputKind)&&hasPreview(job));
  const find=id=>jobs.find(job=>job.id===id&&job.state==='completed'&&hasPreview(job));
  function fill(select,rows,value,placeholder){
    const first=element('option',placeholder);first.value='';
    select.replaceChildren(first,...rows.map(job=>{const option=element('option',job.displayName||'Без имени');option.value=job.id;return option;}));
    select.value=rows.some(job=>job.id===value)?value:'';
  }
  function changed(){save();if(shown)void show();}
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
    swap.addEventListener('click',()=>{[building.x,building.y]=[building.y,building.x];render();changed();});
    const remove=element('button','Убрать','button quiet compact');remove.type='button';
    remove.addEventListener('click',()=>{scene.buildings.splice(index,1);if(!scene.buildings.length)scene.buildings.push(emptyBuilding());render();changed();});
    const title=element('div');title.append(caption,choice);head.append(title,swap,remove);
    const fields=element('div',undefined,'scene-fields');fields.append(...FIELDS.map(item=>field(building,index,item)));
    node.append(head,fields);return node;
  }
  function render(){
    fill(byId('scene-plan'),ready(kind=>kind==='dwg'),scene.plan,'Без генплана');
    byId('scene-buildings').replaceChildren(...scene.buildings.map(row));
    byId('scene-add').disabled=scene.buildings.length>=MAX_BUILDINGS;
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
    if(!planJob&&!chosen.length){say('Выберите генплан или сооружение из своих моделей. Сначала загрузите файлы в разделе выше.',true);return;}
    say('Собираем сцену…');
    try{
      const [plan,...models]=await Promise.all([planJob?preview(planJob):null,...chosen.map(item=>preview(item.job))]);
      if(turn!==epoch)return;
      const placed=chosen.map((item,order)=>({...item,model:models[order]})),set=placed.filter(item=>item.at);
      const notes=placed.filter(item=>!item.at).map(item=>`Сооружение ${item.index+1}: укажите числами X, Y и отметку нуля.`);
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
  byId('scene-plan').addEventListener('change',()=>{scene.plan=byId('scene-plan').value;changed();});
  byId('scene-add').addEventListener('click',()=>{if(scene.buildings.length<MAX_BUILDINGS){scene.buildings.push(emptyBuilding());save();render();}});
  byId('scene-show').addEventListener('click',()=>{void show();});
  byId('scene-reset').addEventListener('click',()=>viewer?.reset());
  render();
  return {
    // Список заданий меняется при опросе; поля перерисовываются только когда изменился сам выбор.
    jobs(list){
      jobs=Array.isArray(list)?list:[];
      const key=JSON.stringify(jobs.filter(job=>job.state==='completed'&&hasPreview(job)).map(job=>[job.id,job.inputKind,job.displayName]));
      if(key!==listed){listed=key;render();}
    },
    clear,show,
    scene:()=>scene,
  };
}
