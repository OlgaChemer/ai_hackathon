// Карточки решений в основной области; принятые решения остаются в истории чата.
// Решение уходит на сервер; карточку закрывает событие resolved из ленты — так же она выглядит и в истории.
import { api } from "./api.js";
import { el, html, rub } from "./format.js";
import { PhoneCall, formatPhone, normalizePhone } from "./phone.js";
import { $, addMsg, chatLog, config, stage, state } from "./state.js";
import { sayPrompt, voiceHint } from "./voicechat.js";
import { voice } from "./voice.js";
import { roomDescription } from "./speech.js";
import { setChatExpanded } from "./shell.js";

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
    await api.confirm(state.sid, kind, approved, { ...data, _label: label, ...(said ? { _said: said } : {}) }, card.dataset.decisionId);
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

function updateDecisionLayout() {
  $("chatPanel").classList.toggle("has-decision",
    Boolean($("chatLog").querySelector(".confirm:not(.is-done)")));
}

function presentDecision(card, live) {
  $("chatLog").append(card);
  updateDecisionLayout();
  if (live) { setChatExpanded(true); chatLog.scrollTop = chatLog.scrollHeight; }
}

function archiveDecision(card) {
  card.querySelectorAll("button, input, select").forEach((x) => (x.disabled = true));
  const extra = card.querySelector("[data-extra]");
  if ((extra?.childElementCount || card.classList.contains('confirm-clarify')) && !card.querySelector(".decision-archive")) {
    const details = el(html`<details class="decision-archive"><summary>Параметры решения</summary></details>`);
    extra.replaceWith(details);
    details.append(extra);
    if (card.classList.contains('confirm-clarify')) {
      const question = card.querySelector(':scope > p');
      if (question) details.prepend(question);
    }
  }
  updateDecisionLayout();
}

