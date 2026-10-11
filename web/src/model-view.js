// Читатель упрощённой модели, которую пишет сам сервис: бинарный FBX 7400,
// только треугольники, развёртка по углам, встроенные PNG. Чужие файлы он не разбирает:
// любое отступление от этого вида даёт отказ, и просмотр остаётся нейтральным.
export const MODEL_MAX_BYTES=16*1024*1024, MODEL_MAX_TRIANGLES=200_000;
const MAGIC='Kaydara FBX Binary  \x00\x1a\x00', ARRAY_MAX_BYTES=64*1024*1024;
const fail=()=>{throw new Error('Invalid model file');};

function parse(bytes){
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength),text=new TextDecoder('utf-8',{fatal:true});
  if(bytes.length<27+13||text.decode(bytes.subarray(0,23))!==MAGIC||view.getUint32(23,true)!==7400)fail();
  function property(start){
    const type=String.fromCharCode(bytes[start]),at=start+1;
    if(type==='L')return[Number(view.getBigInt64(at,true)),at+8];
    if(type==='I')return[view.getInt32(at,true),at+4];
    if(type==='D')return[view.getFloat64(at,true),at+8];
    if(type==='S'||type==='R'){const end=at+4+view.getUint32(at,true);if(end>bytes.length)fail();const raw=bytes.subarray(at+4,end);return[type==='S'?text.decode(raw):raw,end];}
    if(type==='d'||type==='i'){const count=view.getUint32(at,true),packed=view.getUint32(at+4,true),end=at+12+view.getUint32(at+8,true);if(end>bytes.length||packed>1)fail();return[{type,count,packed,raw:bytes.subarray(at+12,end)},end];}
    return fail();
  }
  function node(at,depth){
    if(depth>8)fail();
    const end=view.getUint32(at,true),count=view.getUint32(at+4,true),size=view.getUint32(at+8,true),named=at+13+bytes[at+12];
    if(end===0)return[null,at+13];
    if(end>bytes.length||named+size>end)fail();
    const name=text.decode(bytes.subarray(at+13,named)),props=[],children=[];let cursor=named;
    for(let n=0;n<count;n++){const [value,next]=property(cursor);props.push(value);cursor=next;}
    if(cursor!==named+size)fail();
    while(cursor<end){const [child,next]=node(cursor,depth+1);cursor=next;if(!child)break;children.push(child);}
    if(cursor!==end)fail();
    return[{name,props,children},end];
  }
  const roots=[];let cursor=27;
  while(true){const [root,next]=node(cursor,0);if(!root)break;roots.push(root);cursor=next;}
  return roots;
}

async function numbers(value){
  if(!value||(value.type!=='d'&&value.type!=='i'))fail();
  const size=value.count*(value.type==='d'?8:4);let raw=value.raw;
  if(size>ARRAY_MAX_BYTES)fail();
  if(value.packed){
    // Распаковка останавливается на заявленном размере: лишнее означает чужой файл.
    const reader=new Blob([raw]).stream().pipeThrough(new DecompressionStream('deflate')).getReader();raw=new Uint8Array(size);let filled=0;
    while(true){const {value:chunk,done}=await reader.read();if(done)break;if(filled+chunk.length>size){await reader.cancel();fail();}raw.set(chunk,filled);filled+=chunk.length;}
    if(filled!==size)fail();
  }else{if(raw.length!==size)fail();raw=raw.slice();}
  return value.type==='d'?new Float64Array(raw.buffer):new Int32Array(raw.buffer);
}

const child=(node,name)=>node.children.find(item=>item.name===name);
const array=(node,name)=>numbers(child(node,name)?.props[0]);

