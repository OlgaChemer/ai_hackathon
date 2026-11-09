// Контроллер: события агента, результаты в чате, подборка и жизнь диалога.
import { initAccount } from "./account.js";
import { api } from "./api.js";
import { addGuard, addLine, createCall, renderCall, setPhase, setStatus, setTyping, showLearned, showSellerInput, updatePrices } from "./call.js";
import { confirmCard, initCards, resolved, staleCards, refreshPhoneAccess } from "./cards.js";
import { savingsChart } from "./chart.js";
import { initChats, refreshChats } from "./chats.js";
import { el, html, sleep } from "./format.js";
import { initShell, navigate, showAgent, showPage, syncShell, refreshContext, setChatExpanded } from "./shell.js";
import { mountMap, unmountMap, focusMap } from "./map.js";
import { $, addMsg, chatLog, config, resetState, revealInChat, scrollChatToEnd, stage, startTurn, state } from "./state.js";
import * as V from "./views.js";
import { SELECTION_PROMPT, initVoiceChat, phoneActive, sayPrompt, talk } from "./voicechat.js";
import { bindMic, recorder, voice } from "./voice.js";
import { EffectQueue } from './effects.js';
import { initWorkshop } from './workshop.js';
initWorkshop();

let gen = 0; // поколение диалога: события старого диалога отбрасываются
let queue = [];
let pumping = false;
const effects = new EffectQueue(128, () => { voice.stop(); state.phone?.hangup(); });
const HANGUP = "(положил трубку)"; // сервер понимает это как конец разговора

// ------------------------------------------------------------------ результаты и подборка

function viewFor(n) {
  const r = state.results;
  switch (n) {
    case 1: return V.profile(r[1]);
    case 2: return V.finance(r[2], state.opened, state.strategy);
    case 5: return V.legal(r[5]);
    case 7: return V.viewing(r[7]);
    case 8: return V.deal(r[8]);
    case 9: return V.summary(r[9]);
    default: return null;
  }
}

function renderStage() {
  unmountMap(stage.querySelector("[data-map]"));
  stage.innerHTML = state.results[4] ? V.selection(state.results[4], state.marks, state.selectionPage)
    : state.searching ? ""
    : V.waiting("Здесь появятся квартиры", "Расскажите агенту о бюджете, городе и пожеланиях. Профиль и решения останутся в чате.");
  if (state.selectionNotice) stage.prepend(el(html`<p class="hint" role="status">${state.selectionNotice}</p>`));
  if (state.searching) {
    stage.setAttribute("aria-busy", "true");
    stage.prepend(el(html`<div class="search-progress" role="status"><span class="search-spinner" aria-hidden="true"></span><span data-search-progress>${state.searching}</span></div>`));
  } else stage.removeAttribute("aria-busy");
  const map = stage.querySelector("[data-map]");
  if (map && state.results[4]?.cards) mountMap(map, V.selectionPage(state.results[4].cards, state.selectionPage).cards);
  syncSelectionButtons();
}

function renderChatResult(step) {
  if (!state.results[step]) return;
  const markup = viewFor(step);
  if (!markup) return;
  let node = chatLog.querySelector(`[data-chat-result="${step}"]`);
  if (!node) { node = document.createElement("div"); node.dataset.chatResult = step; node.className = "chat-result"; chatLog.append(node); }
  node.innerHTML = markup;
  const chart = node.querySelector("[data-chart]");
  if (chart && state.results[2]?.finance) savingsChart(chart, state.results[2].finance, chart.dataset.selected);
  revealInChat(node);
}

