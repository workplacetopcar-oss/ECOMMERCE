'use strict';

function updateSourceComparison() {
  const pane = $('#client-comparison'), f = editBase?.fields;
  if (!pane || !f?.contacto || !f.client_snapshot) return;
  const src=f.contacto, client=f.client_snapshot;
  const rows=[
    ['Razón social',src.cliente,client.name],['Ciudad',src.ciudad,client.city],
    ['Contacto',src.contacto,client.contact],['Dirección',src.direccion,client.address],
    ['Correo',src.correo,client.email],['Teléfono',src.telefono,client.phone]
  ];
  pane.innerHTML='<details open><summary>Comprobante y cliente elegido</summary>'+
    table(['Campo','Comprobante del vendedor','Ficha del directorio'],rows.map(row=>row.map(cell=>e(cell || 'Sin dato'))))+'</details>';
}

function enhanceCatalogEditor() {
  const form=$('#editor');
  if(!form) return;
  if(!$('#catalog-cards',form)){
    const title=$('#lines',form);
    title.insertAdjacentHTML('beforebegin','<details id="catalog-cards"><summary>Consultar fichas y presentaciones Lubristone</summary>'+
      '<label class="field">Buscar ficha por nombre, familia o alias<input id="card-query" type="search" autocomplete="off"></label>'+
      '<div id="card-results" aria-live="polite"></div></details>');
    const selected=$('#client-picked',form);
    selected.insertAdjacentHTML('afterend','<div id="client-comparison"></div>');
    if(role==='nacional') {
      selected.insertAdjacentHTML('afterend',field('profile_source','Fuente de verificación del contacto','','text','placeholder="Soporte de los datos del directorio"')+
        '<button type="button" data-command="save-client">Guardar este contacto en el directorio</button>');
    }
    if(role==='internacional'){
      const save=$('[data-command="save-client"]',form);
      if(save) save.insertAdjacentHTML('beforebegin',field('profile_source','Fuente de verificación del contacto','','text','placeholder="Ej. correo de confirmación del distribuidor"'));
    }
    if(editBase?.fields?.contacto && !editBase.fields.client_snapshot) {
      const lookup=$('[data-lookup="clients"]',form);
      $('.search-input',lookup).value=String(editBase.fields.contacto.cliente||'').split(/\s+/).slice(0,2).join(' ');
      const include=$('input[type=checkbox]',lookup);
      if(include) include.checked=true;
      if($('.search-input',lookup).value) runLookup(lookup).catch(err=>message(err.message,true));
    }
  }
  for (const line of $$('[data-line]',form)) {
    if($('[data-equivalences]',line)) continue;
    const selector=$('.selected-source',line);
    selector.insertAdjacentHTML('afterend','<div class="line-source-note meta"></div><div class="line-equivalent meta"></div>');
    selector.insertAdjacentHTML('afterend','<details data-choice-info><summary>Datos de la selección comercial</summary><div aria-live="polite"></div></details>');
    const box=document.createElement('details');
    box.dataset.equivalences='';
    box.innerHTML='<summary>Códigos del cliente y equivalencias verificadas</summary><div class="equivalences-body"></div>';
    const remove=$('[data-command="remove-line"]',line);
    line.insertBefore(box,remove);
    showProductSources(line);
  }
  updateSourceComparison();
}

async function saveContact() {
  const form=$('#editor'), data=editPayload(form), f=data.fields;
  if(!data.client) throw new Error('Completa la razón social del cliente.');
  const source=valueIn(form,'profile_source');
  if(!source) throw new Error('Indica la fuente de verificación del contacto antes de guardarlo en el directorio.');
  const chosen=f.client_snapshot;
  const fields={};
  for(const k of ['city','region','contact','phone','email','address']) fields[k]=f[k]||'';
  let result;
  if(chosen?.id) {
    const updated=await api('/api/catalog/clients/'+encodeURIComponent(chosen.id)+'/profile',{
      method:'PUT',body:{...fields,country:f.country||'',source_note:source,profile_version:chosen.profile_version||0}});
    result={...chosen,...updated};
  } else {
    result=await api('/api/catalog/clients',{method:'POST',body:{
      ...fields,source_note:source,name:data.client,tax_id:f.tax_id||'',country:f.country||''}});
  }
  editBase.fields={...editBase.fields,client_snapshot:result};
  $('#client-picked').textContent='Contacto guardado · '+result.name;
  const code=$('[name="f_client_code"]',form);
  if(code) code.value=result.id;
  setDirty();
  message('Contacto guardado en el directorio '+(role==='nacional'?'nacional.':'de exportación.'));
}