// Связи файла: рисунок цвета каждого материала, материалы каждой сетки, геометрия с её сеткой.
function wiring(roots){
  const objects=roots.find(root=>root.name==='Objects'),connections=roots.find(root=>root.name==='Connections');
  if(!objects||!connections)fail();
  const byId=new Map(objects.children.map(item=>[item.props[0],item]));
  const media=new Map(),picture=new Map(),slots=new Map(),placed=[];
  for(const {name,props:[kind,source,target,channel]} of connections.children){
    if(name!=='C')continue;
    const pair=`${kind} ${byId.get(source)?.name} ${byId.get(target)?.name}`;
    if(pair==='OO Video Texture'){if(!media.has(target))media.set(target,source);}
    else if(pair==='OO Material Model'){if(!slots.has(target))slots.set(target,[]);slots.get(target).push(source);}
    else if(pair==='OO Geometry Model')placed.push([byId.get(source),target]);
    else if(pair==='OP Texture Material'&&channel==='DiffuseColor'&&!picture.has(target))picture.set(target,source);
  }
  // Один файл текстуры служит нескольким материалам: байты берутся из одного узла.
  const content=new Map();
  for(const [texture,video] of media){const data=child(byId.get(video),'Content')?.props[0];if(data instanceof Uint8Array)content.set(texture,data);}
  return{placed,slots,image:material=>content.get(picture.get(material))||null};
}
// Треугольники одной сетки, разложенные по её материалам; координаты отсчитаны от центра.
async function surfacesOf([geometry,model,vertices,index],links,centre){
  const faces=index.length/3,count=vertices.length/3;
  const unwrapping=child(geometry,'LayerElementUV'),assignment=child(geometry,'LayerElementMaterial');
  const [uv,uvIndex,assigned]=await Promise.all([unwrapping?array(unwrapping,'UV'):null,unwrapping?array(unwrapping,'UVIndex'):null,assignment?array(assignment,'Materials'):null]);
  if(uv&&(uvIndex.length!==index.length||uv.length%2))fail();
  if(assigned&&assigned.length!==faces&&assigned.length!==1)fail();
  const materials=links.slots.get(model)||[],slotOf=face=>assigned?assigned[assigned.length===1?0:face]:-1,sizes=new Map();
  for(let face=0;face<faces;face++)sizes.set(slotOf(face),(sizes.get(slotOf(face))||0)+1);
  const parts=new Map();
  for(const [slot,size] of sizes){
    if(slot!==-1&&!(slot>=0&&slot<materials.length))fail();
    parts.set(slot,{positions:new Float32Array(size*9),uvs:uv?new Float32Array(size*6):null,image:links.image(materials[slot]),filled:0});
  }
  for(let at=0;at<index.length;at++){
    const part=parts.get(slotOf(Math.floor(at/3))),encoded=index[at],vertex=at%3===2?-encoded-1:encoded;
    if(!(vertex>=0&&vertex<count))fail();
    for(let axis=0;axis<3;axis++)part.positions[part.filled*3+axis]=vertices[vertex*3+axis]-centre[axis];
    if(uv){
      const pair=uvIndex[at];
      if(!(pair>=0&&pair<uv.length/2)||!Number.isFinite(uv[pair*2])||!Number.isFinite(uv[pair*2+1]))fail();
      part.uvs[part.filled*2]=uv[pair*2];part.uvs[part.filled*2+1]=uv[pair*2+1];
    }
    part.filled+=1;
  }
  return[...parts.values()].map(({positions,uvs,image})=>({positions,uvs,image}));
}
// Поверхности модели: по одной на сетку и материал. Углы треугольников идут подряд.
export async function readModel(buffer){
  const bytes=new Uint8Array(buffer);
  if(bytes.length>MODEL_MAX_BYTES)fail();
  const links=wiring(parse(bytes)),sources=[],min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];let triangles=0;
  for(const [geometry,model] of links.placed){
    const [vertices,index]=await Promise.all([array(geometry,'Vertices'),array(geometry,'PolygonVertexIndex')]);
    if(!(vertices instanceof Float64Array)||!(index instanceof Int32Array)||vertices.length%3||index.length%3||!index.length)fail();
    triangles+=index.length/3;
    if(triangles>MODEL_MAX_TRIANGLES)fail();
    for(let n=0;n<vertices.length;n++){const value=vertices[n],axis=n%3;if(!Number.isFinite(value))fail();if(value<min[axis])min[axis]=value;if(value>max[axis])max[axis]=value;}
    sources.push([geometry,model,vertices,index]);
  }
  if(!triangles)fail();
  // Перенос к центру идёт в двойной точности, до округления до одинарной.
  const centre=min.map((low,axis)=>low/2+max[axis]/2),surfaces=[];
  for(const source of sources)surfaces.push(...await surfacesOf(source,links,centre));
  return{surfaces,triangles,centre,bounds:{min:min.map((low,axis)=>low-centre[axis]),max:max.map((high,axis)=>high-centre[axis])}};
}
