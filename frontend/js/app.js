// Контроллер: SSE-события агента -> чат, степпер, панели этапов.
import { api } from "./api.js";
import { addGuard, addLine, createCall, renderCall, setPhase, setStatus, setTyping, showLearned, showSellerInput, updatePrices } from "./call.js";
import { savingsChart } from "./chart.js";
import { mountEconomics } from "./economics.js";
import { el, html, rub, sleep } from "./format.js";
import * as V from "./views.js";
import { bindMic, recorder, voice } from "./voice.js";

const STEPS = ["Запрос", "Финансы", "Поиск", "Подбор", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог"];

const $ = (id) => document.getElementById(id);
const chatLog = $("chatLog");
const stage = $("stage");
const stepper = $("stepper");

let state = freshState();
let gen = 0; // поколение сессии: события старой сессии отбрасываются
let queue = [];
let pumping = false;
let cities = [];

function freshState() {
  return {
    sid: null, es: null, running: false, streamEnded: false, results: {}, status: Array(10).fill("idle"), view: 0,
    call: null, marks: { liked: new Set(), disliked: new Set() }, opened: {}, strategy: null, awaiting: null,
  };
}

// ------------------------------------------------------------------ чат

function addMsg(role, text) {
  const node = el(html`<div class="msg msg-${role}">${text}</div>`);
  chatLog.append(node);
  chatLog.scrollTop = chatLog.scrollHeight;
  return node;
}

function settle(card, label) {
  card.classList.add("is-done");
  card.append(el(html`<div class="verdict">Решение: ${label}</div>`));
}

async function decide(card, kind, approved, data, label) {
  card.querySelectorAll("button, input, select").forEach((x) => (x.disabled = true));
  try {
    await api.confirm(state.sid, kind, approved, data);
    settle(card, label);
    if (kind === "call" && approved && state.call) state.call.mode = data.mode;
  } catch (e) {
    card.querySelectorAll("button, input, select").forEach((x) => (x.disabled = false));
    addMsg("error", `Не удалось отправить решение: ${e.message}`);
  }
}

function financeForm(ev) {
  const d = ev.defaults;
  return html`
    <div class="fin-form">
      <label>Доход в месяц, ₽<input type="number" step="5000" min="0" name="monthly_income" value="${d.monthly_income}"></label>
      <label>Расходы в месяц, ₽<input type="number" step="5000" min="0" name="monthly_expenses" value="${d.monthly_expenses}"></label>
      <label>Накопления, ₽<input type="number" step="50000" min="0" name="savings" value="${d.savings}"></label>
      <label>Когда хотите купить
        <select name="horizon_months">
          ${html.raw([[3, "в ближайшие 3 месяца"], [6, "за полгода"], [12, "за год"], [24, "за 2 года"], [36, "за 3 года"]]
            .map(([v, t]) => html`<option value="${v}" ${v === d.horizon_months ? "selected" : ""}>${t}</option>`).join(""))}
        </select>
      </label>
      <fieldset><legend>Отношение к риску</legend>
        ${html.raw([["conservative", "Без риска — только вклады"], ["moderate", "Умеренно — часть в облигациях"], ["aggressive", "Готов к колебаниям ради дохода"]]
          .map(([v, t]) => html`<label class="radio"><input type="radio" name="risk" value="${v}" ${v === d.risk ? "checked" : ""}>${t}</label>`).join(""))}
      </fieldset>
    </div>`;
}

function collectForm(card) {
  const data = {};
  card.querySelectorAll(".fin-form input, .fin-form select").forEach((i) => {
    if (i.type === "radio") { if (i.checked) data[i.name] = i.value; }
    else data[i.name] = i.tagName === "SELECT" ? Number(i.value) : Number(i.value);
  });
  return data;
}

function confirmCard(ev) {
  if (ev.kind === "selection") return; // решения подборки — на экране подбора
  const card = el(html`<div class="confirm confirm-${ev.kind}"><h4>${ev.title}</h4><p>${ev.text}</p><div data-extra></div><div class="actions"></div></div>`);
  const extra = card.querySelector("[data-extra]");
  const actions = card.querySelector(".actions");

  if (ev.kind === "clarify") {
    state.awaiting = "clarify";
    const chips = ev.chips || [];
    extra.innerHTML = html`<p class="hint">Ответьте в поле ниже — текстом или голосом.</p>
      ${html.raw(chips.length ? html`<div class="quick">${html.raw(chips.map((c) => html`<button class="chip-btn" data-quick="${c}">${c}</button>`).join(""))}</div>` : "")}`;
    extra.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => sendText(b.dataset.quick)));
    chatLog.append(card);
    chatLog.scrollTop = chatLog.scrollHeight;
    $("query").focus();
    return;
  }

  let collect = () => ({});
  if (ev.kind === "red_lines") {
    const p = ev.payload;
    extra.innerHTML = html`
      <dl class="redlines">
        <dt>Город</dt><dd>${p.city}</dd>
        <dt>Комнат</dt><dd>${p.rooms === 0 ? "студия" : p.rooms}</dd>
        <dt>Агент соглашается сам</dt><dd>${p.authority_rule}</dd>
      </dl>
      <label style="margin-top:8px">Бюджет, ₽ — выше агент не пойдёт
        <input type="number" step="50000" min="1000000" value="${p.budget_max}" data-budget>
      </label>`;
    collect = () => ({ budget_max: Number(extra.querySelector("[data-budget]").value) || p.budget_max });
  }
  if (ev.kind === "finance") {
    extra.innerHTML = financeForm(ev);
    collect = () => collectForm(card);
  }

  const buttons = {
    red_lines: [["Подтвердить рамки", true, {}], ["Изменить запрос", false, {}]],
    finance: ev.price ? [["Рассчитать стратегию", true, {}], ["Пропустить", false, {}]] : [["Посчитать бюджет", true, {}], ["Взять пример", false, {}]],
    payment: [["Наличными — вся сумма есть", true, { payment: "cash" }], ["Ипотека, на взнос хватает", true, { payment: "mortgage_ready" }], ["Ипотека, взнос ещё копим", true, { payment: "mortgage_saving" }]],
    finance_choice: ev.gap ? [["Перейти к поиску квартиры", true, {}], ["Сначала накопить", false, {}]] : [["Перейти к поиску квартиры", true, {}]],
    call: [["Звонить — продавца играет ИИ", true, { mode: "auto" }], ["Звонить — продавец я", true, { mode: "human" }], ["Пропустить объект", false, {}]],
    price: [[`Подтвердить ${rub(ev.price)}`, true, {}], ["Отклонить, звонить дальше", false, {}]],
    object: [["Выбираю эту квартиру", true, {}], ["Ищем дальше", false, {}]],
    sign: [["Подписать электронно", true, {}], ["Отложить", false, {}]],
  }[ev.kind] || [["Подтвердить", true, {}], ["Отклонить", false, {}]];

  buttons.forEach(([label, approved, data], i) => {
    const b = el(html`<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-small">${label}</button>`);
    b.addEventListener("click", () => decide(card, ev.kind, approved, { ...collect(), ...data }, label));
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
    case 1: return r[1] ? V.profile(r[1]) : V.waiting("Разбираю запрос", "Город, бюджет, комнаты и ваши пожелания. Если чего-то не хватит — спрошу.");
    case 2: return r[2] ? V.finance(r[2], state.opened, state.strategy) : V.waiting("Финансы", "Ответьте в чате, как планируете покупать: наличными, в ипотеку или ещё копите на взнос.");
    case 3: return r[3] ? V.search(r[3]) : V.waiting("Ищу объявления", "Запрашиваю Циан и считаю справедливую цену по похожим квартирам.");
    case 4: return r[4] ? V.selection(r[4], state.marks) : V.waiting("Подбор", "Готовлю подборку.");
    case 5: return r[5] ? V.legal(r[5]) : V.waiting("Проверка", "Смотрю риски по данным объявления.");
    case 7: return r[7] ? V.viewing(r[7]) : V.waiting("Просмотр", "Договариваюсь о времени.");
    case 8: return r[8] ? V.deal(r[8]) : V.waiting("Оформление", "Собираю план сделки.");
    case 9: return r[9] ? V.summary(r[9]) : V.waiting("Итог", "Завершаю сделку.");
    default: return V.welcome(cities);
  }
}

