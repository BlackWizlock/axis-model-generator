import {setupAnalytics} from './analytics.js';
const THEME_KEY = 'axis-theme';
// Same order as axisplatform.ru: the visitor's saved choice, then the system setting, then dark.
export function setupTheme(documentRef = document, windowRef = globalThis) {
  const button = documentRef.getElementById('theme-toggle');
  const classes = documentRef.documentElement.classList;
  let storage;
  try {storage = windowRef.localStorage;} catch { /* The choice then lasts for this page only. */ }
  const saved = () => {try {const value = storage?.getItem(THEME_KEY); return value === 'light' || value === 'dark' ? value : null;} catch {return null;}};
  const media = windowRef.matchMedia?.('(prefers-color-scheme: light)');
  let current;
  const apply = value => {
    current = value;
    classes.toggle('light',value === 'light');
    classes.toggle('dark',value === 'dark');
    button.setAttribute('aria-label',value === 'light' ? 'Включить тёмную тему' : 'Включить светлую тему');
  };
  apply(saved() ?? (media?.matches ? 'light' : 'dark'));
  media?.addEventListener?.('change',event => {if (!saved()) apply(event.matches ? 'light' : 'dark');});
  button.addEventListener('click',() => {
    apply(current === 'light' ? 'dark' : 'light');
    try {storage?.setItem(THEME_KEY,current);} catch { /* Private mode: keep the choice for this page. */ }
  });
}
export function setupShell(documentRef = document, windowRef = globalThis) {
  const button = documentRef.getElementById('menu-toggle');
  const menu = documentRef.getElementById('site-menu');
  const setOpen = open => {button.setAttribute('aria-expanded',String(open)); menu.classList.toggle('menu-open',open);};
  button.addEventListener('click',() => setOpen(button.getAttribute('aria-expanded') !== 'true'));
  menu.addEventListener('click',event => {if (event.target.closest('a')) setOpen(false);});
  documentRef.addEventListener('keydown',event => {if (event.key === 'Escape' && button.getAttribute('aria-expanded') === 'true') {setOpen(false); button.focus();}});
  setupTheme(documentRef,windowRef);
}
setupShell();
setupAnalytics();
