// Экран звонка: счётчик цены, «линейка» коридора и расшифровка разговора.
import { el, html, rub } from "./format.js";
import { bindMic, mic, recorder } from "./voice.js";

const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const SIM_NOTES = {
  auto: "Симуляция: продавца играет ИИ, настоящему продавцу прототип не звонит",
  human: "Продавец — вы: отвечайте текстом или голосом",
  phone: "Настоящий звонок через МТС Exolve: продавец — вы, на телефоне",
};

function pageNote(p) {
  if (!p) return "";
  const at = p.checked_at ? new Date(p.checked_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "";
  return p.alive
    ? html`<p class="page-check is-ok">✓ Объявление в продаже${at ? ` · проверено в ${at}` : ""}</p>`
    : html`<p class="page-check" title="${p.note}">Не удалось проверить, что объявление в продаже (${p.note}) — уточню у продавца</p>`;
}

export function createCall(payload) {
  return {
    listing: payload.listing,
    fair: payload.fair,
    authority: payload.authority,
    arguments: payload.arguments || [],
    questions: payload.questions || [],
    page: payload.page || null, // проверка страницы объявления перед звонком
    sellerName: payload.seller_name || "Продавец",
    sellerRole: payload.seller_role || payload.listing.seller_role || "",
    phase: null,
    learned: null,
    viewing: null,
    base: payload.listing.price,
    ask: payload.listing.price,
    offer: null,
    lines: [],
    guards: [],
    started: false,
    ended: false,
    status: null,
    mode: "auto",
    typing: null,
    awaitingSeller: false,
    root: null,
    shownAsk: payload.listing.price,
  };
}

const savingsText = (c) => (c.base - c.ask > 0 ? `−${rub(c.base - c.ask)}` : "скидки пока нет");

function scale(c) {
  const lo = Math.min(c.fair.low, c.fair.target * 0.975, c.offer ?? Infinity) * 0.995;
  const hi = Math.max(c.base, c.fair.high) * 1.01;
  return { lo, hi, x: (v) => Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100)) };
}

function lineMarkup(line) {
  const who = line.speaker === "agent" ? "ИИ-агент" : "Продавец";
  const offer = line.speaker === "agent" && line.offer ? html` <span class="offer num">· предлагает ${rub(line.offer)}</span>` : "";
  return html`<div class="line line-${line.speaker}"><span class="who">${who}</span><p>${line.text}${html.raw(offer)}</p></div>`;
}

export function renderCall(c, onSellerSubmit) {
  const s = scale(c);
  const seller = c.sellerName;
  const phases = [["clarify", "Уточнение"], ["bargain", "Торг"], ["viewing", "Просмотр"]];
  const order = phases.map((p) => p[0]);
  const statusText = c.ended
    ? c.status === "agreed" ? "Звонок завершён · договорились" : "Звонок завершён · ждём вашего решения"
    : c.started ? "Идёт звонок · запись" : "Готов позвонить";
  const root = el(html`
  <section class="panel call">
    <div class="call-head">
      <div class="call-who">
        <span class="avatar" aria-hidden="true">${seller.trim()[0]}</span>
        <div><strong>${seller}</strong><span>${c.sellerRole} · ${c.listing.address}</span></div>
      </div>
      <div class="call-status ${c.ended ? "is-ended" : ""}"><span class="rec"></span><span data-status>${statusText}</span></div>
    </div>
    <div class="call-meta">
      <ol class="phases" data-phases>
        ${html.raw(phases.map(([k, t]) => html`<li data-phase="${k}" class="${phaseClass(c, k, order)}">${t}</li>`).join(""))}
      </ol>
      <span class="sim-note">${SIM_NOTES[c.mode] || SIM_NOTES.auto}</span>
    </div>
    ${html.raw(pageNote(c.page))}
    ${html.raw(c.questions.length ? html`<p class="call-questions">Агент уточнит: ${c.questions.map((q) => q.label).join(", ")}</p>` : "")}
    <div class="counter">
      <div class="label">Лучшая цена продавца сейчас</div>
      <div class="row">
        <div class="price-now num" data-price>${rub(c.ask)}</div>
        <div class="was num">${rub(c.base)}</div>
        <div class="savings-pill num ${c.base - c.ask > 0 ? "" : "is-zero"}" data-savings>${savingsText(c)}</div>
      </div>
    </div>
    <div class="ruler" aria-hidden="true">
      <div class="rail"></div>
      <div class="corridor" style="left:${s.x(c.fair.low)}%;width:${s.x(c.fair.high) - s.x(c.fair.low)}%"></div>
      <div class="limit" style="left:${s.x(c.authority)}%" title="лимит клиента"></div>
      <div class="marker marker-seller" data-m-seller style="left:${s.x(c.ask)}%"><b data-m-seller-label>продавец ${rub(c.ask)}</b><i></i></div>
      <div class="marker marker-agent" data-m-agent style="left:${s.x(c.offer ?? c.fair.target)}%;${c.offer ? "" : "opacity:.35"}"><b data-m-agent-label>${c.offer ? `агент ${rub(c.offer)}` : `цель ${rub(c.fair.target)}`}</b><i></i></div>
    </div>
    <div class="ruler-scale num"><span>${rub(Math.round(s.lo / 1e4) * 1e4)}</span><span>коридор справедливой цены ${rub(c.fair.low)} – ${rub(c.fair.high)} · <i class="limit-key"></i>лимит клиента ${rub(c.authority)}</span><span>${rub(Math.round(s.hi / 1e4) * 1e4)}</span></div>
    <div class="transcript" data-transcript>
      ${html.raw(c.lines.map(lineMarkup).join(""))}
      ${html.raw(c.started ? "" : html`<p class="typing">Расшифровка появится, как только агент наберёт номер.</p>`)}
    </div>
    <div data-seller-slot></div>
    <div data-learned>${html.raw(learnedMarkup(c))}</div>
    <details class="args"><summary>Аргументы агента (${c.arguments.length})</summary><ul>${html.raw(c.arguments.map((a) => html`<li>${a}</li>`).join(""))}</ul></details>
  </section>`);
  c.root = root;
  c.shownAsk = c.ask;
  if (c.awaitingSeller) showSellerInput(c, onSellerSubmit);
  else if (c.typing) setTyping(c, c.typing);
  return root;
}