function renderStage() {
  stage.innerHTML = "";
  if (state.view === 6) {
    stage.append(state.call ? renderCall(state.call, submitSeller) : el(V.waiting("Звонок", "Готовлю звонок продавцу.")));
  } else {
    stage.innerHTML = viewFor(state.view);
    const chart = stage.querySelector("[data-chart]");
    if (chart && state.results[2]?.finance) savingsChart(chart, state.results[2].finance, chart.dataset.selected);
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
    case "message": addMsg(ev.role, ev.text); await sleep(ev.role === "agent" ? 300 : 0); break;
    case "step":
      state.status[ev.step] = ev.status === "pending" ? "active" : ev.status;
      if (ev.status === "active") show(ev.step); else renderStepper();
      await sleep(150);
      break;
    case "result":
      state.results[ev.step] = ev.data;
      if (ev.kind === "finance") state.strategy = ev.data.finance?.risk || null;
      state.view = ev.step;
      renderStage();
      await sleep(ev.kind === "selection" ? 0 : 500);
      break;
    case "confirm":
      if (ev.kind === "call") {
        state.call = createCall(ev);
        state.view = 6;
        renderStage();
      }
      if (ev.kind !== "clarify") state.awaiting = ev.kind === "selection" ? "selection" : null;
      confirmCard(ev);
      break;
    case "product_opened":
      state.opened[ev.product] = ev.amount;
      if (state.view === 2) renderStage();
      break;
    case "call_start":
      if (!call) break;
      call.started = true;
      call.mode = ev.mode;
      if (state.view !== 6) show(6); else renderStage();
      setStatus(call, "Идёт звонок · запись");
      setTyping(call, "agent");
      break;
    case "call_phase": if (call) setPhase(call, ev.phase); break;
    case "turn": {
      if (!call) break;
      if (ev.speaker === "seller" && ev.ask) call.ask = ev.ask;
      if (ev.speaker === "agent" && ev.offer) call.offer = ev.offer;
      const node = addLine(call, ev);
      updatePrices(call);
      node?.classList.add("is-speaking");
      const t0 = performance.now();
      await voice.speak(ev.text, ev.speaker);
      if (!voice.enabled) await sleep(Math.min(3000, 800 + ev.text.length * 16));
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
    case "await_seller": if (call) showSellerInput(call, submitSeller); break;
    case "call_end":
      if (!call) break;
      call.ended = true;
      call.status = ev.status;
      setTyping(call, null);
      setStatus(call, ev.status === "agreed" ? "Звонок завершён · договорились" : "Звонок завершён · ждём вашего решения", true);
      showLearned(call, ev.learned, ev.viewing);
      break;
    case "done": finish(ev.status); break;
    case "error": addMsg("error", ev.text); finish("error"); break;
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
  setComposer();
  const notes = {
    completed: "Сделка завершена. Можно начать новый запрос.",
    cancelled: "Остановлено. Уточните запрос и отправьте снова.",
    paused: "Сделка на паузе.",
    saving: "План накоплений сохранён. Когда взнос будет готов — продолжим подбор.",
    exhausted: "Отмеченные объекты закончились — начните новый запрос или вернитесь к подбору.",
    empty: "Ничего не нашлось — попробуйте другой бюджет или город.",
  };
  if (notes[status]) addMsg("note", notes[status]);
}

// ------------------------------------------------------------------ запуск и сообщения

function setComposer() {
  $("sendBtn").textContent = state.running ? "Отправить" : "Поручить агенту";
  $("query").placeholder = state.running ? "Ответ агенту или пожелание: «кухня побольше», «не первый этаж»…" : "Например: ищу 2-комнатную в Казани до 9 млн рядом со школой";
  $("resetBtn").hidden = !state.sid;
}

async function start(query) {
  if (!query.trim()) return;
  state.es?.close();
  voice.stop();
  gen += 1;
  queue = [];
  state = freshState();
  chatLog.innerHTML = "";
  $("query").value = "";
  renderStage();
  try {
    const { id } = await api.start(query);
    state.sid = id;
    state.running = true;
    setComposer();
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
  }
}

async function sendText(text) {
  text = text.trim();
  if (!text) return;
  if (!state.running) return start(text);
  $("query").value = "";
  const marks = { liked: [...state.marks.liked], disliked: [...state.marks.disliked] };
  try {
    await api.message(state.sid, text, marks);
    if (state.awaiting === "clarify") {
      chatLog.querySelectorAll(".confirm-clarify:not(.is-done)").forEach((c) => c.classList.add("is-done"));
    }
    state.awaiting = null;
  } catch (e) {
    addMsg("error", `Сообщение не отправлено: ${e.message}`);
  }
}

async function selectionAction(action) {
  if (state.awaiting !== "selection") return;
  if (action === "proceed" && !state.marks.liked.size && !confirm("Вы ничего не отметили. Позвонить по трём лучшим вариантам?")) return;
  state.awaiting = null;
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
  try {
    await api.confirm(state.sid, "selection", true, { action, liked: [...state.marks.liked], disliked: [...state.marks.disliked] });
  } catch (e) {
    state.awaiting = "selection";
    stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = false));
    addMsg("error", `Не удалось отправить отметки: ${e.message}`);
  }
}

