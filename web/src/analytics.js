const COUNTER = 113547069;
const ORIGIN = 'https://model.axisconsult.ru';
const PAGES = new Set(['/', '/privacy', '/support', '/analytics-consent']);
const ANCHORS = new Set(['', '#main', '#workspace', '#checks-section', '#preview-section', '#formats', '#analytics-consent']);
const QUERY_KEYS = new Set(['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'yclid', '_ym_debug']);
const GOALS = new Set(['upload_started', 'upload_completed', 'diagnosis_started', 'diagnosis_completed',
  'demo_opened', 'report_download_requested', 'contact_clicked']);

function publicPage(location) {
  if (!location || location.origin !== ORIGIN || !PAGES.has(location.pathname) || !ANCHORS.has(location.hash)) return null;
  const query = new URLSearchParams(location.search);
  if ([...query.keys()].some(key => !QUERY_KEYS.has(key))) return null;
  return ORIGIN + location.pathname;
}

function referralOrigin(referrer) {
  try {
    const url = new URL(referrer);
    return url.protocol === 'https:' || url.protocol === 'http:' ? `${url.origin}/` : '';
  } catch {return '';}
}

export function createAnalytics(windowRef, documentRef) {
  let started = false, failed = false, revoked = false, script;
  const pendingJobs = new Set();
  const send = (...args) => {
    if (failed) return false;
    try {windowRef.ym(COUNTER, ...args); return true;} catch {failed = true; return false;}
  };
  const goal = name => started && !failed && publicPage(windowRef?.location) && GOALS.has(name) && send('reachGoal', name);
  return {
    start(consent = false) {
      const page = publicPage(windowRef?.location);
      if (consent !== true || revoked || started || !page || !documentRef?.head) return false;
      started = true;
      if (typeof windowRef.ym !== 'function') {
        const queue = [];
        windowRef.ym = (...args) => {if (queue.length < 100) queue.push(args);};
        windowRef.ym.a = queue; windowRef.ym.l = Date.now();
      }
      send('init', {defer: true, webvisor: false, clickmap: false, trackLinks: false,
        trackHash: false, ecommerce: false, sendTitle: false, disableYtm: true, accurateTrackBounce: true});
      send('hit', page, {referer: referralOrigin(documentRef.referrer)});
      script = documentRef.createElement('script');
      script.async = true; script.src = 'https://mc.yandex.ru/metrika/tag.js'; script.referrerPolicy = 'no-referrer';
      script.onerror = () => {failed = true; if (Array.isArray(windowRef.ym?.a)) windowRef.ym.a.length = 0;};
      try {documentRef.head.append(script);} catch {failed = true;}
      documentRef.addEventListener('click', event => {
        const href = event.target?.closest?.('a')?.getAttribute('href') || '';
        if (href.startsWith('tel:') || href.startsWith('mailto:') || href === 'https://max.ru/id501208311297_bot') goal('contact_clicked');
      });
      return !failed;
    },
    stop() {
      revoked = true; pendingJobs.clear();
      if (Array.isArray(windowRef.ym?.a)) windowRef.ym.a.length = 0;
      else if (started && !failed) send('destruct');
      started = false; failed = true; script?.remove?.();
    },
    goal,
    watch(id) {if (started && !failed && typeof id === 'string' && pendingJobs.size < 100) pendingJobs.add(id);},
    observe(job) {
      if (job?.state !== 'completed' || !pendingJobs.delete(job.id)) return;
      goal('diagnosis_completed');
    }
  };
}

const CONSENT_KEY = 'axis-model-analytics-choice-v1';
const CHOICE_AGE = 30 * 24 * 60 * 60 * 1000;
export function readAnalyticsChoice(storage, now = Date.now()) {
  try {
    const choice = JSON.parse(storage?.getItem(CONSENT_KEY) || 'null');
    if (choice?.version !== 1 || typeof choice.allowed !== 'boolean' || !Number.isSafeInteger(choice.at) ||
        choice.at > now || now - choice.at >= CHOICE_AGE) return null;
    if (choice.allowed && (!/^[a-f0-9]{64}$/.test(choice.receipt) || !Number.isSafeInteger(choice.expiresAt) || choice.expiresAt * 1000 <= now)) return null;
    return choice.allowed;
  } catch {return null;}
}
let client;
export async function bindAnalyticsChoice(analytics, windowRef, documentRef) {
  let storage, expiryTimer, memoryReceipt, decision = 0;
  try {storage = windowRef.localStorage;} catch { /* No persistent choice means analytics stays off on the next page. */ }
  const status = documentRef.getElementById('analytics-status');
  const allow = documentRef.getElementById('analytics-allow');
  const decline = documentRef.getElementById('analytics-decline');
  const revoke = documentRef.getElementById('analytics-revoke');
  if (!status || !allow || !decline || !revoke) return;
  const saved = () => {try {return JSON.parse(storage?.getItem(CONSENT_KEY) || 'null');} catch {return null;}};
  const unchanged = (record, current) => current === decision && JSON.stringify(saved()) === JSON.stringify(record);
  const persist = record => {try {const text = JSON.stringify(record);storage?.setItem(CONSENT_KEY,text);return storage?.getItem(CONSENT_KEY) === text;} catch {return false;}};
  const show = allowed => {
    status.textContent = allowed === true ? 'Аналитика разрешена. Согласие можно отозвать в любой момент.' :
      allowed === false ? 'Аналитика выключена. Загрузка и проверка пакетов доступны.' :
      'Разрешить необязательную аналитику посещений и действий? Без согласия счётчик не загружается.';
    allow.hidden = allowed === true; decline.hidden = allowed !== null; revoke.hidden = allowed !== true;
  };
  const request = async body => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await windowRef.fetch('/api/analytics/consent', {method:'POST', credentials:'omit', cache:'no-store',
        referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal:controller.signal});
      if (!response.ok) throw new Error('Analytics choice unavailable');
      return await response.json();
    } finally {clearTimeout(timer);}
  };
  const activate = (value, receipt) => {
    if (value.allowed !== true || !Number.isSafeInteger(value.expiresAt) || value.expiresAt * 1000 <= Date.now()) throw new Error('Invalid choice confirmation');
    if (!/^[a-f0-9]{64}$/.test(receipt)) throw new Error('Missing receipt');
    memoryReceipt = receipt; analytics.start(true); show(true);
    windowRef.clearTimeout?.(expiryTimer);
    expiryTimer = windowRef.setTimeout?.(() => {analytics.stop(); windowRef.location.reload();}, Math.min(value.expiresAt * 1000 - Date.now(), 2147483647));
  };
  allow.addEventListener('click', async () => {
    if (allow.disabled) return;
    const current = ++decision; let expected = saved(); allow.disabled = true;
    try {
      const pending = expected?.pendingWithdrawal;
      if (pending) {await request({action:'withdraw',receipt:pending});if (!unchanged(expected,current)) return;persist({version:1,allowed:false,at:Date.now()});expected = saved();}
      const response = await windowRef.fetch('/analytics-consent', {credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('Consent text unavailable');
      const page = await response.text();
      const marker = page.match(/<!-- analytics-consent-document-v1:start -->([\s\S]*?)<!-- analytics-consent-document-v1:end -->/);
      if (!marker || page.split('<!-- analytics-consent-document-v1:start -->').length !== 2 || page.split('<!-- analytics-consent-document-v1:end -->').length !== 2) throw new Error('Invalid consent document');
      const text = marker[1];
      const digest = await windowRef.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
      const documentSha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2,'0')).join('');
      if (!unchanged(expected,current)) return;
      const confirmed = await request({action:'grant',version:1,documentSha256});
      if (!unchanged(expected,current)) {if (/^[a-f0-9]{64}$/.test(confirmed.receipt)) await request({action:'withdraw',receipt:confirmed.receipt}); return;}
      if (!/^[a-f0-9]{64}$/.test(confirmed.receipt)) throw new Error('Invalid choice receipt');
      if (!persist({version:1,allowed:true,at:Date.now(),receipt:confirmed.receipt,expiresAt:confirmed.expiresAt})) {
        await request({action:'withdraw',receipt:confirmed.receipt});throw new Error('Choice storage unavailable');
      }
      activate(confirmed, confirmed.receipt);
    } catch {if (current === decision) {show(false);status.textContent = 'Не удалось подтвердить согласие. Аналитика выключена; загрузка и проверка работают.';}}
    finally {allow.disabled = false;}
  });
  const withdrawChoice = async () => {
    const current = ++decision;show(false);
    const record = saved();
    const receipt = record?.pendingWithdrawal || record?.receipt || memoryReceipt;
    if (!receipt) {persist({version:1,allowed:false,at:Date.now()});return;}
    analytics.stop();allow.disabled = true;
    const stored = persist({version:1,allowed:false,at:Date.now(),pendingWithdrawal:receipt});
    const expected = saved();
    try {if (receipt) await request({action:'withdraw',receipt});if (!unchanged(expected,current)) return;memoryReceipt = null;persist({version:1,allowed:false,at:Date.now()});}
    catch {if (!unchanged(expected,current)) return;if (!stored) {memoryReceipt = receipt;allow.hidden = true;revoke.hidden = false;status.textContent = 'Аналитика остановлена. Не удалось сохранить отзыв: повторите отзыв на этой странице.';return;}}
    windowRef.location.reload();
  };
  decline.addEventListener('click', withdrawChoice);
  revoke.addEventListener('click', withdrawChoice);
  windowRef.addEventListener?.('storage', event => {
    if (event.key === CONSENT_KEY) {decision++;analytics.stop(); windowRef.location.reload();}
  });
  const choice = readAnalyticsChoice(storage), record = saved(), current = decision;
  show(choice === true ? null : choice);
  try {
    if (/^[a-f0-9]{64}$/.test(record?.pendingWithdrawal)) {await request({action:'withdraw',receipt:record.pendingWithdrawal});if (!unchanged(record,current)) return;persist({version:1,allowed:false,at:Date.now()});}
    if (choice === true) {
      const confirmed = await request({action:'check',receipt:record.receipt});
      if (!unchanged(record,current)) return;
      if (confirmed.allowed === true) activate(confirmed, record.receipt);
      else {persist({version:1,allowed:false,at:Date.now()});show(false);}
    }
  } catch {if (current === decision) {show(false);status.textContent = 'Подтверждение аналитики недоступно. Проверка пакетов работает без неё.';}}
}
export function setupAnalytics(windowRef = globalThis.window, documentRef = globalThis.document) {
  if (client) return;
  client = createAnalytics(windowRef, documentRef);
  void bindAnalyticsChoice(client, windowRef, documentRef);
}
export function trackGoal(name) {return client?.goal(name) || false;}
export function watchDiagnostic(id) {client?.watch(id);}
export function observeDiagnostic(job) {client?.observe(job);}
