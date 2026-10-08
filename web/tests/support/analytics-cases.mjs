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
  test('analytics sends only canonical public page and origin referrer with recording disabled', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc);
    assert.equal(client.start(), true); assert.equal(client.start(), false);
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
      assert.equal(client.start(), false, url); assert.equal(client.goal('upload_started'), false);
      assert.deepEqual(env.commands, []); assert.deepEqual(env.scripts, []);
    }
  });
  test('goals allow fixed event names only and never attach application data', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start();
    assert.equal(client.goal('upload_completed'), true); assert.equal(client.goal('private-model.zip'), false);
    assert.deepEqual(env.commands.at(-1), [113547069, 'reachGoal', 'upload_completed']);
    assert.equal(JSON.stringify(env.commands).includes('private'), false);
  });
  test('contact events use a fixed goal and stop after navigation to a private URL', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start();
    env.listeners.click({target: {closest: () => ({getAttribute: () => 'mailto:info@axisconsult.ru'})}});
    assert.deepEqual(env.commands.at(-1), [113547069, 'reachGoal', 'contact_clicked']);
    env.win.location = new URL('https://model.axisconsult.ru/?token=private');
    assert.equal(Boolean(client.goal('upload_completed')), false); assert.equal(env.commands.length, 3);
  });
  test('SDK queue is bounded and discarded when the remote script cannot load', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); delete env.win.ym; env.doc.referrer = '';
    const client = createAnalytics(env.win, env.doc); assert.equal(client.start(), true);
    for (let i = 0; i < 150; i++) client.goal('upload_started');
    assert.equal(env.win.ym.a.length, 100); assert.equal(env.win.ym.a[1][3].referer, '');
    env.scripts[0].onerror(); assert.equal(env.win.ym.a.length, 0); assert.equal(client.goal('upload_started'), false);
  });
  test('diagnostic completion counts only newly accepted completed jobs once', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start();
    client.observe({id: 'old-private-id', state: 'completed'});
    client.watch('new-private-id'); client.observe({id: 'new-private-id', state: 'running'});
    assert.equal(env.commands.length, 2);
    client.observe({id: 'new-private-id', state: 'completed'}); client.observe({id: 'new-private-id', state: 'completed'});
    assert.deepEqual(env.commands.at(-1), [113547069, 'reachGoal', 'diagnosis_completed']);
    assert.equal(env.commands.length, 3); assert.equal(JSON.stringify(env.commands).includes('private-id'), false);
  });
  test('SDK load or execution failure cannot interrupt an upload or navigation', async () => {
    const {createAnalytics} = await import('../../src/analytics.js');
    const env = environment(); const client = createAnalytics(env.win, env.doc); client.start();
    env.scripts[0].onerror(); assert.equal(client.goal('upload_started'), false);
    const throwing = environment(); throwing.win.ym = () => {throw new Error('blocked SDK');};
    const other = createAnalytics(throwing.win, throwing.doc);
    assert.doesNotThrow(() => other.start()); assert.equal(other.goal('upload_started'), false);
  });
}
