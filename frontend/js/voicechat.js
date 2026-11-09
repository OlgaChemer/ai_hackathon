// Голосовой режим: разговор с агентом без кнопок. Решения на карточках и отметки в подборке — тоже голосом.
import { $, addMsg, config, state } from "./state.js";
import { Talk } from "./talk.js";
import { voice } from "./voice.js";
import { api } from "./api.js";

let handlers = { sendText: async () => {}, selectionAction: async () => {}, setVote: () => {}, submitSeller: async () => {} };

/** Подключить кнопки голосового режима; handlers — действия приложения, которые нужны голосу. */
export function initVoiceChat(h) {
  handlers = h;
  const choices = $("talkVoice");
  voice.watchVoices(() => {
    choices.replaceChildren(new Option("Автоматически — русский", ""), ...voice.choices.map(v => new Option(v.name, v.id)));
    choices.value = voice.choices.some(v => v.id === voice.voiceId) ? voice.voiceId : "";
    $("talkVoiceNote").textContent = voice.choices.length ? "Голоса вашего устройства" : "Используется системный голос браузера";
  });
  choices.addEventListener("change", () => { voice.voiceId = choices.value; });
  const rate = $("talkRate");
  const rateLabel = $("talkRateValue");
  rate.value = String(voice.rate);
  rateLabel.value = `${voice.rate.toLocaleString("ru-RU")}×`;
  rate.addEventListener("input", () => { voice.rate = rate.value; rateLabel.value = `${voice.rate.toLocaleString("ru-RU")}×`; });
  $("talkSample").addEventListener("click", () => {
    talk.interrupt();
    talk.say("Например, квартира стоит 42 млн ₽, а её площадь — 53,4 м². Что для вас важнее: район или площадь?");
  });
  $("talkBtn").addEventListener("click", () => (talk.on ? stopTalk() : startTalk()));
  $("welcomeTalkBtn").addEventListener("click", () => {
    handlers.newChat();
    startTalk(); // AudioContext создаётся в обработчике нажатия, до сетевых ожиданий.
  });
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
    financing: "Скажите «покупать сейчас» или «копить».",
    products: "Скажите «подключить» или «не нужно».",
  }[ev.kind] || "Скажите «да» или «нет».";
}

