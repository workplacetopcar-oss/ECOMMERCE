'use strict';

const role = document.body.dataset.role;
const intake = ['nacional', 'internacional'].includes(role);
const admin = role === 'administrador';
const content = document.querySelector('#content');
const notice = document.querySelector('#notice');
const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHTML;
const channelName = {nacional:'Nacional',internacional:'Internacional',calidad:'Calidad',administrador:'Administración'};
const actorName = {nacional:'Zulay',internacional:'Angélica',calidad:'Juan Diego',administrador:'Don Carlos'};
const statuses = {borrador:'Borrador',enviado:'Sin acuse',analisis:'En análisis',informacion:'Falta información',parcial:'Respuesta parcial',respondido:'Respuesta recibida',cerrado:'Cerrado',anulado:'Anulado'};
function metricsReturn() {
  const value=new URLSearchParams(location.search).get('volver');
  if(!value)return '';
  const target=new URL(value,location.origin);
  return target.origin===location.origin && target.pathname==='/indicadores' ? target.pathname+target.search+target.hash : '';
}
const origins = {rsales_formulario:'Formulario RSales nacional',nacional_manual:'Manual nacional',internacional_manual:'Manual internacional'};
const actionNames = {cancel:'Anulación',credit_apply:'Aplicación de nota crédito',create:'Apertura',created:'Apertura',update:'Edición',submit:'Envío a Calidad',ack:'Acuse de Calidad',request_info:'Solicitud de información',provide_info:'Información aportada',report_draft:'Borrador técnico',publish:'Publicación técnica',decision:'Decisión comercial',dispatch:'Despacho',receipt:'Recepción',resolve:'Cumplimiento',communicate:'Comunicación al cliente',amend:'Ampliación del expediente',close:'Cierre',reopen:'Reapertura',report_read:'Lectura del informe',letter:'Generación de carta',link_order:'Vinculación de pedido',upload:'Archivo adjunto',import:'Importación'};
const csrf = $('meta[name="csrf-token"]').content;
let route = '', revision = 0, dirty = false, busy = false, caseData, lastList, pending, filterTimer;
let sourceStatus = null;
const listPositions={bandeja:0,historial:0};
let listRoute='bandeja';
const isList=path=>path==='historial' || path==='bandeja';
function markNavigation() {
  const toggle=$('#menu-toggle');
  toggle.setAttribute('aria-expanded','false');
  $('#main-navigation').classList.remove('expanded');
  const path=route==='nuevo'&&role==='nacional'?'contacto':route;
  for(const a of $$('#main-navigation a')) {
    const selected=a.hash==='#'+(path.startsWith('caso/')?listRoute:path);
    a.classList.toggle('active',selected);
    if(selected)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');
  }
}
$('#menu-toggle').addEventListener('click',evt=>{
  const expanded=$('#main-navigation').classList.toggle('expanded');
  evt.currentTarget.setAttribute('aria-expanded',String(expanded));
});
let editBase, editLines = new Map(), lookupItems = new WeakMap();
let pdfToken = '', pdfPreview;
const queryOf = values => new URLSearchParams(Object.entries(values).filter(([,v])=>v!=='' && v!=null)).toString();
const freshKey = () => crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(24)),n=>n.toString(16).padStart(2,'0')).join('');
const dateInColombia = (value = Date.now()) => new Intl.DateTimeFormat('sv-SE',{timeZone:'America/Bogota'}).format(new Date(value));
const day = v => v?.slice(0,10) || '—';
function stamp(v) {
  if (!v) return '—';
  const value = new Date(v);
  return Number.isFinite(+value) ? value.toLocaleString('es-CO',{timeZone:'America/Bogota',dateStyle:'short',timeStyle:'short'}) : v;
}
function message(text = '', error = false) {
  notice.textContent = text;
  notice.className = text ? 'notice' + (error ? ' error' : '') : '';
  notice.setAttribute('role',error?'alert':'status');
  if (error) notice.scrollIntoView({block:'center'});
}
function tokenFor(body) {
  if (!pending || pending.body !== body) pending = {body, key:freshKey()};
  return pending.key;
}
async function api(url, options = {}) {
  const {method = 'GET', body, meta, key} = options;
  const headers = {};
  let payload;
  if (method !== 'GET') {
    headers['X-CSRF-Token'] = csrf;
    if (key) headers['Idempotency-Key'] = key;
    if (body instanceof FormData) payload = body;
    else { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body ?? {}); }
  }
  let resp;
  try {
    resp=await fetch(url,{method,headers,body:payload,credentials:'same-origin',cache:'no-store'});
  } catch(err) {
    if(!(err instanceof TypeError)) throw err;
    throw new Error('No se recibió la respuesta del servidor. Comprueba la conexión y vuelve a intentar.');
  }
  if (resp.redirected && new URL(resp.url).pathname.includes('login')) throw new Error('La sesión venció. Inicia sesión de nuevo.');
  const type = resp.headers.get('Content-Type') || '';
  const result = type.includes('json') ? await resp.json() : {};
  if (!resp.ok) {
    const detail = [result.error,result.message,result.description].find(v=>typeof v==='string');
    throw new Error(detail || (resp.status===409?'El expediente cambió. Consulta su última versión antes de guardar.':('La operación no se completó ('+resp.status+').')));
  }
  return meta ? {data:result,total:Number(resp.headers.get('X-Total-Count')||0),counts:JSON.parse(resp.headers.get('X-Status-Counts') || '{}')} : result;
}
function field(name,label,value='',type='text',attrs='') {
  const nameAttr='name="'+e(name)+'"';
  return '<label class="field">'+e(label)+(type==='textarea' ? '<textarea '+nameAttr+' '+attrs+'>'+e(value)+'</textarea>' : '<input '+nameAttr+' type="'+e(type)+'" value="'+e(value)+'" '+attrs+'>')+'</label>';
}
function select(name,label,items,current='',attrs='') {
  return '<label class="field">'+e(label)+'<select name="'+e(name)+'" '+attrs+'>'+Object.entries(items).map(([v,l])=>'<option value="'+e(v)+'"'+(String(v)===String(current)?' selected':'')+'>'+e(l)+'</option>').join('')+'</select></label>';
}
function facts(entries) {
  const list = entries.filter(([,v])=>v!==undefined && v!==null && v!=='');
  return list.length ? '<dl class="facts">'+list.map(([k,v])=>'<div><dt>'+e(k)+'</dt><dd>'+e(v)+'</dd></div>').join('')+'</dl>' : '';
}
function table(heads,rows) {
  return '<div class="table-wrap" tabindex="0" role="region" aria-label="'+e(heads.join(', '))+'"><table><thead><tr>'+heads.map(h=>'<th scope="col">'+e(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(v=>'<td>'+v+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
function head(title,subtitle,actions='') {
  return '<header class="page-head"><div><h1>'+e(title)+'</h1>'+(subtitle?'<p class="caption">'+e(subtitle)+'</p>':'')+'</div><div class="actions">'+actions+'</div></header>';
}
function panel(title,body,id='') { return '<section class="panel"'+(id?' id="'+e(id)+'" tabindex="-1"':'')+'><h2>'+e(title)+'</h2>'+body+'</section>'; }
function go(hash) { if (location.hash.slice(1)===hash) navigate(); else location.hash=hash; }
function setCrumb(text) {
  $('#crumb').innerHTML = text ? '<a href="#'+listRoute+'">'+(listRoute==='historial'?'Historial':'Bandeja')+'</a> &nbsp;/&nbsp; '+e(text) : 'Reclamos y Calidad';
}
function formValues(form) { return Object.fromEntries(new FormData(form)); }
function setDirty(v=true) {
  dirty=v;
  const editor=$('#editor');
  if(v && editor) {
    syncDocScopes(editor);
    for(const invalid of $$('[aria-invalid=true]',editor)) {
      invalid.removeAttribute('aria-invalid');
      invalid.removeAttribute('aria-describedby');
    }
    for(const hint of $$('.field-error',editor)) hint.remove();
  }
  const review=$('#review-preview'), status=$('#edit-state');
  if(v && review) review.hidden=true;
  if(v && status) status.textContent='Hay cambios pendientes de guardar.';
}
addEventListener('beforeunload', ev=>{if(dirty){ev.preventDefault();ev.returnValue='';}});
content.addEventListener('input',ev=>{if (ev.target.closest('[data-dirty]') && !ev.target.closest('[data-lookup]')) setDirty();});
function resetViewport() { window.scrollTo(0,0); }
async function navigate() {
  const next=decodeURIComponent(location.hash.slice(1)) || 'bandeja';
  if (next==='contenido') {history.replaceState(null,'','#'+(route||'bandeja'));$('#contenido').focus();return;}
  if (next!==route && dirty && !confirm('Hay cambios sin guardar. ¿Salir del expediente?')) {
    history.replaceState(null,'','#'+route); return;
  }
  if (isList(route)) {
    listPositions[route]=window.scrollY;
    clearTimeout(filterTimer);
    const form=$('#filters');
    if(form) {
      const visible=formValues(form);
      if(Object.entries(visible).some(([k,v])=>filters[k]!==v)) Object.assign(filters,{page:1},visible);
      keepFilters();
    }
  }
  if(isList(next)){listRoute=next;filters=readFilters(next);}
  route=next; revision++; dirty=false; pending=null; pdfToken=''; caseData=null;
  markNavigation();message(); setCrumb(''); content.innerHTML='<p class="empty">Cargando…</p>';
  const current = revision;
  try {
    if (isList(route)) await showList(false,true);
    else if (route==='nuevo' && intake) showEdit(null);
    else if (route==='contacto' && role==='nacional') showPDF();
    else {
      const match=/^(caso|editar)\/(\d+)$/.exec(route);
      if (!match) { go('bandeja'); return; }
      const data=await api('/api/cases/'+match[2]);
      if (current!==revision) return;
      caseData=data;
      if(match[1]==='editar' && intake && data.status==='borrador') showEdit(data);
      else showCase(data);
    }
    if(!isList(route)) resetViewport();
  } catch (err) {
    if (current!==revision) return;
    content.innerHTML='<div class="actions"><button data-command="reload">Reintentar</button><a class="btn" href="#bandeja">Volver a la bandeja</a></div>';message(err.message,true);
  }
}
function readFilters(view) {
  let saved={};
  try {saved=JSON.parse(sessionStorage.getItem('reclamos.filtros.'+role+'.'+view)||'{}');} catch {}
  return Object.assign({q:'',status:'',channel:'',origin:'',page:1,sort:role==='calidad'?'age':''},saved);
}
let filters=readFilters(listRoute);
function keepFilters() { try {sessionStorage.setItem('reclamos.filtros.'+role+'.'+listRoute,JSON.stringify(filters));} catch {} }
function ageDays(sent) {
  if(!sent)return '—';
  const today=Date.parse(dateInColombia()), start=Date.parse(dateInColombia(sent));
  return Math.max(0,Math.round((today-start)/86400000))+' días';
}
function syncCounts(counts) {
  for(const item of $$('.queue-count')) {
    const value=counts[item.dataset.state || 'todos'];
    item.textContent=Number.isInteger(value)?String(value):'';
  }
}
let searchVersion=0;
async function showList(quiet=false,restore=false) {
  const requestVersion=++searchVersion;
  const pageSize=25;
  const response=await api('/api/cases?'+queryOf({...filters,limit:pageSize}),{meta:true});
  if(!isList(route)||requestVersion!==searchVersion) return;
  lastList=Array.isArray(response.data)?response.data:response.data.items||[];
  const total=response.total || response.data.total || lastList.length;
  const quality=role==='calidad';
  const rows=lastList.map(c=>[
    '<a class="case-no" href="#caso/'+encodeURIComponent(c.id)+'">RC-'+e(c.id)+'</a>',
    '<b>'+e(c.client || 'Cliente por completar')+'</b><span class="meta">'+e(channelName[c.channel]||c.channel)+'</span>',
    e(day(c.date || c.created_at)), '<span class="badge">'+e(c.label || statuses[c.status] || c.status)+'</span>',
    e(actorName[c.next_actor]||c.next_actor||'—')+(c.next_action?'<span class="meta">'+e(c.next_action)+'</span>':''),
    ...(quality?[e(stamp(c.submitted_at)),e(ageDays(c.submitted_at))]:[]),e(stamp(c.updated_at))]);
  const mobile=lastList.map(c=>'<article class="mobile-case"><a href="#caso/'+encodeURIComponent(c.id)+'"><strong>RC-'+e(c.id)+'</strong> · '+e(c.client || 'Por identificar')+'</a><span class="badge">'+e(c.label || statuses[c.status])+'</span>'+
    '<p>'+e([actorName[c.next_actor],c.next_action].filter(Boolean).join(' · ') || 'Sin acción pendiente')+'</p>'+
    '<details><summary>Fechas y canal</summary>'+facts([['Canal',channelName[c.channel]],['Recepción',c.date],['Última actuación',stamp(c.updated_at)],...(quality?[['Envío',stamp(c.submitted_at)],['Espera',ageDays(c.submitted_at)]]:[])])+'</details></article>').join('');
  const resultHTML=(rows.length?'<div class="wide-list">'+table(['Expediente','Cliente y canal','Recepción','Estado','Siguiente responsable',...(quality?['Envío a Calidad','Antigüedad']:[]),'Última actuación'],rows)+'</div><div class="mobile-list">'+mobile+'</div>':'<p class="empty">No hay expedientes que coincidan con esta vista.</p>')+
    '<div class="pager"><span>'+e(total)+' expediente'+(total===1?'':'s')+'</span><button data-page="-1" '+(filters.page<=1?'disabled':'')+'>Anterior</button><span>Página '+e(filters.page)+'</span><button data-page="1" '+(filters.page*pageSize>=total?'disabled':'')+'>Siguiente</button></div>';
  if(quiet && $('#list-results')) {$('#list-results').innerHTML=resultHTML;syncCounts(response.counts);return;}
  const actions=role==='nacional'?'<a class="btn primary" href="#contacto">Revisar CONTACTO de RSales</a><a class="btn" href="#nuevo">Crear reclamo manual</a>':role==='internacional'?'<a class="btn primary" href="#nuevo">Nuevo reclamo internacional</a>':'';
  const tabs={'':'Todos',borrador:'Borradores',enviado:'Sin acuse',analisis:'En análisis',informacion:'Me pidieron información',parcial:'Respuesta parcial',respondido:'Respuesta recibida',por_verificar:'Por verificar',cerrado:'Cerrados',anulado:'Anulados'};
  if(role==='calidad') {delete tabs.borrador; tabs.informacion='Falta información del área';}
  const tabsHTML='<div class="queue-tabs" aria-label="Filtrar por estado">'+Object.entries(tabs).map(([v,l])=>'<button data-status="'+v+'" aria-pressed="'+(v===filters.status)+'">'+e(l)+' <span class="queue-count" data-state="'+v+'"></span></button>').join('')+'</div>';
  const filterHTML='<form id="filters" class="filters">'+field('q','Buscar',filters.q,'search','placeholder="Caso, cliente, lote, producto, factura o fecha"')+
    (!intake?select('channel','Canal',{'':'Todos',nacional:'Nacional',internacional:'Internacional'},filters.channel):'')+
    select('origin','Origen de captura',{'':'Todos los orígenes',...(role==='internacional'?{internacional_manual:origins.internacional_manual}:role==='nacional'?{rsales_formulario:origins.rsales_formulario,nacional_manual:origins.nacional_manual}:origins)},filters.origin)+
    (quality?select('sort','Ordenar por',{age:'Mayor antigüedad',channel:'Canal',next:'Siguiente responsable'},filters.sort):'')+'</form>';
  const historyView=route==='historial';
  content.innerHTML=head(historyView?'Historial':quality?'Mesa de Calidad':admin?'Seguimiento':'Mis reclamos',quality?'Recepción y seguimiento técnico':admin?'Seguimiento de ambos canales':channelName[role],historyView?'':actions)+
    '<section class="panel">'+filterHTML+tabsHTML+'<div id="list-results">'+resultHTML+'</div></section><div id="source-cut" class="meta"></div>';
  syncCounts(response.counts);
  if(restore) window.scrollTo(0,listPositions[route]);
  loadCut();
}
async function loadCut() {
  try {
    sourceStatus=await api('/api/catalog/status');
    const dest=$('#source-cut');
    if(dest && sourceStatus) {
      const cut=sourceStatus.cut || sourceStatus.cutoff || sourceStatus.as_of || sourceStatus.date || sourceStatus.fecha_corte || sourceStatus.completed_at;
      dest.textContent=sourceStatus.complete ? 'Corte de productos · '+stamp(sourceStatus.products_at || cut)+'  /  Corte de clientes · '+stamp(sourceStatus.clients_at || cut) : 'Directorio y catálogo pendientes de carga.';
    }
  } catch {/* The catalog error is also shown on an explicit search. */}
}
function changeFilters(update) {
  Object.assign(filters,{page:1},update);keepFilters();
  showList(!!$('#filters') && !('status' in update)).catch(err=>message(err.message,true));
}
const clientFields={client_code:'ID interno del cliente',rsales_code:'Código RSales del cliente',region:'Región',country:'País',tax_id:'Identificación fiscal',city:'Ciudad',seller:'Vendedor',contact:'Persona de contacto',phone:'Teléfono',email:'Correo',address:'Dirección',medium:'Medio de recepción'};
const extraFields={noticed_at:'Fecha en que se detectó',requested:'Solución solicitada',stage:'Etapa de detección'};
function lookup(type,label,hint,option) {
  return '<div class="lookup" data-lookup="'+type+'"><label class="field">'+e(label)+'<input type="search" class="search-input" autocomplete="off" placeholder="'+e(hint)+'"></label>'+
    (option?'<label class="check"><input type="checkbox"> '+e(option)+'</label>':'')+'<div class="results" aria-live="polite"></div></div>';
}
function docRow(d={}, index) {
  const key=d.key || (Number.isInteger(index)?'doc-'+index:freshKey());
  const scope=d.scope || 'case';
  return '<details class="line-card editor-card" data-document data-document-key="'+e(key)+'" open><summary>Documento comercial</summary><div class="grid">'+
    select('type','Tipo de documento',{factura:'Factura',oc:'Orden de compra',pedido:'Pedido',exportacion:'Documento de exportación',otro:'Otro'},d.type)+
    field('number','Número o referencia',d.number)+field('date','Fecha del documento',d.date,'date')+
    select('scope','A qué corresponde',Object.fromEntries([['case','Todo el expediente'],...(scope==='case'?[]:[[scope,'Referencia guardada']])]),scope)+
    field('support','Soporte o folio declarado',d.support)+'</div><button type="button" class="text-button" data-command="remove-doc">Quitar documento</button></details>';
}
function syncDocScopes(form) {
  if(!form) return;
  const lines=$$('[data-line]',form).map((el,i)=>[el.dataset.line,'Referencia '+(i+1)+' · '+(valueIn(el,'description') || valueIn(el,'reported_code') || 'Por identificar')]);
  for(const scope of $$('[data-document] select[name=scope]',form)) {
    const current=scope.value;
    const items=[['case','Todo el expediente'],...lines];
    if(current && !items.some(([k])=>k===current)) items.push([current,'Referencia retirada · revisar']);
    if(items.length===scope.options.length && items.every(([k,v],i)=>scope.options[i].value===k && scope.options[i].text===v)) continue;
    scope.replaceChildren(...items.map(([k,v])=>new Option(v,k,false,k===current)));
  }
}
const declaredMeasures=['unidad','caja','display','balde','tambor','tanque','litro','galón'];
const countMeasures=new Set(['unidad','unidades','caja','cajas','display','displays','balde','baldes','tambor','tambores','tanque','tanques']);
const editorPqr={producto:'Reclamo de producto',servicio:'Queja de servicio',pregunta:'Pregunta'};
function countryChoices() {
  const names=new Intl.DisplayNames(['es-CO'],{type:'region',fallback:'none'});
  const countries=new Set();
  for(const a of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') for(const b of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') {
    const name=names.of(a+b);
    if(name) countries.add(name);
  }
  return [...countries].sort((a,b)=>a.localeCompare(b,'es')).concat('Por verificar');
}
function choicesList(id,options) {
  return '<datalist id="'+id+'">'+options.map(value=>'<option value="'+e(value)+'"></option>').join('')+'</datalist>';
}
function printSource(data) {
  if (!data || typeof data !== 'object') return '';
  const pairs = Object.entries(data);
  if (!pairs.length) return '';
  return '<dl class="facts">'+pairs.map(([k,v])=>'<div><dt>'+e(sourceLabels[k] || k.replaceAll('_',' '))+'</dt><dd>'+
    (v && typeof v==='object'?printSource(v):e(v ?? 'Pendiente'))+'</dd></div>').join('')+'</dl>';
}
const sourceLabels={difference:'Diferencia por revisar',currency:'Moneda',declared_amount:'Importe declarado',calculated_amount:'Importe calculado',numero_contacto:'Contacto',fecha:'Fecha',fecha_origen:'Fecha declarada',id_solicitud:'ID de solicitud',version_formulario:'Versión de formulario',marcas_fuente:'Fechas del comprobante',calidad_fuente:'Seguimiento declarado · preguntas 44–49',codigo_origen:'Código reportado',campos:'Preguntas',fotos:'Páginas de evidencia',lineas:'Referencias originales',type:'Tipo',number:'Número',date:'Fecha',quantity:'Cantidad',line_id:'Línea',note:'Detalle',report_id:'Versión del informe',media:'Medio',support:'Soporte',actor:'Responsable',case_id:'Expediente',client_snapshot:'Ficha de cliente elegida',product_snapshot:'Producto del catálogo elegido',documents:'Documentos',...clientFields,...extraFields};
function lineCard(line={}) {
  const idx=line.fields?.editor_key || (line.id?'line-'+line.id:freshKey());
  editLines.set(idx,line);
  const f=line.fields || {};
  const lselect=select('incidence','Incidencia',{'daño físico':'Daño físico','servicio':'Servicio','otro':'Otra incidencia'},line.incidence||'otro');
  return '<details class="line-card editor-card" data-line="'+e(idx)+'" open><summary>Referencia reclamada'+(line.description?' · '+e(line.description):'')+'</summary>'+
    lookup('products','Buscar en el catálogo','Código, producto, color o presentación','Incluir inactivas')+
    '<div class="selected-source meta">'+(f.product_snapshot?'Selección del catálogo · '+e(f.product_snapshot.code):'Referencia manual o del comprobante')+'</div>'+
    '<div class="grid">'+field('code','SKU comercial confirmado',line.code)+field('reported_code','Código reportado en el soporte',f.reported_code || f.codigo_origen)+field('client_product_code','Código del cliente',f.client_product_code)+field('source_description','Descripción libre del documento',f.source_description)+field('description','Descripción',line.description)+field('color','Color',f.color)+field('size','Tamaño',f.size)+field('presentation','Presentación comercial',line.presentation)+field('quantity','Cantidad reclamada',line.quantity,'number','min="0" step="any"')+field('unit','Medida reclamada (elige o escribe otra)',line.unit || 'unidad','text','list="medidas-reclamo"')+field('lot','Lote o fecha impresa',line.lot,'text','placeholder="Pendiente si se desconoce"')+field('part','Parte afectada',f.part)+lselect+'<label class="check"><input type="checkbox" name="common_reason" '+(f.common_reason?'checked':'')+'> Aplicar el relato del cliente a esta referencia</label>'+field('reason','Motivo en palabras del cliente',f.individual_reason ?? (f.common_reason?'':line.reason),'textarea')+field('documents','Facturas u OC de esta línea',f.documents)+field('evidence_pending','Evidencia por recibir',f.evidence_pending)+'</div>'+
    (Array.isArray(f.fotos)?'<p class="meta inline-space">Fotos en el comprobante · '+e(f.fotos.map(p=>'Pregunta '+p.campo+', página '+p.pagina).join(' · '))+'</p>':'')+
    '<button type="button" class="text-button" data-command="remove-line">Quitar referencia</button></details>';
}
function showEdit(data) {
  const v=data || {fields:{},lines:[]};
  const f=v.fields||{};
  editBase=v;editLines=new Map();
  const intl=role==='internacional';
  setCrumb(data?'Revisar RC-'+data.id:'Nuevo reclamo');
  const caseTitle=data?'Revisar RC-'+data.id:intl?'Nuevo reclamo internacional':'Nuevo reclamo nacional';
  const frozenPDF=data?.origin==='rsales_formulario';
  const source=f.contacto || f.pdf_source || f.source_pdf || f.original || f.origen_contacto;
  const block = (id, title, body) => '<section class="panel form-block" id="block-'+id+'"><h2>'+e(title)+'</h2>'+body+'</section>';
  const documents = '<details class="inline-space" '+(intl || f.documents?.length?'open':'')+'><summary>Facturas, órdenes de compra y otros documentos</summary><div id="documents">'+(Array.isArray(f.documents)?f.documents:[]).map(docRow).join('')+'</div><button type="button" data-command="add-doc">Agregar factura u otro documento</button></details>';
  const origin=v.origin || role+'_manual';
  const opening = facts([['Origen de captura',origins[origin]],['Responsable',actorName[role]],['Formulario de origen',source?.formulario],['Versión de origen',source?.version_formulario],['Solicitud de origen',source?.id_solicitud],['Número de CONTACTO',source?.numero_contacto]]) +
    lookup('clients',intl?'Buscar en el directorio de exportación':'Buscar en el maestro nacional de RSales',intl?'Nombre, alias, país o identificación':'Nombre, código o ciudad',intl?'':'Incluir país por verificar')+
    '<p id="client-picked" class="paper-note">'+e(f.client_snapshot?'Cliente seleccionado · '+(f.client_snapshot.name || v.client):'Puedes completar los datos manualmente.')+'</p>'+
    '<div class="grid">'+field('client','Razón social',v.client)+Object.entries(clientFields).map(([key,label])=>field('f_'+key,label,f[key] || '',key==='email'?'email':key==='phone'?'tel':'text',key==='medium'?'list="medios-reclamo"':key==='country'?'list="paises-reclamo"':'')).join('')+'</div>'+
    (intl?'<div class="inline-space"><button type="button" data-command="save-client">Guardar este contacto en el directorio</button></div><div id="client-codes" class="inline-space"></div>':'')+
    documents;
  const problem = '<div class="grid">'+field('date','Fecha de recepción en Lubristone',v.date || dateInColombia(),'date')+field('f_noticed_at','Fecha en que el cliente notó el problema',f.noticed_at,'date')+
    select('kind','Tipo de PQR',editorPqr,f.pqr_type || v.kind)+select('f_stage','Etapa de detección',{'Se desconoce':'Se desconoce',bodega:'En bodega',recepcion:'Al recibir',transito:'En tránsito',uso:'Durante el uso',otra:'Otra'},f.stage)+field('description','Relato del cliente',v.description,'textarea')+field('f_requested','Solución solicitada',f.requested,'textarea')+'</div>';
  const products = '<div id="lines">'+(v.lines.length?v.lines:v.kind==='servicio'?[]:[{}]).map(lineCard).join('')+'</div><button type="button" data-command="add-line">Agregar otra referencia</button>'+
    '<div class="evidence-editor"><h3>Evidencias del reclamo</h3><div id="saved-evidence">'+editorEvidence(v)+'</div>'+
    (v.id?'<div class="grid">'+evidenceTarget(v,'evidence_line')+'<label class="field">Foto, video o documento<input name="editor_file" type="file"></label></div><button type="button" data-command="editor-upload">Adjuntar archivo</button>':'')+
    '<button type="submit" value="stay" class="text-button">'+(v.id?'Guardar cambios y actualizar referencias':'Preparar la carga de archivos')+'</button></div>';
  const review = (frozenPDF?(source?'<details><summary>Datos originales del comprobante</summary>'+printSource(source)+'</details>':'')+field('note','Motivo de las correcciones', '', 'textarea','placeholder="Indica qué cambió respecto del comprobante."'):'')+
    '<p id="edit-state" class="meta">'+(v.id?'Borrador guardado · RC-'+v.id+' · '+e(stamp(v.updated_at)):'Borrador nuevo, pendiente de guardar.')+'</p>'+
    '<div class="form-footer"><button type="button" class="primary" data-command="review-case">Revisar antes de enviar</button><button type="submit" value="save">Guardar '+(frozenPDF?'revisión':'borrador')+'</button><a href="'+(v.id?'#caso/'+v.id:'#bandeja')+'">Volver</a></div><div id="review-preview" hidden></div>';
  content.innerHTML=head(caseTitle,data?'Borrador · '+day(v.created_at):'Recepción '+channelName[role].toLowerCase())+
    '<form id="editor" data-dirty>'+block('client','Cliente y origen',opening)+block('issue','Problema reportado',problem)+
    block('products','Productos y evidencias',products)+block('review','Revisión y envío',review)+
    choicesList('medidas-reclamo',declaredMeasures)+choicesList('medios-reclamo',['Vendedor','Correo','Llamada','WhatsApp','Otro'])+
    choicesList('paises-reclamo',countryChoices())+'</form>';
  syncDocScopes($('#editor'));
  if(typeof enhanceCatalogEditor==='function') enhanceCatalogEditor();
  if (intl) {
    const clientLookup=$('[data-lookup=clients]');
    runLookup(clientLookup).catch(err=>message(err.message,true));
    if(f.client_snapshot?.id) loadCodes(f.client_snapshot.id);
  }
}
function evidenceTarget(c,name) {
  const options={'':'Todo el expediente'};
  for(const line of c.lines) options['line:'+line.id]='Línea · '+lineLabel(line);
  for(const d of c.fields?.documents || []) {
    if(d.number) options['doc:'+(d.key || JSON.stringify({type:d.type,number:d.number,date:d.date || ''}))]=(d.type || 'Documento')+' · '+d.number;
  }
  return select(name,'Asociar evidencia a',options);
}
function setEvidenceTarget(data,choice) {
  if(choice.startsWith('doc:')) data.set('document_key',choice.slice(4));
  if(choice.startsWith('line:')) data.set('line_id',choice.slice(5));
}
function evidenceLabel(f,c) {
  if(f.document) return [f.document.type,f.document.number].filter(Boolean).join(' · ');
  return f.line_id?lineLabel(c.lines.find(l=>l.id===f.line_id)||{id:f.line_id}):'Todo el expediente';
}
function editorEvidence(c) {
  const rows=(c.files || []).map(f=>{
    return ['<a href="/api/cases/'+c.id+'/files/'+f.id+'">'+e(f.filename)+'</a>',e(evidenceLabel(f,c))];
  });
  return rows.length?table(['Archivo guardado','Asociado a'],rows)+evidenceGallery(c):'<p class="paper-note">Sin archivos guardados.</p>';
}
function reviewEditor() {
  const form=$('#editor');
  for(const prior of $$('.field-error',form)) prior.remove();
  let first=null;
  function problem(control,text) {
    if(!control || control.nextElementSibling?.classList.contains('field-error')) return;
    const hint=document.createElement('small');
    hint.className='field-error';
    hint.id='validation-'+freshKey();
    hint.textContent=text;
    control.after(hint);
    control.setAttribute('aria-invalid','true');
    control.setAttribute('aria-describedby',hint.id);
    first ??=control;
  }
  for(const invalid of $$(':invalid',form)) problem(invalid,invalid.validationMessage);
  const payload=editPayload(form), issue=$('#block-issue',form);
  if(!payload.client) problem(form.elements.client,'Escribe o selecciona la razón social.');
  if(!payload.date) problem($('[name=date]',issue),'Indica la fecha de recepción.');
  if(payload.kind==='servicio' && !payload.description) problem($('[name=description]',issue),'Describe la pregunta o queja del cliente.');
  const cards=$$('[data-line]',form);
  if(!payload.lines.length && payload.kind==='producto') problem(cards[0]?.querySelector('[name=description]') || $('[data-command=add-line]',form),'Agrega al menos una referencia identificable.');
  const totals=new Map();
  for(const line of payload.lines) {
    const card=cards.find(el=>el.dataset.line===line.fields.editor_key);
    if(!(line.code || line.description || line.fields.reported_code || line.fields.client_product_code || line.fields.source_description))
      problem($('[name=description]',card),'Identifica el producto por descripción o código.');
    const measure=line.unit.toLocaleLowerCase('es-CO').trim();
    if(!(line.quantity>0)) problem($('[name=quantity]',card),'Indica una cantidad mayor que cero.');
    else if(countMeasures.has(measure) && !Number.isInteger(line.quantity))
      problem($('[name=quantity]',card),'Para esta medida se requieren envases enteros.');
    if(!measure || ['otra','otro'].includes(measure)) problem($('[name=unit]',card),'Selecciona o escribe el nombre de la medida declarada.');
    if(!line.reason) {
      const control=line.fields.common_reason?$('[name=description]',issue):$('[name=reason]',card);
      problem(control,'Escribe el motivo de esta referencia o aplica un relato común con contenido.');
    }
    totals.set(measure,(totals.get(measure)||0)+line.quantity);
  }
  for(const doc of $$('[data-document]',form)) {
    if(valueIn(doc,'scope')!=='case' && !payload.lines.some(l=>l.fields.editor_key===valueIn(doc,'scope')))
      problem($('[name=scope]',doc),'Vincula una referencia vigente o elige todo el expediente.');
    if((valueIn(doc,'support')||valueIn(doc,'date')) && !valueIn(doc,'number'))
      problem($('[name=number]',doc),'Identifica el documento o quita la fila vacía.');
  }
  if(form.elements.editor_file?.files.length) problem(form.elements.editor_file,'Adjunta el archivo seleccionado antes de enviar.');
  const box=$('#review-preview');
  if(first) {
    box.hidden=true;
    for(let el=first.parentElement;el;el=el.parentElement) if(el.tagName==='DETAILS') el.open=true;
    first.focus();
    return;
  }
  const docs=payload.fields.documents.map(doc=>doc.number).join(', ');
  const files=(editBase.files||[]).map(item=>item.filename).join(', ');
  box.hidden=false;
  box.innerHTML='<h3>Revisión del expediente</h3>'+facts([['Cliente',payload.client],['Canal',channelName[role]],['Fecha de recepción',payload.date],['Tipo',editorPqr[payload.fields.pqr_type]],['Documentos',docs],['Evidencias guardadas',files],['Recibe',actorName.calidad+' · Calidad']])+
    '<p class="notes">'+e(payload.description)+'</p>'+
    (payload.lines.length?table(['Referencia','Presentación','Cantidad reclamada','Lote','Motivo'],payload.lines.map(l=>[e(l.code || l.description?lineLabel(l):l.fields.source_description||l.fields.reported_code||l.fields.client_product_code),e(l.presentation||'Por verificar'),e(l.quantity)+' '+e(l.unit),e(l.lot||'Pendiente'),e(l.reason)])):'')+
    (totals.size?'<p><strong>Totales por medida</strong> · '+Array.from(totals,([unit,quantity])=>e(quantity.toLocaleString('es-CO',{maximumFractionDigits:3}))+' '+e(unit)).join(' · ')+'</p>':'')+
    '<button type="submit" class="primary inline-space" value="submit">Confirmar envío a Calidad</button>';
  box.scrollIntoView({block:'nearest'});
}
async function uploadEditorEvidence() {
  const form=$('#editor'), input=form.elements.editor_file, file=input.files[0];
  if(!file) throw new Error('Selecciona una foto, video o documento.');
  const payload=new FormData();
  const lid=form.elements.evidence_line.value;
  payload.set('file',file);
  setEvidenceTarget(payload,lid);
  payload.set('version',editBase.version);
  const path='/api/cases/'+editBase.id+'/files';
  const key=tokenFor([path,editBase.version,lid,file.name,file.size,file.lastModified].join('/'));
  const updated=await api(path,{method:'POST',body:payload,key});
  pending=null;
  editBase.files=updated.files;
  editBase.version=updated.version;
  editBase.updated_at=updated.updated_at;
  input.value='';
  $('#saved-evidence').innerHTML=editorEvidence(updated);
  $('#review-preview').hidden=true;
  $('#edit-state').textContent='Archivo guardado · RC-'+updated.id+' · '+stamp(updated.updated_at);
  message('Archivo guardado en el expediente.');
}
function valueIn(scope,name) {return scope.querySelector('[name="'+name+'"]')?.value.trim() || '';}
function editPayload(form) {
  const f={...editBase.fields};
  $$('[name^="f_"]',form).forEach(el=>f[el.name.slice(2)]=el.value.trim());
  const sourceDocs=Array.isArray(editBase.fields?.documents)?editBase.fields.documents:[];
  f.documents=$$('[data-document]',form).map(el=>{
    const key=el.dataset.documentKey, previous=sourceDocs.find((d,i)=>(d.key || 'doc-'+i)===key);
    return {...previous,key,type:valueIn(el,'type'),number:valueIn(el,'number'),date:valueIn(el,'date'),scope:valueIn(el,'scope'),support:valueIn(el,'support')};
  }).filter(d=>d.number||d.date||d.support);
  const issue=$('#block-issue',form), description=valueIn(issue,'description');
  f.pqr_type=valueIn(issue,'kind');
  const lines=$$('[data-line]',form).map(el=>{
    const old=editLines.get(el.dataset.line)||{}, extras={...old.fields};
    extras.editor_key=el.dataset.line;
    extras.common_reason=!!$('[name=common_reason]',el)?.checked;
    for(const k of ['part','reported_code','client_product_code','source_description','color','size','documents','evidence_pending']) extras[k]=valueIn(el,k);
    const line={...old,fields:extras};
    for(const k of ['code','description','presentation','unit','lot','reason','incidence']) line[k]=valueIn(el,k);
    extras.individual_reason=line.reason;
    if(extras.common_reason) line.reason=description;
    line.quantity=Number(valueIn(el,'quantity')||0);
    return line;
  }).filter(l=>l.id || l.code || l.description || l.quantity || l.reason || l.fields.reported_code || l.fields.client_product_code || l.fields.source_description || l.fields.evidence_pending);
  return {version:editBase.version,client:valueIn(form,'client'),date:valueIn(issue,'date'),description,kind:f.pqr_type==='producto'?'producto':'servicio',fields:f,lines,note:valueIn(form,'note')};
}
async function loadCodes(clientId) {
  const node=$('#client-codes'); if(!node) return;
  const {items}=await api('/api/catalog/codes?'+queryOf({client_id:clientId}));
  if(node!==$('#client-codes')) return;
  node.innerHTML=items.length?'<details><summary>Códigos documentados para este cliente ('+items.length+')</summary>'+table(['Código del cliente','Descripción','Presentación'],items.map(c=>[e(c.codigo),e(c.descripcion),e(c.presentacion)]))+'</details>':'';
}
const latestLookup=new WeakMap();
async function runLookup(el) {
  const ticket=(latestLookup.get(el)||0)+1;latestLookup.set(el,ticket);
  const kind=el.dataset.lookup,q=$('input[type=search]',el).value.trim(),check=$('input[type=checkbox]',el)?.checked;
  const results=$('.results',el);
  if (!q && !['clients'].includes(kind)) {results.textContent='';return;}
  results.textContent='Buscando…';
  try {
    const response=await api((kind==='orders'?'/api/pedidos':'/api/catalog/'+kind)+'?'+queryOf({q,page_size:12,inactive:kind==='products'&&check?1:'',unverified:kind==='clients'&&check?1:''}));
    if(!el.isConnected || latestLookup.get(el)!==ticket) return;
    const items=response.items||[];
    lookupItems.set(el,items);
    results.innerHTML=items.length ? items.map((item,i)=>{
      let label,info;
      if(kind==='products'){label=item.code+' · '+item.description;info=[item.presentacion_etiquetada || 'Presentación por verificar',item.state==='Y'?'Activo':'Histórico',item.group_code, item.family_code,item.line_code].filter(Boolean).join(' · ');}
      if(kind==='clients'){label=item.name;info=[item.id,item.country,item.city,item.tax_id,item.warning].filter(Boolean).join(' · ');}
      if(kind==='orders'){label=item.number || item.code;info=[item.code,item.channel,item.created].filter(Boolean).join(' · ');}
      return '<button type="button" data-pick="'+i+'">'+e(label)+'<small>'+e(info)+'</small></button>';
    }).join('') : '<p class="meta">Sin coincidencias.</p>';
  } catch(err) { if(latestLookup.get(el)===ticket) results.textContent=err.message; }
}
function pick(btn) {
  const el=btn.closest('[data-lookup]'),item=lookupItems.get(el)?.[Number(btn.dataset.pick)];
  if(!item) return;
  const set=(scope,key,value)=>{const input=$('[name="'+key+'"]',scope);if(input) input.value=value??'';};
  if(el.dataset.lookup==='products'){
    const line=el.closest('[data-line]'), old=editLines.get(line.dataset.line);
    applyCatalogProduct(line,item);
    for(const [key,val] of Object.entries({code:item.code,description:item.description,presentation:item.presentacion_etiquetada||''})) set(line,key,val);
    old.fields={...old.fields,product_snapshot:item};$('.selected-source',line).textContent='Selección del catálogo · '+item.code;
  } else if(el.dataset.lookup==='clients') {
    const form=$('#editor');
    set(form,'client',item.name);
    for(const [key,val] of Object.entries({client_code:item.id,country:item.country,city:item.city,tax_id:item.tax_id,contact:item.contact,phone:item.phone,email:item.email,address:item.address})) {
      const originalKey={city:'ciudad',contact:'contacto',phone:'telefono',email:'correo',address:'direccion'}[key];
      const original=editBase.fields?.contacto;
      set(form,'f_'+key,val || (originalKey && original && typeof original==='object'?original[originalKey]:'') || '');
    }
    applyCatalogClient(form,item);
    editBase.fields={...editBase.fields,client_snapshot:item};
    $('#client-picked').textContent=[item.source,item.name,item.warning].filter(Boolean).join(' · ');
    if(role==='internacional') loadCodes(item.id).catch(err=>message(err.message,true));
  } else {
    const form=el.closest('form');set(form,'order_id',item.id);$('.chosen-order',form).textContent='Pedido elegido · '+(item.number||item.code);
  }
  $('.results',el).textContent='';setDirty();
}
function showPDF() {
  setCrumb('Revisar CONTACTO de RSales');
  content.innerHTML=head('Revisar CONTACTO de RSales','Comprobante nacional de contacto con el cliente')+
    panel('Importar comprobante PDF','<form id="pdf-preview"><label class="field">Comprobante original<input name="file" type="file" accept="application/pdf,.pdf" required></label><div class="form-footer"><button class="primary">Ver vista previa</button><a href="#nuevo">Crear reclamo manual</a></div></form>')+'<div id="pdf-data"></div>';
}
function renderPreview(response) {
  pdfToken=response.token;pdfPreview=response.parsed||{};
  const p=pdfPreview;
  const lines=p.lineas||[];
  $('#pdf-data').innerHTML=panel('Vista previa del comprobante',(response.warning?'<p class="notice">'+e(response.warning)+'</p>':'')+
    facts([['Contacto',p.numero_contacto],['Formulario',p.formulario],['Versión',p.version_formulario],['ID de solicitud',p.id_solicitud],['Empresa',p.cliente],['Asesor',p.vendedor],['Encargado',p.contacto],['Ciudad',p.ciudad],['Teléfono',p.telefono],['Dirección',p.direccion],['Correo',p.correo],['Fecha',p.fecha],['Páginas',p.paginas],['Expediente existente',response.existing_case?.id || response.existing_case]])+
    (lines.length?table(['Código reportado','Referencia','Cantidad','Motivo','Fotos del PDF'],lines.map(l=>[e(l.codigo_origen),e(l.descripcion),e(l.cantidad)+' '+e(l.unidad),e(l.motivo),contactPhotoGallery('/api/contacto/preview/'+encodeURIComponent(response.token)+'/fotos',l.fotos || [])])):'')+
    (response.source_changed?'<details open><summary>Cambios frente a la versión anterior</summary>'+
      (Object.keys(response.source_differences||{}).length?printSource(response.source_differences):'<p>El archivo cambió. Los datos extraídos coinciden.</p>')+'</details>':'')+
    '<details><summary>Datos originales y seguimiento</summary>'+printSource(p)+'</details><div class="form-footer"><button type="button" class="primary" data-command="import-pdf">Guardar comprobante y revisar</button></div>');
}
const lineLabel=l=>[l.code,l.description].filter(Boolean).join(' · ') || ('Línea '+l.id);
function lineSelect(c,optional=false) {
  const options=optional?{'':'Todo el expediente'}:{};
  c.lines.forEach(l=>options[l.id]=lineLabel(l));
  return select('line_id','Referencia',options);
}
function formAction(id,title,body,label=title,open=false) {
  return '<details class="action-box no-print" '+(open?'open':'')+'><summary>'+e(title)+'</summary><form data-action="'+id+'" data-dirty><div class="grid">'+body+'</div><div class="form-footer"><button class="primary">'+e(label)+'</button></div></form></details>';
}
function actionButton(id,label,primary=true){return '<button type="button" data-action-button="'+id+'" class="'+(primary?'primary':'')+'">'+e(label)+'</button>';}
const amount=(name,label,value='',attrs='')=>field(name,label,value,'number','min="0" step="any" '+attrs);
const noteInput=(label='Motivo o detalle')=>field('note',label,'','textarea','required');
function reportForm(c) {
  const last=[...c.reports].reverse().find(r=>r.cycle===c.cycle);
  const draft=last?.status==='draft'?last:{};
  const files={ '':'Sin informe adjunto' };
  c.files.filter(f=>f.actor==='calidad' && (!f.kind || f.kind==='technical')).forEach(f=>files[f.id]=f.filename);
  return '<details class="action-box no-print"><summary>Redactar informe técnico</summary><form data-action="report" data-dirty>'+
    '<div class="grid">'+field('report','Informe técnico',draft.report,'textarea')+select('file_id','Informe de Calidad adjunto',files)+field('summary','Explicación para el vendedor',draft.summary,'textarea')+field('note','Motivo de revisión de una respuesta publicada','','textarea')+'</div>'+
    c.lines.map(l=>{
      const previous=draft.findings?.find(f=>Number(f.line_id)===l.id)||{};
      const files=c.files.filter(f=>f.line_id===l.id);
      const evidence=files.map(f=>'<a href="/api/cases/'+c.id+'/files/'+f.id+'">'+e(f.filename)+'</a>').join(' · ');
      const pdf=c.events.find(ev=>ev.action==='import')?.payload.file_id;
      return '<fieldset data-finding="'+e(l.id)+'"><legend>'+e(lineLabel(l))+'</legend><div class="finding-workspace"><div><h3>Reporte del cliente</h3>'+
        facts([['Cantidad reclamada',l.quantity+' '+l.unit],['Lote',l.lot||'Pendiente']])+'<p class="notes">'+e(l.reason||c.description)+'</p><p>'+evidence+'</p>'+
        (pdf?contactPhotoGallery('/api/cases/'+c.id+'/contacto/'+pdf+'/fotos',l.fields.fotos||l.fields.photos||[]):'')+'</div>'+
        '<div><h3>Análisis por referencia</h3><label class="check"><input type="checkbox" name="cover" '+(previous.line_id?'checked':'')+'> Incluir esta referencia en el informe</label><div class="grid">'+
        field('finding','Hallazgo',previous.finding,'textarea')+field('evidence_reviewed','Evidencia revisada',previous.evidence_reviewed)+field('cause','Causa confirmada o pendiente',previous.cause,'text','placeholder="Pendiente si falta determinarla"')+
        select('attributed_stage','Etapa atribuida con sustento',{'':'Pendiente',recepcion:'Recepción',bodega:'Bodega',transito:'Tránsito',uso:'Durante el uso',otra:'Otra'},previous.attributed_stage)+
        field('result','Resultado técnico',previous.result)+amount('confirmed','Cantidad confirmada · '+l.unit,previous.confirmed??'', 'max="'+e(l.quantity)+'"')+'</div></div></div></fieldset>';
    }).join('')+'<div class="form-footer"><button type="submit" name="report_action" value="report_draft">Guardar borrador técnico</button><button type="submit" class="primary" name="report_action" value="publish">Publicar respuesta</button></div></form></details>';
}
function workForms(c) {
  let html='';
  const owner=role===c.channel,manager=owner||admin, quality=role==='calidad', hasLines=c.lines.length>0;
  const state=c.status, closed=['cerrado','anulado'].includes(state), sent=state!=='borrador';
  if(closed) return (state==='anulado'?admin:manager)?formAction('reopen','Reabrir expediente',noteInput('Motivo de reapertura')):'';
  if(owner && !sent) {
    html+='<div class="actions no-print"><a class="btn" href="#editar/'+c.id+'">Editar borrador</a>'+actionButton('submit','Enviar a Calidad')+'</div>';
  }
  if(quality && state==='enviado') html+=actionButton('ack','Acusar recibo');
  if(quality && ['analisis','parcial'].includes(state)) html+=formAction('request_info','Pedir información al área',lineSelect(c,true)+noteInput('Qué información necesita Calidad'));
  if(quality && ['analisis','informacion','parcial','respondido'].includes(state)) html+=reportForm(c);
  if(owner && state==='informacion') html+=formAction('provide_info','Responder solicitud de Calidad',noteInput('Información aportada'),'Enviar información',true);
  if((role==='nacional'||admin) && c.channel==='nacional' && hasLines){
    html+=formAction('link_order','Vincular pedido nacional',lineSelect(c)+amount('quantity','Cantidad de la línea asignada a este pedido','','required')+
      '<div class="wide">'+lookup('orders','Buscar pedido','Número o código exacto del pedido','')+'<input type="hidden" name="order_id"><p class="chosen-order meta"></p></div>'+noteInput('Soporte del vínculo'));
  }
  if(manager && sent){
    if(c.channel==='internacional') html+=creditForm(c);
    if(!hasLines && c.kind==='servicio'){
      html+=formAction('decision','Registrar decisión de servicio',select('type','Tipo de solución',{rejected:'Rechazo sustentado',credit:'Nota crédito',other:'Otra solución'})+field('authorized_by','Persona que autorizó','','text','required')+noteInput());
      html+=formAction('resolve','Documentar cumplimiento del servicio',field('date','Fecha',dateInColombia(),'date','required')+field('support','Soporte','','text','required')+noteInput());
    }
    if(hasLines){
      const line=c.lines[0];
      html+=formAction('decision','Registrar decisión comercial',lineSelect(c)+amount('replacement','A reponer',line.replacement)+amount('rejected','Rechazadas',line.rejected)+amount('credit','Con nota crédito',line.credit)+amount('other','Otra solución',line.other)+field('authorized_by','Persona que autorizó','','text','required')+noteInput('Solución ofrecida y motivo de la decisión'));
      const attached={'':'Sin archivo asociado'};
      c.files.filter(f=>f.kind==='evidence').forEach(f=>attached[f.id]=f.filename);
      const movement=lineSelect(c)+amount('quantity','Cantidad de este movimiento','','required')+field('date','Fecha del movimiento',dateInColombia(),'date','required')+field('support','Número o referencia del soporte','','text','required')+select('file_id','Archivo de respaldo',attached);
      html+=formAction('dispatch','Registrar despacho parcial o total',movement)+formAction('receipt','Confirmar recepción',movement)+formAction('resolve','Documentar crédito u otra solución',select('type','Solución',{credit:'Nota crédito',other:'Otra solución'})+movement);
    }
    const reports={'':'Solución comercial sin informe asociado'};
    c.reports.filter(r=>r.status==='published').forEach(r=>reports[r.id]='Informe '+r.id+' · '+stamp(r.created_at));
    html+=formAction('communicate','Registrar comunicación al cliente',field('sender','Quién comunicó','','text','required')+field('medium','Medio usado','','text','required')+field('date','Fecha',dateInColombia(),'date','required')+select('report_id','Versión comunicada',reports)+noteInput('Qué se comunicó'));
    html+=formAction('amend','Ampliar expediente',select('category','Tipo de ampliación',{inspeccion:'Inspección',lote:'Lote o trazabilidad',reproceso:'Reproceso y recuperación',financiero:'Compromiso financiero',documento:'Factura u orden de compra',reincidencia:'Reincidencia',otro:'Otro'})+
      lineSelect(c,true)+field('date','Fecha declarada',dateInColombia(),'date')+field('warehouse','Bodega o lugar')+field('responsible','Responsable')+field('lot','Lote verificado')+field('document','Documentos asociados')+select('file_id','Soporte del expediente',{'':'Sin adjunto',...Object.fromEntries(c.files.filter(f=>f.kind==='evidence').map(f=>[f.id,f.filename]))})+field('stage','Etapa o relación con el aviso inicial')+
      '<details class="wide"><summary>Cuantificación y recuperación</summary><div class="grid">'+field('unit','Medida original')+amount('quantity','Cantidad declarada')+amount('good_box','Buen estado con caja')+amount('good_without_box','Buen estado sin caja')+amount('transferred','Trasvasado a tambores')+amount('unrecovered','Sin recuperar')+field('equivalence','Equivalencia verificada y fuente')+field('currency','Moneda')+amount('declared_amount','Importe declarado')+amount('calculated_amount','Importe calculado')+field('credit_stage','Etapa del crédito')+'</div></details>'+noteInput('Hecho, resultado y procedencia de la información'));
    if(state==='respondido') {
      const pending=c.closure_issues || [];
      html+=formAction('close','Cerrar expediente',(pending.length?'<div class="wide"><h3>Requisitos pendientes</h3><ul>'+pending.map(item=>'<li>'+e(item)+'</li>').join('')+'</ul></div>':'')+field('note','Observaciones del cierre','','textarea'));
    }
  }
  if(admin) html+=formAction('cancel','Anular expediente',noteInput('Motivo de la anulación'));
  return html;
}
function fileList(c) {
  const rows=c.files.map(f=>['<a href="/api/cases/'+c.id+'/files/'+f.id+'">'+e(f.filename)+'</a>',e(evidenceLabel(f,c)),e(actorName[f.actor]||f.actor),e(stamp(f.created_at))]);
  return (rows.length?table(['Archivo','Asociación','Responsable','Fecha'],rows)+evidenceGallery(c):'<p class="paper-note">Sin archivos adjuntos.</p>')+
    (!['cerrado','anulado'].includes(c.status)?'<details class="action-box no-print"><summary>Adjuntar evidencia o informe</summary><form id="upload" data-dirty><div class="grid">'+evidenceTarget(c,'target')+'<label class="field">Foto, video o documento<input type="file" name="file" required></label>'+(role==='calidad'?select('kind','Visibilidad del archivo',{technical:'Informe interno hasta publicación',evidence:'Evidencia compartida desde la carga'}):'')+'</div><div class="form-footer"><button class="primary">Guardar archivo</button></div></form></details>':'');
}
function reportList(c) {
  if(!c.reports.length) return '<p class="paper-note">Todavía no hay una respuesta técnica disponible.</p>';
  return [...c.reports].reverse().map(r=>'<details class="entry" data-report-id="'+r.id+'"><summary>Informe '+r.id+' · '+(r.status==='draft'?'Borrador':'Publicado')+' · '+e(stamp(r.created_at))+'</summary><p class="meta">Ciclo '+e(r.cycle)+' · '+e(channelName[r.actor]||r.actor)+'</p><div class="notes">'+e(r.report)+'</div>'+facts([['Explicación para el vendedor',r.summary]])+
    (r.findings?.length?table(['Referencia','Hallazgo','Evidencia revisada','Causa','Etapa atribuida','Resultado','Daño confirmado'],r.findings.map(f=>[e(lineLabel(c.lines.find(l=>l.id===Number(f.line_id))||{id:f.line_id})),e(f.finding),e(f.evidence_reviewed||'—'),e(f.cause),e(f.attributed_stage||'Pendiente'),e(f.result),e(f.confirmed)])):'')+
    (r.file_id?'<a class="btn inline-space" href="/api/cases/'+c.id+'/files/'+r.file_id+'">Descargar informe adjunto</a> ':'')+
    (r.status==='published'?'<a class="btn no-print inline-space" href="/api/cases/'+c.id+'/letter/'+r.id+'">Descargar carta</a>':'')+'</details>').join('');
}
function labelSnapshot(c) {
  const f=c.fields || {};
  const items=Object.entries(clientFields).map(([k,l])=>[l,f[k]]);
  const docs=Array.isArray(f.documents)?f.documents:[];
  return facts([...items,...Object.entries(extraFields).map(([k,l])=>[l,f[k]])])+
    (docs.length?table(['Documento','Número','Fecha'],docs.map(d=>[e(d.type),e(d.number),e(day(d.date))])):'')+
    (f.client_snapshot?'<details><summary>Ficha seleccionada al abrir el caso</summary>'+printSource(f.client_snapshot)+'</details>':'')+
    (c.origin==='rsales_formulario'?'<details><summary>Datos del comprobante original</summary>'+printSource(f.contacto || Object.fromEntries(Object.entries(f).filter(([k])=>['formulario','version_formulario','id_solicitud','numero_contacto','vendedor','cliente','contacto','telefono','ciudad','correo','direccion','fecha_origen','marcas_fuente','calidad_fuente','lineas'].includes(k))))+'</details>':'');
}
function nextButton(c,work) {
  if(!work)return '';
  const labels=role==='calidad'?{enviado:'Ir al acuse de recibo',analisis:'Ir al análisis técnico',informacion:'Consultar solicitud',parcial:'Completar informe técnico',respondido:'Revisar informe'}:
    {borrador:'Revisar y enviar',informacion:'Responder a Calidad',respondido:'Gestionar respuesta',cerrado:'Reabrir expediente'};
  return '<button class="primary no-print" type="button" data-command="case-work">'+e(labels[c.status]||'Registrar actuación')+'</button>';
}
function showCase(c) {
  caseData=c;setCrumb('RC-'+c.id);const lines=c.lines||[], events=c.events||[];
  const reportHTML=reportList(c),work=workForms(c);
  const covered=c.quality_progress?.answered ?? lines.filter(l=>l.report_cycle===c.cycle).length;
  const sections={'case-summary':'Resumen','case-products':'Productos y evidencias','case-reports':'Calidad','case-solution':'Solución y cumplimiento','case-timeline':'Historial de actuaciones'};
  if(!lines.length) delete sections['case-solution'];
  const returnTo=metricsReturn();
  const backLink=returnTo ? '<a class="btn" data-return-metrics href="'+e(returnTo)+'">Volver al seguimiento</a>' : '<a class="btn" href="#'+listRoute+'">Volver '+(listRoute==='historial'?'al historial':'a la bandeja')+'</a>';
  const readerNav='<nav class="case-nav no-print" aria-label="Secciones del expediente">'+Object.entries(sections).map(([id,title])=>'<button type="button" data-section="'+id+'">'+e(title)+'</button>').join('')+'</nav>';
  const latestRequest=[...events].reverse().find(ev=>ev.action==='request_info');
  const technicalState=c.label==='Cliente atendido · Calidad pendiente' ? '<span class="badge">'+e(c.status==='parcial'?'Respuesta técnica parcial':statuses[c.status])+'</span>' : '';
  const lineCells=lines.map(l=>['<strong>'+e(lineLabel(l))+'</strong><span class="meta">'+e(l.presentation||'Presentación por verificar')+'</span>',e(l.reason || c.description),e(l.lot||'Pendiente'),e(l.quantity)+' '+e(l.unit),e(l.report_cycle===c.cycle?l.confirmed:'Pendiente')]);
  const originalPDF=events.find(ev=>ev.action==='import')?.payload.file_id;
  const balances=lines.map(l=>[e(lineLabel(l)),e(l.quantity)+' '+e(l.unit),e(l.replacement),e(l.rejected),e(l.credit),e(l.other),e(l.pending)]);
  const movements=lines.map(l=>[e(lineLabel(l)),e(l.unit),e(l.dispatched),e(l.received),e(Math.max(0,l.dispatched-l.received)),e(Math.max(0,l.replacement-l.dispatched)),e(l.credit_done),e(l.other_done)]);
  const links=c.order_links||[];
  const flow=events.slice().reverse().map(ev=>'<details class="entry"><summary>'+e(actionNames[ev.action]||ev.action)+' · '+e(stamp(ev.created_at))+'</summary>'+facts([['Responsable',actorName[ev.actor]||ev.actor],['Ciclo',ev.cycle],['Versión del expediente',ev.version]])+printSource(ev.payload)+'</details>').join('');
  content.innerHTML=head('RC-'+c.id,c.client||'Cliente por completar',nextButton(c,work)+'<button type="button" data-command="print-case">Imprimir expediente</button>'+backLink)+
    readerNav+panel('Resumen','<span class="badge attention">'+e(c.label||statuses[c.status])+'</span> '+technicalState+facts([['Estado técnico',c.technical_status||statuses[c.status]],['Gestión comercial',c.commercial_status],['Próxima acción',c.next_action],['Canal',channelName[c.channel]],['Origen',origins[c.origin]],['Recepción',c.date],['Siguiente responsable',actorName[c.next_actor]||c.next_actor],['Enviado a Calidad',c.submitted_at?stamp(c.submitted_at):'Pendiente'],['Última actuación',stamp(c.updated_at)]])+'<p class="notes">'+e(c.description)+'</p><details class="inline-space"><summary>Cliente, documentos y datos de apertura</summary>'+labelSnapshot(c)+'</details>','case-summary')+
    (c.status==='informacion'&&latestRequest?panel('Solicitud de Calidad','<p class="notes">'+e(latestRequest.payload.note)+'</p>'+facts([['Referencia',latestRequest.payload.line_id],['Fecha',stamp(latestRequest.created_at)]])):'')+
    panel('Lo que reportó el cliente',(lines.length?table(['Referencia','Motivo','Lote','Cantidad reclamada','Daño confirmado'],lineCells):'<p class="paper-note">Sin líneas de producto.</p>')+
      lines.map(l=>'<details class="entry"><summary>Detalle de la línea '+l.id+'</summary>'+printSource(l.fields)+(originalPDF?contactPhotoGallery('/api/cases/'+c.id+'/contacto/'+originalPDF+'/fotos',l.fields.fotos || l.fields.photos || []):'')+'</details>').join('')+
      (links.length?'<h3 class="inline-space">Pedidos vinculados</h3>'+table(['Referencia','Pedido','Cantidad asignada','Verificación'],links.map(l=>[e(lineLabel(lines.find(x=>x.id===l.line_id)||{id:l.line_id})),e(l.number||l.order_id),e(l.quantity),'Referencia del pedido por verificar'])):''),'case-products')+
    panel('Evidencias y soportes',fileList(c))+
    panel('Respuesta de Calidad',(lines.length?'<p class="paper-note">'+covered+' de '+lines.length+' referencias con respuesta técnica en el ciclo '+e(c.cycle)+'.</p>':'')+reportHTML,'case-reports')+creditSummary(c)+
    (lines.length?panel('Solución comercial',table(['Referencia','Reclamadas','A reponer','Rechazadas','Crédito','Otra solución','Por decidir'],balances),'case-solution')+
      panel('Cumplimiento de la solución',table(['Referencia','Medida','Despachadas','Recibidas','Por confirmar recepción','Por despachar','Crédito comprobado','Otra solución cumplida'],movements)):'')+
    (work?'<section class="panel no-print" id="case-work" tabindex="-1"><h2>Actuaciones disponibles</h2>'+work+'</section>':'')+panel('Historial de actuaciones',flow || '<p class="paper-note">Sin actuaciones registradas.</p>','case-timeline');
}
async function runWrite(task) {
  if(busy) return;
  busy=true;
  const active=$$('button:not(:disabled)',content);
  active.forEach(el=>el.disabled=true);
  message('Guardando…');
  content.inert=true;
  try { await task(); }
  catch(err) {
    const state=$('#edit-state');
    if(state) state.textContent='No se confirmó el guardado. Los datos siguen en el formulario para corregir o reintentar.';
    message(err.message,true);
  }
  finally {content.inert=false;busy=false;active.forEach(el=>el.disabled=false);}
}
async function sendAction(data) {
  const path='/api/cases/'+caseData.id+'/actions';
  const body={...data,version:caseData.version};
  const result=await api(path,{method:'POST',body,key:tokenFor(path+JSON.stringify(body))});
  pending=null;dirty=false;showCase(result);message((data.action==='submit'?'Enviado a Calidad':'Actuación guardada')+' · RC-'+result.id+' · '+stamp(result.updated_at));
}
function buildAction(form,btn) {
  const type=form.dataset.action;
  if(type==='credit_apply') return creditPayload(form);
  const data={...formValues(form),action:type};
  for(const k of ['line_id','file_id','report_id','order_id']) {
    if(!data[k]) delete data[k];
    else data[k]=Number(data[k]);
  }
  if(type==='report'){
    data.action=btn?.value || 'report_draft';
    data.findings=$$('[data-finding]',form).filter(el=>$('[name=cover]',el).checked).map(el=>({
      line_id:Number(el.dataset.finding),finding:valueIn(el,'finding'),cause:valueIn(el,'cause'),evidence_reviewed:valueIn(el,'evidence_reviewed'),attributed_stage:valueIn(el,'attributed_stage'),result:valueIn(el,'result'),confirmed:Number(valueIn(el,'confirmed') || 0)
    }));
    for(const k of ['finding','cause','result','confirmed','cover','evidence_reviewed','attributed_stage']) delete data[k];
  }
  return data;
}
function changedSelection(el) {
  if(!el.matches('[name=line_id]') || el.form?.dataset.action!=='decision') return;
  const line=caseData.lines.find(l=>String(l.id)===el.value);
  if(!line)return;
  for(const name of ['replacement','rejected','other','credit']) $('[name="'+name+'"]',el.form).value=line[name];
}
content.addEventListener('submit',evt=>{
  const form=evt.target;
  const intent=evt.submitter?.value || 'save';
  evt.preventDefault();
  if(form.id==='filters') {changeFilters(formValues(form));return;}
  runWrite(async()=>{
    if(form.id==='editor'){
      const path='/api/cases'+(editBase.id?'/'+editBase.id:'');
      const body=editPayload(form);
      if(form.elements.editor_file?.files.length) throw new Error('Adjunta el archivo seleccionado antes de continuar.');
      if(intent==='submit') {
        if($('#review-preview').hidden) throw new Error('Revisa el expediente antes de enviarlo.');
        body.submit=true;
      }
      const result=await api(path,{method:editBase.id?'PUT':'POST',body,key:tokenFor(path+JSON.stringify(body))});
      dirty=false;
      pending=null;
      if(intent==='stay'){
        route='editar/'+result.id;
        history.replaceState(null,'','#'+route);
        showEdit(result);
        $('#block-products').scrollIntoView({block:'start'});
      } else {
        route='caso/'+result.id;
        history.replaceState(null,'','#'+route);
        showCase(result);
        resetViewport();
      }
      markNavigation();
      const sent=intent==='submit' && result.submitted_at;
      message((sent?'Enviado a Calidad':'Borrador guardado')+' · RC-'+result.id+' · '+stamp(sent?result.submitted_at:result.updated_at));
    } else if(form.id==='pdf-preview'){
      pdfToken='';
      const result=await api('/api/contacto/preview',{method:'POST',body:new FormData(form)});
      renderPreview(result);message('Vista previa disponible. Revisa el comprobante antes de guardarlo.');
    } else if(form.id==='upload') {
      const file=form.elements.file.files[0];if(!file) throw new Error('Selecciona el archivo.');
      const payload=new FormData(form);payload.set('version',caseData.version);
      setEvidenceTarget(payload,form.elements.target.value);
      const path='/api/cases/'+caseData.id+'/files';
      const key=tokenFor([path,caseData.version,form.elements.target.value,file.name,file.size,file.lastModified].join('/'));
      const result=await api(path,{method:'POST',body:payload,key});
      dirty=false;pending=null;showCase(result);message('Archivo guardado en el expediente.');
    } else if(form.dataset.action) await sendAction(buildAction(form,evt.submitter));
  });
});
const lookupTimers=new WeakMap();
content.addEventListener('input',evt=>{
  const el=evt.target, box=el.closest('[data-lookup]');
  if(box){
    clearTimeout(lookupTimers.get(box));lookupTimers.set(box,setTimeout(()=>runLookup(box),230));
  } else if(el.closest('#filters')) {
    clearTimeout(filterTimer);filterTimer=setTimeout(()=>{const form=$('#filters');if(form)changeFilters(formValues(form));},220);
  } else changedSelection(el);
});
content.addEventListener('change',evt=>{
  const el=evt.target;
  if(el.type==='file'&&el.form?.id==='pdf-preview'){ pdfToken='';$('#pdf-data').replaceChildren();message(); }
  if(el.type==='file'&&el.form?.id==='upload') dirty=true;
});
content.addEventListener('click',evt=>{
  const returning=evt.target.closest('[data-return-metrics]');
  if(returning && window.navigation) {
    const entries=navigation.entries(), i=navigation.currentEntry?.index;
    if(i>0 && entries[i-1]?.url===returning.href){evt.preventDefault();history.back();return;}
  }
  const btn=evt.target.closest('button');
  if(!btn || btn.disabled)return;
  const cmd=btn.dataset.command;
  if('section' in btn.dataset){
    const section=document.getElementById(btn.dataset.section);
    section?.focus({preventScroll:true});section?.scrollIntoView({block:'start'});return;
  }
  if('pick' in btn.dataset) {pick(btn);return;}
  if('status' in btn.dataset) {changeFilters({status:btn.dataset.status});return;}
  if('page' in btn.dataset) {changeFilters({page:Math.max(1,Number(filters.page)+Number(btn.dataset.page))});return;}
  if('actionButton' in btn.dataset) {runWrite(()=>sendAction({action:btn.dataset.actionButton}));return;}
  if(cmd==='reload'){navigate();return;}
  if(cmd==='review-case'){reviewEditor();return;}
  if(cmd==='editor-upload'){runWrite(uploadEditorEvidence);return;}
  if(cmd==='add-doc') {$('#documents').insertAdjacentHTML('beforeend',docRow());setDirty();}
  if(cmd==='add-line') {$('#lines').insertAdjacentHTML('beforeend',lineCard());if(typeof enhanceCatalogEditor==='function')enhanceCatalogEditor();setDirty();}
  if(cmd==='remove-doc'){btn.closest('[data-document]').remove();setDirty();}
  if(cmd==='remove-line'){
    const row=btn.closest('[data-line]');if(!valueIn(row,'description') || confirm('¿Quitar esta referencia del borrador?')) {editLines.delete(row.dataset.line);row.remove();setDirty();}
  }
  if(cmd==='case-work'){
    const target=$('#case-work');
    if(target) {
      target.scrollIntoView({block:'start'});target.focus({preventScroll:true});
      const next=role==='calidad' && caseData.status!=='enviado'?target.querySelector('form[data-action="report"]') : null;
      if(next) next.closest('details').open=true;
    }
  }
  if(cmd==='print-case'){
    const opened=$$('details:not([open]):not(.action-box)',content);
    opened.forEach(el=>el.open=true);
    window.addEventListener('afterprint',()=>opened.forEach(el=>el.open=false),{once:true});
    window.print();
  }
  if(cmd==='save-client')runWrite(()=>saveContact());
  if(cmd==='import-pdf')runWrite(async()=>{
    if(!pdfToken)throw new Error('Genera la vista previa del comprobante.');
    const result=await api('/api/contacto/import',{method:'POST',body:{token:pdfToken}});
    dirty=false;go('editar/'+result.id);
  });
});
content.addEventListener('toggle',async evt=>{
  const el=evt.target;
  if(!el.open || !el.matches?.('details[data-report-id]') || !intake || caseData?.channel!==role)return;
  const id=Number(el.dataset.reportId);
  if(caseData.events.some(x=>x.action==='report_read'&&x.payload.report_id===id&&x.actor===role))return;
  const path='/api/cases/'+caseData.id+'/actions';
  try {
    const updated=await api(path,{method:'POST',body:{version:caseData.version,action:'report_read',report_id:id},key:freshKey()});
    if(caseData?.id===updated.id)caseData.events=updated.events;
  } catch(err){message(err.message,true);}
},true);
addEventListener('hashchange',navigate);
let polling=false;
async function refreshVisible() {
  if(document.hidden || busy || dirty || polling) return;
  polling=true;
  const start=revision;
  try{
    if(isList(route)) await showList(true);
    else if(route.startsWith('caso/') && caseData){
      const current=await api('/api/cases/'+caseData.id);
      if(start!==revision || dirty || busy || caseData?.id!==current.id) return;
      if(current.version!==caseData.version || current.events.length!==caseData.events.length){
        const open=$$('details[open]',content).map(el=>el.querySelector('summary')?.textContent);
        const focused=document.activeElement;
        if(focused && ['INPUT','TEXTAREA','SELECT'].includes(focused.tagName))return;
        showCase(current);
        $$('details',content).forEach(el=>{if(open.includes(el.querySelector('summary')?.textContent))el.open=true;});
      }
    }
  }catch(err){if(start===revision && !busy && !dirty)message(err.message,true);}
  finally{polling=false;}
}
setInterval(refreshVisible,8500);
addEventListener('focus',refreshVisible);
if(document.readyState==='complete') navigate();
else document.addEventListener('DOMContentLoaded',navigate,{once:true});
