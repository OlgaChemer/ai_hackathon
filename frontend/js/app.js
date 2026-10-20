// Контроллер демо: SSE-события агента -> чат, степпер, панели этапов.
import { api } from "./api.js";
import { addGuard, addLine, createCall, renderCall, setStatus, setTyping, showSellerInput, updatePrices } from "./call.js";
import { mountEconomics } from "./economics.js";
import { el, html, rub, sleep } from "./format.js";
import * as V from "./views.js";
import { voice } from "./voice.js";

const STEPS = [
  "Запрос", "Финансы", "Поиск", "Цена", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог",
];

const $ = (id) => document.getElementById(id);
const chatLog = $("chatLog");
const stage = $("stage");
const stepper = $("stepper");

let state = freshState();
let gen = 0; // поколение сессии: события старой сессии отбрасываются
let queue = [];
let pumping = false;

function freshState() {
  return { sid: null, es: null, running: false, results: {}, status: Array(10).fill("idle"), view: 0, shortlist: [], legalById: {}, call: null };
}

// ------------------------------------------------------------------ чат

function addMsg(role, text) {
  const node = el(html`<div class="msg msg-${role}">${text}</div>`);
  chatLog.append(node);
  chatLog.scrollTop = chatLog.scrollHeight;
  return node;
}

