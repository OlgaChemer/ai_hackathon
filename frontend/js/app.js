// Контроллер: лента событий агента -> чат, степпер, экраны этапов; жизнь диалога (открыть, продолжить, новый).
import { initAccount } from "./account.js";
import { api } from "./api.js";
import { addGuard, addLine, createCall, renderCall, setPhase, setStatus, setTyping, showLearned, showSellerInput, updatePrices } from "./call.js";
import { confirmCard, initCards, resolved, staleCards } from "./cards.js";
import { savingsChart } from "./chart.js";
import { initChats, refreshChats } from "./chats.js";
import { mountEconomics } from "./economics.js";
import { el, html, sleep } from "./format.js";
import { mountMap } from "./map.js";
import { $, addMsg, chatLog, config, resetState, stage, state } from "./state.js";
import * as V from "./views.js";
import { SELECTION_PROMPT, initVoiceChat, phoneActive, sayPrompt, talk } from "./voicechat.js";
import { bindMic, recorder, voice } from "./voice.js";

const STEPS = ["Запрос", "Финансы", "Поиск", "Подбор", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог"];
const stepper = $("stepper");

let gen = 0; // поколение диалога: события старого диалога отбрасываются
let queue = [];
let pumping = false;
const HANGUP = "(положил трубку)"; // сервер понимает это как конец разговора

// ------------------------------------------------------------------ чат

// ------------------------------------------------------------------ голосовой режим

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
    default: return V.welcome(config.cities);
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
    const map = stage.querySelector("[data-map]");
    if (map && state.results[4]?.cards) mountMap(map, state.results[4].cards);
    syncSelectionButtons();
  }
  renderStepper();
}

/** Кнопки подборки активны, только пока агент ждёт выбора. */
function syncSelectionButtons() {
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = state.awaiting !== "selection"));
}

function show(n) {
  if (state.view !== n) {
    state.view = n;
    renderStage();
  } else renderStepper();
}

// ------------------------------------------------------------------ настоящий звонок (МТС Exolve)

async function speakPhone(text) {
  try {
    await state.phone.say(text);
  } catch (e) {
    addMsg("error", `Агент не смог произнести фразу: ${e.message}`);
  }
}

async function listenPhone(call, myGen) {
  const phone = state.phone;
  if (!phone || phone.ended) return submitSeller(HANGUP);
  const r = await phone.listen((st) => {
    if (gen !== myGen) return;
    setTyping(call, st === "listening" ? "seller" : "agent");
    setStatus(call, st === "listening" ? "Идёт звонок · слушаю продавца" : "Идёт звонок · распознаю ответ");
  });
  if (gen !== myGen || call.ended) return;
  setStatus(call, "Идёт звонок · запись");
  if (r.text) return submitSeller(r.text);
  if (r.ended) return submitSeller(HANGUP);
  // не распознали — даём ответить текстом, разговор не теряется
  addMsg("error", `${r.error} Впишите ответ продавца вручную.`);
  showSellerInput(call, submitSeller);
}

// ------------------------------------------------------------------ события агента

