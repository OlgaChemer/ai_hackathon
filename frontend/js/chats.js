// Список диалогов, как в чат-ботах: новые сверху, любой можно открыть и продолжить.
// Диалоги этого браузера, а после входа — и с других устройств пользователя.
import { api } from "./api.js";
import { html, plural } from "./format.js";
import { setChatExpanded } from "./shell.js";

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
let rows = [];
let requestVersion = 0;
let historyState = 'idle';

export function initChats(callbacks) {
  ctx = callbacks;
  const btn = document.getElementById("chatsBtn");
  panel = document.getElementById("chatsPanel");
  btn.addEventListener("click", () => (panel.hidden ? show() : hide()));
  panel.addEventListener("click", (e) => {
    const item = e.target.closest("[data-chat]");
    if (item) { hide(); openSaved(item.dataset.chat); return; }
    if (e.target.closest("[data-new-chat]")) { hide(); ctx.create(); }
  });
  const history = document.getElementById('historyPage');
  history.addEventListener('click', (event) => {
    const item = event.target.closest('[data-history-chat]');
    if (item) {
      // Перейти в тот же диалог; просмотр истории не создаёт нового подбора.
      openSaved(item.dataset.historyChat);
    }
    if (event.target.closest('[data-history-retry]')) refreshChats();
    if (event.target.closest('[data-history-reset]')) {
      document.getElementById('historyQuery').value = '';
      document.getElementById('historyStatus').value = 'all';
      renderHistory();
    }
  });
  document.getElementById('historyQuery').addEventListener('input', renderHistory);
  document.getElementById('historyStatus').addEventListener('change', renderHistory);
  document.addEventListener('pagechange', (event) => {
    if (event.detail.page === 'history') { hide(); refreshChats(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) hide(); });
  document.addEventListener("click", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) hide();
  });
}

function openSaved(id) {
  if (location.hash !== `#chat-${id}`) window.history.pushState(null, '', `#chat-${id}`);
  ctx.open(id);
}

function show() {
  setChatExpanded(true);
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
  const historyPage = document.getElementById('historyPage');
  if (!panel || (panel.hidden && historyPage.hidden)) return;
  const version = ++requestVersion;
  rows = [];
  historyState = 'loading';
  panel.querySelector('[data-list]').innerHTML = html`<p class="chats-empty">Загружаю диалоги…</p>`;
  if (!historyPage.hidden) {
    document.getElementById('historyCount').textContent = '';
    document.getElementById('historyList').innerHTML = html`<div class="history-empty"><span class="eyebrow">История</span><h2>Загружаю ваши подборы…</h2></div>`;
  }
  let chats = [];
  try {
    chats = (await api.chats()).chats;
  } catch {
    if (version !== requestVersion) return;
    historyState = 'error';
    panel.querySelector("[data-list]").innerHTML = html`<p class="chats-empty">Не удалось загрузить диалоги.</p>`;
    document.getElementById('historyCount').textContent = '';
    document.getElementById('historyList').innerHTML = html`<div class="history-empty"><h2>Не удалось загрузить историю</h2><p>Проверьте соединение и попробуйте ещё раз. Сохранённые подборы останутся на месте.</p><button class="btn btn-ghost" data-history-retry>Повторить</button></div>`;
    return;
  }
  if (version !== requestVersion) return;
  rows = chats;
  historyState = 'ready';
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
  renderHistory();
}

function renderHistory() {
  if (historyState !== 'ready') return;
  const query = document.getElementById('historyQuery').value.trim().toLocaleLowerCase('ru');
  const status = document.getElementById('historyStatus').value;
  const matches = rows.filter(chat => chat.title.toLocaleLowerCase('ru').includes(query)
    && (status === 'all' || (status === 'active' ? chat.live && chat.status !== 'completed' : chat.status === 'completed')));
  document.getElementById('historyCount').textContent = `${matches.length} ${plural(matches.length, 'подбор', 'подбора', 'подборов')}`;
  document.getElementById('historyList').innerHTML = matches.length
    ? matches.map(chat => html`<article class="history-card ${chat.id === ctx.current() ? 'is-current' : ''}">
        <div class="history-card-meta"><span class="${chat.live && chat.status !== 'completed' ? 'chat-live' : 'history-status'}">${chat.status === 'completed' ? 'Сделка завершена' : chat.live ? 'В работе' : STATUS[chat.status] || chat.status}</span><time datetime="${chat.updated_at}">${when(chat.updated_at)}</time></div>
        <h2>${chat.title}</h2><p>${chat.id === ctx.current() ? 'Ваш текущий подбор. Все сообщения и решения сохранены в диалоге.' : 'Запрос, варианты квартир и история решений в одном месте.'}</p>
        <button class="btn btn-ghost" data-history-chat="${chat.id}">${chat.id === ctx.current() ? 'Вернуться к подбору' : 'Открыть подбор'} <span aria-hidden="true">↗</span></button>
      </article>`).join('')
    : rows.length ? html`<div class="history-empty"><h2>По этим условиям ничего не найдено</h2><p>Попробуйте другие слова или сбросьте фильтры.</p><button class="btn btn-ghost" data-history-reset>Сбросить фильтры</button></div>`
    : html`<div class="history-empty"><span class="history-symbol" aria-hidden="true">⌂</span><h2>Здесь будет история вашего поиска</h2><p>Начните с города и бюджета или расскажите агенту о квартире своими словами.</p><div class="actions"><button class="btn" data-action="apartments">Найти квартиру</button><button class="btn btn-ghost" data-action="smart-search">✦ Умный поиск</button></div></div>`;
}
