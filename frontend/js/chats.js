// Список диалогов, как в чат-ботах: новые сверху, любой можно открыть и продолжить.
// Диалоги этого браузера, а после входа — и с других устройств пользователя.
import { api } from "./api.js";
import { html } from "./format.js";

const STATUS = {
  completed: "сделка завершена",
  cancelled: "остановлен",
  paused: "на паузе",
  saving: "копим на взнос",
  exhausted: "варианты закончились",
  empty: "ничего не нашлось",
  error: "ошибка",
  running: "прерван",
};

let ctx = null; // { open(id), create(), current() }
let panel = null;

export function initChats(callbacks) {
  ctx = callbacks;
  const btn = document.getElementById("chatsBtn");
  panel = document.getElementById("chatsPanel");
  btn.addEventListener("click", () => (panel.hidden ? show() : hide()));
  panel.addEventListener("click", (e) => {
    const item = e.target.closest("[data-chat]");
    if (item) { hide(); ctx.open(item.dataset.chat); return; }
    if (e.target.closest("[data-new-chat]")) { hide(); ctx.create(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) hide(); });
  document.addEventListener("click", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) hide();
  });
}

function show() {
  panel.hidden = false;
  document.getElementById("chatsBtn").setAttribute("aria-expanded", "true");
  refreshChats();
}

function hide() {
  panel.hidden = true;
  document.getElementById("chatsBtn").setAttribute("aria-expanded", "false");
}

function when(iso) {
  const d = new Date(iso);
  const today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (days === 0) return `сегодня, ${time}`;
  if (days === 1) return `вчера, ${time}`;
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

/** Перерисовать список, если он открыт. */
export async function refreshChats() {
  if (!panel || panel.hidden) return;
  let chats = [];
  try {
    chats = (await api.chats()).chats;
  } catch {
    panel.querySelector("[data-list]").innerHTML = html`<p class="chats-empty">Не удалось загрузить диалоги.</p>`;
    return;
  }
  const current = ctx.current();
  panel.querySelector("[data-list]").innerHTML = chats.length
    ? chats.map((c) => html`
        <button class="chat-item ${c.id === current ? "is-current" : ""}" data-chat="${c.id}">
          <span class="chat-title">${c.title}</span>
          <span class="chat-meta">${when(c.updated_at)} · ${html.raw(c.live
            ? '<span class="chat-live">агент работает</span>'
            : html`${STATUS[c.status] || c.status}`)}</span>
        </button>`).join("")
    : html`<p class="chats-empty">Пока нет диалогов. Опишите, какую квартиру ищете, — диалог появится здесь.</p>`;
}
