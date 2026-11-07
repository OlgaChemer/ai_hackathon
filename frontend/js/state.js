// Общее состояние экрана: текущий диалог, шаги, подборка, звонок. Один объект на всё приложение —
// модули меняют его поля, а новый диалог сбрасывает его на месте (resetState), а не заменяет.
import { el, html } from "./format.js";

export const $ = (id) => document.getElementById(id);
export const chatLog = $("chatLog");
export const stage = $("stage");

// настройки сервера (/api/health): звонки на телефон, города
export const config = { phoneReady: false, sellerPhoneDefault: "" };

function freshState() {
  return {
    sid: null, es: null, running: false, streamEnded: false, results: {},
    call: null, phone: null, marks: { liked: new Set(), disliked: new Set() }, opened: {}, strategy: null, awaiting: null,
    cards: {}, lastSeq: 0, retries: 0, selectionNotice: null, selectionPage: 1, // карточки решений по виду; последнее полученное событие ленты
  };
}

export const state = freshState();

export function resetState() {
  for (const key of Object.keys(state)) delete state[key];
  Object.assign(state, freshState());
}

export function addMsg(role, text) {
  const previous=chatLog.lastElementChild;
  if (role === "agent" && previous?.classList.contains("msg-agent") && previous.dataset.text === text) return previous;
  const node = el(html`<div class="msg msg-${role} ${role}">${role === "agent" || role === "user" ? html.raw(html`<span class="msg-who">${role === "agent" ? "Агент" : "Вы"}</span>`) : ""}${text}</div>`);
  node.dataset.text=text;
  chatLog.append(node);
  const peek = $("chatPeek");
  if (peek) peek.textContent = text;
  const thinking = chatLog.querySelector(".msg-thinking"); // «агент думает» — всегда последним
  if (thinking) chatLog.append(thinking);
  chatLog.scrollTop = chatLog.scrollHeight;
  return node;
}
