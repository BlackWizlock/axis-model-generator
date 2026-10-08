import {setupAnalytics} from './analytics.js';
export function setupShell(documentRef = document) {
  const button = documentRef.getElementById('menu-toggle');
  const menu = documentRef.getElementById('site-menu');
  const theme = documentRef.getElementById('theme-toggle');
  const setOpen = open => {button.setAttribute('aria-expanded',String(open)); menu.classList.toggle('menu-open',open);};
  button.addEventListener('click',() => setOpen(button.getAttribute('aria-expanded') !== 'true'));
  menu.addEventListener('click',event => {if (event.target.closest('a')) setOpen(false);});
  documentRef.addEventListener('keydown',event => {if (event.key === 'Escape' && button.getAttribute('aria-expanded') === 'true') {setOpen(false); button.focus();}});
  theme.addEventListener('click',() => {
    const light = documentRef.documentElement.classList.toggle('light');
    theme.setAttribute('aria-label',light ? 'Включить тёмную тему' : 'Включить светлую тему');
  });
}
setupShell();
setupAnalytics();
