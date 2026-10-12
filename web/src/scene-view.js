import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {extent} from './scene.js';
const PLAN_COLOUR=0x4fb8a8,BUILDING_COLOURS=[0xb9c4d2,0xd9b98a,0x9fb7e0,0xcfa3c4,0xa9cf9b,0xe0a08f];
// Совмещённый вид: части уже лежат в общих координатах сцены, здесь они только рисуются.
export function mountScene(container,parts,markers=[]){
  let renderer;
  try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});}
  catch{container.textContent='Трёхмерный просмотр недоступен в этом браузере.';return{dispose(){},reset(){}};}
  const scene=new THREE.Scene();scene.background=new THREE.Color('#191e25');
  const camera=new THREE.PerspectiveCamera(38,1,0.01,10000);camera.up.set(0,0,1);
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;renderer.domElement.tabIndex=0;controls.listenToKeyEvents(renderer.domElement);
  const owned=[];let disposed=false,building=0;
  for(const part of parts){
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(part.positions,3));geometry.setIndex(part.indices);geometry.computeVertexNormals();
    const colour=part.plan?PLAN_COLOUR:BUILDING_COLOURS[building++%BUILDING_COLOURS.length];
    const material=new THREE.MeshStandardMaterial({color:colour,roughness:0.85,metalness:0.02,side:THREE.DoubleSide});
    owned.push(geometry,material);scene.add(new THREE.Mesh(geometry,material));
  }
  const box=extent(parts.map(part=>part.positions))||{min:[0,0,0],max:[1,1,1]};
  const bounds=new THREE.Box3(new THREE.Vector3(...box.min),new THREE.Vector3(...box.max));
  const center=bounds.getCenter(new THREE.Vector3()),radius=Math.max(bounds.getSize(new THREE.Vector3()).length()/2,0.01);
  // Точка вставки видна сама: вертикальная веха в заданных координатах.
  for(const point of markers){
    const height=Math.max(radius*0.12,1),geometry=new THREE.CylinderGeometry(height*0.02,height*0.02,height,8),material=new THREE.MeshBasicMaterial({color:0xff6b4a});
    const pole=new THREE.Mesh(geometry,material);pole.rotation.x=Math.PI/2;pole.position.set(point[0],point[1],point[2]+height/2);owned.push(geometry,material);scene.add(pole);
  }
  scene.add(new THREE.HemisphereLight(0xffffff,0x415365,2.4));
  const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(4,-8,10);scene.add(light);
  const reset=()=>{const aspect=Math.max(camera.aspect,0.01);const distance=radius/Math.sin(THREE.MathUtils.degToRad(camera.fov/2))/Math.min(aspect,1)*1.25;camera.near=Math.max(distance/10000,0.00001);camera.far=distance+radius*20;camera.position.copy(center).add(new THREE.Vector3(1,-1.5,1).normalize().multiplyScalar(distance));camera.updateProjectionMatrix();controls.target.copy(center);controls.update();};
  const resize=()=>{if(disposed)return;const width=Math.max(container.clientWidth,1),height=Math.max(container.clientHeight,1);renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio||1,2));renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix();};
  renderer.domElement.setAttribute('role','img');renderer.domElement.setAttribute('aria-label','Генплан и сооружения в общих координатах');
  const contextLost=event=>{event.preventDefault();renderer.setAnimationLoop(null);container.textContent='Трёхмерный просмотр остановлен браузером. Соберите сцену ещё раз.';};
  renderer.domElement.addEventListener('webglcontextlost',contextLost);container.replaceChildren(renderer.domElement);resize();reset();
  const observer=new ResizeObserver(resize);observer.observe(container);
  renderer.setAnimationLoop(()=>{if(!disposed){controls.update();renderer.render(scene,camera);}});
  return{reset,dispose(){if(disposed)return;disposed=true;observer.disconnect();renderer.setAnimationLoop(null);renderer.domElement.removeEventListener('webglcontextlost',contextLost);controls.dispose();for(const item of owned)item.dispose();renderer.dispose();renderer.forceContextLoss();container.replaceChildren();}};
}
