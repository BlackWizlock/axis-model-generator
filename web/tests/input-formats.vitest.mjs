import {expect, test} from 'vitest';
import {allowedExtensions, acceptsFile, kindForFile} from '../src/input-formats.js';
test('unavailable native engines do not enter upload allowlist', () => {
  const rows = [{id:'zip-fbx', extensions:['.zip'], upload:true},
    {id:'rvt', extensions:['.rvt'], upload:false}];
  expect(allowedExtensions(rows)).toBe('.zip');
  expect(acceptsFile({name:'MODEL.RVT'}, 'rvt', rows)).toBe(false);
  expect(acceptsFile({name:'MODEL.ZIP'}, 'zip-fbx', rows)).toBe(true);
});
test('standalone FBX is accepted only under its own kind', () => {
  const rows = [{id:'zip-fbx', extensions:['.zip'], upload:true}, {id:'fbx', extensions:['.fbx'], upload:true}];
  expect(allowedExtensions(rows)).toBe('.zip,.fbx');
  expect(acceptsFile({name:'HOUSE.FBX'}, 'fbx', rows)).toBe(true);
  expect(acceptsFile({name:'house.fbx'}, 'zip-fbx', rows)).toBe(false);
  expect(acceptsFile({name:'house.zip'}, 'fbx', rows)).toBe(false);
});
test('a file picks its kind only when the current kind does not accept it', () => {
  const rows = [{id:'portable-package', extensions:['.zip'], upload:true}, {id:'zip-fbx', extensions:['.zip'], upload:true},
    {id:'fbx', extensions:['.fbx'], upload:true}, {id:'rvt', extensions:['.rvt'], upload:false}];
  expect(kindForFile({name:'HOUSE.FBX'}, 'portable-package', rows)).toBe('fbx');
  expect(kindForFile({name:'site.zip'}, 'fbx', rows)).toBe('zip-fbx');
  expect(kindForFile({name:'site.zip'}, 'portable-package', rows)).toBe('portable-package');
  expect(kindForFile({name:'model.rvt'}, 'fbx', rows)).toBe('fbx');
  expect(kindForFile({name:'site.zip'}, 'fbx', null)).toBe('fbx');
});

import {api, ApiError} from '../src/api.js';
const format = (changes={}) => ({id:'zip-fbx',extensions:['.zip'],upload:true,diagnostics:true,preview:false,generation:false,reason:null,...changes});
test('allowlist deduplicates case-insensitive matches and preserves explicit ZIP kind',()=>{
 const rows=[format(),format({id:'portable-package'})];
 expect(allowedExtensions(rows)).toBe('.zip');
 expect(acceptsFile({name:'model.ZIP'},'portable-package',rows)).toBe(true);
 expect(acceptsFile({name:'model.zip'},'unknown',rows)).toBe(false);
 for(const invalidRows of [null,{},[],[{id:'zip-fbx',upload:'true',extensions:['.zip']}], [{id:'zip-fbx',upload:true,extensions:['.*']}]]){
  expect(allowedExtensions(invalidRows)).toBe('');expect(acceptsFile({name:'model.zip'},'zip-fbx',invalidRows)).toBe(false);
 }
});

const malformedMatrices = [undefined,null,{},Array(33).fill(format()),[format(),format()],
 [format({id:'unknown'})],[format({extensions:[]})],[format({extensions:['zip']})],
 [format({extensions:['.abcdefghi']})],[format({extensions:Array(9).fill('.zip')})],
 [format({reason:42})],[format({reason:'x'.repeat(2049)})],
 ...['upload','diagnostics','preview','generation'].map(key=>[format({[key]:'true'})])];
test.each(malformedMatrices.map((rows,index)=>[index,rows]))('config API decoder rejects malformed matrix %i',async(index,inputFormats)=>{
 const previous=globalThis.fetch;
 globalThis.fetch=async()=>new Response(JSON.stringify({inputFormats}),{headers:{'Content-Type':'application/json'}});
 try{await expect(api('/api/config')).rejects.toMatchObject({code:'invalid_config'});}finally{globalThis.fetch=previous;}
});
test('config API decoder accepts boolean matrix without changing capabilities',async()=>{
 const previous=globalThis.fetch,valid={inputFormats:[format()],limits:{uploadBytes:256*1024*1024}};
 globalThis.fetch=async()=>new Response(JSON.stringify(valid),{headers:{'Content-Type':'application/json'}});
 try{expect((await api('/api/config')).inputFormats).toEqual(valid.inputFormats);}finally{globalThis.fetch=previous;}
 expect(ApiError).toBeDefined();
});
