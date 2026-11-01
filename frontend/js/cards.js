// Карточки решений в чате: рамки, оплата, семья, где искать, финансы, звонок, цена, просмотр, подписание.
// Решение уходит на сервер; карточку закрывает событие resolved из ленты — так же она выглядит и в истории.
import { api } from "./api.js";
import { el, html, rub } from "./format.js";
import { PhoneCall, formatPhone, normalizePhone } from "./phone.js";
import { $, addMsg, chatLog, config, stage, state } from "./state.js";
import { sayPrompt, voiceHint } from "./voicechat.js";
import { voice } from "./voice.js";

let sendText = async () => {};

/** sendText — отправить сообщение агенту (кнопки-подсказки в уточнении). */
export function initCards(handlers) {
  sendText = handlers.sendText;
}

function settle(card, label) {
  card.classList.add("is-done");
  card.append(el(html`<div class="verdict">Решение: ${label}</div>`));
}

// Решение уходит на сервер; карточку закрывает событие resolved из ленты — так же она выглядит и в истории.
async function decide(card, kind, approved, data, label) {
  card.querySelectorAll("button, input, select").forEach((x) => (x.disabled = true));
  const said = card.dataset.said; // решение сказано голосом — фраза попадёт в историю диалога
  delete card.dataset.said;
  try {
    await api.confirm(state.sid, kind, approved, { ...data, _label: label, ...(said ? { _said: said } : {}) });
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

function familyForm(ev) {
  const d = ev.defaults;
  const hasCap = d.matcap > 0;
  return html`
    <div class="fin-form family-form">
      <label>Сколько детей
        <select name="children">
          ${html.raw([[0, "Детей нет"], [1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5 и больше"]]
            .map(([v, t]) => html`<option value="${v}" ${v === d.children ? "selected" : ""}>${t}</option>`).join(""))}
        </select>
      </label>
      <label data-age>Возраст младшего, полных лет<input type="number" min="0" max="25" step="1" name="youngest_age" value="${d.youngest_age}" placeholder="например, 4"></label>
      <label class="radio"><input type="checkbox" name="has_matcap" ${hasCap ? "checked" : ""}>Есть неизрасходованный маткапитал</label>
      <label data-cap ${hasCap ? "" : "hidden"}>Сумма маткапитала, ₽<input type="number" min="0" step="1000" name="matcap" value="${d.matcap || d.matcap_default}"></label>
    </div>`;
}

function bindFamilyForm(card) {
  const kids = card.querySelector("[name=children]");
  const age = card.querySelector("[data-age]");
  const has = card.querySelector("[name=has_matcap]");
  const cap = card.querySelector("[data-cap]");
  const sync = () => { age.hidden = kids.value === "0"; cap.hidden = !has.checked; };
  kids.addEventListener("change", sync);
  has.addEventListener("change", sync);
  sync();
  return () => {
    const children = Number(kids.value);
    const ageVal = card.querySelector("[name=youngest_age]").value;
    return {
      children,
      youngest_age: children && ageVal !== "" ? Number(ageVal) : null,
      matcap: has.checked ? Number(card.querySelector("[name=matcap]").value) || 0 : 0,
    };
  };
}

function collectForm(card) {
  const data = {};
  card.querySelectorAll(".fin-form input, .fin-form select").forEach((i) => {
    if (i.type === "radio") { if (i.checked) data[i.name] = i.value; }
    else data[i.name] = i.tagName === "SELECT" ? Number(i.value) : Number(i.value);
  });
  return data;
}

export function confirmCard(ev) {
  if (ev.kind === "selection") return; // решения подборки — на экране подбора
  const live = !ev.replay; // из истории — без голоса, звонка и фокуса
  const card = el(html`<div class="confirm confirm-${ev.kind}"><h4>${ev.title}</h4><p>${ev.text}</p><div data-extra></div><div class="actions"></div></div>`);
  const extra = card.querySelector("[data-extra]");
  const actions = card.querySelector(".actions");
  state.cards[ev.kind] = card;

  if (ev.kind === "clarify") {
    state.awaiting = "clarify";
    const chips = ev.chips || [];
    if (live) sayPrompt(chips.length ? `${ev.text} Например: ${chips.slice(0, 3).join(", или ")}.` : ev.text);
    extra.innerHTML = html`<p class="hint">Ответьте в поле ниже — текстом или голосом.</p>
      ${html.raw(chips.length ? html`<div class="quick">${html.raw(chips.map((c) => html`<button class="chip-btn" data-quick="${c}">${c}</button>`).join(""))}</div>` : "")}`;
    extra.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => sendText(b.dataset.quick)));
    chatLog.append(card);
    chatLog.scrollTop = chatLog.scrollHeight;
    if (live) $("query").focus();
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
  if (ev.kind === "family") {
    extra.innerHTML = familyForm(ev);
    collect = bindFamilyForm(card);
  }
  if (ev.kind === "call" && config.phoneReady) {
    // регистрируемся в Exolve заранее: по клику звонок уходит сразу
    if (live) {
      if (!state.phone || state.phone.ended) state.phone = new PhoneCall();
      state.phone.connect().catch((e) => console.warn("Exolve:", e.message));
    }
    extra.innerHTML = html`<label class="phone-field">Ваш телефон — агент позвонит вам как продавцу
      <input type="tel" inputmode="tel" placeholder="+7 999 123-45-67" value="${storedPhone()}" data-phone autocomplete="tel"></label>`;
  }

  const buttons = {
    red_lines: [["Подтвердить рамки", true, {}], ["Изменить запрос", false, {}]],
    finance: ev.price ? [["Рассчитать стратегию", true, {}], ["Пропустить", false, {}]] : [["Посчитать бюджет", true, {}]],
    payment: [["Наличными — вся сумма есть", true, { payment: "cash" }], ["Ипотека, на взнос хватает", true, { payment: "mortgage_ready" }], ["Ипотека, взнос ещё копим", true, { payment: "mortgage_saving" }]],
    family: [["Подобрать программу", true, {}], ["Пропустить", false, {}]],
    housing: [["Новостройки и вторичка", true, { housing: "both" }], ["Только новостройки", true, { housing: "new" }], ["Только вторичка", true, { housing: "secondary" }]],
    call: [
      ...(config.phoneReady ? [["Позвонить мне на телефон", true, { mode: "phone" }]] : []),
      ["Звонить — продавца играет ИИ", true, { mode: "auto" }], ["Звонить — продавец я (в чате)", true, { mode: "human" }], ["Пропустить объект", false, {}],
    ],
    price: [[`Подтвердить ${rub(ev.price)}`, true, {}], ["Отклонить, звонить дальше", false, {}]],
    object: [["Выбираю эту квартиру", true, {}], ["Ищем дальше", false, {}]],
    sign: [["Подписать электронно", true, {}], ["Отложить", false, {}]],
  }[ev.kind] || [["Подтвердить", true, {}], ["Отклонить", false, {}]];

  // голосом: что сказать вслух и как ответить
  const hint = voiceHint(ev);
  card.dataset.hint = hint;
  const spoken = {
    red_lines: () => `Проверим рамки: ${ev.payload.city}, ${ev.payload.rooms === 0 ? "студия" : `${ev.payload.rooms}-комнатная`}, бюджет до ${rub(ev.payload.budget_max)}. ${ev.text}`,
    finance: () => `${ev.title}.`,
    payment: () => `${ev.title}`,
    family: () => `${ev.title}. ${ev.text}`,
  }[ev.kind]?.() ?? `${ev.title}. ${ev.text}`;
  if (live) sayPrompt(`${spoken} ${hint}`);

  buttons.forEach(([label, approved, data], i) => {
    const b = el(html`<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-small">${label}</button>`);
    b.addEventListener("click", () => {
      if (data.mode === "phone") return dialSeller(card, label);
      if (ev.kind === "call" && state.phone) { state.phone.hangup(); state.phone = null; } // заготовленный звонок не нужен
      return decide(card, ev.kind, approved, { ...collect(), ...data }, label);
    });
    actions.append(b);
  });
  chatLog.append(card);
  chatLog.scrollTop = chatLog.scrollHeight;
  if (!live) return;
  actions.querySelector("button")?.focus({ preventScroll: true });
  if (matchMedia("(max-width: 860px)").matches) card.scrollIntoView({ block: "center", behavior: "smooth" });
}

/** Клиент решил (событие resolved): карточка закрывается с подписью решения. */
export function resolved(ev) {
  const card = state.cards[ev.kind];
  if (ev.kind === "selection") {
    stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
  } else if (card && !card.classList.contains("is-done")) {
    if (ev.kind === "clarify") card.classList.add("is-done");
    else settle(card, ev.label || (ev.approved === null ? "ответили сообщением" : ev.approved ? "да" : "нет"));
  }
  delete state.cards[ev.kind];
  if (state.awaiting === ev.kind) state.awaiting = null;
}

/** Агент этого диалога больше не работает (сервер перезапускался или диалог остановлен): старые кнопки не нажать. */
export function staleCards() {
  for (const [kind, card] of Object.entries(state.cards)) {
    if (kind === "selection") continue; // кнопки подборки — на экране, их выключаем ниже
    if (kind === "clarify") card.classList.add("is-done");
    else settle(card, "не принято — напишите сообщение, чтобы продолжить диалог");
  }
  state.cards = {};
  state.awaiting = null;
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
}

function storedPhone() {
  try { return localStorage.getItem("sellerPhone") || config.sellerPhoneDefault; } catch { return config.sellerPhoneDefault; }
}

async function dialSeller(card, label) {
  const input = card.querySelector("[data-phone]");
  const number = normalizePhone(input?.value);
  card.querySelector(".mic-error")?.remove();
  const note = (text, isError = false) => {
    card.querySelector(".dial-note")?.remove();
    card.querySelector(".actions").before(el(html`<p class="dial-note ${isError ? "mic-error" : ""}">${text}</p>`));
  };
  if (!number) { note("Введите российский номер: +7 и 10 цифр.", true); input?.focus(); return; }
  try { localStorage.setItem("sellerPhone", input.value.trim()); } catch { /* приватный режим */ }
  const buttons = card.querySelectorAll("button, input");
  buttons.forEach((x) => (x.disabled = true));
  voice.stop();
  const phone = state.phone && !state.phone.ended ? state.phone : new PhoneCall();
  state.phone = phone;
  const steps = {
    connecting: "Подключаюсь к МТС Exolve…",
    dialing: `Звоню на ${formatPhone(number)}…`,
    ringing: `Звоню на ${formatPhone(number)} — возьмите трубку. Вы — продавец квартиры.`,
    answered: "Трубку взяли — агент начинает разговор.",
  };
  try {
    await phone.dial(number, (st) => note(steps[st] || st));
  } catch (e) {
    if (state.phone === phone) state.phone = null;
    buttons.forEach((x) => (x.disabled = false));
    note(e.message, true);
    return;
  }
  // продавец положил трубку: ближайшее ожидание ответа (listenPhone) сообщит агенту
  phone.onEnded = () => {
    if (state.phone === phone && state.call && !state.call.ended) addMsg("note", "Продавец положил трубку.");
  };
  await decide(card, "call", true, { mode: "phone" }, `${label}: ${formatPhone(number)}`);
}
