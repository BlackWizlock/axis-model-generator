import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const MAX_VERTICES=300_000, MAX_TRIANGLES=200_000, MAX_WIRE=16*1024*1024;
const vector=value=>Array.isArray(value)&&value.length===3&&value.every(Number.isFinite);
export function previewReason(code){
  const reasons={zip_fbx_preview_not_verified:'Просмотр ZIP с FBX пока не подтверждён. Технический отчёт остаётся доступен.',preview_budget:'Геометрия превышает безопасный бюджет просмотра.',preview_unsupported:'Формат геометрии пока не поддерживается для просмотра.',preview_roundtrip_error:'Точность геометрии недостаточна для безопасного просмотра.',webgl_unavailable:'Браузер не смог включить WebGL. Вы можете прочитать и скачать отчёт.'};
  return reasons[code]||'Трёхмерный просмотр недоступен. Проверьте технический отчёт.';
}
export function validatePreview(value){
  if(!value||value.schemaVersion!==1||!['synthetic','uploaded_package'].includes(value.provenance)||value.coordinates!=='processing-metres-rebased')throw new Error('Invalid preview contract');
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
export function mountPreview(container,document){
  const preview=validatePreview(document);let renderer;
  try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});}
  catch{container.textContent=previewReason('webgl_unavailable');return{dispose(){},reset(){}};}
  const scene=new THREE.Scene();scene.background=new THREE.Color('#191e25');
  const camera=new THREE.PerspectiveCamera(38,1,0.01,10000);camera.up.set(0,0,1);
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;renderer.domElement.tabIndex=0;controls.listenToKeyEvents(renderer.domElement);
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(preview.positions,3));geometry.setIndex(preview.indices);geometry.computeVertexNormals();
  const material=new THREE.MeshStandardMaterial({color:0x9baabb,roughness:0.85,metalness:0.02,side:THREE.DoubleSide});
  scene.add(new THREE.Mesh(geometry,material));scene.add(new THREE.HemisphereLight(0xffffff,0x415365,2.4));
  const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(4,-8,10);scene.add(light);
  const bounds=new THREE.Box3(new THREE.Vector3(...preview.bounds.min),new THREE.Vector3(...preview.bounds.max));
  const center=bounds.getCenter(new THREE.Vector3());const size=bounds.getSize(new THREE.Vector3());const radius=Math.max(size.length()/2,0.01);
  const grid=new THREE.GridHelper(radius*4,20,0x455366,0x2e3845);grid.rotation.x=Math.PI/2;grid.position.set(center.x,center.y,preview.bounds.min[2]-radius*0.02);scene.add(grid);
  const reset=()=>{const aspect=Math.max(camera.aspect,0.01);const distance=radius/Math.sin(THREE.MathUtils.degToRad(camera.fov/2))/Math.min(aspect,1)*1.25;camera.near=Math.max(distance/10000,0.00001);camera.far=distance+radius*20;camera.position.copy(center).add(new THREE.Vector3(1,-1.5,1).normalize().multiplyScalar(distance));camera.updateProjectionMatrix();controls.target.copy(center);controls.update();};
  let disposed=false;
  const resize=()=>{if(disposed)return;const width=Math.max(container.clientWidth,1),height=Math.max(container.clientHeight,1);renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio||1,2));renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix();};
  renderer.domElement.setAttribute('role','img');renderer.domElement.setAttribute('aria-label',preview.provenance==='synthetic'?'Синтетическая демонстрация геометрии':'Проверенная геометрия вашего пакета, нейтральные материалы');
  const contextLost=event=>{event.preventDefault();renderer.setAnimationLoop(null);container.textContent=previewReason('webgl_unavailable');};
  renderer.domElement.addEventListener('webglcontextlost',contextLost);container.replaceChildren(renderer.domElement);resize();reset();
  const observer=new ResizeObserver(resize);observer.observe(container);
  renderer.setAnimationLoop(()=>{if(!disposed){controls.update();renderer.render(scene,camera);}});
  return{reset,dispose(){if(disposed)return;disposed=true;observer.disconnect();renderer.setAnimationLoop(null);renderer.domElement.removeEventListener('webglcontextlost',contextLost);controls.dispose();geometry.dispose();material.dispose();grid.geometry.dispose();for(const item of Array.isArray(grid.material)?grid.material:[grid.material])item.dispose();renderer.dispose();renderer.forceContextLoss();container.replaceChildren();}};
}