async function handle(ev, myGen) {
  const call = state.call;
  const quiet = Boolean(ev.replay); // история диалога: рисуем сразу, без пауз, голоса и звонка
  const pause = (ms) => (quiet ? null : sleep(ms));
  if (ev.seq) state.lastSeq = Math.max(state.lastSeq, ev.seq);
  switch (ev.type) {
    case "provider": setProvider(ev); break;
    case "message":
      addMsg(ev.role, ev.text);
      if (!quiet && ev.role === "agent" && talk.on && !phoneActive()) talk.say(ev.text);
      await pause(ev.role === "agent" ? 300 : 0);
      break;
    case "step":
      state.status[ev.step] = ev.status === "pending" ? "active" : ev.status;
      if (quiet) { if (ev.status === "active") state.view = ev.step; break; } // экран нарисуем один раз в конце истории
      if (ev.status === "active") show(ev.step); else renderStepper();
      await pause(150);
      break;
    case "result":
      state.results[ev.step] = ev.data;
      if (ev.kind === "finance") state.strategy = ev.data.finance?.risk || null;
      if (ev.kind === "selection") {
        state.marks = { liked: new Set(ev.data.liked || []), disliked: new Set(ev.data.disliked || []) };
      }
      state.view = ev.step;
      if (!quiet) renderStage();
      await pause(ev.kind === "selection" ? 0 : 500);
      break;
    case "confirm":
      if (ev.kind === "call") {
        state.call = createCall(ev);
        state.view = 6;
        if (!quiet) renderStage();
      }
      if (ev.kind !== "clarify") state.awaiting = ev.kind === "selection" ? "selection" : null;
      if (ev.kind === "selection") {
        state.cards.selection = true;
        syncSelectionButtons();
        if (!quiet) sayPrompt(SELECTION_PROMPT);
      }
      confirmCard(ev);
      break;
    case "resolved": resolved(ev); break;
    case "product_opened":
      state.opened[ev.product] = ev.amount;
      if (!quiet && state.view === 2) renderStage();
      break;
    case "call_start":
      if (!call) break;
      call.started = true;
      call.mode = ev.mode;
      if (quiet) { state.view = 6; break; }
      if (state.view !== 6) show(6); else renderStage();
      setStatus(call, "Идёт звонок · запись");
      setTyping(call, "agent");
      break;
    case "call_phase": if (call) setPhase(call, ev.phase); break;
    case "turn": {
      if (!call) break;
      if (ev.speaker === "seller" && ev.ask) call.ask = ev.ask;
      if (ev.speaker === "agent" && ev.offer) call.offer = ev.offer;
      call.awaitingSeller = false;
      const node = addLine(call, ev);
      updatePrices(call);
      if (quiet) break;
      node?.classList.add("is-speaking");
      const t0 = performance.now();
      if (call.mode === "phone") {
        if (ev.speaker === "agent" && state.phone && !state.phone.ended) await speakPhone(ev.text);
        else await sleep(300);
      } else {
        // в голосовом режиме звонок звучит всегда
        await voice.speak(ev.text, ev.speaker, talk.on);
        if (!voice.enabled && !talk.on) await sleep(Math.min(3000, 800 + ev.text.length * 16));
        else await sleep(Math.max(0, 300 - (performance.now() - t0)));
      }
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
    case "await_seller":
      if (!call) break;
      if (quiet) { call.awaitingSeller = true; break; } // поле ответа покажем, если агент и сейчас ждёт (replay_end)
      if (call.mode === "phone") await listenPhone(call, myGen);
      else showSellerInput(call, submitSeller);
      break;
    case "call_end":
      if (!call) break;
      call.ended = true;
      call.awaitingSeller = false;
      call.status = ev.status;
      setTyping(call, null);
      setStatus(call, ev.status === "agreed" ? "Звонок завершён · договорились" : "Звонок завершён · ждём вашего решения", true);
      showLearned(call, ev.learned, ev.viewing);
      if (!quiet && state.phone) { state.phone.hangup(); state.phone = null; }
      break;
    case "done": // сделка подписана; агент остаётся на связи — можно спрашивать дальше
      addMsg("note", "Сделка завершена. Можно задать вопрос по ней или начать новый диалог.");
      if (!quiet) refreshChats();
      break;
    case "typing": // агент думает (сигнал без номера, в истории его нет)
      showThinking(ev.on);
      break;
    case "error":
      addMsg("error", ev.text);
      if (quiet) break;
      if (talk.on) talk.say(`Ошибка: ${ev.text}`);
      finish();
      break;
    case "replay_end":
      // история показана; дальше — живые события, если агент работает
      state.running = ev.live;
      state.lastSeq = Math.max(state.lastSeq, ev.seq || 0);
      renderStage(); // экран этапа — один раз, по итогам истории
      if (!ev.live) {
        staleCards();
        state.es?.close();
      } else if (call?.awaitingSeller && !call.ended && call.mode === "human") {
        showSellerInput(call, submitSeller);
      }
      setComposer();
      refreshChats();
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
  try {
    const r = await api.seller(state.sid, text);
    if (r?.blocked) { // реплику отклонил фильтр (попытка управлять агентом) — агент ждёт ответ продавца дальше
      setTyping(state.call, null);
      showSellerInput(state.call, submitSeller);
    }
  } catch (e) {
    addMsg("error", `Ответ продавца не отправлен: ${e.message}`);
    setTyping(state.call, null);
    showSellerInput(state.call, submitSeller);
  }
}

/** Агент остановился (ошибка): лента закрыта, сообщение поднимет его снова. */
function finish() {
  state.running = false;
  state.streamEnded = true;
  state.es?.close();
  showThinking(false);
  setComposer();
  refreshChats();
}

/** «Агент думает…» внизу чата, пока модель решает, что делать. */
function showThinking(on) {
  chatLog.querySelector(".msg-thinking")?.remove();
  if (!on) return;
  chatLog.append(el(html`<div class="msg msg-thinking" aria-label="Агент думает"><span></span><span></span><span></span></div>`));
  chatLog.scrollTop = chatLog.scrollHeight;
}

// ------------------------------------------------------------------ запуск и сообщения

function setComposer() {
  const inChat = Boolean(state.sid);
  $("sendBtn").textContent = inChat ? "Отправить" : "Поручить агенту";
  $("query").placeholder = state.running
    ? "Ответ агенту или пожелание: «кухня побольше», «не первый этаж»…"
    : inChat ? "Продолжить диалог: «а если трёшку?», «покажи дешевле»…" : "Например: ищу 2-комнатную в Казани до 9 млн рядом со школой";
  $("resetBtn").hidden = !inChat;
}

/** Закрыть текущий диалог на экране: звук, звонок, поток событий. */
function leaveChat() {
  state.es?.close();
  state.phone?.hangup();
  voice.stop();
  talk.interrupt();
  gen += 1;
  queue = [];
  resetState();
  chatLog.innerHTML = "";
  renderStage();
}

/** Лента диалога: история после after, затем живые события. Связь оборвалась — переподключаемся с того же места. */
function openStream(id, after) {
  state.es?.close();
  state.streamEnded = false;
  const myGen = gen;
  const es = api.chatStream(id, after);
  state.es = es;
  es.onmessage = (m) => {
    if (gen !== myGen) return;
    const ev = JSON.parse(m.data);
    state.retries = 0;
    if (ev.type === "error" && !ev.replay) state.streamEnded = true;
    enqueue(ev);
  };
  es.onerror = () => {
    es.close();
    if (gen !== myGen || !state.running || state.streamEnded) return;
    if (state.retries++ < 5) {
      setTimeout(() => { if (gen === myGen && state.running) openStream(id, state.lastSeq); }, 1500 * state.retries);
    } else {
      addMsg("error", "Связь с агентом прервалась. Агент продолжает работу — откройте диалог позже из списка «Диалоги».");
      state.running = false;
      setComposer();
    }
  };
}

function setHash(id) {
  const want = id ? `#chat-${id}` : "";
  if (location.hash !== want) history.replaceState(null, "", want || location.pathname + location.search);
}

async function start(query) {
  if (!query.trim()) return;
  leaveChat();
  $("query").value = "";
  try {
    const { id } = await api.start(query);
    state.sid = id;
    state.running = true;
    setHash(id);
    setComposer();
    openStream(id, 0);
  } catch (e) {
    addMsg("error", `Агент не запустился: ${e.message}`);
  }
}

/** Открыть диалог из списка или по ссылке #chat-<id>. */
async function openChat(id) {
  if (!id || id === state.sid) return;
  leaveChat();
  state.sid = id;
  state.running = true; // до replay_end: он скажет, работает ли агент
  setHash(id);
  setComposer();
  openStream(id, 0);
}

/** Новый диалог: пустой экран, приветствие. */
function newChat() {
  leaveChat();
  setHash(null);
  addMsg("agent", "Опишите, какую квартиру ищете: город, бюджет, комнаты и что для вас важно.");
  setComposer();
  refreshChats();
}


async function sendText(text) {
  text = text.trim();
  if (!text) return;
  if (!state.sid) return start(text);
  $("query").value = "";
  const marks = { liked: [...state.marks.liked], disliked: [...state.marks.disliked] };
  try {
    const r = await api.chatMessage(state.sid, text, marks);
    if (r.continued) {
      // агент уже закончил — продолжает этот же диалог новым запуском: подключаемся к ленте с того же места
      state.running = true;
      state.cards = {};
      setComposer();
      openStream(state.sid, state.lastSeq);
      return;
    }
    // вопрос агенту («что такое СБР?») или отклонённое фильтром сообщение карточку не закрывают:
    // подборка и подтверждение ждут дальше, их кнопки должны работать
    if (r.handled_as === "question" || r.handled_as === "blocked") return;
    if (state.awaiting === "clarify") {
      chatLog.querySelectorAll(".confirm-clarify:not(.is-done)").forEach((c) => c.classList.add("is-done"));
    }
    state.awaiting = null;
  } catch (e) {
    addMsg("error", `Сообщение не отправлено: ${e.message}`);
  }
}

/** said — фраза, если решение сказано голосом (попадёт в историю диалога). */
async function selectionAction(action, said = null) {
  if (state.awaiting !== "selection") return;
  if (action === "proceed" && !state.marks.liked.size) {
    if (said) talk.say("Вы ничего не отметили — позвоню по трём лучшим вариантам.");
    else if (!confirm("Вы ничего не отметили. Позвонить по трём лучшим вариантам?")) return;
  }
  state.awaiting = null;
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
  const label = action === "proceed" ? "звонить по отмеченным" : "подобрать ещё";
  try {
    await api.confirm(state.sid, "selection", true, {
      action, liked: [...state.marks.liked], disliked: [...state.marks.disliked], _label: label, ...(said ? { _said: said } : {}),
    });
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
$("resetBtn").addEventListener("click", newChat);

bindMic($("micBtn"), $("query"), {
  onFinal: (text) => sendText(text),
  onError: (message) => addMsg("error", message),
});
$("micBtn").hidden = false;

stage.addEventListener("click", async (e) => {
  const ex = e.target.closest("[data-example]");
  if (ex) return start(ex.dataset.example);

  const vote = e.target.closest("[data-vote]");
  if (vote) return setVote(vote.closest("[data-id]").dataset.id, vote.dataset.vote, true);
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
/** Отметка «нравится / не то». toggle — повторное нажатие снимает отметку (клик); голосом — просто ставим. */
function setVote(id, dir, toggle) {
  const { liked, disliked } = state.marks;
  const up = dir === "up";
  const set = up ? liked : disliked;
  const other = up ? disliked : liked;
  if (toggle && set.has(id)) set.delete(id); else { set.add(id); other.delete(id); }
  const card = stage.querySelector(`.offer[data-id="${CSS.escape(id)}"]`);
  if (card) {
    card.classList.toggle("is-liked", liked.has(id));
    card.classList.toggle("is-disliked", disliked.has(id));
    card.querySelector('[data-vote="up"]')?.setAttribute("aria-pressed", String(liked.has(id)));
    card.querySelector('[data-vote="down"]')?.setAttribute("aria-pressed", String(disliked.has(id)));
  }
  const counter = stage.querySelector("[data-liked-count]");
  if (counter) counter.textContent = `(${liked.size})`;
}

initCards({ sendText });
initVoiceChat({ sendText, selectionAction, setVote, submitSeller });

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

const fromHash = () => location.hash.match(/^#chat-([0-9a-f]{12})$/)?.[1];
window.addEventListener("hashchange", () => { const id = fromHash(); if (id) openChat(id); });
initChats({ open: openChat, create: newChat, current: () => state.sid });
initAccount({ onChange: () => refreshChats() });
if (fromHash()) openChat(fromHash());
else {
  addMsg("agent", "Здравствуйте! Я агент покупки квартиры. Расскажите текстом или голосом, что ищете: город, бюджет, комнаты и что для вас важно.");
  setComposer();
  renderStage();
}
api.health().then((h) => {
  config.cities = { featured: h.featured_cities || [], total: (h.cities || []).length };
  recorder.available = Boolean(h.stt?.installed && h.stt.status !== "error");
  config.phoneReady = Boolean(h.phone?.configured);
  config.sellerPhoneDefault = h.phone?.seller_phone || "";
  setProvider(h);
  if (state.view === 0) renderStage();
})
  .catch(() => setProvider({ provider: "offline", reason: "сервер недоступен" }));
