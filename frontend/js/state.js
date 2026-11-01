// Общее состояние экрана: текущий диалог, шаги, подборка, звонок. Один объект на всё приложение —
// модули меняют его поля, а новый диалог сбрасывает его на месте (resetState), а не заменяет.
import { el, html } from "./format.js";

export const $ = (id) => document.getElementById(id);
export const chatLog = $("chatLog");
export const stage = $("stage");

// настройки сервера (/api/health): звонки на телефон, города
export const config = { phoneReady: false, sellerPhoneDefault: "", cities: {} };

function freshState() {
  return {
    sid: null, es: null, running: false, streamEnded: false, results: {}, status: Array(10).fill("idle"), view: 0,
    call: null, phone: null, marks: { liked: new Set(), disliked: new Set() }, opened: {}, strategy: null, awaiting: null,
    cards: {}, lastSeq: 0, retries: 0, // карточки решений по виду; последнее полученное событие ленты
  };
}

export const state = freshState();

export function resetState() {
  for (const key of Object.keys(state)) delete state[key];
  Object.assign(state, freshState());
}

export function addMsg(role, text) {
  const node = el(html`<div class="msg msg-${role}">${text}</div>`);
  chatLog.append(node);
  const thinking = chatLog.querySelector(".msg-thinking"); // «агент думает» — всегда последним
  if (thinking) chatLog.append(thinking);
  chatLog.scrollTop = chatLog.scrollHeight;
  return node;
}
