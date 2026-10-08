import test from 'node:test';
import assert from 'node:assert/strict';
test('shared mobile menu Escape restores focus and navigation closes; manual light theme',async()=>{
 const events={},nodes={};let focused=false;
 for(const id of ['menu-toggle','site-menu','theme-toggle']){
  const attributes={'aria-expanded':'false'},classes=new Set();nodes[id]={setAttribute(k,v){attributes[k]=v;},getAttribute(k){return attributes[k];},focus(){focused=true;},addEventListener(k,fn){events[`${id}:${k}`]=fn;},classList:{toggle(k,force){const enabled=force===undefined?!classes.has(k):force;if(enabled)classes.add(k);else classes.delete(k);return force;}}};
 }
 const classes=new Set(['light']);const previous=globalThis.document;
 globalThis.document={getElementById:id=>nodes[id],addEventListener(k,fn){events[k]=fn;},documentElement:{classList:{toggle(k){if(classes.has(k)){classes.delete(k);return false;}classes.add(k);return true;}}}};
 try{
  await import('../src/shell.js');events['menu-toggle:click']();assert.equal(nodes['menu-toggle'].getAttribute('aria-expanded'),'true');events.keydown({key:'Escape'});assert.equal(focused,true);assert.equal(nodes['menu-toggle'].getAttribute('aria-expanded'),'false');
  events['menu-toggle:click']();events['site-menu:click']({target:{closest:()=>({})}});assert.equal(nodes['menu-toggle'].getAttribute('aria-expanded'),'false');events['theme-toggle:click']();assert.equal(classes.has('light'),false);
 }finally{globalThis.document=previous;}
});
