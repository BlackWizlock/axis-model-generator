import test from 'node:test';
import assert from 'node:assert/strict';
test('shared mobile menu Escape restores focus and navigation closes; theme follows saved choice, then system, then dark',async()=>{
 const events={},nodes={};let focused=false;
 const classList=(classes=new Set())=>({has:k=>classes.has(k),toggle(k,force){const enabled=force===undefined?!classes.has(k):force;if(enabled)classes.add(k);else classes.delete(k);return enabled;}});
 for(const id of ['menu-toggle','site-menu','theme-toggle']){
  const attributes={'aria-expanded':'false'};nodes[id]={setAttribute(k,v){attributes[k]=v;},getAttribute(k){return attributes[k];},focus(){focused=true;},addEventListener(k,fn){events[`${id}:${k}`]=fn;},classList:classList()};
 }
 const root=classList();const documentRef={getElementById:id=>nodes[id],addEventListener(k,fn){events[k]=fn;},documentElement:{classList:root}};
 const previous=globalThis.document;globalThis.document=documentRef;
 try{
  const {setupShell,setupTheme}=await import('../src/shell.js');
  assert.equal(root.has('dark'),true);assert.equal(root.has('light'),false);assert.equal(nodes['theme-toggle'].getAttribute('aria-label'),'Включить светлую тему');
  events['menu-toggle:click']();assert.equal(nodes['menu-toggle'].getAttribute('aria-expanded'),'true');events.keydown({key:'Escape'});assert.equal(focused,true);assert.equal(nodes['menu-toggle'].getAttribute('aria-expanded'),'false');
  events['menu-toggle:click']();events['site-menu:click']({target:{closest:()=>({})}});assert.equal(nodes['menu-toggle'].getAttribute('aria-expanded'),'false');
  const saved=new Map();let systemChange;
  const windowRef={localStorage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>saved.set(k,v)},matchMedia:()=>({matches:true,addEventListener(_,fn){systemChange=fn;}})};
  setupShell(documentRef,windowRef);assert.equal(root.has('light'),true);assert.equal(root.has('dark'),false);assert.equal(nodes['theme-toggle'].getAttribute('aria-label'),'Включить тёмную тему');
  systemChange({matches:false});assert.equal(root.has('dark'),true);systemChange({matches:true});assert.equal(root.has('light'),true);
  events['theme-toggle:click']();assert.equal(root.has('dark'),true);assert.equal(saved.get('axis-theme'),'dark');
  systemChange({matches:true});assert.equal(root.has('dark'),true,'a saved choice outranks the system setting');
  events['theme-toggle:click']();assert.equal(saved.get('axis-theme'),'light');
  setupTheme(documentRef,{localStorage:{getItem:()=>'light',setItem(){throw new Error('blocked');}}});assert.equal(root.has('light'),true);
  events['theme-toggle:click']();assert.equal(root.has('dark'),true,'a blocked storage still switches the page');
  setupTheme(documentRef,{get localStorage(){throw new Error('denied');}});assert.equal(root.has('dark'),true);
  setupTheme(documentRef,{localStorage:{getItem(){throw new Error('denied');}}});assert.equal(root.has('dark'),true);
 }finally{globalThis.document=previous;}
});

import {readFile} from 'node:fs/promises';
test('static picker is closed until config and contains no native allowlist',async()=>{
 const html=await readFile(new URL('../index.html',import.meta.url),'utf8');
 const picker=html.match(/<input[^>]+id="input-file"[^>]*>/)[0];
 assert.match(picker,/disabled/);assert.doesNotMatch(picker,/accept=/);
 assert.match(html,/id="input-format-reasons"/);assert.doesNotMatch(html,/Для RVT нужен подготовленный пакет/);
});
