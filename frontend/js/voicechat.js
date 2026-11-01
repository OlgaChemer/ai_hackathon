// Голосовой режим: разговор с агентом без кнопок. Решения на карточках и отметки в подборке — тоже голосом.
import { $, addMsg, chatLog, config, stage, state } from "./state.js";
import { Talk } from "./talk.js";
import { voice } from "./voice.js";

let handlers = { sendText: async () => {}, selectionAction: async () => {}, setVote: () => {}, submitSeller: async () => {} };

/** Подключить кнопки голосового режима; handlers — действия приложения, которые нужны голосу. */
export function initVoiceChat(h) {
  handlers = h;
  $("talkBtn").addEventListener("click", () => (talk.on ? stopTalk() : startTalk()));
  $("talkEnd").addEventListener("click", stopTalk);
  $("talkOrb").addEventListener("click", () => {
    if (talk.busy) talk.interrupt();
    else talk.capture?.stop(); // договорил — отправить сразу, не дожидаясь паузы
  });
}

export const talk = new Talk({
  onText: (text) => routeVoice(text),
  canListen: () => {
    if (state.phone?.session && !state.phone.ended) return false; // идёт звонок на телефон
    const c = state.call;
    if (c?.started && !c.ended) return c.mode === "human" && c.awaitingSeller; // в звонке слушаем только «продавца»
    return true;
  },
  onState: (st, detail) => renderTalk(st, detail),
  onLevel: (rms) => $("talkOrb").style.setProperty("--lvl", Math.min(1, rms * 14).toFixed(2)),
});

export const phoneActive = () => Boolean(state.phone?.session && !state.phone.ended);

export function sayPrompt(text) {
  state.lastPrompt = text;
  if (talk.on && !phoneActive()) talk.say(text);
}

export function voiceHint(ev) {
  return {
    red_lines: "Скажите «да», если всё верно, или «изменить».",
    finance: ev.price ? "Скажите «посчитай» или «пропустить»." : "Скажите «посчитай» — цифры можно поправить на карточке.",
    payment: "Скажите: наличными, ипотека и взнос есть — или копим на взнос.",
    family: "Заполните семью на карточке и скажите «подобрать» — или «пропустить».",
    housing: "Скажите: «и там, и там», «только новостройки» или «только вторичка».",
    call: config.phoneReady
      ? "Скажите: «позвони мне», «продавца сыграет ИИ», «продавец я» или «пропустить»."
      : "Скажите: «продавца сыграет ИИ», «продавец я» или «пропустить».",
    price: "Подтверждаете? Скажите «да» или «отклонить».",
    object: "Скажите «беру» или «ищем дальше».",
    sign: "Скажите «подписать» или «отложить».",
  }[ev.kind] || "Скажите «да» или «нет».";
}

