// Локальные зависимости; HTML модели очищается до вставки в страницу.
import {marked} from '../vendor/marked-18.1.3/marked.esm.js';
import DOMPurify from '../vendor/dompurify-3.4.17/purify.es.mjs';

export function markdown(text){
  const node=document.createElement('div');node.className='workshop-report';
  node.innerHTML=DOMPurify.sanitize(marked.parse(String(text||''),{gfm:true,breaks:true}),{
    ALLOWED_TAGS:['h1','h2','h3','h4','h5','h6','p','br','strong','em','ul','ol','li','table','thead','tbody','tr','th','td','pre','code','blockquote','a','hr','del'],
    ALLOWED_ATTR:['href','title','start'],ALLOW_DATA_ATTR:false,
  });
  for(const link of node.querySelectorAll('a')){
    try{if(!['http:','https:'].includes(new URL(link.getAttribute('href'),location.href).protocol))link.removeAttribute('href');}
    catch{link.removeAttribute('href');}
    link.target='_blank';link.rel='noopener noreferrer';
  }
  for(const table of node.querySelectorAll('table')){
    const scroll=document.createElement('div');scroll.className='workshop-table-scroll';table.replaceWith(scroll);scroll.append(table);
  }
  return node;
}
