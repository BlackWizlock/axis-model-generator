const COUNTER = 113547069;
const ORIGIN = 'https://model.axisconsult.ru';
const PAGES = new Set(['/', '/privacy', '/support']);
const ANCHORS = new Set(['', '#main', '#workspace', '#checks-section', '#preview-section', '#formats']);
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
  let started = false, failed = false;
  const pendingJobs = new Set();
  const send = (...args) => {
    if (failed) return false;
    try {windowRef.ym(COUNTER, ...args); return true;} catch {failed = true; return false;}
  };
  const goal = name => started && !failed && publicPage(windowRef?.location) && GOALS.has(name) && send('reachGoal', name);
  return {
    start() {
      const page = publicPage(windowRef?.location);
      if (started || !page || !documentRef?.head) return false;
      started = true;
      if (typeof windowRef.ym !== 'function') {
        const queue = [];
        windowRef.ym = (...args) => {if (queue.length < 100) queue.push(args);};
        windowRef.ym.a = queue; windowRef.ym.l = Date.now();
      }
      send('init', {defer: true, webvisor: false, clickmap: false, trackLinks: false,
        trackHash: false, ecommerce: false, sendTitle: false, disableYtm: true, accurateTrackBounce: true});
      send('hit', page, {referer: referralOrigin(documentRef.referrer)});
      const script = documentRef.createElement('script');
      script.async = true; script.src = 'https://mc.yandex.ru/metrika/tag.js'; script.referrerPolicy = 'no-referrer';
      script.onerror = () => {failed = true; if (Array.isArray(windowRef.ym?.a)) windowRef.ym.a.length = 0;};
      try {documentRef.head.append(script);} catch {failed = true;}
      documentRef.addEventListener('click', event => {
        const href = event.target?.closest?.('a')?.getAttribute('href') || '';
        if (href.startsWith('tel:') || href.startsWith('mailto:') || href === 'https://max.ru/id501208311297_bot') goal('contact_clicked');
      });
      return !failed;
    },
    goal,
    watch(id) {if (started && !failed && typeof id === 'string' && pendingJobs.size < 100) pendingJobs.add(id);},
    observe(job) {
      if (job?.state !== 'completed' || !pendingJobs.delete(job.id)) return;
      goal('diagnosis_completed');
    }
  };
}

let client;
export function setupAnalytics() {
  if (!client) client = createAnalytics(globalThis.window, globalThis.document);
  client.start();
}
export function trackGoal(name) {return client?.goal(name) || false;}
export function watchDiagnostic(id) {client?.watch(id);}
export function observeDiagnostic(job) {client?.observe(job);}