async function fetchCards() {
  const input=$('#card-query'), results=$('#card-results');
  if(!input||!results) return;
  const q=input.value.trim();
  results.textContent='Buscando fichas…';
  const res=await api('/api/catalog/cards?'+queryOf({q,page_size:50}));
  if(!results.isConnected||input.value.trim()!==q) return;
  results.innerHTML=res.items.length?res.items.map(card=>'<details><summary>'+e(card.nombre)+'</summary>'+
    facts([['Familia',card.familia],['Alias',(card.alias||[]).join(', ')]])+
    '<ul>'+card.presentaciones_catalogo.map(t=>'<li>'+e(t)+'</li>').join('')+'</ul>'+
    (card.pendientes_y_discrepancias?.length?'<p>'+e(card.pendientes_y_discrepancias.join(' '))+'</p>':'')+'</details>').join(''):
    '<p class="meta">Sin coincidencias.</p>';
}

function productInfo(box) {
  const line=box.closest('[data-line]'), selection=editLines.get(line.dataset.line)?.fields?.product_snapshot;
  const body=$('div',box);
  if(!selection) {body.textContent='Selecciona un producto para consultar su registro de origen.';return;}
  body.innerHTML=facts([['SKU',selection.code],['Descripción de origen',selection.description],
    ['Referencia',selection.reference||'Sin dato en RSales'],['Grupo',selection.group_code],
    ['Familia',selection.family_code],['Línea',selection.line_code],['Impuesto',selection.tax],
    ['Estado',selection.state],['Presentación',selection.presentacion_etiquetada||'Por verificar']]);
}

async function loadEquivalences(box) {
  const body=$('.equivalences-body',box), client=editBase?.fields?.client_snapshot;
  if(!client){body.textContent='Elige una ficha de cliente para consultar sus códigos.';return;}
  body.textContent='Consultando referencias…';
  const q='?'+queryOf({client_id:client.id});
  const [codes,eqs]=await Promise.all([
    role==='internacional'?api('/api/catalog/codes'+q):Promise.resolve({items:[]}),
    api('/api/catalog/equivalences'+q)]);
  if(!box.isConnected||client!==editBase?.fields?.client_snapshot) return;
  box._codes=codes.items;
  box._equivalences=eqs.items;
  body.innerHTML=(codes.items.length?'<label class="field">Código documentado para '+e(client.name)+
    '<select data-document-code><option value="">Elegir código del soporte</option>'+
    codes.items.map((x,i)=>'<option value="'+i+'">'+e(x.codigo+' · '+x.descripcion+' · '+x.presentacion)+'</option>').join('')+
    '</select></label>':'')+
    (eqs.items.length?table(['Código reportado','Presentación','SKU verificado','Versión','Respaldo'],
      eqs.items.map(r=>[r.reported_code,r.presentation,r.sku,r.version,r.reason].map(e))):'<p class="meta">Sin equivalencias verificadas para este cliente.</p>')+
    field('match_reason','Respaldo de la equivalencia','','text','placeholder="Documento o confirmación revisada"')+
    '<button type="button" data-confirm-match>Confirmar equivalencia para este cliente</button>';
}

async function saveEquivalence(btn) {
  const box=btn.closest('[data-equivalences]'),line=box.closest('[data-line]');
  const client=editBase?.fields?.client_snapshot;
  const product=editLines.get(line.dataset.line)?.fields?.product_snapshot;
  if(!client||!product || product.code!==valueIn(line,'code')) throw new Error('Selecciona la ficha del cliente y un SKU del catálogo para confirmar.');
  const reported=valueIn(line,'client_product_code')||valueIn(line,'reported_code'), presentation=valueIn(line,'presentation'), reason=valueIn(line,'match_reason');
  if(!reported||!presentation||!reason) throw new Error('Completa código reportado, presentación y respaldo.');
  const previous=(box._equivalences||[]).find(r=>r.reported_code===reported && r.presentation===presentation);
  btn.disabled=true;
  try {
    const result=await api('/api/catalog/equivalences',{method:'POST',body:{client_id:client.id,reported_code:reported,
      presentation,sku:product.code,reason,version:previous?.version||0}});
    await loadEquivalences(box);
    message('Equivalencia confirmada. Versión '+result.version+'.');
  } finally {btn.disabled=false;}
}

