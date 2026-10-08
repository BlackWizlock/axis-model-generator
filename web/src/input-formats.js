const KNOWN_IDS = new Set(['zip-fbx','portable-package','fbx','ifc','glb','gltf','obj','rvt','dwg','skp','3dm']);
const CAPABILITIES = ['upload','diagnostics','preview','generation'];
const extensionValid = value => typeof value === 'string' && /^\.[a-z0-9]{1,8}$/.test(value);
function validRow(row) {
  return !!row && KNOWN_IDS.has(row.id) && Array.isArray(row.extensions) &&
    row.extensions.length > 0 && row.extensions.length <= 8 && row.extensions.every(extensionValid);
}
export function validInputFormats(rows) {
  return Array.isArray(rows) && rows.length <= 32 && new Set(rows.map(row => row?.id)).size === rows.length &&
    rows.every(row => validRow(row) && CAPABILITIES.every(key => typeof row[key] === 'boolean') &&
      (row.reason === null || (typeof row.reason === 'string' && row.reason.length <= 2048)));
}
function uploadRows(rows) {
  if (!Array.isArray(rows) || rows.length > 32 || !rows.every(validRow) || new Set(rows.map(row => row.id)).size !== rows.length) return [];
  return rows.filter(row => row.upload === true);
}
export function allowedExtensions(rows) {
  return [...new Set(uploadRows(rows).flatMap(row => row.extensions))].join(',');
}
export function acceptsFile(file, kind, rows) {
  if (typeof file?.name !== 'string') return false;
  const row = uploadRows(rows).find(item => item.id === kind);
  return !!row && row.extensions.some(extension => file.name.toLowerCase().endsWith(extension));
}
export function unavailableFormatText(row) {
  const label = row.id.toUpperCase();
  if (row.reason === 'engine_unavailable') return `Обработка ${label} пока недоступна: серверный движок не подключён`;
  return `Обработка ${label} пока недоступна${row.reason ? `: ${row.reason}` : ''}`;
}
export function rejectedFileText(file, rows) {
  const name = typeof file?.name === 'string' ? file.name.toLowerCase() : '';
  const row = Array.isArray(rows) ? rows.find(item => validRow(item) && item.upload === false && item.extensions.some(extension => name.endsWith(extension))) : null;
  return row ? unavailableFormatText(row) : 'Этот формат пока не поддерживается. Выберите доступный формат и его тип.';
}