function confirmCard(ev) {
  const card = el(html`<div class="confirm"><h4>${ev.title}</h4><p>${ev.text}</p><div data-extra></div><div class="actions"></div></div>`);
  const extra = card.querySelector("[data-extra]");
  const actions = card.querySelector(".actions");
  let collect = () => ({});

  if (ev.kind === "red_lines") {
    const p = ev.payload;
    extra.innerHTML = html`
      <dl class="redlines">
        <dt>Комнат</dt><dd>${p.rooms}</dd>
        <dt>Школа рядом</dt><dd>${p.near_school ? "да" : "не важно"}</dd>
        <dt>Агент соглашается сам</dt><dd>${p.authority_rule}</dd>
      </dl>
      <label style="margin-top:8px">Бюджет, ₽ — выше агент не пойдёт
        <input type="number" step="50000" min="1000000" value="${p.budget_max}" data-budget>
      </label>`;
    collect = () => ({ budget_max: Number(extra.querySelector("[data-budget]").value) || p.budget_max });
  }

  const buttons = {
    red_lines: [["Подтвердить рамки", true, {}], ["Изменить запрос", false, {}]],
    consent: [["Разрешить", true, {}], ["Не разрешать", false, {}]],
    call: [["Звонить — продавца играет ИИ", true, { mode: "auto" }], ["Звонить — продавец я", true, { mode: "human" }], ["Пропустить объект", false, {}]],
    price: [[`Подтвердить ${rub(ev.price)}`, true, {}], ["Отклонить, искать дальше", false, {}]],
    object: [["Выбираю эту квартиру", true, {}], ["Ищем дальше", false, {}]],
    sign: [["Подписать электронно", true, {}], ["Отложить", false, {}]],
  }[ev.kind] || [["Подтвердить", true, {}], ["Отклонить", false, {}]];

  buttons.forEach(([label, approved, data], i) => {
    const b = el(html`<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-small">${label}</button>`);
    b.addEventListener("click", async () => {
      actions.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try {
        await api.confirm(state.sid, ev.kind, approved, { ...collect(), ...data });
        card.classList.add("is-done");
        card.append(el(html`<div class="verdict">Решение: ${label}</div>`));
        if (ev.kind === "call" && approved && state.call) state.call.mode = data.mode;
      } catch (e) {
        actions.querySelectorAll("button").forEach((x) => (x.disabled = false));
        addMsg("error", `Не удалось отправить решение: ${e.message}`);
      }
    });
    actions.append(b);
  });
  chatLog.append(card);
  chatLog.scrollTop = chatLog.scrollHeight;
  actions.querySelector("button")?.focus({ preventScroll: true });
  if (matchMedia("(max-width: 860px)").matches) card.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ------------------------------------------------------------------ степпер и сцена

function renderStepper() {
  stepper.innerHTML = STEPS.map((t, i) => {
    const n = i + 1;
    const st = state.status[n];
    const cls = [st === "active" ? "is-active" : "", st === "done" ? "is-done" : "", state.view === n ? "is-viewing" : ""].join(" ");
    const dot = st === "done" ? "✓" : n;
    return html`<li class="${cls}"><button data-step="${n}" ${st === "idle" ? "disabled" : ""} aria-current="${state.view === n ? "step" : "false"}"><span class="dot">${dot}</span>${t}</button></li>`;
  }).join("");
}

function viewFor(n) {
  const r = state.results;
  switch (n) {
    case 1: return r[1] ? V.profile(r[1]) : V.waiting("Разбираю запрос", "Выделяю бюджет, район и важные для вас условия.");
    case 2: return r[2] ? V.mortgage(r[2]) : V.waiting("Финансы", "Подтвердите в чате, можно ли использовать данные Сбер ID для предодобрения.");
    case 3: return r[3] ? V.search(r[3], state.legalById) : V.waiting("Ищу объекты", "Фильтрую объявления Домклик и убираю дубли.");
    case 4: return r[4] ? V.fair(r[4], state.shortlist) : V.waiting("Считаю справедливую цену", "Сравниваю с закрытыми сделками в районе.");
    case 5: return r[5] ? V.legal(r[5], state.shortlist) : V.waiting("Проверяю", "ЕГРН, ФССП, реестр банкротств.");
    case 7: return r[7] ? V.viewing(r[7]) : V.waiting("Просмотр", "Договариваюсь о времени.");
    case 8: return r[8] ? V.deal(r[8]) : V.waiting("Оформление", "Собираю план сделки.");
    case 9: return r[9] ? V.summary(r[9]) : V.waiting("Итог", "Завершаю сделку.");
    default: return V.welcome();
  }
}

function renderStage() {
  stage.innerHTML = "";
  if (state.view === 6) {
    stage.append(state.call ? renderCall(state.call, submitSeller) : el(V.waiting("Звонок", "Готовлю звонок продавцу.")));
  } else {
    stage.innerHTML = viewFor(state.view);
  }
  renderStepper();
}

function show(n) {
  if (state.view !== n) {
    state.view = n;
    renderStage();
  } else renderStepper();
}

// ------------------------------------------------------------------ события агента

async function handle(ev, myGen) {
  const call = state.call;
  switch (ev.type) {
    case "provider": setProvider(ev); break;
    case "message": addMsg(ev.role, ev.text); await sleep(ev.role === "agent" ? 350 : 0); break;
    case "step":
      state.status[ev.step] = ev.status === "pending" ? "active" : ev.status;
      if (ev.status === "active") show(ev.step); else renderStepper();
      await sleep(200);
      break;
    case "result": {
      state.results[ev.step] = ev.data;
      if (ev.kind === "search") state.shortlist = ev.data.shortlist;
      if (ev.kind === "legal") state.legalById = Object.fromEntries(ev.data.map((c) => [c.listing_id, c]));
      state.view = ev.step;
      renderStage();
      await sleep(700);
      break;
    }
    case "confirm":
      if (ev.kind === "call") {
        state.call = createCall(ev);
        state.view = 6;
        renderStage();
      }
      confirmCard(ev);
      break;
    case "call_start":
      if (!call) break;
      call.started = true;
      call.mode = ev.mode;
      if (state.view !== 6) show(6);
      setStatus(call, "Идёт звонок · запись");
      setTyping(call, "agent");
      break;
    case "turn": {
      if (!call) break;
      if (ev.speaker === "seller" && ev.ask) call.ask = ev.ask;
      if (ev.speaker === "agent" && ev.offer) call.offer = ev.offer;
      const node = addLine(call, ev);
      updatePrices(call);
      node?.classList.add("is-speaking");
      const t0 = performance.now();
      await voice.speak(ev.text, ev.speaker);
      if (!voice.enabled) await sleep(Math.min(3200, 900 + ev.text.length * 18));
      else await sleep(Math.max(0, 300 - (performance.now() - t0)));
      node?.classList.remove("is-speaking");
      if (gen !== myGen) return;
      if (!call.ended && !call.awaitingSeller) setTyping(call, ev.speaker === "agent" ? (call.mode === "human" ? null : "seller") : "agent");
      break;
    }
    case "price":
      if (!call) break;
      call.ask = ev.ask;
      if (ev.offer) call.offer = ev.offer;
      updatePrices(call);
      break;
    case "guard": if (call) addGuard(call, ev.text); break;
    case "fallback": addMsg("note", "Модель не ответила — агент продолжает по сценарной стратегии."); break;
    case "await_seller":
      if (call) showSellerInput(call, submitSeller);
      break;
    case "call_end":
      if (!call) break;
      call.ended = true;
      call.status = ev.status;
      setTyping(call, null);
      setStatus(call, ev.status === "agreed" ? "Звонок завершён · договорились" : "Звонок завершён · ждём вашего решения", true);
      break;
    case "done":
      finish(ev.status);
      break;
    case "error":
      addMsg("error", ev.text);
      finish("error");
      break;
  }
}

function enqueue(ev) {
  queue.push({ ev, gen });
  if (!pumping) pump();
}

async function pump() {
  pumping = true;
  while (queue.length) {
    const { ev, gen: g } = queue.shift();
    if (g !== gen) continue;
    try { await handle(ev, g); } catch (e) { console.error(e); }
  }
  pumping = false;
}

async function submitSeller(text) {
  setTyping(state.call, "agent");
  try { await api.seller(state.sid, text); } catch (e) { addMsg("error", `Ответ продавца не отправлен: ${e.message}`); }
}

function finish(status) {
  state.running = false;
  state.es?.close();
  $("sendBtn").disabled = false;
  const notes = {
    completed: "Сделка завершена. Можно начать новый запрос.",
    cancelled: "Остановлено. Уточните запрос и отправьте снова.",
    paused: "Сделка на паузе.",
    exhausted: "Объекты закончились — расширьте параметры поиска.",
    empty: "Ничего не нашлось — попробуйте другой бюджет или район.",
  };
  if (notes[status]) addMsg("note", notes[status]);
}

// ------------------------------------------------------------------ запуск

async function start(query) {
  if (!query.trim()) return;
  state.es?.close();
  voice.stop();
  gen += 1;
  queue = [];
  state = freshState();
  chatLog.innerHTML = "";
  $("query").value = "";
  $("sendBtn").disabled = true;
  renderStage();
  try {
    const { id } = await api.start(query);
    state.sid = id;
    state.running = true;
    const myGen = gen;
    const es = api.stream(id);
    state.es = es;
    es.onmessage = (m) => {
      if (gen !== myGen) return;
      const ev = JSON.parse(m.data);
      if (ev.type === "done" || ev.type === "error") state.streamEnded = true;
      enqueue(ev);
    };
    es.onerror = () => {
      es.close();
      if (gen === myGen && state.running && !state.streamEnded) {
        addMsg("error", "Связь с агентом прервалась. Отправьте запрос ещё раз.");
        finish("error");
      }
    };
  } catch (e) {
    addMsg("error", `Агент не запустился: ${e.message}`);
    $("sendBtn").disabled = false;
  }
}

function setProvider(p) {
  const node = $("provider");
  const names = { deepseek: "DeepSeek", gigachat: "GigaChat", offline: "Офлайн-сценарий" };
  const degraded = p.degraded || p.provider === "offline";
  node.textContent = p.degraded ? `${names[p.provider] || p.provider} · запасной режим` : names[p.provider] || p.provider;
  node.title = p.reason ? `Модель недоступна: ${p.reason}` : `Модель: ${p.model}`;
  node.classList.toggle("is-offline", degraded);
}

// ------------------------------------------------------------------ обработчики UI

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  start($("query").value);
});
$("query").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
stage.addEventListener("click", (e) => {
  const ex = e.target.closest("[data-example]");
  if (ex) start(ex.dataset.example);
});
stepper.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-step]");
  if (b && !b.disabled) show(Number(b.dataset.step));
});

const voiceBtn = $("voiceToggle");
const syncVoice = () => voiceBtn.setAttribute("aria-pressed", String(voice.enabled));
voiceBtn.hidden = !voice.supported;
voiceBtn.addEventListener("click", () => { voice.enabled = !voice.enabled; syncVoice(); });
syncVoice();

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    const which = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("is-active", t === tab);
      t.setAttribute("aria-selected", String(t === tab));
    });
    $("view-deal").hidden = which !== "deal";
    $("view-economics").hidden = which !== "economics";
    if (which === "economics") mountEconomics($("view-economics"));
  }),
);

addMsg("agent", "Здравствуйте! Я агент покупки квартиры. Напишите, что ищете, — дальше я всё сделаю сам и буду спрашивать только ключевые решения.");
renderStage();
api.health().then(setProvider).catch(() => setProvider({ provider: "offline", reason: "сервер недоступен" }));
