import {api, ApiError, opaqueId} from './api.js';
import {acceptsFile,validInputFormats,rejectedFileText} from './input-formats.js';
import {Sha256} from './sha256.js';
export const UPLOAD_CAP = 256 * 1024 * 1024;
export const CHUNK_BYTES = 8 * 1024 * 1024;
export function uploadStageText(phase) {
  const labels = {preparation:'Подготовка файла',hash:'Подготовка и контрольная сумма',transport:'Передача файла',ack:'Подтверждение части сервером',verify:'Проверка целостности файла',finalizing:'Проверка целостности файла',ready:'Файл принят',queue:'Ожидание в очереди',queued:'Ожидание в очереди',processing:'Проверка модели',running:'Проверка модели'};
  return labels[phase] || 'Подготовка файла';
}

export function hashFile(file, onProgress, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {reject(new DOMException('Отменено', 'AbortError')); return;}
    const worker = new Worker(new URL('./hash-worker.js', import.meta.url), {type:'module'});
    let settled = false;
    const finish = (callback, value) => {if (settled) return; settled = true; worker.terminate(); signal?.removeEventListener('abort', abort); callback(value);};
    const abort = () => finish(reject, new DOMException('Отменено', 'AbortError'));
    signal?.addEventListener('abort', abort, {once:true});
    worker.onerror = () => finish(reject, new ApiError('hash_failed'));
    worker.onmessage = ({data}) => {
      if (data.type === 'progress') onProgress(data.done, data.total);
      if (data.type === 'done') finish(resolve, data.digest);
      if (data.type === 'error') finish(reject, new ApiError('hash_failed'));
    };
    worker.postMessage({file});
  });
}
function descriptorVersion(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) throw new ApiError('invalid_response');
  const version = row?.descriptorVersion === undefined ? 0 : row.descriptorVersion;
  if (version !== 0 && version !== 1) throw new ApiError('invalid_response');
  return version;
}
export function validateUpload(row, source) {
  opaqueId(row.id);
  if (descriptorVersion(row) !== descriptorVersion(source) || row.sha256 !== source.sha256 || row.kind !== source.kind || row.totalBytes !== source.bytes || row.chunkBytes !== CHUNK_BYTES ||
      !Number.isSafeInteger(row.acknowledgedBytes) || row.acknowledgedBytes < 0 || row.acknowledgedBytes > source.bytes ||
      (row.acknowledgedBytes !== source.bytes && row.acknowledgedBytes % CHUNK_BYTES !== 0) ||
      !['receiving','finalizing','ready','failed','deleting','deleted'].includes(row.state) ||
      row.nextPart !== (row.acknowledgedBytes < source.bytes ? row.acknowledgedBytes / CHUNK_BYTES + 1 : null)) throw new ApiError('invalid_response');
  return row;
}
export function sendPart(input) {
  const {blob,id,part,offset,digest,csrf,onProgress,signal,total} = input;
  return new Promise((resolve,reject) => {
    const xhr = new XMLHttpRequest(); let settled = false;
    const finish = (fn,value) => {if (settled) return; settled = true; signal?.removeEventListener('abort',abort); fn(value);};
    const abort = () => {xhr.abort(); finish(reject,new DOMException('Отменено','AbortError'));};
    if (signal?.aborted) {abort(); return;}
    xhr.open('PUT', `/api/uploads/${opaqueId(id)}/chunks/${part}`); xhr.withCredentials = true; xhr.timeout = 90_000;
    for (const [name,value] of Object.entries({'Content-Type':'application/octet-stream','X-CSRF-Token':csrf,'Upload-Offset':String(offset),'Upload-Chunk-Sha256':digest})) xhr.setRequestHeader(name,value);
    xhr.upload.onprogress = event => onProgress({phase:'transport',done:offset + Math.min(event.loaded,blob.size),total,acknowledged:offset,part});
    xhr.onerror = () => finish(reject,new ApiError('upload_network'));
    xhr.ontimeout = () => finish(reject,new ApiError('upload_timeout',408));
    xhr.onabort = () => finish(reject,new DOMException('Отменено','AbortError'));
    xhr.onload = () => {
      if (xhr.responseText.length > 65536 || !/^application\/json(?:\s*;|$)/i.test(xhr.getResponseHeader('Content-Type') || '')) {finish(reject,new ApiError('invalid_response',xhr.status)); return;}
      let value; try {value = JSON.parse(xhr.responseText);} catch {finish(reject,new ApiError('invalid_response')); return;}
      if (xhr.status < 200 || xhr.status >= 300) {finish(reject,ApiError.from(value.error,xhr.status)); return;}
      finish(resolve,value);
    };
    signal?.addEventListener('abort',abort,{once:true}); xhr.send(blob);
  });
}
function pause(signal) {
  return new Promise((resolve,reject) => {
    const abort = () => {clearTimeout(timer); reject(new DOMException('Отменено','AbortError'));};
    const timer = setTimeout(() => {signal?.removeEventListener('abort',abort); resolve();},1000);
    signal?.addEventListener('abort',abort,{once:true}); if (signal?.aborted) abort();
  });
}
export async function uploadFile(file,kind,csrf,onProgress,signal,limits={}) {
  const cap = Math.min(UPLOAD_CAP,Number.isSafeInteger(limits.uploadBytes) ? limits.uploadBytes : UPLOAD_CAP);
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > cap) throw new ApiError('upload_too_large',413);
  const rows = limits.inputFormats === undefined ? [
    {id:'zip-fbx',extensions:['.zip'],upload:true}, {id:'portable-package',extensions:['.zip'],upload:true}
  ] : limits.inputFormats;
  if ((limits.inputFormats !== undefined && !validInputFormats(rows)) || !acceptsFile(file,kind,rows)) {
    const error = new ApiError('invalid_upload',422); error.message = rejectedFileText(file,rows); throw error;
  }
  const sha256 = await hashFile(file,(done,total) => onProgress({phase:'hash',done,total}),signal);
  signal?.throwIfAborted(); const source = {sha256,kind,bytes:file.size,descriptorVersion:1}; let row;
  try {
    const pending = await api('/api/uploads',{signal});
    if (!Array.isArray(pending.uploads) || pending.uploads.length > 20) throw new ApiError('invalid_response');
    pending.uploads.forEach(descriptorVersion);
    row = pending.uploads.find(item => descriptorVersion(item) === source.descriptorVersion && item.sha256 === sha256 && item.totalBytes === file.size && item.kind === kind && ['receiving','finalizing','ready'].includes(item.state));
    if (!row) {
      // Keep reservation response even if cancellation races it; delete only this ID.
      const reserved = await api('/api/uploads',{method:'POST',csrf,body:{kind,displayName:file.name,bytes:file.size,sha256,descriptorVersion:1}});
      row = {id:opaqueId(reserved.id)};
      if (descriptorVersion(reserved) !== source.descriptorVersion) throw new ApiError('invalid_response');
      signal?.throwIfAborted(); row = await api(`/api/uploads/${row.id}`,{signal});
    }
    validateUpload(row,source); onProgress({phase:'ack',done:row.acknowledgedBytes,total:file.size,part:row.nextPart,resumed:row.acknowledgedBytes > 0});
    while (row.state === 'receiving' && row.acknowledgedBytes < file.size) {
      signal?.throwIfAborted(); const offset = row.acknowledgedBytes;
      const blob = file.slice(offset,Math.min(offset + CHUNK_BYTES,file.size));
      const bytes = new Uint8Array(await blob.arrayBuffer()); signal?.throwIfAborted();
      const digest = new Sha256().update(bytes).digestHex();
      row = validateUpload(await sendPart({blob,id:row.id,part:row.nextPart,offset,digest,csrf,onProgress,signal,total:file.size}),source);
      onProgress({phase:'ack',done:row.acknowledgedBytes,total:file.size,part:row.nextPart});
    }
    if (row.state === 'receiving') row = validateUpload(await api(`/api/uploads/${row.id}/complete`,{method:'POST',csrf,signal}),source);
    const deadline = Date.now() + 180_000;
    while (row.state === 'finalizing' && Date.now() < deadline) {
      onProgress({phase:'verify',done:row.acknowledgedBytes,total:file.size});
      await pause(signal); row = validateUpload(await api(`/api/uploads/${row.id}`,{signal}),source);
    }
    if (row.state !== 'ready') throw new ApiError(row.reason || 'upload_timeout',row.state === 'failed' ? 409 : 408);
    onProgress({phase:'ready',done:file.size,total:file.size}); return row;
  } catch (error) {
    if (row?.id && error.name === 'AbortError' && !limits.preserveOnAbort) {
      try {await api(`/api/uploads/${opaqueId(row.id)}`,{method:'DELETE',csrf});} catch {throw new ApiError('upload_cleanup_pending');}
    }
    // A disconnect preserves acknowledged parts. Reselect the identical file to resume.
    throw error;
  }
}
