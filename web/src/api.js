import {validInputFormats} from './input-formats.js';
const SAFE_MESSAGES = {
  401: 'Сессия завершена. Обновите страницу, чтобы продолжить.', 403: 'Действие не разрешено. Обновите страницу и повторите попытку.',
  404: 'Результат недоступен.', 408: 'Время ожидания истекло. Повторите попытку.',
  409: 'Действие уже выполнено или состояние изменилось.', 410: 'Срок хранения результата истёк.',
  413: 'Файл превышает допустимый размер.', 422: 'Проверьте данные формы.',
  429: 'Достигнут лимит запросов. Попробуйте позже.', 503: 'Сервис временно недоступен.',
  507: 'Недостаточно места для загрузки. Удалите ненужные задания.'
};
const TRANSPORT_MESSAGES={
  request_network:'Не удалось связаться с сервером. Проверьте сеть. Обновите список заданий после восстановления связи.',
  upload_network:'Передача файла прервана. Проверьте сеть и восстановите соединение.',
  request_timeout:'Время ожидания ответа сервера истекло. Перед повторным действием проверьте список заданий: сервер мог принять запрос.'
};
let csrfToken = null;
export class ApiError extends Error {
  constructor(code, status = 0) {super(TRANSPORT_MESSAGES[code] || SAFE_MESSAGES[status] || 'Не удалось выполнить запрос. Попробуйте ещё раз.'); this.name = 'ApiError'; this.code = code; this.status = status;}
  static from(error, status) {
    const result = new ApiError(typeof error?.code === 'string' ? error.code : 'request_failed',status);
    const safe = value => typeof value === 'string' && value.length <= 2048 ? value : '';
    const action = safe(error?.nextAction), title = safe(error?.title);
    if (title && action) result.message = `${title}. ${safe(error.why)} ${action}`;
    result.requestId = /^[a-f0-9]{32}$/.test(error?.request_id || '') ? error.request_id : null;
    if (result.requestId) result.message += ` Код обращения: ${result.requestId}.`;
    return result;
  }

}
export function setCsrfToken(value) {csrfToken = typeof value === 'string' ? value : null;}
export function opaqueId(id) {
  if (typeof id !== 'string' || !/^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/.test(id)) throw new ApiError('invalid_id');
  return id;
}
export function artifactPath(jobId, artifactId) {return `/api/jobs/${opaqueId(jobId)}/artifacts/${opaqueId(artifactId)}`;}
export async function readJson(response, maxBytes = 64 * 1024) {
  if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('Content-Type') || '')) throw new ApiError('unexpected_content_type', response.status);
  const declared = Number(response.headers.get('Content-Length'));
  if (declared > maxBytes) throw new ApiError('response_too_large');
  const reader = response.body?.getReader(); const chunks = []; let total = 0;
  if (!reader) throw new ApiError('invalid_response');
  try {
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      total += value.length;
      if (total > maxBytes) {await reader.cancel(); throw new ApiError('response_too_large');}
      chunks.push(value);
    }
  } finally {reader.releaseLock();}
  const bytes = new Uint8Array(total); let offset = 0;
  for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.length;}
  try {const result = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes)); if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error(); return result;}
  catch {throw new ApiError('invalid_response');}
}
export async function api(path, options = {}) {
  if (typeof path !== 'string' || (path.length>2048 || !/^\/api\/[a-zA-Z0-9_/?=&.%+-]+$/.test(path)) || path.includes('..') || path.includes('//')) throw new ApiError('invalid_route');
  const {body, maxBytes, csrf, ...rest} = options;
  const method = (rest.method || 'GET').toUpperCase(); const headers = new Headers(rest.headers);
  headers.set('Accept', 'application/json');headers.delete('X-CSRF-Token');
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD'].includes(method) && !['/api/auth/login', '/api/auth/register', '/api/auth/guest'].includes(path)) {
    const token = csrf || csrfToken; if (!token) throw new ApiError('csrf_missing', 403); headers.set('X-CSRF-Token', token);
  }
  const deadline=AbortSignal.timeout(30_000);
  const signal=rest.signal?AbortSignal.any([rest.signal,deadline]):deadline;
  let response;
  try {response = await fetch(path, {...rest, method, headers, credentials:'same-origin', redirect:'error', cache:'no-store', body:body === undefined ? undefined : JSON.stringify(body),signal});}
  catch(error){if(rest.signal?.aborted)throw error;throw new ApiError(deadline.aborted?'request_timeout':'request_network',deadline.aborted?408:0);}
  if (response.status === 204) return {};
  const value = await readJson(response, maxBytes);
  if (!response.ok) throw ApiError.from(value.error, response.status);
  if (path === '/api/config' && !validInputFormats(value.inputFormats)) throw new ApiError('invalid_config');
  return value;
}
