import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const MAX_VERTICES=300_000, MAX_TRIANGLES=200_000, MAX_WIRE=16*1024*1024;
const vector=value=>Array.isArray(value)&&value.length===3&&value.every(Number.isFinite);
export function previewReason(code){
  const reasons={zip_fbx_preview_not_verified:'Просмотр ZIP с FBX пока не подтверждён. Технический отчёт остаётся доступен.',preview_budget:'Геометрия превышает безопасный бюджет просмотра.',preview_unsupported:'Формат геометрии пока не поддерживается для просмотра.',preview_roundtrip_error:'Точность геометрии недостаточна для безопасного просмотра.',webgl_unavailable:'Браузер не смог включить WebGL. Вы можете прочитать и скачать отчёт.'};
  return reasons[code]||'Трёхмерный просмотр недоступен. Проверьте технический отчёт.';
}
export function validatePreview(value){
  if(!value||value.schemaVersion!==1||!['synthetic','uploaded_package','reduced_model'].includes(value.provenance)||value.coordinates!=='processing-metres-rebased')throw new Error('Invalid preview contract');
  if(!vector(value.origin)||!vector(value.bounds?.min)||!vector(value.bounds?.max)||value.bounds.min.some((n,i)=>n>value.bounds.max[i]))throw new Error('Invalid preview bounds');
  if(!Number.isSafeInteger(value.vertexCount)||value.vertexCount<3||value.vertexCount>MAX_VERTICES||!Number.isSafeInteger(value.triangleCount)||value.triangleCount<1||value.triangleCount>MAX_TRIANGLES)throw new Error('Preview budget exceeded');
  if(!Array.isArray(value.positions)||value.positions.length!==value.vertexCount*3||!Array.isArray(value.indices)||value.indices.length!==value.triangleCount*3)throw new Error('Invalid preview counts');
  if(!Array.isArray(value.limitations)||value.limitations.length>32||value.limitations.some(n=>typeof n!=='string'||n.length>1024))throw new Error('Invalid limitations');
  const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
  for(let i=0;i<value.positions.length;i++){
    const n=value.positions[i],rounded=Math.fround(n);
    if(!Number.isFinite(n)||!Number.isFinite(rounded)||Math.abs(n-rounded)>1e-5)throw new Error('Invalid preview positions');
    const axis=i%3;min[axis]=Math.min(min[axis],n);max[axis]=Math.max(max[axis],n);
  }
  if(min.some((n,i)=>Math.abs(n-value.bounds.min[i])>1e-5)||max.some((n,i)=>Math.abs(n-value.bounds.max[i])>1e-5))throw new Error('Inconsistent preview bounds');
  if(!value.indices.every(n=>Number.isSafeInteger(n)&&n>=0&&n<value.vertexCount))throw new Error('Invalid preview indices');
  if(new TextEncoder().encode(JSON.stringify(value)).byteLength>MAX_WIRE)throw new Error('Preview wire budget exceeded');
  return value;
}
const PICTURE_MAX_SIDE=8192,PICTURE_MAX_PIXELS=96_000_000;
// PNG называет свой размер в заголовке: слишком большой рисунок не раскрывается.
export function pictureFits(bytes){
  if(!(bytes instanceof Uint8Array)||bytes.length<24||bytes[0]!==0x89||bytes[1]!==0x50||bytes[2]!==0x4e||bytes[3]!==0x47)return false;
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength),width=view.getUint32(16),height=view.getUint32(20);
  return width>=1&&height>=1&&width<=PICTURE_MAX_SIDE&&height<=PICTURE_MAX_SIDE;
}
// Модель принимается к показу, только если это та же геометрия, что в проверенном документе просмотра.
export function matchingModel(preview,model){
  if(!model||model.triangles!==preview.triangleCount||!Array.isArray(model.surfaces)||!model.surfaces.length)return null;
  for(let axis=0;axis<3;axis++)if(Math.abs(model.bounds.min[axis]-preview.bounds.min[axis])>1e-3||Math.abs(model.bounds.max[axis]-preview.bounds.max[axis])>1e-3)return null;
  return model;
}
// Рисунки, которые будут раскрыты: каждый в пределах стороны, все вместе в пределах общего числа точек.
function shownPictures(surfaces){
  const shown=new Set();let pixels=0;
  for(const {uvs,image} of surfaces){
    if(!uvs||shown.has(image)||!pictureFits(image))continue;
    const view=new DataView(image.buffer,image.byteOffset,image.byteLength),size=view.getUint32(16)*view.getUint32(20);
    if(pixels+size<=PICTURE_MAX_PIXELS){shown.add(image);pixels+=size;}
  }
  return shown;
}
// Число поверхностей, которые будут показаны с рисунком исходника.
export function texturedSurfaces(preview,model){
  const surfaces=matchingModel(preview,model)?.surfaces||[],shown=shownPictures(surfaces);
  return surfaces.filter(surface=>surface.uvs&&shown.has(surface.image)).length;
}
function surfaceGeometry(surface){
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(surface.positions,3));
  if(surface.uvs){
    // Рисунок передаётся без переворота, поэтому вертикальная координата отсчитывается сверху.
    const uvs=new Float32Array(surface.uvs.length);for(let n=0;n<uvs.length;n+=2){uvs[n]=surface.uvs[n];uvs[n+1]=1-surface.uvs[n+1];}
    geometry.setAttribute('uv',new THREE.BufferAttribute(uvs,2));
  }
  geometry.computeVertexNormals();return geometry;
}
export function mountPreview(container,document,model=null){
  const preview=validatePreview(document),shown=matchingModel(preview,model),textured=texturedSurfaces(preview,model);let renderer;
  try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});}
  catch{container.textContent=previewReason('webgl_unavailable');return{textured:0,dispose(){},reset(){}};}
  const scene=new THREE.Scene();scene.background=new THREE.Color('#191e25');
  const camera=new THREE.PerspectiveCamera(38,1,0.01,10000);camera.up.set(0,0,1);
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;renderer.domElement.tabIndex=0;controls.listenToKeyEvents(renderer.domElement);
  const material=new THREE.MeshStandardMaterial({color:0x9baabb,roughness:0.85,metalness:0.02,side:THREE.DoubleSide});
  const owned=[material],pictures=new Map();let disposed=false;
  if(shown){
    const fitting=shownPictures(shown.surfaces);
    for(const surface of shown.surfaces){
      const geometry=surfaceGeometry(surface),mesh=new THREE.Mesh(geometry,material);owned.push(geometry);scene.add(mesh);
      if(surface.uvs&&fitting.has(surface.image)&&typeof createImageBitmap==='function'){if(!pictures.has(surface.image))pictures.set(surface.image,[]);pictures.get(surface.image).push(mesh);}
    }
    // Один файл рисунка раскрывается один раз и служит всем своим поверхностям.
    for(const [bytes,meshes] of pictures)createImageBitmap(new Blob([bytes],{type:'image/png'})).then(bitmap=>{
      if(disposed){bitmap.close();return;}
      const texture=new THREE.Texture(bitmap);texture.flipY=false;texture.colorSpace=THREE.SRGBColorSpace;texture.wrapS=texture.wrapT=THREE.RepeatWrapping;texture.anisotropy=Math.min(8,renderer.capabilities.getMaxAnisotropy());texture.needsUpdate=true;
      const drawn=new THREE.MeshBasicMaterial({map:texture,side:THREE.DoubleSide});owned.push(drawn,{dispose(){texture.dispose();bitmap.close();}});
      for(const mesh of meshes)mesh.material=drawn;
    },()=>{});
  }else{
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(preview.positions,3));geometry.setIndex(preview.indices);geometry.computeVertexNormals();
    owned.push(geometry);scene.add(new THREE.Mesh(geometry,material));
  }
  scene.add(new THREE.HemisphereLight(0xffffff,0x415365,2.4));
  const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(4,-8,10);scene.add(light);
  const bounds=new THREE.Box3(new THREE.Vector3(...preview.bounds.min),new THREE.Vector3(...preview.bounds.max));
  const center=bounds.getCenter(new THREE.Vector3());const size=bounds.getSize(new THREE.Vector3());const radius=Math.max(size.length()/2,0.01);
  const grid=new THREE.GridHelper(radius*4,20,0x455366,0x2e3845);grid.rotation.x=Math.PI/2;grid.position.set(center.x,center.y,preview.bounds.min[2]-radius*0.02);scene.add(grid);
  const reset=()=>{const aspect=Math.max(camera.aspect,0.01);const distance=radius/Math.sin(THREE.MathUtils.degToRad(camera.fov/2))/Math.min(aspect,1)*1.25;camera.near=Math.max(distance/10000,0.00001);camera.far=distance+radius*20;camera.position.copy(center).add(new THREE.Vector3(1,-1.5,1).normalize().multiplyScalar(distance));camera.updateProjectionMatrix();controls.target.copy(center);controls.update();};
  const resize=()=>{if(disposed)return;const width=Math.max(container.clientWidth,1),height=Math.max(container.clientHeight,1);renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio||1,2));renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix();};
  const label=preview.provenance==='synthetic'?'Синтетическая демонстрация геометрии':preview.provenance!=='reduced_model'?'Проверенная геометрия вашего пакета, нейтральные материалы':textured?'Упрощённая модель из вашего FBX с текстурами исходника':'Упрощённая модель из вашего FBX, нейтральные материалы';
  renderer.domElement.setAttribute('role','img');renderer.domElement.setAttribute('aria-label',label);
  const contextLost=event=>{event.preventDefault();renderer.setAnimationLoop(null);container.textContent=previewReason('webgl_unavailable');};
  renderer.domElement.addEventListener('webglcontextlost',contextLost);container.replaceChildren(renderer.domElement);resize();reset();
  const observer=new ResizeObserver(resize);observer.observe(container);
  renderer.setAnimationLoop(()=>{if(!disposed){controls.update();renderer.render(scene,camera);}});
  return{textured,reset,dispose(){if(disposed)return;disposed=true;observer.disconnect();renderer.setAnimationLoop(null);renderer.domElement.removeEventListener('webglcontextlost',contextLost);controls.dispose();for(const item of owned)item.dispose();grid.geometry.dispose();for(const item of Array.isArray(grid.material)?grid.material:[grid.material])item.dispose();renderer.dispose();renderer.forceContextLoss();container.replaceChildren();}};
}
