import {test} from 'vitest';
import assert from 'node:assert/strict';
import {readModel,MODEL_MAX_BYTES} from '../src/model-view.js';
import {pictureFits,matchingModel,texturedSurfaces,mountPreview} from '../src/preview.js';
import {MODEL_BASE64,PICTURE_BASE64} from './support/model-fixture.mjs';
const bytes=text=>Uint8Array.from(Buffer.from(text,'base64'));
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-6,`${actual} is not ${expected}`);
test('model reader returns the surfaces the writer stored',async()=>{
 const model=await readModel(bytes(MODEL_BASE64).buffer),picture=bytes(PICTURE_BASE64);
 assert.equal(model.triangles,46);assert.deepEqual(model.centre,[115.5,3.5,1.5]);
 assert.deepEqual(model.bounds,{min:[-15.5,-3.5,-1.5],max:[15.5,3.5,1.5]});
 assert.deepEqual(model.surfaces.map(surface=>[surface.positions.length/9,Boolean(surface.uvs),Boolean(surface.image)]),[[40,true,true],[2,true,true],[2,false,false],[2,false,false]]);
 const [wall,sign,plain,glass]=model.surfaces;
 // Массивы стены длиннее 128 байт и лежат в файле сжатыми.
 assert.deepEqual([...wall.positions.slice(0,9)],[-15.5,3.5,-1.5,-14,3.5,-1.5,-14,3.5,-0.75]);
 [0,0,0.2,0,0.2,0.25].forEach((value,n)=>near(wall.uvs[n],value));
 [0.8,0.75,1,1,0.8,1].forEach((value,n)=>near(wall.uvs[wall.uvs.length-6+n],value));
 assert.deepEqual([...sign.uvs],[0.25,0.25,0.75,0.25,0.75,0.75,0.25,0.25,0.75,0.75,0.25,0.75]);
 assert.deepEqual([...wall.image],[...picture]);
 // Один файл рисунка на два материала: байты общие, раскрывать его дважды не нужно.
 assert.equal(sign.image,wall.image);
 // Треугольники блока разложены по материалам: первый и последний, затем два средних.
 assert.deepEqual([...plain.positions.slice(0,9)],[14.5,-3.5,-1.5,15.5,-3.5,-1.5,15,-3,1.5]);
 assert.deepEqual([...glass.positions.slice(0,9)],[15.5,-3.5,-1.5,15.5,-2.5,-1.5,15,-3,1.5]);
});
test('model reader refuses anything but a whole file of the writer',async()=>{
 const whole=bytes(MODEL_BASE64);
 const broken=[new Uint8Array(0),whole.slice(0,200),whole.slice(0,whole.length>>1),new Uint8Array(MODEL_MAX_BYTES+1)];
 const magic=whole.slice();magic[0]=0x4c;broken.push(magic);
 const version=whole.slice();version[23]=0x4c;broken.push(version);
 // Смещение конца первого узла уведено за пределы файла.
 const offset=whole.slice();offset[30]=0xff;broken.push(offset);
 for(const item of broken)await assert.rejects(readModel(item.buffer),/Invalid model file|Offset|bounds|range/i);
});
test('only the geometry of the checked view is shown with pictures',async()=>{
 const model=await readModel(bytes(MODEL_BASE64).buffer),preview={triangleCount:46,bounds:model.bounds};
 assert.equal(matchingModel(preview,model),model);assert.equal(texturedSurfaces(preview,model),2);
 assert.equal(texturedSurfaces({...preview,triangleCount:45},model),0);
 assert.equal(texturedSurfaces({...preview,bounds:{min:[-15.5,-3.5,-1.4],max:model.bounds.max}},model),0);
 assert.equal(texturedSurfaces(preview,null),0);assert.equal(matchingModel(preview,{...model,surfaces:[]}),null);
 const picture=bytes(PICTURE_BASE64);assert.equal(pictureFits(picture),true);
 const huge=picture.slice();huge[18]=0x40;assert.equal(pictureFits(huge),false);
 const empty=picture.slice();empty.fill(0,16,24);assert.equal(pictureFits(empty),false);
 assert.equal(pictureFits(new Uint8Array(64)),false);assert.equal(pictureFits(null),false);assert.equal(pictureFits(picture.slice(0,20)),false);
 // Рисунки сверх общего числа точек остаются нераскрытыми, уже принятый рисунок служит всем своим поверхностям.
 const wide=()=>{const copy=picture.slice();copy.set([0,0,0x20,0,0,0,0x20,0],16);return copy;},first=wide(),second=wide(),painted=model.surfaces.find(surface=>surface.uvs);
 assert.equal(pictureFits(first),true);
 assert.equal(texturedSurfaces(preview,{...model,surfaces:[{...painted,image:first},{...painted,image:second},{...painted,image:first}]}),2);
});
test('WebGL failure with a model leaves text and claims no pictures',async()=>{
 const model=await readModel(bytes(MODEL_BASE64).buffer);
 const document={schemaVersion:1,provenance:'reduced_model',coordinates:'processing-metres-rebased',origin:[0,0,0],bounds:{min:[0,0,0],max:[1,1,0]},positions:[0,0,0,1,0,0,0,1,0],indices:[0,1,2],vertexCount:3,triangleCount:1,limitations:[]};
 const container={textContent:''};const view=mountPreview(container,document,model);
 assert.match(container.textContent,/WebGL/);assert.equal(view.textured,0);assert.doesNotThrow(()=>view.reset());assert.doesNotThrow(()=>view.dispose());
});