function phaseClass(c, k, order) {
  if (!c.phase) return "";
  const i = order.indexOf(k), cur = order.indexOf(c.phase);
  return i < cur || (c.ended && i <= cur) ? "is-done" : i === cur ? "is-on" : "";
}

function learnedMarkup(c) {
  if (!c.ended || (!c.viewing && !c.learned)) return "";
  const facts = Object.entries(c.learned || {});
  return html`<div class="learned-box">
    <h3>Что агент узнал</h3>
    ${html.raw(facts.length ? html`<dl>${html.raw(facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`).join(""))}</dl>` : html`<p class="hint">Продавец не ответил на вопросы — уточню позже.</p>`)}
    ${html.raw(c.viewing ? html`<p class="viewing-slot">Просмотр: <b>${c.viewing}</b></p>` : "")}
  </div>`;
}

export function setPhase(c, phase) {
  c.phase = phase;
  const list = q(c, "[data-phases]");
  if (!list) return;
  const order = ["clarify", "bargain", "viewing"];
  list.querySelectorAll("li").forEach((li) => (li.className = phaseClass(c, li.dataset.phase, order)));
}

export function showLearned(c, learned, viewing) {
  c.learned = learned;
  c.viewing = viewing;
  const slot = q(c, "[data-learned]");
  if (slot) slot.innerHTML = learnedMarkup(c);
  setPhase(c, c.phase);
}

function q(c, sel) {
  return c.root?.isConnected ? c.root.querySelector(sel) : null;
}

export function addLine(c, line) {
  c.lines.push(line);
  q(c, ".typing")?.remove();
  const t = q(c, "[data-transcript]");
  if (!t) return null;
  const node = el(lineMarkup(line));
  t.append(node);
  t.scrollTop = t.scrollHeight;
  return node;
}

export function addGuard(c, text) {
  c.guards.push(text);
  const t = q(c, "[data-transcript]");
  if (!t) return;
  t.append(el(html`<div class="guard">Рамки клиента: ${text}</div>`));
  t.scrollTop = t.scrollHeight;
}

export function setTyping(c, who) {
  c.typing = who;
  q(c, ".typing")?.remove();
  const t = q(c, "[data-transcript]");
  if (!t || !who) return;
  t.append(el(html`<p class="typing">${who === "agent" ? "Агент подбирает ответ…" : "Продавец отвечает…"}</p>`));
  t.scrollTop = t.scrollHeight;
}

export function setStatus(c, text, ended = false) {
  const s = q(c, "[data-status]");
  if (s) s.textContent = text;
  if (ended) q(c, ".call-status")?.classList.add("is-ended");
}

export function updatePrices(c) {
  if (!c.root?.isConnected) return;
  const s = scale(c);
  const ms = q(c, "[data-m-seller]");
  const ma = q(c, "[data-m-agent]");
  ms.style.left = `${s.x(c.ask)}%`;
  q(c, "[data-m-seller-label]").textContent = `продавец ${rub(c.ask)}`;
  if (c.offer) {
    ma.style.left = `${s.x(c.offer)}%`;
    ma.style.opacity = 1;
    q(c, "[data-m-agent-label]").textContent = `агент ${rub(c.offer)}`;
  }
  const pill = q(c, "[data-savings]");
  pill.textContent = savingsText(c);
  pill.classList.toggle("is-zero", c.base - c.ask <= 0);
  tweenPrice(c, c.ask);
}

function tweenPrice(c, to) {
  const node = q(c, "[data-price]");
  const from = c.shownAsk;
  c.shownAsk = to;
  if (!node || from === to || reduceMotion) { if (node) node.textContent = rub(to); return; }
  const t0 = performance.now();
  const dur = 900;
  const frame = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    node.textContent = rub(Math.round((from + (to - from) * e) / 1000) * 1000);
    if (k < 1) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

export function showSellerInput(c, onSubmit) {
  c.awaitingSeller = true;
  setTyping(c, null);
  const slot = q(c, "[data-seller-slot]");
  if (!slot) return;
  slot.innerHTML = "";
  const form = el(html`
    <form class="seller-input">
      ${html.raw(mic.supported || recorder.supported ? html`<button type="button" class="mic" title="Ответить голосом" aria-label="Ответить голосом"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z" fill="currentColor"/></svg></button>` : "")}
      <input type="text" placeholder="Вы — продавец. Ответьте агенту, например: «8 900 000, ниже не могу»" aria-label="Ответ продавца" autocomplete="off">
      <button type="submit" class="btn btn-primary btn-small">Ответить</button>
    </form>`);
  const input = form.querySelector("input");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    c.awaitingSeller = false;
    slot.innerHTML = "";
    onSubmit(text);
  });
  const micBtn = form.querySelector(".mic");
  if (micBtn) {
    bindMic(micBtn, input, {
      onFinal: () => form.requestSubmit(),
      onError: (message) => {
        form.querySelector(".mic-error")?.remove();
        form.append(el(html`<p class="mic-error">${message}</p>`));
      },
    });
  }
  slot.append(form);
  input.focus();
}