let cardTimer;
content.addEventListener('input',ev=>{
  if(ev.target.id==='card-query'){
    clearTimeout(cardTimer);
    cardTimer=setTimeout(()=>fetchCards().catch(err=>message(err.message,true)),180);
  }
});
content.addEventListener('toggle',ev=>{
  const box=ev.target;
  if(!box.open) return;
  if(box.id==='catalog-cards') fetchCards().catch(err=>message(err.message,true));
  if(box.matches('[data-choice-info]')) productInfo(box);
  if(box.matches('[data-equivalences]')) loadEquivalences(box).catch(err=>message(err.message,true));
},true);
content.addEventListener('change',ev=>{
  if(!ev.target.matches('[data-document-code]')||ev.target.value==='') return;
  const box=ev.target.closest('[data-equivalences]'),line=box.closest('[data-line]'), choice=box._codes[Number(ev.target.value)];
  const customerCode=$('[name="client_product_code"]',line);
  if(customerCode) customerCode.value=choice.codigo;
  if(!valueIn(line,'description')) $('[name="description"]',line).value=choice.descripcion;
  if(!valueIn(line,'presentation')) $('[name="presentation"]',line).value=choice.presentacion;
  const old=editLines.get(line.dataset.line);
  old.fields={...old.fields,documented_code:choice};
  setDirty();
});
content.addEventListener('click',ev=>{
  const confirm=ev.target.closest('[data-confirm-match]');
  if(confirm) saveEquivalence(confirm).catch(err=>message(err.message,true));
  const picker=ev.target.closest('[data-pick]');
  if(!picker) return;
  const group=picker.closest('[data-lookup]');
  if(group?.dataset.lookup==='clients') {
    updateSourceComparison();
    $$('[data-equivalences][open]').forEach(box=>loadEquivalences(box).catch(err=>message(err.message,true)));
  }
  if(group?.dataset.lookup==='products') {
    showProductSources(group.closest('[data-line]'));
    const info=$('[data-choice-info]',group.closest('[data-line]'));
    if(info?.open) productInfo(info);
  }
});

// Direct entry at #nuevo can render the form before this deferred file has loaded.
if ($('#editor')) enhanceCatalogEditor();

function contactPhotoGallery(base, photos) {
  if (!Array.isArray(photos) || !photos.length) return '';
  const figures=photos.filter(photo=>Number.isInteger(photo.campo) && Number.isInteger(photo.pagina)).map(photo=>{
    const caption='Pregunta '+photo.campo+' · Pág. '+photo.pagina;
    if(photo.disponible===false) return '<span class="meta">'+e(caption)+' · Consultar el original</span>';
    const src=e(base+'/'+photo.campo);
    return '<figure style="margin:0;min-width:0"><a target="_blank" rel="noopener" href="'+src+'" aria-label="'+e('Abrir fotografía · '+caption)+'">'+
      '<img src="'+src+'" loading="lazy" alt="'+e('Evidencia del comprobante · '+caption)+'" width="120" height="112" style="object-fit:contain;background:#f6f7f8;max-width:100%">'+
      '</a><figcaption class="meta">'+e(caption)+'</figcaption></figure>';
  });
  return '<div class="contact-photos" style="display:flex;gap:8px;flex-wrap:wrap">'+figures.join('')+'</div>';
}

function applyCatalogClient(form, item) {
  const set=(key,value)=>{const el=$('[name="'+key+'"]',form);if(el)el.value=value||'';};
  set('f_region',item.region||item.raw?.region);
  set('f_rsales_code',item.rsales_code);
  let aliases=$('#client-aliases',form);
  if(!aliases) {
    aliases=document.createElement('p');
    aliases.className='meta';aliases.id='client-aliases';
    $('#client-picked')?.after(aliases);
  }
  const list=(item.aliases||item.raw?.alias||[]).filter(Boolean);
  aliases.textContent=list.length?'También registrado como '+list.join(' · '):'';
  queueMicrotask(()=>{
    updateSourceComparison();
    $$('[data-equivalences][open]',form).forEach(box=>loadEquivalences(box).catch(err=>message(err.message,true)));
  });
}

