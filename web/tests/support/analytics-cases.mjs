import assert from 'node:assert/strict';

function environment(url = 'https://model.axisconsult.ru/?utm_source=test#workspace') {
  const commands = [], scripts = [], listeners = {};
  const win = {location: new URL(url), ym: (...args) => commands.push(args)};
  const doc = {referrer: 'https://example.org/path?secret=private',
    createElement: () => ({}), head: {append: script => scripts.push(script)},
    addEventListener: (event, handler) => {listeners[event] = handler;}};
  return {win, doc, commands, scripts, listeners};
}

export function analyticsCases(test) {
  test('no choice or refusal never loads SDK and never queues goals', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc);
    assert.equal(client.start(), false); assert.equal(client.start(false), false);
    client.watch('private-id'); client.observe({id:'private-id',state:'completed'});
    assert.equal(Boolean(client.goal('upload_started')), false);
    assert.deepEqual(env.commands, []); assert.deepEqual(env.scripts, []);
  });
  test('withdrawal clears queues and newly watched jobs, destroys SDK and stops goals', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start(true);
    client.watch('private-id'); client.stop();
    assert.deepEqual(env.commands.at(-1), [113547069,'destruct']);
    const count=env.commands.length; client.observe({id:'private-id',state:'completed'});
    assert.equal(Boolean(client.goal('upload_completed')), false); assert.equal(client.start(true), false);
    assert.equal(env.commands.length,count);
  });
  test('analytics sends only canonical public page and origin referrer with recording disabled', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc);
    assert.equal(client.start(true), true); assert.equal(client.start(true), false);
    const init = env.commands[0]; assert.equal(init[0], 113547069); assert.equal(init[1], 'init');
    for (const option of ['webvisor', 'clickmap', 'trackLinks', 'trackHash', 'ecommerce', 'sendTitle']) assert.equal(init[2][option], false);
    assert.equal(init[2].defer, true); assert.equal(init[2].disableYtm, true);
    assert.deepEqual(env.commands[1], [113547069, 'hit', 'https://model.axisconsult.ru/', {referer: 'https://example.org/'}]);
    assert.equal(env.scripts.length, 1); assert.equal(env.scripts[0].src, 'https://mc.yandex.ru/metrika/tag.js');
    assert.equal(env.scripts[0].referrerPolicy, 'no-referrer');
  });
  test('private paths, unknown URL parameters and other hosts never load a counter', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    for (const url of ['https://model.axisconsult.ru/api/jobs/private', 'https://model.axisconsult.ru/?token=private', 'https://model.axisconsult.ru/#private-model', 'http://model.axisconsult.ru/', 'https://localhost/']) {
      const env = environment(url); const client = createAnalytics(env.win, env.doc);
      assert.equal(client.start(true), false, url); assert.equal(client.goal('upload_started'), false);
      assert.deepEqual(env.commands, []); assert.deepEqual(env.scripts, []);
    }
  });
  test('goals allow fixed event names only and never attach application data', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start(true);
    assert.equal(client.goal('upload_completed'), true); assert.equal(client.goal('private-model.zip'), false);
    assert.deepEqual(env.commands.at(-1), [113547069, 'reachGoal', 'upload_completed']);
    assert.equal(JSON.stringify(env.commands).includes('private'), false);
  });
  test('contact events use a fixed goal and stop after navigation to a private URL', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start(true);
    env.listeners.click({target: {closest: () => ({getAttribute: () => 'mailto:info@axisconsult.ru'})}});
    assert.deepEqual(env.commands.at(-1), [113547069, 'reachGoal', 'contact_clicked']);
    env.win.location = new URL('https://model.axisconsult.ru/?token=private');
    assert.equal(Boolean(client.goal('upload_completed')), false); assert.equal(env.commands.length, 3);
  });
  test('SDK queue is bounded and discarded when the remote script cannot load', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); delete env.win.ym; env.doc.referrer = '';
    const client = createAnalytics(env.win, env.doc); assert.equal(client.start(true), true);
    for (let i = 0; i < 150; i++) client.goal('upload_started');
    assert.equal(env.win.ym.a.length, 100); assert.equal(env.win.ym.a[1][3].referer, '');
    env.scripts[0].onerror(); assert.equal(env.win.ym.a.length, 0); assert.equal(client.goal('upload_started'), false);
  });
  test('diagnostic completion counts only newly accepted completed jobs once', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start(true);
    client.observe({id: 'old-private-id', state: 'completed'});
    client.watch('new-private-id'); client.observe({id: 'new-private-id', state: 'running'});
    assert.equal(env.commands.length, 2);
    client.observe({id: 'new-private-id', state: 'completed'}); client.observe({id: 'new-private-id', state: 'completed'});
    assert.deepEqual(env.commands.at(-1), [113547069, 'reachGoal', 'diagnosis_completed']);
    assert.equal(env.commands.length, 3); assert.equal(JSON.stringify(env.commands).includes('private-id'), false);
  });
  test('SDK load or execution failure cannot interrupt an upload or navigation', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start(true);
    env.scripts[0].onerror(); assert.equal(client.goal('upload_started'), false);
    const throwing = environment(); throwing.win.ym = () => {throw new Error('blocked SDK');};
    const other = createAnalytics(throwing.win, throwing.doc);
    assert.doesNotThrow(() => other.start(true)); assert.equal(other.goal('upload_started'), false);
  });
  test('preference rejects expired, malformed or future consent', async () => {
    const {readAnalyticsChoice} = await import('../../src/analytics.js');
    const storage = raw => ({getItem:()=>raw}); const now=40*86400000;
    for (const raw of ['bad','null',JSON.stringify({version:2,allowed:true,at:now}),JSON.stringify({version:1,allowed:true,at:now+1}),JSON.stringify({version:1,allowed:true,at:0})]) assert.equal(readAnalyticsChoice(storage(raw),now),null);
    assert.equal(readAnalyticsChoice(storage(JSON.stringify({version:1,allowed:false,at:now})),now),false);
    assert.equal(readAnalyticsChoice(storage(JSON.stringify({version:1,allowed:true,at:now,receipt:'a'.repeat(64),expiresAt:now/1000+60})),now),true);
    assert.equal(readAnalyticsChoice({getItem:()=>{throw new Error('disabled');}},now),null);
  });
  test('choice UI allows refusal then consent, withdrawal reloads without replay', async () => {
    const {createAnalytics,bindAnalyticsChoice} = await import('../../src/analytics.js');
    const env=environment();const buttons=Object.fromEntries(['analytics-status','analytics-allow','analytics-decline','analytics-revoke'].map(id=>[id,{hidden:id==='analytics-revoke',addEventListener:(type,fn)=>{buttons[id].click=fn;}}]));
    env.doc.getElementById=id=>buttons[id];let raw=null,reloads=0;
    env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};env.win.location.reload=()=>{reloads++;};
    env.win.crypto=globalThis.crypto; const actions=[];env.win.fetch=async(path,options)=>{
      if(path==='/analytics-consent')return{ok:true,text:async()=>'<html><!-- analytics-consent-document-v1:start -->Consent<!-- analytics-consent-document-v1:end --></html>'};
      const body=JSON.parse(options.body);actions.push(body.action);return{ok:true,json:async()=>body.action==='grant'?{allowed:true,receipt:'a'.repeat(64),expiresAt:Math.floor(Date.now()/1000)+60}:{allowed:false}};
    };
    const client=createAnalytics(env.win,env.doc);await bindAnalyticsChoice(client,env.win,env.doc);
    assert.equal(env.scripts.length,0);buttons['analytics-decline'].click();assert.equal(env.scripts.length,0);
    await buttons['analytics-allow'].click();assert.equal(env.scripts.length,1);assert.equal(buttons['analytics-revoke'].hidden,false);
    await buttons['analytics-revoke'].click();assert.deepEqual(actions,['grant','withdraw']);assert.equal(reloads,1);assert.equal(JSON.parse(raw).allowed,false);assert.equal(Boolean(client.goal('demo_opened')),false);
  });

  test('server refusal or network failure leaves analytics off and does not throw into application', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');
    for(const fail of [true,false,'storage']){
      const env=environment();const buttons={};for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={hidden:id==='analytics-revoke',addEventListener:(type,fn)=>{buttons[id].click=fn;}};
      env.doc.getElementById=id=>buttons[id];env.win.crypto=globalThis.crypto;
      if(fail==='storage')env.win.localStorage={getItem:()=>null,setItem:()=>{throw new Error('blocked');}};
      env.win.fetch=async(path,options)=>{if(path==='/analytics-consent')return{ok:true,text:async()=>'<html><!-- analytics-consent-document-v1:start -->Consent<!-- analytics-consent-document-v1:end --></html>'};if(fail===true)throw new Error('offline');if(fail==='storage')return{ok:true,json:async()=>JSON.parse(options.body).action==='grant'?{allowed:true,receipt:'a'.repeat(64),expiresAt:Math.floor(Date.now()/1000)+60}:{allowed:false}};return{ok:false};};
      const client=createAnalytics(env.win,env.doc);await bindAnalyticsChoice(client,env.win,env.doc);await buttons['analytics-allow'].click();
      assert.equal(env.scripts.length,0);assert.equal(Boolean(client.goal('upload_started')),false);assert.match(buttons['analytics-status'].textContent,/загрузка и проверка работают/);
    }
  });
  test('stored receipt is checked before SDK; expiration and cross-tab withdrawal stop it', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');
    const env=environment();const buttons={};for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={hidden:id==='analytics-revoke',addEventListener:()=>{}};
    env.doc.getElementById=id=>buttons[id];let raw=JSON.stringify({version:1,allowed:true,at:Date.now(),receipt:'a'.repeat(64),expiresAt:Math.floor(Date.now()/1000)+60});
    env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};let finish,expire,storageListener,reloads=0;
    env.win.fetch=()=>new Promise(resolve=>{finish=resolve;});env.win.setTimeout=(fn)=>{expire=fn;};env.win.clearTimeout=()=>{};env.win.addEventListener=(event,fn)=>{storageListener=fn;};env.win.location.reload=()=>{reloads++;};
    const client=createAnalytics(env.win,env.doc);const initial=bindAnalyticsChoice(client,env.win,env.doc);assert.equal(env.scripts.length,0);
    finish({ok:true,json:async()=>({allowed:true,expiresAt:Math.floor(Date.now()/1000)+60})});await initial;assert.equal(env.scripts.length,1);
    expire();assert.equal(Boolean(client.goal('demo_opened')),false);assert.equal(reloads,1);
    raw=JSON.stringify({version:1,allowed:false,at:Date.now()});storageListener({key:'axis-model-analytics-choice-v1'});assert.equal(reloads,2);
  });

  test('withdrawal with storage and network failure stops SDK and keeps a retry on current page', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');
    const env=environment(),buttons={};for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={addEventListener:(type,fn)=>{buttons[id].click=fn;}};
    env.doc.getElementById=id=>buttons[id];let blocked=false,raw=JSON.stringify({version:1,allowed:true,at:Date.now(),receipt:'a'.repeat(64),expiresAt:Math.floor(Date.now()/1000)+60}),reloads=0;
    env.win.localStorage={getItem:()=>{if(blocked)throw new Error('blocked');return raw;},setItem:(key,value)=>{if(blocked)throw new Error('blocked');raw=value;}};
    env.win.fetch=async()=>{if(blocked)throw new Error('offline');return{ok:true,json:async()=>({allowed:true,expiresAt:Math.floor(Date.now()/1000)+60})};};env.win.location.reload=()=>{reloads++;};
    const client=createAnalytics(env.win,env.doc);await bindAnalyticsChoice(client,env.win,env.doc);assert.equal(env.scripts.length,1);blocked=true;await buttons['analytics-revoke'].click();
    assert.equal(Boolean(client.goal('demo_opened')),false);assert.equal(reloads,0);assert.equal(buttons['analytics-allow'].hidden,true);assert.equal(buttons['analytics-revoke'].hidden,false);
  });

  test('late initial withdrawal cannot overwrite a newer grant', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');const env=environment(),buttons={};
    for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={addEventListener:(type,fn)=>{buttons[id].click=fn;}};env.doc.getElementById=id=>buttons[id];env.win.crypto=globalThis.crypto;
    let raw=JSON.stringify({version:1,allowed:false,at:Date.now(),pendingWithdrawal:'b'.repeat(64)}),finish,first=true;env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};
    env.win.fetch=async(path,options)=>{if(path==='/analytics-consent')return{ok:true,text:async()=>'<html><!-- analytics-consent-document-v1:start -->Consent<!-- analytics-consent-document-v1:end --></html>'};
      const action=JSON.parse(options.body).action;if(action==='withdraw'&&first){first=false;return new Promise(resolve=>{finish=resolve;});}return{ok:true,json:async()=>action==='grant'?{allowed:true,receipt:'a'.repeat(64),expiresAt:Math.floor(Date.now()/1000)+60}:{allowed:false}};};
    const client=createAnalytics(env.win,env.doc),initial=bindAnalyticsChoice(client,env.win,env.doc);await buttons['analytics-allow'].click();assert.equal(JSON.parse(raw).allowed,true);
    finish({ok:true,json:async()=>({allowed:false})});await initial;assert.equal(JSON.parse(raw).receipt,'a'.repeat(64));assert.equal(JSON.parse(raw).allowed,true);
  });

  test('decline during delayed startup check withdraws known receipt before discarding it', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');const env=environment(),buttons={};
    for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={addEventListener:(type,fn)=>{buttons[id].click=fn;}};env.doc.getElementById=id=>buttons[id];
    let raw=JSON.stringify({version:1,allowed:true,at:Date.now(),receipt:'a'.repeat(64),expiresAt:Math.floor(Date.now()/1000)+60}),finish,finishWithdrawal;const actions=[];
    env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};env.win.location.reload=()=>{};env.win.fetch=(path,options)=>{const body=JSON.parse(options.body);actions.push(body);return new Promise(resolve=>{if(body.action==='check')finish=resolve;else finishWithdrawal=resolve;});};
    const client=createAnalytics(env.win,env.doc),initial=bindAnalyticsChoice(client,env.win,env.doc);const refusal=buttons['analytics-decline'].click();
    assert.equal(JSON.parse(raw).pendingWithdrawal,'a'.repeat(64));assert.equal(actions[1].action,'withdraw');assert.equal(actions[1].receipt,'a'.repeat(64));
    finish({ok:true,json:async()=>({allowed:true,expiresAt:Math.floor(Date.now()/1000)+60})});await initial;assert.equal(env.scripts.length,0);
    finishWithdrawal({ok:true,json:async()=>({allowed:false})});await refusal;assert.equal(JSON.parse(raw).pendingWithdrawal,undefined);assert.equal(JSON.parse(raw).allowed,false);
  });
  test('decline retains expired pending withdrawal while server is unavailable', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');const env=environment(),buttons={};
    for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={addEventListener:(type,fn)=>{buttons[id].click=fn;}};env.doc.getElementById=id=>buttons[id];
    let raw=JSON.stringify({version:1,allowed:false,at:Date.now()-31*86400000,pendingWithdrawal:'b'.repeat(64)});const actions=[];env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};env.win.location.reload=()=>{};
    env.win.fetch=async(path,options)=>{actions.push(JSON.parse(options.body));throw new Error('offline');};
    const client=createAnalytics(env.win,env.doc);await bindAnalyticsChoice(client,env.win,env.doc);await buttons['analytics-decline'].click();
    assert.equal(JSON.parse(raw).pendingWithdrawal,'b'.repeat(64));assert.equal(JSON.parse(raw).allowed,false);assert.equal(actions.length,2);assert.equal(actions.every(value=>value.action==='withdraw'&&value.receipt==='b'.repeat(64)),true);assert.equal(env.scripts.length,0);
  });

  test('every delayed analytics write preserves a newer receipt from another tab before its storage event', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');
    for(const phase of ['check','startup-pending','allow-pending','grant','withdraw']){
      const env=environment(),buttons={};for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={addEventListener:(type,fn)=>{buttons[id].click=fn;}};env.doc.getElementById=id=>buttons[id];env.win.crypto=globalThis.crypto;env.win.location.reload=()=>{};
      const expiry=Math.floor(Date.now()/1000)+60,old={version:1,allowed:true,at:Date.now(),receipt:'a'.repeat(64),expiresAt:expiry};
      let raw=phase==='grant'?null:JSON.stringify(phase.includes('pending')?{version:1,allowed:false,at:Date.now(),pendingWithdrawal:'a'.repeat(64)}:old),finish,withdrawals=0;
      env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};
      env.win.fetch=async(path,options)=>{if(path==='/analytics-consent')return{ok:true,text:async()=>'<html><!-- analytics-consent-document-v1:start -->Consent<!-- analytics-consent-document-v1:end --></html>'};const action=JSON.parse(options.body).action;if(action==='withdraw')withdrawals++;
        if(phase==='allow-pending'&&withdrawals===1)throw new Error('offline');
        const delay=(phase==='check'&&action==='check')||(phase==='startup-pending'&&action==='withdraw')||(phase==='allow-pending'&&withdrawals===2)||(phase==='grant'&&action==='grant')||(phase==='withdraw'&&action==='withdraw');
        if(delay)return new Promise(resolve=>{finish=resolve;});return{ok:true,json:async()=>action==='check'?{allowed:true,expiresAt:expiry}:{allowed:false}};};
      const client=createAnalytics(env.win,env.doc);let pending=bindAnalyticsChoice(client,env.win,env.doc);if(['allow-pending','grant','withdraw'].includes(phase)){await pending;pending=buttons[phase==='withdraw'?'analytics-revoke':'analytics-allow'].click();}
      for(let n=0;!finish&&n<50;n++)await new Promise(resolve=>setTimeout(resolve,0));assert.equal(typeof finish,'function',phase);
      const newer=JSON.stringify({...old,receipt:'b'.repeat(64)});raw=newer;finish({ok:true,json:async()=>phase==='grant'?{allowed:true,receipt:'c'.repeat(64),expiresAt:expiry}:{allowed:false}});await pending;assert.equal(raw,newer,phase);assert.equal(Boolean(client.goal('demo_opened')),false,phase);
    }
  });

  test('refusal prioritizes newer saved receipt before storage event over active memory', async () => {
    const {createAnalytics,bindAnalyticsChoice}=await import('../../src/analytics.js');const env=environment(),buttons={};for(const id of ['analytics-status','analytics-allow','analytics-decline','analytics-revoke'])buttons[id]={addEventListener:(type,fn)=>{buttons[id].click=fn;}};env.doc.getElementById=id=>buttons[id];
    const expiry=Math.floor(Date.now()/1000)+60;let raw=JSON.stringify({version:1,allowed:true,at:Date.now(),receipt:'a'.repeat(64),expiresAt:expiry});const actions=[];env.win.localStorage={getItem:()=>raw,setItem:(key,value)=>{raw=value;}};env.win.location.reload=()=>{};env.win.fetch=async(path,options)=>{const body=JSON.parse(options.body);actions.push(body);return{ok:true,json:async()=>body.action==='check'?{allowed:true,expiresAt:expiry}:{allowed:false}};};
    const client=createAnalytics(env.win,env.doc);await bindAnalyticsChoice(client,env.win,env.doc);assert.equal(env.scripts.length,1);raw=JSON.stringify({...JSON.parse(raw),receipt:'b'.repeat(64)});await buttons['analytics-decline'].click();
    assert.equal(actions[1].action,'withdraw');assert.equal(actions[1].receipt,'b'.repeat(64));assert.equal(Boolean(client.goal('demo_opened')),false);assert.equal(JSON.parse(raw).allowed,false);
  });

}