const norm = (t) => t.toLowerCase().replace(/ё/g, "е").replace(/[.,!?…«»"]/g, " ").replace(/\s+/g, " ").trim();
const YES = /^(да|ага|угу|давай|хорошо|ладно|ок|окей|конечно|верно|все верно|поехали|подтвер|согласен|согласна|годится|подходит)/;
const NO = /^(нет|не надо|не нужно|не хочу|отмена|отмени)/;

// какую кнопку карточки нажать по фразе: [образец, часть надписи кнопки или её номер]
const VOICE_PICK = {
  red_lines: [[/измен|друг/, "Изменить"], [/подтвер/, "Подтвердить"], [NO, "Изменить"], [YES, "Подтвердить"]],
  finance: [[/пропус/, "Пропустить"], [NO, "Пропустить"], [/посчит|рассчит|бюджет|стратег/, 0], [YES, 0]],
  payment: [[/налич|вся сумма|целиком|сразу/, "Наличными"], [/коп|не хватает|нет взноса|накоп/, "копим"], [/ипотек|хватает|взнос есть|есть взнос/, "хватает"]],
  family: [[/пропус|не надо|^нет/, "Пропустить"], [/подбер|подобр|посчит|готово|заполн/, "Подобрать"], [YES, "Подобрать"]],
  housing: [[/там и там|обе|оба|везде|и то и|и новостр\w* и втор|и втор\w* и новостр/, "Новостройки и вторичка"],
            [/втор/, "Только вторичка"], [/новостро|первичк|застройщ/, "Только новостройки"]],
  call: [
    [/пропус|не звони|друг(ой|ую|ая)/, "Пропустить"],
    [/продавец (я|буду)|я продавец|я сыграю|я сам|сам буду|в чате/, "продавец я"],
    [/(^|\s)ии(\s|$)|нейросет|искусствен|робот|сыгра|играет|автомат/, "играет ИИ"],
    [/телефон|позвони мне|мне позвони|на мой|набери/, "Позвонить мне"],
    [YES, 0],
  ],
  price: [[/не подтвер|отклон|дорого|дальше|^нет/, "Отклонить"], [/подтвер|беру|соглас/, "Подтвердить"], [YES, "Подтвердить"]],
  object: [[/дальше|не (беру|нрав|подход)|^нет/, "Ищем дальше"], [/беру|выбира|эту/, "Выбираю"], [YES, "Выбираю"]],
  sign: [[/отлож|позже|потом|^нет/, "Отложить"], [/подпис/, "Подписать"], [YES, "Подписать"]],
};

function voiceButton(card, t) {
  const kind = [...card.classList].find((c) => c.startsWith("confirm-"))?.slice(8);
  const buttons = [...card.querySelectorAll(".actions button:not(:disabled)")];
  const rules = VOICE_PICK[kind] || [[NO, 1], [YES, 0]];
  for (const [re, target] of rules) {
    if (!re.test(t)) continue;
    const b = typeof target === "number" ? buttons[target] : buttons.find((x) => x.textContent.includes(target));
    if (b) return b;
  }
  return null;
}

const ORDINALS = [["перв", 1], ["втор", 2], ["трет", 3], ["четв", 4], ["пят", 5], ["шест", 6], ["седьм", 7], ["восьм", 8], ["девят", 9], ["десят", 10]];

// «вторая нравится», «первая и третья не то» -> отметки в подборке
function voteByVoice(t) {
  const cards = state.results[4]?.cards || [];
  const down = /не то|не нрав|не подход|не хоч|убер|минус|плох|мимо/.test(t);
  const up = !down && /нрав|подход|отлич|хорош|лайк|плюс|беру|отмет|интерес|супер|класс/.test(t);
  if (!up && !down) return false;
  const nums = new Set();
  for (const [stem, n] of ORDINALS) if (new RegExp(`(^|\\s)${stem}\\w*`).test(t)) nums.add(n);
  for (const m of t.matchAll(/(?:номер|№)\s*(\d+)/g)) nums.add(Number(m[1]));
  const ids = [...nums].map((n) => cards[n - 1]?.id).filter(Boolean);
  ids.forEach((id) => handlers.setVote(id, up ? "up" : "down", false));
  if (ids.length) talk.say(up ? "Отметил." : "Убрал.");
  return ids.length > 0;
}

async function routeVoice(text) {
  const t = norm(text);
  if (/^(стоп|хватит|выключи (голос|микрофон)|заверши(ть)? разговор)/.test(t)) return stopTalk();
  const c = state.call;
  if (c?.started && !c.ended && c.mode === "human" && c.awaitingSeller) {
    c.awaitingSeller = false;
    c.root?.querySelector("[data-seller-slot]")?.replaceChildren();
    return handlers.submitSeller(text);
  }
  const card = [...chatLog.querySelectorAll(".confirm:not(.is-done):not(.confirm-clarify)")].pop();
  if (card && card.querySelector(".actions button:not(:disabled)")) {
    const b = voiceButton(card, t);
    if (!b) return talk.say(`Не понял. ${card.dataset.hint || ""}`);
    card.dataset.said = text; // фраза придёт в ленту вместе с решением
    b.click();
    return;
  }
  if (state.awaiting === "selection") {
    if (voteByVoice(t)) return addMsg("user", text);
    if (/звони|позвони|созвон|продавц|поехали/.test(t)) return handlers.selectionAction("proceed", text);
  }
  return handlers.sendText(text);
}

const TALK_TEXT = {
  listening: ["Слушаю", "Говорите — отвечу, когда замолчите"],
  hearing: ["Слышу вас…", "Договорите — я подожду паузу"],
  processing: ["Распознаю…", ""],
  thinking: ["Агент думает…", "Ответит через пару секунд"],
  speaking: ["Агент говорит", "Нажмите на кружок, чтобы перебить"],
  waiting: ["Идёт звонок", "Продолжим, когда звонок закончится"],
};

function renderTalk(st, detail = "") {
  const panel = $("talkPanel");
  panel.dataset.state = st;
  const [title, sub] = TALK_TEXT[st] || ["", ""];
  $("talkState").textContent = title;
  $("talkHint").textContent = detail || (st === "listening" && state.running ? "Можно ответить или добавить пожелание" : sub);
  if (st !== "listening" && st !== "hearing") $("talkOrb").style.setProperty("--lvl", "0");
}

async function startTalk() {
  voice.stop();
  try {
    await talk.start();
  } catch (e) {
    addMsg("error", e.message);
    return;
  }
  $("composer").hidden = true;
  $("talkPanel").hidden = false;
  $("talkBtn").setAttribute("aria-pressed", "true");
  const card = [...chatLog.querySelectorAll(".confirm:not(.is-done)")].pop();
  if (card && state.lastPrompt) talk.say(state.lastPrompt);
  else if (state.awaiting === "selection") talk.say(SELECTION_PROMPT);
  else if (!state.running) talk.say("Слушаю. Расскажите, какую квартиру ищете: город, бюджет, комнаты и что для вас важно.");
  else talk.say("Слушаю.");
}

function stopTalk() {
  talk.stop();
  $("composer").hidden = false;
  $("talkPanel").hidden = true;
  $("talkBtn").setAttribute("aria-pressed", "false");
}

export const SELECTION_PROMPT = "Подборка на экране. Скажите, что нравится — например, «вторая нравится» или «третья не то», "
  + "— или добавьте пожелание. Когда будете готовы, скажите «звони».";