export function confirmCard(ev) {
  if (ev.kind === "selection") return; // решения подборки — на экране подбора
  const live = !ev.replay; // из истории — без голоса, звонка и фокуса
  const card = el(html`<div class="confirm confirm-${ev.kind}"><h4>${ev.title}</h4><p>${ev.text}</p><div data-extra></div><div class="actions"></div></div>`);
  const extra = card.querySelector("[data-extra]");
  const actions = card.querySelector(".actions");
  card.dataset.decisionId = ev.decision_id || '';
  card.decisionEvent=ev;
  if (state.cards[ev.kind]?.nodeType && !state.cards[ev.kind].classList.contains('is-done')) {
    state.cards[ev.kind].classList.add('is-done');
    archiveDecision(state.cards[ev.kind]);
  }
  state.cards[ev.kind] = card;

  if (ev.kind === "clarify") {
    state.awaiting = "clarify";
    const chips = ev.chips || [];
    if (live) sayPrompt(chips.length ? `${ev.text} Например: ${chips.slice(0, 3).join(", или ")}.` : ev.text);
    extra.innerHTML = html`<p class="hint">Ответьте в диалоге с агентом — текстом или голосом.</p>
      <button type="button" class="btn btn-ghost btn-small" data-answer-chat>Написать ответ</button>
      ${html.raw(chips.length ? html`<div class="quick">${html.raw(chips.map((c) => html`<button class="chip-btn" data-quick="${c}">${c}</button>`).join(""))}</div>` : "")}`;
    extra.querySelector("[data-answer-chat]").addEventListener("click", () => {
      setChatExpanded(true);
      $("query").focus();
    });
    extra.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => sendText(b.dataset.quick)));
    presentDecision(card, live);
    chatLog.scrollTop = chatLog.scrollHeight;
    if (live) card.querySelector("[data-quick]")?.focus({ preventScroll: true });
    return;
  }

  let collect = () => ({});
  if (ev.kind === "red_lines") {
    const p = ev.payload;
    extra.innerHTML = html`
      <dl class="redlines">
        <dt>Территория</dt><dd>${[p.city, ...(p.cities || []).filter(c => c !== p.city)].join(", ")}</dd>
        <dt>Комнат</dt><dd>${p.rooms_options?.length > 1 ? p.rooms_options.join("–") : p.rooms === 0 ? "студия" : p.rooms}</dd>
        <dt>Торгуюсь сам</dt><dd>${p.authority_rule}</dd>
      </dl>
      <div class="price-range" data-price-range>
        <div class="price-range__head">
          <span>Ниже не предлагаю — <b data-min-label></b></span>
          <span>Выше не торгуюсь — <b data-max-label></b></span>
        </div>
        <div class="price-range__track">
          <div class="price-range__fill" data-fill></div>
          <input type="range" data-min-range aria-label="Нижняя граница цены">
          <input type="range" data-max-range aria-label="Верхняя граница цены">
        </div>
        <p class="hint">По умолчанию нижняя граница на 30% ниже верхней — квартиры дешевле в подборку не попадут.</p>
      </div>`;
    const step = Number(p.step) || 500000;
    const floor = Number(p.floor) || 1000000;
    const top = Math.max(Math.round((p.budget_max * 1.5) / step) * step, p.budget_max + step);
    const lo0 = Math.max(floor, Math.min(Number(p.budget_min) || Math.round((p.budget_max * 0.7) / step) * step, p.budget_max - step));
    const rangeMin = extra.querySelector("[data-min-range]");
    const rangeMax = extra.querySelector("[data-max-range]");
    const fill = extra.querySelector("[data-fill]");
    const minLabel = extra.querySelector("[data-min-label]");
    const maxLabel = extra.querySelector("[data-max-label]");
    for (const input of [rangeMin, rangeMax]) {
      input.min = String(floor); input.max = String(top); input.step = String(step);
    }
    rangeMin.value = String(lo0);
    rangeMax.value = String(Math.max(lo0 + step, p.budget_max));
    const draw = (changed) => {
      let lo = Number(rangeMin.value), hi = Number(rangeMax.value);
      if (lo > hi - step) {
        if (changed === "min") { lo = hi - step; rangeMin.value = String(lo); }
        else { hi = lo + step; rangeMax.value = String(hi); }
      }
      const span = top - floor || 1;
      fill.style.left = `${((lo - floor) / span) * 100}%`;
      fill.style.width = `${((hi - lo) / span) * 100}%`;
      minLabel.textContent = rub(lo);
      maxLabel.textContent = rub(hi);
    };
    rangeMin.addEventListener("input", () => draw("min"));
    rangeMax.addEventListener("input", () => draw("max"));
    draw();
    collect = () => ({ budget_min: Number(rangeMin.value), budget_max: Number(rangeMax.value) });
  }
  if (ev.kind === "extras") {
    const p = ev.payload || {};
    const groups = [
      { key: "housing", title: "Тип жилья", options: [["secondary", "Вторичка"], ["new", "Новостройка"], ["both", "Всё равно"]] },
      { key: "apartments", title: "Апартаменты", options: [["hide", "Не показывать"], ["only", "Только апартаменты"], ["any", "Всё равно"]] },
      { key: "condition", title: "Ремонт", options: [["repair", "Без ремонта"], ["cosmetic", "Косметический"], ["ready", "Евроремонт"], ["ready", "Дизайнерский"]] },
    ];
    const state = {
      housing: p.housing || null,
      apartments: p.apartments || null,
      condition: p.condition || null,
      not_first: !!p.not_first_floor,
      not_last: !!p.not_last_floor,
      picked: {},                      // индекс выбранного варианта в группе (для «Евроремонт» и «Дизайнерский»)
    };
    const box = extra.querySelector("[data-extras]") || extra;
    const render = () => {
      const single = (g) => g.options.map(([value, label], i) => {
        const on = state.picked[g.key] === i;
        return `<button type="button" class="chip-btn${on ? " is-on" : ""}" data-group="${g.key}" data-index="${i}" data-value="${value}">${label}</button>`;
      }).join("");
      const floor = (key, label, on) =>
        `<button type="button" class="chip-btn${on ? " is-on" : ""}" data-group="floor" data-value="${key}">${label}</button>`;
      box.innerHTML = `<div class="extras">
        ${groups.map((g) => `<div class="extras__row"><span class="extras__title">${g.title}</span><div class="quick">${single(g)}</div></div>`).join("")}
        <div class="extras__row"><span class="extras__title">Этаж</span><div class="quick">
          ${floor("not_first", "Не первый", state.not_first)}${floor("not_last", "Не последний", state.not_last)}
        </div></div>
      </div>`;
      box.querySelectorAll("[data-group]").forEach((b) => b.addEventListener("click", () => {
        const group = b.dataset.group;
        if (group === "floor") {
          const key = b.dataset.value === "not_last" ? "not_last" : "not_first";
          state[key] = !state[key];
        } else {
          const idx = Number(b.dataset.index);
          const g = groups.find((x) => x.key === group);
          state.picked[group] = state.picked[group] === idx ? null : idx;
          state[group] = state.picked[group] === null ? null : g.options[idx][0];
        }
        render();
      }));
    };
    render();
    collect = () => {
      const data = { not_first_floor: state.not_first, not_last_floor: state.not_last };
      if (state.housing) data.housing = state.housing;
      if (state.apartments) data.apartments = state.apartments;
      if (state.condition) data.condition = state.condition;
      return data;
    };
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
    extra.innerHTML = html`<label class="phone-field">Ваш телефон — агент позвонит вам как продавцу
      <input type="tel" inputmode="tel" placeholder="+7 999 123-45-67" value="${storedPhone()}" data-phone autocomplete="tel"></label>`;
  }
  if (ev.kind === 'call') {
    extra.insertAdjacentHTML('beforeend', html`<label class="phone-field">Что уточнить у продавца? Измените или добавьте вопросы, по одному на строку.
      <textarea data-seller-questions rows="5" maxlength="4500">${(ev.questions || []).map(q => q.question || q.label).join('\n')}</textarea></label>`);
    collect = () => ({questions: collectQuestions(card)});
  }

  const buttons = {
    red_lines: [["Подтвердить рамки", true, {}], ["Изменить запрос", false, {}]],
    extras: [["Применить", true, {}], ["Пропустить", false, {}]],
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
    sign: [["Подтвердить демо-план", true, {}], ["Отложить", false, {}]],
  }[ev.kind] || [["Подтвердить", true, {}], ["Отклонить", false, {}]];

  // голосом: что сказать вслух и как ответить
  const hint = voiceHint(ev);
  card.dataset.hint = hint;
  const spoken = {
    red_lines: () => `Подтвердите диапазон цены: ${[ev.payload.city, ...(ev.payload.cities || [])].join(", ")}, ${roomDescription(ev.payload.rooms_options?.length ? ev.payload.rooms_options : [ev.payload.rooms])}, от ${rub(ev.payload.budget_min || Math.round(ev.payload.budget_max * 0.7))} до ${rub(ev.payload.budget_max)}. Выше не пойду, ниже предлагать не буду. Итоговую цену подтверждаете вы.`,
    extras: () => "Четыре быстрых вопроса: тип жилья, апартаменты, ремонт и этаж. Можно пропустить — тогда ищу по известному.",
    finance: () => `${ev.title}.`,
    payment: () => `${ev.title}`,
    family: () => "Есть ли у вас дети? Возраст и маткапитал можно указать на карточке.",
    housing: () => "Посмотреть вторичку, новостройки или оба варианта?",
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
  presentDecision(card, live);
  chatLog.scrollTop = chatLog.scrollHeight;
  if (!live) return;
  actions.querySelector("button")?.focus({ preventScroll: true });
}