function setProvider(p) {
  const node = $("provider");
  const names = { deepseek: "DeepSeek", gigachat: "GigaChat", offline: "Без модели (правила)" };
  const degraded = p.degraded || p.provider === "offline";
  node.textContent = p.degraded ? `${names[p.provider] || p.provider} · запасной режим` : names[p.provider] || p.provider;
  node.title = p.reason ? `Модель недоступна: ${p.reason}` : `Модель: ${p.model}`;
  node.classList.toggle("is-offline", degraded);
}

// ------------------------------------------------------------------ обработчики UI

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  sendText($("query").value);
});
$("query").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("resetBtn").addEventListener("click", () => {
  state.es?.close();
  voice.stop();
  gen += 1;
  queue = [];
  state = freshState();
  chatLog.innerHTML = "";
  addMsg("agent", "Начнём заново. Опишите, какую квартиру ищете.");
  setComposer();
  renderStage();
});

bindMic($("micBtn"), $("query"), {
  onFinal: (text) => sendText(text),
  onError: (message) => addMsg("error", message),
});
$("micBtn").hidden = false;

stage.addEventListener("click", async (e) => {
  const ex = e.target.closest("[data-example]");
  if (ex) return start(ex.dataset.example);

  const vote = e.target.closest("[data-vote]");
  if (vote) {
    const id = vote.closest("[data-id]").dataset.id;
    const { liked, disliked } = state.marks;
    const up = vote.dataset.vote === "up";
    const set = up ? liked : disliked;
    const other = up ? disliked : liked;
    if (set.has(id)) set.delete(id); else { set.add(id); other.delete(id); }
    const card = vote.closest(".offer");
    card.classList.toggle("is-liked", liked.has(id));
    card.classList.toggle("is-disliked", disliked.has(id));
    card.querySelector('[data-vote="up"]').setAttribute("aria-pressed", String(liked.has(id)));
    card.querySelector('[data-vote="down"]').setAttribute("aria-pressed", String(disliked.has(id)));
    const counter = stage.querySelector("[data-liked-count]");
    if (counter) counter.textContent = `(${liked.size})`;
    return;
  }
  const sel = e.target.closest("[data-selection]");
  if (sel) return selectionAction(sel.dataset.selection);

  const strat = e.target.closest("[data-strategy]");
  if (strat) {
    state.strategy = strat.dataset.strategy;
    return renderStage();
  }
  const open = e.target.closest("[data-open]");
  if (open && state.sid) {
    open.disabled = true;
    try { await api.product(state.sid, open.dataset.open, Number(open.dataset.amount)); }
    catch (err) { open.disabled = false; addMsg("error", `Не удалось: ${err.message}`); }
  }
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

addMsg("agent", "Здравствуйте! Я агент покупки квартиры. Расскажите текстом или голосом, что ищете: город, бюджет, комнаты и что для вас важно.");
setComposer();
renderStage();
api.health().then((h) => {
  cities = h.cities || [];
  recorder.available = Boolean(h.stt?.installed && h.stt.status !== "error");
  setProvider(h);
  if (state.view === 0) renderStage();
})
  .catch(() => setProvider({ provider: "offline", reason: "сервер недоступен" }));
