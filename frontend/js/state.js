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

// Начало текущего ответа агента. Экран встаёт на него, а не на конец длинного ответа (расчёт ипотеки и т. п.).
let turnAnchor = null;

/** Пользователь написал или принял решение — следующий элемент агента станет началом нового ответа. */
export function startTurn() { turnAnchor = null; }

/** Показать новый элемент ответа: начало ответа — у верхнего края, короткий ответ — целиком внизу, как раньше. */
export function revealInChat(node) {
  if (!node?.isConnected) return;
  if (!turnAnchor?.isConnected) turnAnchor = node;
  const top = chatLog.scrollTop + turnAnchor.getBoundingClientRect().top - chatLog.getBoundingClientRect().top - 12;
  chatLog.scrollTop = Math.min(chatLog.scrollHeight - chatLog.clientHeight, Math.max(0, top));
}

/** История показана целиком — открываем её внизу, новый ответ начнётся с чистого якоря. */
export function scrollChatToEnd() { startTurn(); chatLog.scrollTop = chatLog.scrollHeight; }

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
  if (role === "user") scrollChatToEnd();
  else revealInChat(node);
  return node;
}