/** Клиент решил (событие resolved): карточка закрывается с подписью решения. */
export function resolved(ev) {
  const card = state.cards[ev.kind];
  if (ev.decision_id && card?.dataset?.decisionId && ev.decision_id !== card.dataset.decisionId) return;
  if (ev.kind === "selection") {
    stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
  } else if (card && !card.classList.contains("is-done")) {
    if (ev.kind === "clarify") card.classList.add("is-done");
    else settle(card, ev.label || (ev.approved === null ? "ответили сообщением" : ev.approved ? "да" : "нет"));
  }
  if (card && ev.kind !== "selection") archiveDecision(card);
  delete state.cards[ev.kind];
  if (state.awaiting === ev.kind) state.awaiting = null;
}

/** Агент этого диалога больше не работает (сервер перезапускался или диалог остановлен): старые кнопки не нажать. */
export function staleCards() {
  for (const [kind, card] of Object.entries(state.cards)) {
    if (kind === "selection") continue; // кнопки подборки — на экране, их выключаем ниже
    if (kind === "clarify") card.classList.add("is-done");
    else settle(card, "не принято — напишите сообщение, чтобы продолжить диалог");
    archiveDecision(card);
  }
  state.cards = {};
  state.awaiting = null;
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
}

export function refreshPhoneAccess(){
  const card=state.cards.call;
  if(!card?.isConnected||card.classList.contains('is-done')||!card.decisionEvent||card.querySelector('button')?.disabled)return;
  const questions=card.querySelector('[data-seller-questions]')?.value;
  const number=card.querySelector('[data-phone]')?.value;
  delete state.cards.call;
  confirmCard({...card.decisionEvent,replay:true});
  const replacement=state.cards.call;card.replaceWith(replacement);
  if(questions!==undefined)replacement.querySelector('[data-seller-questions]').value=questions;
  if(number!==undefined&&replacement.querySelector('[data-phone]'))replacement.querySelector('[data-phone]').value=number;
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
  const phone = state.phone && !state.phone.ended ? state.phone : new PhoneCall({chat_id:state.sid,decision_id:card.dataset.decisionId});
  state.phone = phone;
  phone.onAudioState = (audioState) => {
    const button = $("resumeCallAudio");
    if (button) button.hidden = audioState === "running" || audioState === "closed";
  };
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
  await decide(card, "call", true, { mode: "phone", questions: collectQuestions(card), phone_grant_token:phone.configuration?.grant_token }, `${label}: ${formatPhone(number)}`);
}

function collectQuestions(card) {
  return (card.querySelector('[data-seller-questions]')?.value || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0,15);
}