const norm = (t) => t.toLowerCase().replace(/ё/g, "е").replace(/[.,!?…«»"]/g, " ").replace(/\s+/g, " ").trim();
// Это только голосовые аналоги кнопок. Свободный ответ целиком отправляется агенту.
const YES = ["да", "ага", "угу", "давай", "хорошо", "ладно", "ок", "окей", "конечно", "верно", "все верно", "согласен", "согласна", "да согласен", "да все верно", "все правильно", "да правильно", "да подтверждаю", "подтверждаю"];
const NO = ["нет", "не надо", "не нужно", "не хочу", "отмена", "отмени"];
const VOICE_PICK = {
  red_lines: [["Изменить", [...NO, "изменить", "изменить сумму", "поправить лимит"]], ["Подтвердить", [...YES, "подтвердить", "подтверждаю лимит"]]],
  finance: [["Пропустить", [...NO, "пропустить"]], [0, [...YES, "посчитай", "рассчитай", "посчитать", "рассчитать стратегию"]]],
  payment: [["Наличными", ["наличными", "наличные", "наличка", "за свои", "вся сумма есть"]],
    ["копим", ["копим", "копим на взнос", "на взнос копим", "взноса нет", "нет взноса"]],
    ["хватает", ["ипотека", "в ипотеку", "взнос есть", "на взнос хватает", "ипотека и взнос есть", "ипотека взнос есть"]]],
  family: [["Пропустить", [...NO, "пропустить"]], ["Подобрать", [...YES, "подобрать", "посчитать", "готово"]]],
  housing: [["Новостройки и вторичка", ["и там и там", "обе", "оба", "везде", "и то и другое", "новостройки и вторичка", "и новостройки и вторичка"]],
    ["Только вторичка", ["вторичка", "только вторичка", "вторичку", "вторичный рынок"]],
    ["Только новостройки", ["новостройки", "новостройку", "только новостройки", "первичка", "от застройщика"]]],
  call: [["Пропустить", [...NO, "пропустить", "не звони", "пропустить объект"]],
    ["продавец я", ["продавец я", "я продавец", "я сыграю", "в чате", "я сам"]],
    ["играет ИИ", ["ии", "продавца сыграет ии", "продавца играет ии", "робот", "автоматически"]],
    ["Позвонить мне", ["позвони мне", "мне позвони", "на мой телефон", "позвонить мне"]], [0, YES]],
  price: [["Отклонить", [...NO, "отклонить", "дорого", "ищем дальше"]], ["Подтвердить", [...YES, "подтвердить", "подтверждаю цену"]]],
  object: [["Ищем дальше", [...NO, "ищем дальше", "дальше", "не подходит"]], ["Выбираю", [...YES, "беру", "выбираю", "выбираю эту квартиру"]]],
  sign: [["Отложить", [...NO, "отложить", "позже", "потом"]], ["Подписать", [...YES, "подписать", "подписываю"]]],
  financing: [["накопить", ["копить", "копим", "накопить", "сначала накопить", "подкопить"]],
    ["Звонить", ["покупать сейчас", "сейчас", "звонить", "звони", "покупаем сейчас", "звонить всё равно"]]],
  products: [["Не нужно", [...NO, "ничего не нужно"]], ["Подключить", [...YES, "подключить", "подключай", "подключить выбранное"]]],
};

function voiceButton(card, text) {
  const kind = [...card.classList].find(c => c.startsWith("confirm-"))?.slice(8);
  const buttons = [...card.querySelectorAll(".actions button:not(:disabled)")];
  const exact = buttons.find(button => norm(button.textContent) === text);
  if (exact) return exact;
  for (const [target, phrases] of VOICE_PICK[kind] || []) {
    if (!phrases.includes(text)) continue;
    return typeof target === "number" ? buttons[target] : buttons.find(button => button.textContent.includes(target));
  }
  return null;
}

const ORDINALS = [["перв", 1], ["втор", 2], ["трет", 3], ["четв", 4], ["пят", 5], ["шест", 6], ["седьм", 7], ["восьм", 8], ["девят", 9], ["десят", 10]];

// «вторая нравится», «первая и третья не то» -> отметки в подборке
function voteByVoice(t) {
  // Только короткая команда отметки; длинная реплика с пожеланиями принадлежит агенту.
  const ordinal = "(?:перв|втор|трет|четв|пят|шест|седьм|восьм|девят|десят)[а-я]*";
  const choice = `(?:${ordinal}|(?:номер|№)\\s*\\d+)`;
  if (!new RegExp(`^(?:(?:отметь|убери)\\s+)?${choice}(?:\\s+и\\s+${choice})*\\s+(?:нравится|нравятся|подходит|подходят|не нравится|не нравятся|не подходит|не подходят|не то|плюс|минус|интересна|интересны)$`).test(t)) return false;
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
  const card = [...$("chatLog").querySelectorAll(".confirm:not(.is-done):not(.confirm-clarify)")].pop();
  if (card && card.querySelector(".actions button:not(:disabled)")) {
    const b = voiceButton(card, t);
    if (!b) return handlers.sendText(text);
    card.dataset.said = text; // фраза придёт в ленту вместе с решением
    b.click();
    return;
  }
  if (state.awaiting === "selection") {
    if (voteByVoice(t)) return addMsg("user", text);
    if (["звони", "позвони", "позвони по отмеченным", "звони по отмеченным", "позвони по выбранным", "звони продавцам", "давай позвоним", "поехали"].includes(t)) return handlers.selectionAction("proceed", text);
  }
  return handlers.sendText(text);
}

const TALK_TEXT = {
  listening: ["Слушаю", "Говорите — отвечу, когда замолчите"],
  hearing: ["Слышу вас…", "Договорите — я подожду паузу"],
  processing: ["Распознаю…", ""],
  thinking: ["Агент думает…", "Подбираю ответ"],
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
  api.voiceMode = true;
  $("composer").hidden = true;
  $("talkPanel").hidden = false;
  $("talkBtn").setAttribute("aria-pressed", "true");
  const card = [...$("chatLog").querySelectorAll(".confirm:not(.is-done)")].pop();
  if (card && state.lastPrompt) talk.say(state.lastPrompt);
  else if (state.awaiting === "selection") talk.say(SELECTION_PROMPT);
  else if (!state.running) talk.say("Расскажите, какую квартиру ищете. Начнём с города и бюджета.");
  else talk.say("Слушаю.");
}

function stopTalk() {
  talk.stop();
  api.voiceMode = false;
  $("composer").hidden = false;
  $("talkPanel").hidden = true;
  $("talkBtn").setAttribute("aria-pressed", "false");
}

export const SELECTION_PROMPT = "Подборка на экране. Скажите, что нравится — например, «вторая нравится» или «третья не то», "
  + "— или добавьте пожелание. Когда будете готовы, скажите «звони».";