function applyCatalogProduct(line, product) {
  const original=$('[name="source_description"]',line);
  const memory=editLines.get(line.dataset.line);
  const sourceDescription=valueIn(line,'description');
  if(original && !original.value && sourceDescription && !memory.fields?.product_snapshot) {
    original.value=sourceDescription;
  }
  for(const [name,data] of [['color',product.color_etiquetado],['size',product.tamano_etiquetado]]) {
    const target=$('[name="'+name+'"]',line);
    if(target && (data||!target.value||memory.fields?.product_snapshot)) target.value=data||'';
  }
  queueMicrotask(()=>showProductSources(line));
  if(!memory.fields?.product_snapshot){
    memory.fields={...memory.fields,
      source_quantity:memory.fields?.source_quantity||valueIn(line,'quantity'),
      source_unit:memory.fields?.source_unit||valueIn(line,'unit')};
  }
}

function showProductSources(line) {
  const memory=editLines.get(line.dataset.line), snapshot=memory?.fields?.product_snapshot;
  const label=$('.line-source-note',line), equivalent=$('.line-equivalent',line);
  if(!label || !equivalent) return;
  const source=valueIn(line,'source_description');
  const lines=[];
  if(source && snapshot && source!==snapshot.description) lines.push('Descripción del soporte: '+source);
  if(snapshot) lines.push('RSales · '+snapshot.code+' · '+snapshot.description);
  label.textContent=lines.join('  /  ');
  const factor=snapshot?.factor_verificado, qty=Number(valueIn(line,'quantity')), code=valueIn(line,'code');
  const unit=valueIn(line,'unit'), presentation=valueIn(line,'presentation');
  const matching = factor && code===snapshot.code && Number.isInteger(qty) && qty>0 &&
    unit.toUpperCase()===factor.presentation && presentation.toUpperCase()===factor.presentation;
  equivalent.textContent=matching?'Equivalencia verificada: '+qty+' '+unit+' × '+factor.units+' = '+
    (qty*factor.units).toLocaleString('es-CO')+' unidades individuales. Fuente: '+factor.source+'.':
    '';
}

function evidenceGallery(c) {
  if(!c?.id) return '';
  const pictures=(c.files||[]).filter(file=>/\.(jpe?g|png|gif|webp)$/i.test(file.filename));
  if(!pictures.length) return '';
  return '<div class="contact-photos" style="display:flex;gap:12px;flex-wrap:wrap">'+pictures.map(file=>{
    const source='/api/cases/'+c.id+'/files/'+file.id+'/foto';
    return '<figure style="margin:0;max-width:150px"><a href="'+source+'?vista=ampliada" target="_blank" rel="noopener">'+
      '<img width="128" height="112" style="max-width:100%;object-fit:contain" src="'+source+'" loading="lazy" alt="'+e(file.filename)+'"></a>'+
      '<figcaption class="meta">'+e(file.filename)+'</figcaption></figure>';
  }).join('')+'</div>';
}

content.addEventListener('input',evt=>{
  const line=evt.target.closest('[data-line]');
  if(line) showProductSources(line);
});

const previewURLs=new WeakMap();
content.addEventListener('change',evt=>{
  const input=evt.target;
  if(input.type!=='file'||input.form?.id==='pdf-preview') return;
  if(previewURLs.has(input)) URL.revokeObjectURL(previewURLs.get(input));
  let target=input.parentElement.querySelector('[data-local-preview]');
  if(!target){
    target=document.createElement('span');target.dataset.localPreview='';
    target.setAttribute('aria-live','polite');
    input.after(target);
  }
  target.replaceChildren();
  const file=input.files[0];
  if(!file)return;
  const note=document.createElement('small');
  note.textContent=file.name+' · Seleccionado, pendiente de guardar';target.append(note);
  if(!['image/jpeg','image/png','image/webp','image/gif'].includes(file.type))return;
  const img=document.createElement('img'), url=URL.createObjectURL(file);
  previewURLs.set(input,url);
  img.alt='Vista previa de '+file.name;
  img.width=120;img.height=100;img.style.objectFit='contain';img.src=url;
  img.addEventListener('load',()=>URL.revokeObjectURL(url),{once:true});
  img.addEventListener('error',()=>URL.revokeObjectURL(url),{once:true});
  target.prepend(img);
});