/** Кнопки подборки активны, только пока агент ждёт выбора. */
function syncSelectionButtons() {
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = state.awaiting !== "selection" ||
    (b.dataset.selection === 'proceed' && !state.results[4]?.cards?.length)));
  stage.querySelectorAll("[data-choose]").forEach((b) => (b.disabled = state.awaiting !== "selection"));
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
  if (ev.seq && ev.type !== "replay_end" && ev.seq <= state.lastSeq) return;
  const call = state.call;
  const quiet = Boolean(ev.replay); // история диалога: рисуем сразу, без пауз, голоса и звонка
  if (ev.seq) state.lastSeq = Math.max(state.lastSeq, ev.seq);
  switch (ev.type) {
    case "provider": setProvider(ev); break;
    case "search_progress":
      state.searching = ev.on ? ev.text || "Ищу и сравниваю квартиры…" : null;
      if (!quiet) {
        const progress = stage.querySelector('[data-search-progress]');
        if (progress && state.searching) progress.textContent = state.searching;
        else renderStage();
      }
      break;
    case "selection_notice":
      state.selectionNotice = ev.text;
      if (!quiet) renderStage();
      break;
    case "message":
      addMsg(ev.role, ev.text);
      if (ev.await_reply) setChatExpanded(true);
      if (!quiet && ev.role === "agent" && talk.on && !phoneActive()) talk.say(ev.text);
      break;
    case "result":
      if (ev.kind === 'selection') state.searching = null;
      if (ev.kind === 'selection' && !ev.data.cards?.length && state.results[4]?.cards?.length) {
        state.selectionNotice = 'Последняя команда не дала результатов. Сохранена предыдущая подборка; она относится к прежним условиям.';
        if (!quiet) renderStage();
        break;
      }
      if (ev.kind === 'selection') state.selectionNotice = null;
      state.results[ev.step] = ev.data;
      if (ev.kind === "profile") refreshContext(ev.data);
      if (ev.kind === 'profile') document.dispatchEvent(new CustomEvent('profilechange',{detail:{chat_id:state.sid}}));
      if (![3, 4, 6].includes(ev.step)) renderChatResult(ev.step);
      if (ev.kind === "selection") setChatExpanded(false);
      if (ev.kind === "finance") state.strategy = ev.data.finance?.risk || null;
      if (ev.kind === "selection") {
        state.selectionPage = 1;
        state.marks = { liked: new Set(ev.data.liked || []), disliked: new Set(ev.data.disliked || []) };
      }
      if (!quiet) renderStage();
      break;
    case "confirm":
      showThinking(false);
      api.decisions[ev.kind] = ev.decision_id;
      if (ev.kind !== "selection") setChatExpanded(true);
      if (ev.kind === "call") {
        state.call = createCall(ev);
        const callNode = renderCall(state.call, submitSeller);
        callNode.classList.add("chat-call");
        chatLog.append(callNode);
        if (!quiet) renderStage();
      }
      if (ev.kind !== "clarify") state.awaiting = ev.kind === "selection" ? "selection" : null;
      if (ev.kind === "selection") {
        const wasSearching = Boolean(state.searching);
        state.searching = null;
        if (wasSearching && !quiet) renderStage();
        state.cards.selection = true;
        syncSelectionButtons();
        if (!quiet) sayPrompt(SELECTION_PROMPT);
      }
      confirmCard(ev);
      break;
    case "resolved":
      if (ev.decision_id && api.decisions[ev.kind] && ev.decision_id !== api.decisions[ev.kind]) break;
      delete api.decisions[ev.kind];
      resolved(ev);
      startTurn(); // решение клиента — дальше начинается новый ответ агента
      break;
    case 'purchase_reset':
      for (const step of ev.clear_steps || []) delete state.results[step];
      for (const kind of ev.kinds || []) {
        if (api.decisions[kind]) resolved({kind,decision_id:api.decisions[kind],approved:null,label:'Условия изменились'});
        delete api.decisions[kind];
      }
      if ((ev.kinds || []).includes('call') && state.call) {
        state.call.ended = true;
        setTyping(state.call,null);
        if (!quiet && state.phone) { state.phone.hangup(); state.phone = null; }
      }
      addMsg('note',ev.text);
      if (!quiet) renderStage();
      break;
    case "product_opened":
      state.opened[ev.product] = ev.amount;
      if (!quiet && state.results[2]) renderChatResult(2);
      break;
    case "call_start":
      if (!call) break;
      call.started = true;
      call.mode = ev.mode;
      if (quiet) break;
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
      effects.enqueue(async () => {
      if (gen !== myGen) return;
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
      });
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
      if (call.mode === "phone") effects.enqueue(async () => { if (gen === myGen) await listenPhone(call,myGen); });
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
      if (!quiet && state.phone) {
        const phone = state.phone;
        effects.enqueue(() => { phone.hangup(); if (state.phone === phone) state.phone = null; });
      }
      break;
    case "done": // сделка подписана; агент остаётся на связи — можно спрашивать дальше
      addMsg("note", "Демонстрационный план покупки подтверждён. Можно задать вопрос по нему или начать новый диалог.");
      if (!quiet) refreshChats();
      break;
    case "typing": // агент думает (сигнал без номера, в истории его нет)
      showThinking(ev.on);
      break;
    case 'resync_required':
      openStream(state.sid,state.lastSeq);
      break;
    case "model_wait":
      showThinking(ev.on, "Модель занята, жду очередь…");
      if (ev.on) setChatExpanded(true);
      break;
    case "error":
      state.searching = null;
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
      scrollChatToEnd();
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
  state.searching = null;
  renderStage();
  setComposer();
  refreshChats();
}

