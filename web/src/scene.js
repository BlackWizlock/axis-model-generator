// Сцена участка: один генплан и сооружения, каждое со своей точкой вставки.
// Здесь только расчёт; состав сцены хранится в браузере и на сервер не уходит.
export const SCENE_KEY='mg-site-scene-v1';
export const MAX_BUILDINGS=12;
const JOB=/^[A-Za-z0-9_-]{1,64}$/;
const FIELDS=['x','y','z','angle','ax','ay','az'];
// Число в записи проектировщика: «138,90» читается так же, как «138.90».
export function parseNumber(text){
  const value=String(text??'').replace(/\s+/g,'').replace(',','.');
  if(!/^[-+]?(\d+\.?\d*|\.\d+)$/.test(value))return null;
  const number=Number(value);return Number.isFinite(number)?number:null;
}
export const emptyBuilding=()=>({job:'',x:'',y:'',z:'',angle:'0',ax:'0',ay:'0',az:'0'});
export const emptyScene=()=>({plan:'',buildings:[emptyBuilding()]});
const text=value=>typeof value==='string'&&value.length<=32?value:'';
const job=value=>typeof value==='string'&&JOB.test(value)?value:'';
export function readScene(raw){
  let value;try{value=JSON.parse(raw);}catch{return emptyScene();}
  if(!value||typeof value!=='object'||!Array.isArray(value.buildings))return emptyScene();
  const buildings=value.buildings.slice(0,MAX_BUILDINGS).filter(row=>row&&typeof row==='object')
    .map(row=>({job:job(row.job),...Object.fromEntries(FIELDS.map(name=>[name,text(row[name])]))}));
  return {plan:job(value.plan),buildings:buildings.length?buildings:[emptyBuilding()]};
}
export const writeScene=scene=>JSON.stringify({plan:scene.plan,buildings:scene.buildings});
// Привязка задана, когда есть X, Y и отметка нуля. Угол и точка модели по умолчанию нулевые.
export function placement(building){
  const [x,y,z]=['x','y','z'].map(name=>parseNumber(building[name]));
  if(x===null||y===null||z===null)return null;
  const [angle,ax,ay,az]=['angle','ax','ay','az'].map(name=>building[name]===''?0:parseNumber(building[name]));
  if([angle,ax,ay,az].includes(null))return null;
  return {x,y,z,angle,anchor:[ax,ay,az]};
}
// Генплан остаётся в координатах чертежа; сцена отсчитывается от base, чтобы числа были малыми.
export function placePlan(preview,base){
  const out=new Float32Array(preview.positions.length),shift=preview.origin.map((value,axis)=>value-base[axis]);
  for(let i=0;i<out.length;i++)out[i]=preview.positions[i]+shift[i%3];
  return out;
}
// Точка модели anchor встаёт в (x, y, z); модель повёрнута вокруг неё, положительный угол против часовой стрелки при взгляде сверху.
export function placeBuilding(preview,at,base){
  const out=new Float32Array(preview.positions.length),radians=at.angle*Math.PI/180,cos=Math.cos(radians),sin=Math.sin(radians);
  const [ox,oy,oz]=preview.origin.map((value,axis)=>value-at.anchor[axis]);
  for(let i=0;i<out.length;i+=3){
    const dx=preview.positions[i]+ox,dy=preview.positions[i+1]+oy;
    out[i]=at.x+cos*dx-sin*dy-base[0];out[i+1]=at.y+sin*dx+cos*dy-base[1];out[i+2]=at.z+preview.positions[i+2]+oz-base[2];
  }
  return out;
}
export function extent(parts){
  const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
  for(const positions of parts)for(let i=0;i<positions.length;i++){const axis=i%3,value=positions[i];if(value<min[axis])min[axis]=value;if(value>max[axis])max[axis]=value;}
  return min[0]<=max[0]?{min,max}:null;
}
const inside=(plan,x,y)=>x>=plan.origin[0]+plan.bounds.min[0]&&x<=plan.origin[0]+plan.bounds.max[0]&&y>=plan.origin[1]+plan.bounds.min[1]&&y<=plan.origin[1]+plan.bounds.max[1];
// Геодезическая запись ставит X на север: такая точка попадает на участок только после перестановки.
export function placementNote(plan,at){
  if(!plan||inside(plan,at.x,at.y))return'';
  return inside(plan,at.y,at.x)?'swapped':'outside';
}
