import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile,writeFile,copyFile,mkdir,rm,readdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../',import.meta.url));const source=path.join(root,'web');const dest=path.join(source,'dist');
const pkg=JSON.parse(await readFile(path.join(source,'node_modules/three/package.json'),'utf8'));
if(pkg.version!=='0.186.1')throw new Error('Three.js version mismatch');
await mkdir(dest,{recursive:true});for(const name of await readdir(dest))await rm(path.join(dest,name),{recursive:true,force:true});await mkdir(path.join(dest,'src'),{recursive:true});await mkdir(path.join(dest,'vendor'),{recursive:true});
const assets=['revit-model.svg','dwg-plan.svg','npm-result.svg','axis-sign.png','max-icon.png','inventory.json','fonts/manrope-latin-wght-normal.woff2','fonts/manrope-cyrillic-wght-normal.woff2','fonts/geologica-latin-700-normal.woff2','fonts/geologica-cyrillic-700-normal.woff2','fonts/MANROPE-OFL.txt','fonts/GEOLOGICA-OFL.txt','fonts/geologica-latin-800-normal.woff2','fonts/geologica-cyrillic-800-normal.woff2'];
await mkdir(path.join(dest,'assets','fonts'),{recursive:true});for(const name of assets)await copyFile(path.join(source,'assets',name),path.join(dest,'assets',name));
const own=['input-formats.js','api.js','auth.js','upload.js','sha256.js','hash-worker.js','jobs.js','preview.js','app.js','checks.js','shell.js','analytics.js'];
for(const name of ['index.html','privacy.html','support.html','analytics-consent.html','styles.css','robots.txt','favicon.ico'])await copyFile(path.join(source,name),path.join(dest,name));
await promisify(execFile)(process.execPath,[path.join(root,'scripts/build-sitemap.mjs'),'--domain','https://model.axisconsult.ru','--kind','static','--artifact',dest],{cwd:source});
for(const name of own){const text=await readFile(path.join(source,'src',name),'utf8');await writeFile(path.join(dest,'src',name),text.replaceAll("from 'three';","from '../vendor/three.module.js';").replaceAll("from 'three/addons/controls/OrbitControls.js';","from '../vendor/OrbitControls.js';"));}
const vendor=[['build/three.module.js','three.module.js'],['build/three.core.js','three.core.js'],['examples/jsm/controls/OrbitControls.js','OrbitControls.js'],['LICENSE','THREE-LICENSE.txt']];
for(const [from,to]of vendor){const text=await readFile(path.join(source,'node_modules/three',from),'utf8');await writeFile(path.join(dest,'vendor',to),text.replace(/from\s+(['"])three\1/g,"from './three.module.js'"));}
// Fail if a new Three.js release introduced a dependency outside this fixed closure.
for(const name of ['three.module.js','three.core.js','OrbitControls.js']){
 const text=await readFile(path.join(dest,'vendor',name),'utf8');
 for(const match of text.replace(/\/\*[\s\S]*?\*\//g,'').matchAll(/(?:from\s+|import\s*\()(['"])([^'"]+)\1/g))if(!['./three.module.js','./three.core.js'].includes(match[2]))throw new Error(`Unexpected vendor dependency: ${match[2]}`);
}
await copyFile(path.join(source,'THIRD-PARTY-NOTICES.md'),path.join(dest,'THIRD-PARTY-NOTICES.md'));
console.log('Built web/dist: own ES modules and Three.js 0.186.1 fixed same-origin closure with MIT license; local Axis brand/fonts with OFL licenses.');
