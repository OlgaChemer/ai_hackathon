// Личная мастерская. Все результаты вставляются как текст, файлы скачиваются с проверкой владельца.
import {state} from './state.js';
import {el,html} from './format.js';

const LABELS={repair:'Ремонт понравившихся',compare:'Сравнение квартир',market:'Исследование рынка'};
const TERMINAL=new Set(['completed','failed','refused','needs_input','interrupted','cancelled']);
let dialog=null,context=null,kind='repair',timer=null,current=null;

async function request(path,options={}) {
  const response=await fetch('/api/workshop'+path,{headers:{'Content-Type':'application/json'},...options});
  if(!response.ok){let message='Мастерская недоступна.';try{const r=await response.json();message=typeof r.detail==='string'?r.detail:'Проверьте параметры задания.';}catch{}throw new Error(message);}
  return response.json();
}

export function initWorkshop(){
  const button=document.getElementById('workshopButton');
  document.addEventListener('accountchange',event=>{
    button.hidden=!event.detail.user?.premium;
    if(button.hidden&&dialog)dialog.close();
  });
  button.addEventListener('click',open);
}

async function open(){
  dialog?.close();
  dialog=el(html`<dialog class="workshop" aria-labelledby="workshopTitle">
    <div class="workshop-header"><div><p class="eyebrow">Личная аналитика · Премиум</p><h2 id="workshopTitle">ИИ-мастерская</h2></div><button class="btn btn-ghost" data-close aria-label="Закрыть мастерскую">Закрыть</button></div>
    <p>Работаем с вашим подбором и реальными объявлениями. Загружать файлы не нужно.</p>
    <label>Подбор<select data-chat aria-label="Выбранный подбор"></select></label>
    <p data-context class="muted" role="status">Получаю ваши подборы…</p>
    <div class="workshop-kinds" role="group" aria-label="Задача"><button class="btn" data-kind="repair">Оценить ремонт</button><button class="btn btn-ghost" data-kind="compare">Сравнить квартиры</button><button class="btn btn-ghost" data-kind="market">Исследовать рынок</button></div>
    <form data-form><div class="workshop-assumptions">
      <label>Ремонт, ₽/м²<input data-rate type="number" min="0" max="300000" step="1000" value="30000" required></label>
      <label>Запас на ремонт, %<input data-reserve type="number" min="0" max="100" step="1" value="15" required></label></div>
      <p class="small muted" data-assumption-note>Сценарные ставки для сравнения, а не рыночная смета. Их можно изменить.</p>
      <label data-wide-wrap hidden><input data-wide type="checkbox"> Исследовать весь город без фильтров подбора; условия покупки сохранятся</label>
      <label>Что ещё учесть?<textarea data-prompt maxlength="1000" rows="3" placeholder="Например: сравни затраты и цену метра, объясни компромиссы. Можно оставить пустым."></textarea></label>
      <div class="workshop-actions"><button class="btn btn-primary" data-start type="submit">Оценить ремонт</button><button class="btn btn-ghost" data-cancel type="button" hidden>Остановить</button></div>
    </form><p data-error class="auth-error" role="alert" hidden></p>
    <section data-progress role="status" aria-live="polite"></section>
    <section data-result></section><section><h3>Мои исследования</h3><div data-history></div></section>
  </dialog>`);
  document.body.append(dialog);dialog.showModal();
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{clearTimeout(timer);dialog.remove();dialog=null;});
  dialog.querySelectorAll('[data-kind]').forEach(button=>button.onclick=()=>setKind(button.dataset.kind));
  dialog.querySelector('[data-chat]').onchange=()=>loadContext(dialog.querySelector('[data-chat]').value);
  dialog.querySelector('[data-form]').onsubmit=start;
  dialog.querySelector('[data-cancel]').onclick=async()=>{try{renderJob(await request('/jobs/'+current.id+'/cancel',{method:'POST'}));}catch(e){error(e.message);}};
  setKind('repair');
  try {await loadContext(state.sid);await history();}catch(e){error(e.message);}
}

function error(message){if(!dialog)return;const node=dialog.querySelector('[data-error]');node.textContent=message;node.hidden=!message;}

function setKind(next){
  kind=next;
  dialog.querySelectorAll('[data-kind]').forEach(button=>{const active=button.dataset.kind===kind;button.classList.toggle('btn-ghost',!active);button.setAttribute('aria-pressed',String(active));});
  dialog.querySelector('.workshop-assumptions').hidden=kind==='market';
  dialog.querySelector('[data-assumption-note]').hidden=kind==='market';
  dialog.querySelector('[data-wide-wrap]').hidden=kind!=='market';
  dialog.querySelector('[data-start]').textContent=LABELS[kind];
}

async function loadContext(id){
  context=await request('/context'+(id?'?chat_id='+encodeURIComponent(id):''));if(!dialog)return;
  const select=dialog.querySelector('[data-chat]');select.replaceChildren();
  for(const chat of context.chats){const option=document.createElement('option');option.value=chat.id;option.textContent=chat.title;select.append(option);}
  select.value=context.chat_id||'';
  const count=context.chat_id===state.sid?state.marks.liked.size:context.liked.length;
  dialog.querySelector('[data-context]').textContent=context.chat_id
    ? `${context.profile.city||'Город пока не указан'} · понравившихся квартир: ${count}. Для ремонта и сравнения отметьте «Нравится» в подборе.`
    : 'Сначала начните подбор квартир с агентом.';
  dialog.querySelector('[data-start]').disabled=!context.chat_id;
}

