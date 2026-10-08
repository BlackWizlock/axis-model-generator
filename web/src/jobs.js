import {api, ApiError, opaqueId} from './api.js';
export const terminal = new Set(['completed','failed','cancelled','interrupted','deleted']);
const labels={queued:'В очереди',running:'Выполняется',completed:'Завершено',failed:'Не удалось завершить',cancelled:'Отменено',interrupted:'Прервано',deleting:'Удаляется',deleted:'Удалено',input_check:'Проверка входа',preview:'Подготовка просмотра',report:'Подготовка отчёта',done:'Обработка закончена',research:'Исследовательский',not_checked:'Не проверено',partial:'Частично',unknown:'Неизвестно',unavailable:'Недоступно',valid:'Без ошибок',invalid:'Есть ошибки',verified:'Проверено',available:'Доступно'};
export function statusText(value){return labels[value]||'Статус не определён';}
export function canPoll(jobs,hidden){return !hidden&&jobs.some(job=>!terminal.has(job.state));}
export function jobsPage(value){if(!Array.isArray(value.items)||value.items.length>50)throw new ApiError('invalid_response');return{jobs:value.items,nextCursor:value.nextCursor||null};}
export function getJobs(cursor){if(cursor&&!/^[a-zA-Z0-9_.=-]{1,512}$/.test(cursor))throw new ApiError('invalid_cursor');return api(`/api/jobs?limit=50${cursor?`&cursor=${encodeURIComponent(cursor)}`:''}`).then(jobsPage);}
export const getJob=id=>api(`/api/jobs/${opaqueId(id)}`);
export const createJob=body=>api('/api/jobs',{method:'POST',body});
export const cancelJob=id=>api(`/api/jobs/${opaqueId(id)}/cancel`,{method:'POST'});
export const deleteJob=id=>api(`/api/jobs/${opaqueId(id)}`,{method:'DELETE'});
export function startPolling(refresh,documentRef=document) {
  let timer=null,stopped=false,running=false,needsPoll=false,failures=0;
  const schedule=()=>{clearTimeout(timer);if(!stopped&&needsPoll&&!documentRef.hidden)timer=setTimeout(tick,Math.min(2000*2**failures,30000));};
  async function tick(){if(stopped||documentRef.hidden||running)return;running=true;try{const jobs=await refresh();if(stopped)return;failures=0;needsPoll=canPoll(jobs,false);}catch{failures++;needsPoll=failures<3;}finally{running=false;schedule();}}
  const visibility=()=>{clearTimeout(timer);if(!documentRef.hidden)void tick();};
  documentRef.addEventListener('visibilitychange',visibility);documentRef.defaultView?.addEventListener('focus',visibility);void tick();
  return {refresh(){failures=0;return tick();},stop(){stopped=true;clearTimeout(timer);documentRef.removeEventListener('visibilitychange',visibility);documentRef.defaultView?.removeEventListener('focus',visibility);}};
}
export function jobAxes(job){
 const coverage=job.coverage||{};
 return [['Техническая проверка',coverage.technical||'not_checked'],['Региональный профиль',coverage.profile||'research'],['Процедура',coverage.procedure||'unknown'],['Внешняя приёмка',coverage.external||'not_checked']];
}
export function jobArtifacts(job){return Array.isArray(job.artifacts)?job.artifacts.filter(item=>item&&['report','preview','thumbnail'].includes(item.kind)):[];}
