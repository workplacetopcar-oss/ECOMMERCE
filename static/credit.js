'use strict';

function creditDocumentRow(doc = {}) {
  return '<fieldset data-credit-row><legend>Aplicación por documento</legend><div class="grid">'+
    select('document_type','Tipo de documento',{factura:'Factura',oc:'Orden de compra'},doc.type || 'factura')+
    field('document_number','Número del documento',doc.number || '', 'text','list="credit-documents" required maxlength="150"')+
    field('document_amount','Importe aplicado','','number','min="0.001" step="0.001" required')+
    '</div><button type="button" class="text-button" data-credit-remove>Quitar documento</button></fieldset>';
}

function creditForm(c) {
  const notes = c.credit_notes || [];
  const docs = Array.isArray(c.fields.documents) ? c.fields.documents.filter(d=>d && d.number && ['factura','oc'].includes(d.type)) : [];
  const choices = Object.fromEntries(c.files.filter(f=>f.kind==='evidence').map(f=>[f.id,f.filename]));
  const lines = c.lines.filter(l=>l.credit>l.credit_done);
  const service = !c.lines.length && c.kind==='servicio';
  return '<details class="action-box no-print"><summary>Aplicar nota crédito a facturas u órdenes</summary>'+
    '<form data-action="credit_apply" data-dirty><div class="grid">'+
    field('number','Número de la nota crédito','','text','list="credit-numbers" maxlength="150" required')+
    '<datalist id="credit-numbers">'+notes.filter(n=>Number(n.remaining)>0).map(n=>'<option value="'+e(n.number)+'"></option>').join('')+'</datalist>'+
    field('total','Importe total de la nota','','number','min="0.001" step="0.001" required')+
    field('currency','Moneda','','text','placeholder="USD" pattern="[a-zA-Z]{3}" maxlength="3" required')+
    field('date','Fecha de aplicación',dateInColombia(),'date','required')+
    select('file_id','Soporte del expediente',{'':'Solo número de nota',...choices})+'</div>'+
    '<datalist id="credit-documents">'+docs.map(d=>'<option value="'+e(d.number)+'"></option>').join('')+'</datalist>'+
    '<div data-credit-rows>'+creditDocumentRow(docs.length===1 ? docs[0] : {})+'</div>'+
    '<button type="button" data-credit-add>Agregar otro documento</button>'+
    (lines.length?'<fieldset><legend>Unidades cubiertas por esta aplicación</legend>'+lines.map(l=>
      '<label class="field">'+e(lineLabel(l))+' · hasta '+e(l.credit-l.credit_done)+' '+e(l.unit)+
      '<input type="number" data-credit-line="'+e(l.id)+'" min="0" max="'+e(l.credit-l.credit_done)+'" step="0.001"></label>').join('')+'</fieldset>':'')+
    (service?'<label class="check"><input type="checkbox" name="settle_service"> Acreditar el cumplimiento del crédito autorizado para este servicio</label>':'')+
    '<div class="form-footer"><button class="primary">Registrar aplicación</button></div></form></details>';
}

function creditPayload(form) {
  return {
    action:'credit_apply',number:valueIn(form,'number'),total:valueIn(form,'total'),currency:valueIn(form,'currency'),
    date:valueIn(form,'date'),file_id:Number(valueIn(form,'file_id'))||null,
    applications:$$('[data-credit-row]',form).map(row=>({
      type:valueIn(row,'document_type'),number:valueIn(row,'document_number'),amount:valueIn(row,'document_amount')
    })),
    covered_lines:$$('[data-credit-line]',form).filter(el=>Number(el.value)>0).map(el=>({
      line_id:Number(el.dataset.creditLine),quantity:el.value
    })),
    settle_service:!!form.elements.settle_service?.checked
  };
}

function creditSummary(c) {
  if (!c.credit_notes?.length) return '';
  return panel('Notas crédito aplicadas',c.credit_notes.map(n=>
    '<article><h3>'+e(n.number)+' · '+e(n.currency)+' '+e(n.total)+'</h3><p>Saldo por aplicar · '+e(n.currency)+' '+e(n.remaining)+'</p>'+
    table(['Fecha','Documento','Importe aplicado','Soporte'],n.applications.map(a=>[
      e(day(a.date)),e((a.type==='oc'?'OC ':'Factura ')+a.number),e(n.currency+' '+a.amount),a.file_id?
      '<a href="/api/cases/'+e(c.id)+'/files/'+e(a.file_id)+'">Descargar soporte</a>':e(n.number)]))+'</article>').join(''));
}

document.addEventListener('click',event=>{
  const form=event.target.closest('form[data-action="credit_apply"]');
  if(!form) return;
  if(event.target.closest('[data-credit-add]')) {
    const rows=form.querySelector('[data-credit-rows]');
    rows.insertAdjacentHTML('beforeend',creditDocumentRow());
    rows.lastElementChild.querySelector('select').focus();
    setDirty();
  }
  if(event.target.closest('[data-credit-remove]')) {
    const rows=form.querySelectorAll('[data-credit-row]');
    if(rows.length>1) {event.target.closest('[data-credit-row]').remove(); setDirty();}
  }
});
document.addEventListener('change',event=>{
  if(!event.target.matches('form[data-action="credit_apply"] input[name="number"]')) return;
  const note=caseData?.credit_notes.find(n=>n.number.toLowerCase()===event.target.value.trim().toLowerCase());
  if (note) {
    event.target.form.elements.total.value=note.total;
    event.target.form.elements.currency.value=note.currency;
  }
});