async function start(event){
  event.preventDefault();error('');if(!context?.chat_id)return;
  const button=dialog.querySelector('[data-start]');button.disabled=true;
  const payload={command_id:crypto.randomUUID(),chat_id:context.chat_id,kind,
    prompt:dialog.querySelector('[data-prompt]').value.trim(),rate:Number(dialog.querySelector('[data-rate]').value),
    reserve:Number(dialog.querySelector('[data-reserve]').value),whole_city:kind==='market'&&dialog.querySelector('[data-wide]').checked,
    liked:context.chat_id===state.sid?[...state.marks.liked]:null};
  try{let job;try{job=await request('/jobs',{method:'POST',body:JSON.stringify(payload)});}catch(e){
      // Сетевой повтор имеет тот же ID; сервер не запустит работу дважды.
      if(e instanceof TypeError)job=await request('/jobs',{method:'POST',body:JSON.stringify(payload)});else throw e;
    }watch(job);
  }catch(e){error(e.message);button.disabled=false;}
}

function renderJob(job){
  if(!dialog)return;current=job;
  const active=!TERMINAL.has(job.status);
  dialog.querySelector('[data-start]').disabled=active||!context?.chat_id;
  dialog.querySelector('[data-cancel]').hidden=!active;
  const progress=dialog.querySelector('[data-progress]');progress.replaceChildren();
  for(const event of job.events.slice(-4)){const p=document.createElement('p');p.textContent=event.text;progress.append(p);}
  progress.setAttribute('aria-busy',String(active));
  const output=dialog.querySelector('[data-result]');output.replaceChildren();
  if(job.error)error(job.error);
  if(job.result){
    const title=document.createElement('h3');title.textContent=LABELS[job.kind];output.append(title);
    const note=document.createElement('p');note.className='small muted';note.textContent='Данные на '+new Date(job.result.source_time).toLocaleString('ru-RU')+'. Исследование не изменяет условия покупки.';output.append(note);
    const report=document.createElement('div');report.className='workshop-report';report.textContent=job.result.text;output.append(report);
    output.append(factTable(job.result.facts));
    const files=document.createElement('div');files.className='workshop-actions';
    for(const name of job.result.artifacts){const link=document.createElement('a');link.className='btn btn-ghost';link.textContent=name==='analysis.xlsx'?'Скачать Excel':name==='analysis.csv'?'Скачать CSV':name;link.href='/api/workshop/jobs/'+encodeURIComponent(job.id)+'/files/'+encodeURIComponent(name);files.append(link);}output.append(files);
  }
}

function factTable(facts){
  const block=document.createElement('section');block.className='workshop-facts';
  const money=value=>typeof value==='number'&&Number.isFinite(value)?Math.round(value).toLocaleString('ru-RU')+' ₽':'Нет данных';
  const note=document.createElement('p');
  let headers,rows;
  if(facts.kind==='market'){
    note.textContent=`Проверенная выборка: ${facts.count} объявлений. Медианная цена ${money(facts.median_price)}, медианная цена метра ${money(facts.median_price_per_m2)}. Это полученная выборка, а не весь рынок.`;
    headers=['Комнаты','Объявлений','Медианная цена'];rows=facts.groups.map(g=>[g.rooms===null?'Не указаны':g.rooms===0?'Студии':g.rooms,g.count,money(g.median_price)]);
  }else{
    note.textContent=`Сценарий ремонта: ${money(facts.assumptions.repair_per_m2)}/м², запас ${facts.assumptions.reserve_pct}%. Это допущение для сравнения, а не смета подрядчика. Мебель, кредитные и юридические расходы отдельно не оценивались.`;
    headers=['Квартира','Площадь','Цена','Ремонт с запасом','Покупка + ремонт'];
    rows=facts.rows.map(r=>[r.address,r.area===null?'Нет данных':r.area+' м²',money(r.price),money(r.repair_with_reserve),money(r.total)]);
  }
  block.append(note);const scroll=document.createElement('div');scroll.style.overflowX='auto';
  const table=document.createElement('table');const head=document.createElement('tr');
  for(const title of headers){const cell=document.createElement('th');cell.textContent=title;head.append(cell);}table.append(head);
  for(const row of rows){const tr=document.createElement('tr');for(const value of row){const cell=document.createElement('td');cell.textContent=String(value);tr.append(cell);}table.append(tr);}
  scroll.append(table);block.append(scroll);return block;
}

function watch(job){
  clearTimeout(timer);renderJob(job);if(!dialog)return;
  if(TERMINAL.has(job.status)){history().catch(e=>error(e.message));return;}
  timer=setTimeout(async()=>{try{watch(await request('/jobs/'+job.id));}catch(e){error(e.message);if(dialog)timer=setTimeout(()=>watch(job),4000);}},1500);
}

async function history(){
  const response=await request('/jobs');if(!dialog)return;
  const target=dialog.querySelector('[data-history]');target.replaceChildren();
  if(!response.jobs.length){target.textContent='Здесь сохранятся ваши отчёты.';return;}
  for(const job of response.jobs){const button=document.createElement('button');button.className='btn btn-ghost';button.textContent=(LABELS[job.kind]||'Исследование')+' · '+({completed:'готово',running:'в работе',queued:'в очереди',failed:'ошибка',cancelled:'отменено',refused:'вне тематики',needs_input:'нужно уточнение',interrupted:'прервано'}[job.status]||job.status);button.onclick=()=>watch(job);target.append(button);}
}
