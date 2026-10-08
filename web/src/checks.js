import {api, ApiError, opaqueId} from './api.js';
const LABELS = {waiting:'Ожидает',checking:'Проверяется',passed:'Пройдена',failed:'Ошибка',warning:'Предупреждение',not_checked:'Не проверено'};
const ICONS = {waiting:'○',checking:'◌',passed:'✓',failed:'!',warning:'△',not_checked:'−'};
export function validateChecks(value,job) {
  if (value.jobId !== job.id || value.inputHash !== job.inputHash || value.attempt !== job.attempt || !Array.isArray(value.checks) || value.checks.length > 256 ||
      value.checks.some(row => !/^[a-z][a-z0-9_.]{0,79}$/.test(row.id) || !LABELS[row.state] || typeof row.title !== 'string') ||
      !value.findings || ['shown','original','omitted'].some(key => !Number.isSafeInteger(value.findings[key]) || value.findings[key] < 0)) throw new ApiError('invalid_response');
  return value;
}
export function completionText(job,checks) {
  if (job.state !== 'completed') return ({running:'Проверка выполняется',failed:'Обработку не удалось завершить',cancelled:'Обработка отменена',interrupted:'Обработка прервана',deleted:'Задание удалено',deleting:'Задание удаляется'})[job.state] || 'Обработка ещё не завершена';
  const failed = checks.checks.filter(row => row.state === 'failed').length;
  const warnings = checks.checks.filter(row => row.state === 'warning').length;
  return `Проверка завершена${failed ? ', найдены ошибки' : ', полный успех не подтверждён'}. Ошибок проверок: ${failed}, предупреждений: ${warnings}. Непроверенные области указаны отдельно.`;
}
export function runSourceLabel(job) {
  const id = opaqueId(job.id);
  if (typeof job.inputHash !== 'string' || !/^[a-f0-9]{64}$/.test(job.inputHash)) throw new ApiError('invalid_response');
  return `Запуск ${id.slice(0,8)} · исходный пакет ${job.inputHash.slice(0,12)}`;
}
export const getChecks = job => api(`/api/jobs/${opaqueId(job.id)}/checks`,{maxBytes:512*1024}).then(value => validateChecks(value,job));
function node(tag,text) {const value = document.createElement(tag); if (text !== undefined) value.textContent = String(text); return value;}
export function elementIdentity(key) {
  if (!key || typeof key !== 'object' || Array.isArray(key)) return 'Элемент: не подтверждён.';
  const documentId = typeof key.document_id === 'string' ? key.document_id : 'не указан';
  const uniqueId = typeof key.unique_id === 'string' ? key.unique_id : 'не указан';
  const links = Array.isArray(key.link_instance_path) && key.link_instance_path.every(value => typeof value === 'string') ? key.link_instance_path : null;
  let linkPath = 'не подтверждён';
  if (links) linkPath = links.length ? links.join(' → ') : 'корневая модель';
  return `Документ: ${documentId}. Элемент (UniqueId): ${uniqueId}. Путь экземпляров связей: ${linkPath}.`;
}
function scalarEvidence(value) {
  const scalar = value && typeof value === 'object' && !Array.isArray(value) ? value.value : value;
  if (typeof scalar === 'string' || typeof scalar === 'boolean' || typeof scalar === 'number' && Number.isFinite(scalar)) return String(scalar);
  return null;
}
function renderFinding(finding,checkId) {
  const block = node('div'); block.className = 'finding';
  if (finding.ruleId !== checkId) block.append(node('h4',finding.title || 'Правило проверки'),node('p',finding.why || 'Проверенное пояснение отсутствует.'),
    node('p',`Как исправить: ${finding.nextAction || 'Скачайте отчёт и передайте код проверки в поддержку.'}`));
  const observed = scalarEvidence(finding.observed), expected = scalarEvidence(finding.expected);
  if (observed !== null) block.append(node('p',`Обнаружено: ${observed}`));
  if (expected !== null) block.append(node('p',`Требуется: ${expected}`));
  if (observed === null || expected === null) block.append(node('p','Часть значений не указана или имеет сложную структуру. Доступные исходные данные можно раскрыть ниже.'));
  block.append(node('p',`Файл: ${finding.file || 'не указан'}. ${elementIdentity(finding.elementKey)}`),node('p',finding.locationExplanation));
  const technical = node('details');
  technical.append(node('summary',`Технические данные · ${finding.ruleId}`),node('p',finding.message || 'Исходное сообщение отсутствует'),
    node('p',`Наблюдается: ${JSON.stringify(finding.observed)}. Ожидается: ${JSON.stringify(finding.expected)}.`));
  block.append(technical); return block;
}
export function renderChecks(container,envelope,isCurrent) {
  const openIds=new Set([...container.querySelectorAll('button[aria-expanded="true"]')].map(button=>button.dataset.checkId));
  container.replaceChildren();
  const identity = node('p',`Отдельный запуск ${envelope.jobId.slice(0,8)} · исходный пакет ${envelope.inputHash.slice(0,12)} · ${envelope.authoritative ? 'Итоговый отчёт' : 'Текущая попытка'}`);
  identity.className = 'small'; container.append(identity);
  const counts = envelope.findings;
  container.append(node('p',`Замечаний: показано ${counts.shown}, всего ${counts.original}, пропущено ${counts.omitted}.`));
  if (counts.truncated) container.append(node('p',envelope.limitExplanation));
  for (const row of envelope.checks) {
    const item = node('article'); item.className = `check-row check-${row.state}`;
    const button = node('button',`${ICONS[row.state]} ${row.title} · ${LABELS[row.state]}`); button.type = 'button'; button.setAttribute('aria-expanded','false');button.dataset.checkId=row.id;
    let loaded=false,loading=false;
    const detail = node('div'); detail.hidden = true; detail.className = 'check-detail';
    button.addEventListener('click',async() => {
      const open = button.getAttribute('aria-expanded') === 'true'; button.setAttribute('aria-expanded',String(!open)); detail.hidden = open;
      if (open || loaded || loading) return;
      loading=true;
      detail.textContent = 'Получаем пояснение…';
      try {
        const value = await api(`/api/jobs/${opaqueId(envelope.jobId)}/checks/${row.id}?limit=100`,{maxBytes:512*1024});
        if (!isCurrent() || value.inputHash !== envelope.inputHash || value.jobId !== envelope.jobId) return;
        detail.replaceChildren(node('h3',value.title),node('p',value.why),node('p',`Как исправить: ${value.nextAction}`));
        for (const finding of value.findings) {
          detail.append(renderFinding(finding,row.id));
        }
        if (value.nextOffset !== null) detail.append(node('p',`Доступны ещё замечания этой проверки. Показано первые 100 из ${value.shownForCheck}; полный сохранённый список в скачиваемом отчёте.`));
        loaded=true;
        const retry = node('a','Исправить пакет и загрузить заново'); retry.href = '/#workspace'; detail.append(retry);
      } catch (error) {if(isCurrent())detail.textContent = error.message;}finally{loading=false;}
    });
    item.append(button,detail); container.append(item);if(openIds.has(row.id))button.click();
  }
}
