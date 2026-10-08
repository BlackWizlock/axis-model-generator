import './shell.js';
import {trackGoal,watchDiagnostic,observeDiagnostic} from './analytics.js';
import {getChecks,renderChecks,completionText,runSourceLabel} from './checks.js';
import {api,ApiError,artifactPath,setCsrfToken} from './api.js';
import {allowedExtensions,acceptsFile,unavailableFormatText,rejectedFileText} from './input-formats.js';
import {guestSession} from './auth.js';
import {uploadFile,UPLOAD_CAP,uploadStageText} from './upload.js';
import {getJobs,getJob,createJob,cancelJob,deleteJob,startPolling,statusText,terminal,jobAxes,jobArtifacts} from './jobs.js';
import {mountPreview,previewReason} from './preview.js';
const byId=id=>document.getElementById(id);
const state={inputFormats:null,user:null,poller:null,upload:null,viewer:null,selected:null,demo:false,selectionEpoch:0,selectedFingerprint:null,jobs:[],cursor:null,limits:{uploadBytes:UPLOAD_CAP}};
function message(id,text,error=false){const node=byId(id);node.textContent=text;node.classList.toggle('error',error);node.setAttribute('role',error?'alert':'status');node.setAttribute('aria-live',error?'assertive':'polite');}
const safeError=error=>error instanceof ApiError?error.message:error?.name==='AbortError'?'Действие отменено.':'Не удалось выполнить действие. Попробуйте ещё раз.';
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(className)node.className=className;return node;}
function action(label,callback){const button=element('button',label,'button quiet');button.type='button';button.addEventListener('click',async()=>{button.disabled=true;try{await callback();}catch(error){message('jobs-message',safeError(error),true);}finally{button.disabled=false;}});return button;}
function resetPreview(){state.viewer?.dispose();state.viewer=null;byId('thumbnail').hidden=true;byId('thumbnail').removeAttribute('src');byId('reset-view').disabled=true;byId('report-panel').hidden=true;byId('report-download').removeAttribute('href');}
function setUser(user){
 state.user=user;state.poller?.stop();state.poller=null;
 byId('upload-submit').disabled=!user;
 if(user){state.limits={...state.limits,...user.limits,inputFormats:state.inputFormats};byId('upload-limit').textContent=`До ${Math.floor(Math.min(state.limits.uploadBytes||UPLOAD_CAP,UPLOAD_CAP)/1024/1024)} MiB · доступ к заданию и результатам до 24 часов`;state.poller=startPolling(refreshJobs);checkChosenFile();}
 else{state.selectionEpoch++;state.upload?.abort();state.jobs=[];state.selected=null;byId('jobs-list').replaceChildren(element('p','Здесь появятся модели из этой сессии.','empty-jobs'));resetPreview();byId('viewer').textContent='Откройте демонстрацию или результат своей модели.';byId('checks-list').replaceChildren();byId('checks-summary').textContent='Не удалось открыть приватную сессию. Следуйте инструкции под формой загрузки.';setCsrfToken(null);}
}
function checkChosenFile(){
 const file=byId('input-file').files[0];
 if(!allowedExtensions(state.inputFormats)){message('upload-message','Загрузка недоступна. Не удалось получить доступные форматы сервера.',true);byId('upload-submit').disabled=true;return false;}
 if(!file){byId('upload-submit').disabled=!state.user||!!state.upload;return false;}
 if(!acceptsFile(file,byId('input-kind').value,state.inputFormats)){message('upload-message',rejectedFileText(file,state.inputFormats),true);byId('upload-submit').disabled=true;return false;}
 if(!Number.isSafeInteger(file.size)||file.size<1||file.size>Math.min(state.limits.uploadBytes||UPLOAD_CAP,UPLOAD_CAP)){message('upload-message','Файл превышает допустимый размер или пуст. Выберите другой файл.',true);byId('upload-submit').disabled=true;return false;}
 byId('upload-submit').disabled=!state.user||!!state.upload;return !!state.user&&!state.upload;
}
function setInputFormats(rows){
 state.inputFormats=rows;state.limits.inputFormats=rows;
 const extensions=allowedExtensions(rows);byId('input-file').setAttribute('accept',extensions);byId('input-file').disabled=!extensions;
 const select=byId('input-kind'),selected=select.value;
 const labels={'portable-package':'Переносимый пакет v1','zip-fbx':'ZIP с FBX'};
 select.replaceChildren(...rows.filter(row=>row.upload).map(row=>{const option=element('option',labels[row.id]||row.id.toUpperCase());option.value=row.id;return option;}));
 select.value=rows.find(row=>row.upload&&row.id===selected)?.id||rows.find(row=>row.upload)?.id||'';
 byId('input-format-reasons').replaceChildren(...rows.filter(row=>!row.upload).map(row=>element('p',unavailableFormatText(row),'small')));
 checkChosenFile();
}
function chosenFileChanged(){if(checkChosenFile())message('upload-message','');}
byId('input-kind').addEventListener('change',chosenFileChanged);
byId('input-file').addEventListener('change',chosenFileChanged);
const dropZone=byId('upload-zone');
dropZone.addEventListener('dragover',event=>{event.preventDefault();dropZone.classList.add('dragging');});
dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragging'));
dropZone.addEventListener('drop',event=>{event.preventDefault();dropZone.classList.remove('dragging');if(state.upload||!event.dataTransfer?.files.length)return;byId('input-file').files=event.dataTransfer.files;chosenFileChanged();});
const localDate=new Date();byId('submission-date').value=`${localDate.getFullYear()}-${String(localDate.getMonth()+1).padStart(2,'0')}-${String(localDate.getDate()).padStart(2,'0')}`;
function updateUploadHeartbeat(started){byId('upload-heartbeat').textContent=`${uploadStageText(state.uploadPhase)} · последний ответ сервера: ${state.uploadReply?`${Math.floor((Date.now()-state.uploadReply)/1000)} с назад`:'ещё не получен'}. Время ожидания: ${Math.floor((Date.now()-started)/1000)} с.`;}
byId('upload-form').addEventListener('submit',async event=>{
 event.preventDefault();if(!state.user||state.upload)return;const file=byId('input-file').files[0];if(!checkChosenFile())return;
 state.selectionEpoch++;state.selected=null;state.demo=false;state.selectedFingerprint=null;state.checkSummary=null;state.checkFingerprint=null;resetPreview();byId('preview-provenance').textContent='НОВЫЙ ПАКЕТ';byId('viewer').textContent='Новый пакет. Результат появится после обработки.';message('preview-message','');byId('checks-list').replaceChildren();byId('checks-summary').textContent='Новый пакет. Проверки начнутся после принятия файла сервером.';
 const controller=new AbortController();state.upload=controller;byId('upload-submit').disabled=true;byId('upload-cancel').hidden=false;byId('upload-progress').hidden=false;byId('upload-progress').value=0;
 trackGoal('upload_started');
 let uploaded,created=false;state.uploadReply=null;state.uploadPhase='preparation';const started=Date.now();const ageTimer=setInterval(()=>{if(!document.hidden)updateUploadHeartbeat(started);},1000);
 try{
  const input={region:byId('region').value,procedure:'diagnostic',submissionDate:byId('submission-date').value};
  uploaded=await uploadFile(file,byId('input-kind').value,state.user.csrfToken,progress=>{state.uploadPhase=progress.phase;if(['ack','verify','ready'].includes(progress.phase))state.uploadReply=Date.now();const percent=Math.floor(progress.done/progress.total*100);byId('upload-progress').value=percent;const labels={hash:`Подготовка и контрольная сумма: ${percent}%`,transport:`Передача: ${percent}%, подтверждено сервером ${progress.acknowledged} байт`,ack:`Подтверждено ${progress.done} из ${progress.total} байт${progress.resumed?' · продолжение загрузки':''}`,verify:'Сервер проверяет целостность файла. Проверка модели ещё не началась.',ready:'Файл принят. Создаём задание.'};message('upload-message',labels[progress.phase]);byId('checks-summary').textContent=labels[progress.phase]||uploadStageText(progress.phase);},controller.signal,state.limits);
  controller.signal.throwIfAborted();trackGoal('upload_completed');byId('upload-cancel').hidden=true;state.uploadPhase='queue';byId('checks-summary').textContent='Файл принят. Создаём задание и ожидаем начала проверок.';message('upload-message','Файл принят. Создаём задание. После появления в очереди его можно отменить.');const job=await createJob({uploadId:uploaded.id,...input});created=true;watchDiagnostic(job.id);trackGoal('diagnosis_started');
  message('upload-message','Файл принят. Задание добавлено в вашу очередь.');byId('input-file').value='';await state.poller?.refresh();await openJob(job.id,{scroll:false});
 }catch(error){
  if(uploaded&&!created&&error.name==='AbortError'){try{await api(`/api/uploads/${uploaded.id}`,{method:'DELETE'});}catch{message('upload-message','Очистка загрузки пока не подтверждена. Обновите страницу перед повторной загрузкой.',true);return;}}
  byId('checks-summary').textContent=safeError(error);message('upload-message',`${safeError(error)} ${error?.name==='AbortError'?'':'Повторно выберите тот же файл и нажмите загрузить для продолжения с подтверждённой части.'}`,error?.name!=='AbortError');
 }finally{clearInterval(ageTimer);if(created)byId('upload-heartbeat').textContent='';else updateUploadHeartbeat(started);state.upload=null;byId('upload-submit').disabled=!state.user;checkChosenFile();byId('upload-cancel').hidden=true;byId('upload-progress').hidden=true;}
});
byId('upload-cancel').addEventListener('click',()=>{state.upload?.abort();message('upload-message','Отмена загрузки…');});
function renderJobs(){
 const list=byId('jobs-list');list.replaceChildren();byId('more-jobs').hidden=!state.cursor;
 if(!state.jobs.length){list.append(element('p','Здесь появятся ваши задания после первой загрузки.','empty-jobs'));return;}
 for(const job of state.jobs){
  const row=element('article',undefined,'job-row');const title=element('div');title.append(element('strong',job.displayName||'Модельный пакет'));
  const date=typeof job.submissionDate==='string'?job.submissionDate:'Дата не определена';title.append(element('p',`${job.region==='moscow'?'Москва':job.region==='moscow-oblast'?'Московская область':'Регион не определён'} · диагностика · ${date}`,'small'));
  const stage=element('div',undefined,'job-state');stage.append(element('p',statusText(job.state)));stage.append(element('p',job.cancelRequested?'Отмена запрошена · ждём остановки':statusText(job.stage),'small'));
  const actions=element('div',undefined,'job-actions');
  if(job.state!=='deleting'&&job.state!=='deleted')actions.append(action('Открыть',()=>openJob(job.id)));
  if(!terminal.has(job.state)&&job.state!=='deleting')actions.append(action('Отменить',async()=>{await cancelJob(job.id);job.cancelRequested=true;renderJobs();message('jobs-message','Запрос отмены сохранён. Ожидаем остановки обработки.');}));
  if(job.state!=='deleted'&&job.state!=='deleting')actions.append(action('Удалить',async()=>{if(!window.confirm('Удалить это задание и его приватные файлы?'))return;await deleteJob(job.id);job.state='deleting';if(state.selected===job.id){state.selected=null;state.selectionEpoch++;resetPreview();byId('viewer').textContent='Удаление запрошено. Доступ к результату закрыт.';byId('checks-list').replaceChildren();byId('checks-summary').textContent='Удаление запрошено. Проверки этого задания закрыты.';}renderJobs();message('jobs-message','Удаление запрошено. Ожидаем подтверждённой очистки.');await state.poller?.refresh();}));
  row.append(title,stage,actions);list.append(row);
 }
}
async function refreshJobs(){
 const user=state.user;if(!user)return[];
 try{const result=await getJobs();if(state.user!==user)return[];if(!Array.isArray(result.jobs))throw new ApiError('invalid_response');state.jobs=result.jobs;state.cursor=result.nextCursor||null;renderJobs();state.lastReply=Date.now();message('jobs-message',`Ответ сервера получен ${new Date(state.lastReply).toLocaleTimeString('ru-RU')}.`);
  if(state.selected){const job=state.jobs.find(n=>n.id===state.selected);if(job&&!terminal.has(job.state)&&job.state!=='deleting')await refreshChecks(job);else if(job&&jobFingerprint(job)!==state.selectedFingerprint)await openJob(job.id,{scroll:false});}
  state.jobs.forEach(observeDiagnostic);return state.jobs;
 }catch(error){if(state.user===user){message('jobs-message',`${safeError(error)} Связь не подтверждена. Последний ответ: ${state.lastReply?new Date(state.lastReply).toLocaleTimeString('ru-RU'):'ещё не получен'}. Нажмите Обновить после восстановления сети.`,true);if(error.status===401){setUser(null);message('session-message','Сессия завершена. Обновите страницу, чтобы начать новую.',true);}}throw error;}
}
byId('refresh-jobs').addEventListener('click',()=>{void state.poller?.refresh();});
byId('more-jobs').addEventListener('click',async()=>{
 if(!state.cursor)return;const button=byId('more-jobs');button.disabled=true;const user=state.user;
 try{const result=await getJobs(state.cursor);if(user!==state.user)return;if(!Array.isArray(result.jobs))throw new ApiError('invalid_response');const ids=new Set(state.jobs.map(job=>job.id));state.jobs.push(...result.jobs.filter(job=>!ids.has(job.id)));state.cursor=result.nextCursor||null;renderJobs();}
 catch(error){message('jobs-message',safeError(error),true);}finally{button.disabled=false;}
});
function jobFingerprint(job){return JSON.stringify([job.state,job.stage,job.cancelRequested,jobArtifacts(job).map(item=>[item.id,item.kind])]);}
function showAxes(job){const container=byId('report-axes');container.replaceChildren();for(const[label,status]of jobAxes(job)){const item=element('div',label);item.append(element('strong',statusText(status)));container.append(item);}}
async function refreshChecks(job){
 const epoch=state.selectionEpoch;const checks=await getChecks(job);
 if(epoch!==state.selectionEpoch||state.selected!==job.id)return;
 const fingerprint=JSON.stringify([checks.attempt,checks.authoritative,checks.checks.map(row=>[row.id,row.state,row.count,row.sequence])]);state.checkSummary=completionText(job,checks);byId('checks-summary').textContent=`${state.checkSummary} Этап: ${statusText(job.stage)}. Последний ответ ${new Date().toLocaleTimeString('ru-RU')}.`;
 showAxes({coverage:checks.coverage});if(state.checkFingerprint===fingerprint)return;state.checkFingerprint=fingerprint;renderChecks(byId('checks-list'),checks,()=>epoch===state.selectionEpoch&&state.selected===job.id&&state.checkFingerprint===fingerprint);
}
async function openJob(id,{scroll=true}={}){
 const epoch=++state.selectionEpoch;const user=state.user;if(!user)return;state.selected=id;state.demo=false;byId('checks-list').replaceChildren();byId('checks-summary').textContent='Загружаем результаты проверок…';state.checkSummary=null;state.checkFingerprint=null;resetPreview();byId('preview-provenance').textContent='ПРИВАТНЫЙ ПАКЕТ';byId('viewer').textContent='Открываем результат вашего пакета…';message('preview-message','');
 if(scroll)byId('preview-section').scrollIntoView({block:'start'});
 try{
  const job=await getJob(id);if(epoch!==state.selectionEpoch||user!==state.user)return;
  observeDiagnostic(job);const source=runSourceLabel(job);byId('preview-provenance').textContent=`ПРИВАТНЫЙ ПАКЕТ · ${source}`;
  state.selectedFingerprint=jobFingerprint(job);await refreshChecks(job);if(epoch!==state.selectionEpoch||user!==state.user)return;showAxes(job);byId('report-panel').hidden=false;byId('report-download').hidden=true;byId('report-summary').textContent=`${source}. Отчёт появится после обработки. Завершение обработки не подтверждает нормативную готовность.`;byId('report-findings').replaceChildren();const report=jobArtifacts(job).find(n=>n.kind==='report');const preview=jobArtifacts(job).find(n=>n.kind==='preview');
  if(report){const path=artifactPath(id,report.id);const document=await api(path,{maxBytes:4*1024*1024});if(epoch!==state.selectionEpoch||user!==state.user)return;byId('report-panel').hidden=false;byId('report-download').href=path;byId('report-download').hidden=false;const findings=Array.isArray(document.findings)?document.findings:[];
   byId('report-summary').textContent=`${source}. ${state.checkSummary||''} Обработка: ${statusText(job.state)}. Замечаний в отчёте: ${findings.length}${document.report_truncated?' · отчёт сокращён':''}. Завершение обработки не подтверждает нормативную готовность.`;
   const list=byId('report-findings');list.replaceChildren();for(const finding of findings.slice(0,1000)){const row=element('li');row.textContent=[finding.rule_id,finding.status,finding.message].filter(n=>typeof n==='string').join(' · ');list.append(row);}if(findings.length>1000)list.append(element('li','Показаны первые 1000 замечаний. Полный список доступен в скачиваемом отчёте.'));
  }
  const thumbnail=jobArtifacts(job).find(n=>n.kind==='thumbnail');byId('thumbnail').hidden=!thumbnail;if(thumbnail)byId('thumbnail').src=artifactPath(id,thumbnail.id);
  if(job.failure){message('preview-message',`${job.failure.title}. ${job.failure.why} ${job.failure.nextAction} Код обращения: ${job.diagnosticId}.`,true);}
  if(preview){const document=await api(artifactPath(id,preview.id),{maxBytes:16*1024*1024});if(epoch!==state.selectionEpoch||user!==state.user)return;if(document.provenance!=='uploaded_package')throw new ApiError('invalid_preview');state.viewer=mountPreview(byId('viewer'),document);byId('reset-view').disabled=false;message('preview-message','Проверенная геометрия вашего пакета. Нейтральные материалы; нормативная готовность не подтверждена.');}
  else{const reason=job.capabilities?.preview?.reason||(job.inputKind==='zip-fbx'?'zip_fbx_preview_not_verified':null);byId('viewer').textContent=!terminal.has(job.state)&&job.state!=='deleting'?'Результат появится после обработки.':previewReason(reason);}
 }catch(error){if(epoch===state.selectionEpoch&&user===state.user){byId('viewer').textContent='Не удалось открыть результат.';message('preview-message',safeError(error),true);}}
}
byId('demo-button').addEventListener('click',async()=>{
 const button=byId('demo-button');button.disabled=true;const epoch=++state.selectionEpoch;state.selected=null;state.demo=true;resetPreview();byId('preview-provenance').textContent='СИНТЕТИЧЕСКАЯ ДЕМОНСТРАЦИЯ';byId('viewer').textContent='Загружаем демонстрацию…';byId('preview-section').scrollIntoView({block:'start'});
 try{const preview=await api('/api/demo/preview',{maxBytes:16*1024*1024});if(epoch!==state.selectionEpoch)return;if(preview.provenance!=='synthetic')throw new ApiError('invalid_preview');state.viewer=mountPreview(byId('viewer'),preview);trackGoal('demo_opened');byId('reset-view').disabled=false;message('preview-message','Учебная модель для демонстрации просмотра. Она не подтверждает чтение модели Revit или соответствие региональным требованиям.');}
 catch(error){if(epoch===state.selectionEpoch){byId('viewer').textContent='Демонстрация пока недоступна.';message('preview-message',safeError(error),true);}}finally{button.disabled=false;}
});
byId('reset-view').addEventListener('click',()=>state.viewer?.reset());
window.addEventListener('pagehide',()=>{state.selectionEpoch++;state.limits.preserveOnAbort=true;state.poller?.stop();state.poller=null;state.upload?.abort();resetPreview();});
window.addEventListener('pageshow',async event=>{
 if(!event.persisted)return;state.limits.preserveOnAbort=false;
 if(state.user){state.poller?.stop();state.poller=startPolling(refreshJobs);}
 if(state.selected)await openJob(state.selected,{scroll:false});else if(state.demo)byId('demo-button').click();
});
async function start(){
 try{const config=await api('/api/config');if(config.sourceLink!=='https://github.com/BlackWizlock/axis-model-generator')throw new ApiError('invalid_config');state.limits=config.limits||state.limits;setInputFormats(config.inputFormats);message('service-message','Техническая диагностика. Исследовательские профили; генерация НПМ, ВПМ и IFC пока недоступна.');}
 catch(error){message('service-message',safeError(error),true);}
 try{await guestSession();setUser(await api('/api/auth/me'));message('session-message',allowedExtensions(state.inputFormats)?'Готово к загрузке.':'Приватная сессия готова. Загрузка недоступна до получения форматов сервера.');}catch(error){setUser(null);message('session-message',safeError(error),true);}
}
void start();

byId('report-download').addEventListener('click',()=>{const link=byId('report-download');if(!link.hidden&&link.getAttribute('href'))trackGoal('report_download_requested');});
