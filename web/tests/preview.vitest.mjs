import {test} from 'vitest';
import assert from 'node:assert/strict';
import {validatePreview, previewReason} from '../src/preview.js';
const scene=()=>({schemaVersion:1,provenance:'synthetic',coordinates:'processing-metres-rebased',origin:[0,0,0],bounds:{min:[0,0,0],max:[1,1,0]},positions:[0,0,0,1,0,0,0,1,0],indices:[0,1,2],vertexCount:3,triangleCount:1,limitations:['Neutral materials']});
test('strict valid preview',()=>assert.equal(validatePreview(scene()).triangleCount,1));
for(const mutate of [p=>p.positions[0]=NaN,p=>p.indices[2]=3,p=>p.indices[0]=-1,p=>p.vertexCount=4,p=>p.origin[0]=Infinity,p=>p.positions[1]=1e100,p=>p.provenance='private',p=>p.bounds.max=[-1,1,0],p=>p.triangleCount=200001]) test('preview rejects unsafe geometry',()=>{const p=scene();mutate(p);assert.throws(()=>validatePreview(p));});
test('legacy and WebGL reasons remain honest',()=>{assert.match(previewReason('zip_fbx_preview_not_verified'),/ZIP.*FBX/);assert.match(previewReason('webgl_unavailable'),/WebGL/);});
import {mountPreview} from '../src/preview.js';
test('WebGL failure leaves report-independent text and usable lifecycle',()=>{
 const container={textContent:''};const view=mountPreview(container,scene());assert.match(container.textContent,/WebGL/);assert.doesNotThrow(()=>view.reset());assert.doesNotThrow(()=>view.dispose());
});