/** «Агент думает…» внизу чата, пока модель решает, что делать. */
function showThinking(on, text = "") {
  chatLog.querySelector(".msg-thinking")?.remove();
  if (!on) return;
  const node = el(text
    ? html`<div class="msg msg-thinking" role="status">${text}</div>`
    : html`<div class="msg msg-thinking" aria-label="Агент думает"><span></span><span></span><span></span></div>`);
  chatLog.append(node);
  revealInChat(node);
}

// ------------------------------------------------------------------ запуск и сообщения

function setComposer() {
  const inChat = Boolean(state.sid);
  $("sendBtn").textContent = inChat ? "Отправить" : "Поручить агенту";
  $("query").placeholder = state.running
    ? "Ответ агенту или пожелание: «кухня побольше», «не первый этаж»…"
    : inChat ? "Продолжить диалог: «а если трёшку?», «покажи дешевле»…" : "Например: ищу 2-комнатную в Казани до 9 млн рядом со школой";
  $("resetBtn").hidden = !inChat;
  syncShell();
}

/** Закрыть текущий диалог на экране: звук, звонок, поток событий. */
function leaveChat() {
  state.es?.close();
  state.phone?.hangup();
  voice.stop();
  talk.interrupt();
  gen += 1;
  effects.reset();
  queue = [];
  resetState();
  chatLog.innerHTML = "";

  document.querySelector("section.stage").classList.remove("has-decision");
  $("chatPeek").textContent = "";
  refreshContext(null);
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
    if (state.es !== es) return;
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
  const want = id ? `#chat-${id}` : "#agent";
  if (location.hash !== want) history.replaceState(null, "", want || location.pathname + location.search);
}

async function start(query) {
  showAgent();
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
    $("query").value = query;
    setComposer();
    setChatExpanded(true);
    addMsg("error", `Агент не запустился: ${e.message}`);
  }
}

/** Открыть диалог из списка или по ссылке #chat-<id>. */
async function openChat(id) {
  showAgent();
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
  navigate('agent', {fresh:true});
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
  const selection = state.results[4];
  if (Number.isInteger(selection?.selection_revision)) {
    marks.visible_listing_ids = V.selectionPage(selection.cards, state.selectionPage).cards.map(c => c.id);
    marks.selection_revision = selection.selection_revision;
  }
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
    if (["question", "blocked", "unresolved"].includes(r.handled_as)) return;
    // Актуальное ожидание задаёт только SSE confirm/resolved. Поздний HTTP-ответ
    // не должен стирать уже пришедшую карточку следующего шага.
  } catch (e) {
    addMsg("error", `Сообщение не отправлено: ${e.message}`);
  }
}

/** said — фраза, если решение сказано голосом (попадёт в историю диалога). */
async function selectionAction(action, said = null, listingId = null) {
  if (state.awaiting !== "selection") return;
  if (action === "proceed" && !state.marks.liked.size) {
    if (said) talk.say("Вы ничего не отметили — позвоню по трём лучшим вариантам.");
    else if (!confirm("Вы ничего не отметили. Позвонить по трём лучшим вариантам?")) return;
  }
  state.awaiting = null;
  syncSelectionButtons();
  const label = listingId ? 'позвонить по выбранной квартире' : action === "proceed" ? "звонить по отмеченным" : action === 'liked' ? 'показать понравившиеся' : "подобрать ещё";
  try {
    await api.confirm(state.sid, "selection", true, {
      action, liked: [...state.marks.liked], disliked: [...state.marks.disliked], _label: label, ...(said ? { _said: said } : {}),
      ...(listingId ? {listing_id: listingId} : {}),
    });
  } catch (e) {
    state.awaiting = "selection";
    syncSelectionButtons();
    addMsg("error", `Не удалось отправить отметки: ${e.message}`);
  }
}

