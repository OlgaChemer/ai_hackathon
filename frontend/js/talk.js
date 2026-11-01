// Голосовой режим: разговор с агентом без кнопок.
// Микрофон слушает сам; замолчали — фраза уходит агенту. Ответ агента звучит голосом браузера,
// пока он говорит — не слушаем (иначе агент услышит сам себя). Нажали на кружок — агент замолкает.
import { capturePhrase, mic, recorder, transcribe, voice } from "./voice.js";
import { sleep } from "./format.js";

// Whisper на тишине и шуме иногда «слышит» титры из обучающих видео
const JUNK = /субтитр|продолжение следует|спасибо за просмотр|подписывайтесь|dimatorzok|редактор|корректор/i;

const plural = (n, one, few, many) => {
  const k = Math.floor(Math.abs(n)) % 100;
  if (!Number.isInteger(n)) return few; // «9,5 миллиона»
  if (k % 10 === 1 && k !== 11) return one;
  return k % 10 >= 2 && k % 10 <= 4 && (k < 12 || k > 14) ? few : many;
};

/** Текст для голоса: «9 000 000 ₽» -> «9000000 рублей», «9,5 млн» -> «9,5 миллиона», без эмодзи и стрелок. */
function speechText(text) {
  return text
    .replace(/(\d)[\s  ](?=\d{3}(?!\d))/g, "$1")
    .replace(/(\d+(?:,\d+)?)\s*млн\.?/g, (_, n) => `${n} ${plural(Number(n.replace(",", ".")), "миллион", "миллиона", "миллионов")}`)
    .replace(/(\d+(?:,\d+)?)\s*тыс\.?/g, (_, n) => `${n} ${plural(Number(n.replace(",", ".")), "тысяча", "тысячи", "тысяч")}`)
    .replace(/м²/g, " квадратных метров")
    .replace(/(^|\s)ИИ(?=[\s,.!?-]|$)/g, "$1И И")
    .replace(/[→←↑↓•·✓✕«»"]/g, " ")
    .replace(/\p{Extended_Pictographic}/gu, "");
}

export class Talk {
  /**
   * onText(text) — человек сказал фразу; canListen() — можно ли сейчас слушать (не идёт звонок);
   * onState(state, detail) — для интерфейса: listening | hearing | processing | speaking | waiting | off.
   */
  constructor({ onText, canListen, onState, onLevel }) {
    this.onText = onText;
    this.canListen = canListen;
    this.onState = onState;
    this.onLevel = onLevel;
    this.on = false;
    this.stream = null;
    this.capture = null;
    this.phrases = [];      // что агенту осталось сказать
    this.speaking = false;
    this.processing = false; // распознаём фразу человека — агент ждёт
    this.state = "off";
  }

  #set(state, detail = "") {
    if (this.state === state && !detail) return;
    this.state = state;
    this.onState(state, detail);
  }

  async start() {
    if (this.on) return;
    if (recorder.available && recorder.supported) {
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      } catch (e) {
        throw new Error(e.name === "NotAllowedError"
          ? "Браузер не дал доступ к микрофону. Разрешите микрофон для этой страницы (значок слева в адресной строке)."
          : `Микрофон не включился: ${e.message}`);
      }
    } else if (!mic.supported) {
      throw new Error("Этот браузер не умеет распознавать речь. Откройте страницу в Chrome или Edge.");
    }
    this.on = true;
    this.run = (this.run || 0) + 1; // циклы прошлого включения завершатся сами
    this.#loop(this.run);
    this.#speakLoop(this.run);
  }

  #alive(run) { return this.on && this.run === run; }

  stop() {
    this.on = false;
    this.phrases = [];
    voice.stop();
    this.capture?.stop();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.#set("off");
  }

  /** Агент говорит фразу (встаёт в очередь). */
  say(text) {
    if (!this.on || !text) return;
    // длинный текст — по предложениям: Chrome обрывает длинные фразы, а перебить можно между ними
    const parts = speechText(text).replace(/\s+/g, " ").match(/[^.!?…]+[.!?…]*\s*/g) || [text];
    let chunk = "";
    for (const p of parts) {
      if ((chunk + p).length > 220 && chunk) { this.phrases.push(chunk.trim()); chunk = ""; }
      chunk += p;
    }
    if (chunk.trim()) this.phrases.push(chunk.trim());
    // человек ещё не начал говорить — перестаём слушать, агент отвечает
    if (this.capture && !this.capture.heard()) { this.capture.aborted = true; this.capture.stop(); }
  }

  /** Перебить агента: замолкает и сразу слушает. */
  interrupt() {
    this.phrases = [];
    voice.stop();
  }

  get busy() { return this.speaking || this.phrases.length > 0; }
  get userBusy() { return this.processing || Boolean(this.capture?.heard()); }

  async #speakLoop(run) {
    while (this.#alive(run)) {
      if (!this.phrases.length) { await sleep(120); continue; }
      if (this.userBusy) { await sleep(120); continue; } // человек договаривает — агент ждёт
      this.speaking = true;
      this.#set("speaking");
      while (this.on && this.phrases.length) await voice.speak(this.phrases.shift(), "agent", true);
      this.speaking = false;
      await sleep(250); // хвост звука из колонок не должен попасть в микрофон
    }
  }

  async #loop(run) {
    while (this.#alive(run)) {
      if (this.busy || !this.canListen()) {
        if (!this.busy) this.#set("waiting");
        await sleep(200);
        continue;
      }
      this.#set("listening");
      const r = this.stream ? await this.#whisperPhrase() : await this.#browserPhrase();
      if (!this.#alive(run) || !r) continue;
      if (r.text && r.text.length > 1 && !JUNK.test(r.text)) {
        try { await this.onText(r.text); } catch (e) { console.error(e); }
        // даём агенту ответить, прежде чем снова слушать; если он молча работает — слушаем дальше
        this.#set("thinking");
        const until = performance.now() + 6000;
        while (this.#alive(run) && !this.busy && performance.now() < until) await sleep(150);
      } else if (r.error && !/Не разобрал|не услышал|короткая|не ответил за 12/i.test(r.error)) {
        this.#set("listening", r.error);
        await sleep(1500);
      }
    }
  }

  // Запись фразы и распознавание на сервере (Whisper)
  async #whisperPhrase() {
    const cap = capturePhrase(this.stream, {
      threshold: 0.012, silenceMs: 1100, maxMs: 30000, waitMs: 15000,
      onLevel: (rms) => {
        this.onLevel?.(rms);
        if (cap?.heard() && this.state === "listening") this.#set("hearing");
      },
    });
    this.capture = cap;
    const { blob, heard, ms } = await cap.result;
    if (heard && !cap.aborted) this.processing = true; // до снятия capture — чтобы агент не вклинился
    this.capture = null;
    if (!this.on || cap.aborted || !heard || ms < 500) { this.processing = false; return null; }
    this.#set("processing");
    try {
      return await transcribe(blob);
    } finally {
      this.processing = false;
    }
  }

  // Без Whisper — распознавание браузера (Chrome/Edge)
  async #browserPhrase() {
    const s = mic.listen((text) => { if (text) this.#set("hearing", text); });
    this.capture = { stop: () => s.stop(), heard: () => this.state === "hearing", aborted: false };
    const cap = this.capture;
    const r = await s.result;
    this.capture = null;
    return cap.aborted ? null : r;
  }
}