function setProvider(p) {
  const node = $("provider");
  const names = { deepseek: "DeepSeek", gigachat: "GigaChat", offline: "Без модели (правила)" };
  const degraded = p.degraded || p.provider === "offline";
  node.textContent = p.degraded ? `${names[p.provider] || p.provider} · недоступен` : names[p.provider] || p.provider;
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

async function handleResultClick(e) {
  const favorites = e.target.closest('[data-favorites]');
  if (favorites && !favorites.disabled) {
    return state.awaiting === 'selection' ? selectionAction('liked') : sendText(`Покажи ${Math.min(30,state.marks.liked.size)} понравившихся квартир`);
  }
  const page = e.target.closest('[data-selection-page]');
  if (page && !page.disabled && state.results[4]) {
    state.selectionPage = V.selectionPage(state.results[4].cards, Number(page.dataset.selectionPage)).page;
    renderStage();
    const nav = stage.querySelector('[data-pagination="top"]');
    nav?.scrollIntoView({behavior:'instant', block:'start'});
    nav?.querySelector('[aria-current="page"]')?.focus({preventScroll:true});
    return;
  }
  const mapButton = e.target.closest('[data-show-map]');
  if (mapButton && !mapButton.disabled) {
    await focusMap(stage.querySelector('[data-map]'), mapButton.dataset.showMap);
    return;
  }
  const choose = e.target.closest("[data-choose]");
  if (choose && !choose.disabled) {
    if (state.awaiting !== "selection") {
      addMsg("agent", "Подборка ещё обновляется — кнопка «Выбрать» станет активной через пару секунд.");
      return;
    }
    setVote(choose.dataset.choose, "up", false);   // отметить как выбранную
    selectionAction("proceed", null, choose.dataset.choose);
    return;
  }
  const vote = e.target.closest("[data-vote]");
  if (vote) return setVote(vote.closest("[data-id]").dataset.id, vote.dataset.vote, true);
  const sel = e.target.closest("[data-selection]");
  if (sel) return selectionAction(sel.dataset.selection);

  const strat = e.target.closest("[data-strategy]");
  if (strat) {
    state.strategy = strat.dataset.strategy;
    return renderChatResult(2);
  }
  const open = e.target.closest("[data-open]");
  if (open && state.sid) {
    open.disabled = true;
    try { await api.product(state.sid, open.dataset.open, Number(open.dataset.amount)); }
    catch (err) { open.disabled = false; addMsg("error", `Не удалось: ${err.message}`); }
  }
 }
stage.addEventListener("click", handleResultClick);
chatLog.addEventListener("click", handleResultClick);
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
  const favorites = stage.querySelector('[data-favorites]');
  if (favorites) favorites.disabled = !liked.size;
  const favoriteCount = stage.querySelector('[data-favorites-count]');
  if (favoriteCount) favoriteCount.textContent = `(${liked.size})`;
}

initCards({ sendText });
initVoiceChat({ sendText, selectionAction, setVote, submitSeller,
  newChat: () => { newChat(); setChatExpanded(true); },
});

$("resumeCallAudio").addEventListener("click", () => {
  state.phone?.unlockAudio().catch(() => addMsg("error", "Не удалось возобновить звук. Вернитесь на страницу звонка и попробуйте ещё раз."));
});

const voiceBtn = $("voiceToggle");
const syncVoice = () => voiceBtn.setAttribute("aria-pressed", String(voice.enabled));
voiceBtn.hidden = !voice.supported;
voiceBtn.addEventListener("click", () => { voice.enabled = !voice.enabled; syncVoice(); });
syncVoice();

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    navigate(tab.dataset.tab === "economics" ? "economics" : "agent");
  }),
);

const fromHash = () => location.hash.match(/^#chat-([0-9a-f]{12})$/)?.[1];
const route = () => {
  const id = fromHash();
  if (id) { openChat(id); return; }
  const page = {'#welcome':'welcome', '#history':'history', '#economics':'economics', '#agent':'agent', '#apartments':'apartments'}[location.hash]
    || (new URLSearchParams(location.search).has('agent') ? 'agent' : 'welcome');
  showPage(page);
  if (page === 'agent') setChatExpanded(true);
};
initShell({ start });
window.addEventListener("hashchange", route);
window.addEventListener("popstate", route);
initChats({ open: openChat, create: newChat, current: () => state.sid });
initAccount({
  onChange: (why) => {
    // после выхода подбор аккаунта в этом браузере недоступен — не оставляем его на экране
    if (why === "logout" && state.sid) newChat();
    refreshChats();
  },
});
if (fromHash()) openChat(fromHash());
else {
  addMsg("agent", "Здравствуйте! Я агент покупки квартиры. Расскажите текстом или голосом, что ищете: город, бюджет, комнаты и что для вас важно.");
  setComposer();
  renderStage();
  route();
}
let healthRevision=0;
async function refreshHealth(){
 const revision=++healthRevision;
 try{
  const h=await api.health();if(revision!==healthRevision)return;
  recorder.available = Boolean(h.stt?.installed && h.stt.status !== "error");
  const changed=config.phoneReady!==Boolean(h.phone?.ready);
  config.phoneReady = Boolean(h.phone?.ready);
  if(changed)refreshPhoneAccess();
  config.sellerPhoneDefault = h.phone?.seller_phone || "";
  setProvider(h);
 }catch{
  if(revision===healthRevision){config.phoneReady=false;refreshPhoneAccess();setProvider({provider:'offline',reason:'сервер недоступен'});}
 }
}
document.addEventListener('accountchange',()=>{config.phoneReady=false;refreshPhoneAccess();refreshHealth();});
refreshHealth();
