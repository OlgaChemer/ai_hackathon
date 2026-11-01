// Настоящий звонок продавцу через МТС Exolve: браузер звонит по WebRTC (JsSIP).
// В линию идёт голос агента (синтез Exolve), голос продавца — на распознавание (Whisper). На сайте разговор не звучит.
import JsSIP from "../vendor/jssip.esm.js";
import { capturePhrase, transcribe } from "./voice.js";

const STUN = ["stun:webrtc.exolve.ru:3478"];

const FAIL = {
  Busy: "Номер занят.",
  Rejected: "Звонок отклонили.",
  "No Answer": "Никто не взял трубку.",
  Unavailable: "Номер недоступен.",
  "Not Found": "Номер не найден.",
  "Address Incomplete": "Номер набран не полностью.",
  Canceled: "Звонок отменён.",
  "Connection Error": "Нет связи с сервером Exolve (проверьте интернет/VPN).",
  "Request Timeout": "Сервер Exolve не ответил.",
  "SIP Failure Code": "Exolve не пропустил звонок: проверьте баланс и определяемый номер у SIP ID.",
  Forbidden: "Exolve запретил звонок: проверьте баланс и определяемый номер у SIP ID.",
};

let config = null;

function pinGet() { try { return localStorage.getItem("phonePin") || ""; } catch { return ""; } }
function pinSet(v) { try { if (v) localStorage.setItem("phonePin", v); else localStorage.removeItem("phonePin"); } catch { /* приватный режим */ } }
const pinHeaders = () => (pinGet() ? { "X-Phone-Pin": pinGet() } : {});

export async function phoneConfig() {
  if (config) return config;
  let res = await fetch("/api/phone", { headers: pinHeaders() });
  if (res.status === 403) {
    const pin = prompt("PIN для звонков (задан на сервере в PHONE_PIN):");
    if (!pin) throw new Error("Звонок отменён: нужен PIN.");
    pinSet(pin.trim());
    res = await fetch("/api/phone", { headers: pinHeaders() });
    if (res.status === 403) pinSet("");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Сервер не отдал настройки звонка (${res.status})`);
  config = data;
  return config;
}

/** «8 (999) 123-45-67» -> «79991234567»; null, если это не российский номер. */
export function normalizePhone(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (d.length === 11 && d[0] === "8") d = `7${d.slice(1)}`;
  if (d.length === 10) d = `7${d}`;
  return d.length === 11 && d[0] === "7" ? d : null;
}

export const formatPhone = (d) => `+${d[0]} ${d.slice(1, 4)} ${d.slice(4, 7)}-${d.slice(7, 9)}-${d.slice(9)}`;

// Exolve требует внешний адрес клиента в заголовке XGRemoteIP (так делает их Web SDK), без него — 403.
async function realIp() {
  for (let attempt = 0; attempt < 2; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    try {
      const res = await fetch("https://webrtc.exolve.ru/client-info", { method: "POST", signal: ctrl.signal });
      const j = await res.json();
      if (j.ip && j.port) return `${j.ip}:${j.port}`;
    } catch { /* повторим */ } finally {
      clearTimeout(timer);
    }
  }
  return null;
}

// Короткие реплики, пока агент распознаёт ответ и думает: так пауза не кажется обрывом связи.
const FILLERS = ["Так, понял.", "Угу, секунду.", "Понял вас.", "Хорошо, секунду."];

export class PhoneCall {
  constructor() {
    this.ua = null;
    this.session = null;
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.out = this.ctx.createMediaStreamDestination(); // то, что слышит собеседник
    this.remote = null;
    this.player = null;
    this.ended = false;
    this.onEnded = () => {};
    this.ready = null;      // регистрация в Exolve (делаем заранее, чтобы звонок шёл сразу)
    this.queue = Promise.resolve(); // фразы агента звучат строго по очереди
    this.playing = null;
    this.capture = null;
    this.fillers = [];
    this.fillerNo = 0;
  }

  /** Регистрируется в Exolve. Можно вызвать заранее — dial() тогда начнёт звонок сразу. */
  connect() {
    this.ready ??= this.#register().catch((e) => { this.ready = null; throw e; });
    return this.ready;
  }

  async #register() {
    const cfg = await phoneConfig();
    const ip = await realIp();
    if (!ip) throw new Error("Не удалось связаться с webrtc.exolve.ru (проверьте интернет/VPN).");
    const id = Math.random().toString(36).slice(2, 12);
    try { this.ua?.stop(); } catch { /* прошлая попытка */ }
    this.ua = new JsSIP.UA({
      uri: `sip:${cfg.username}@${cfg.realm}`,
      realm: cfg.realm,
      sockets: [new JsSIP.WebSocketInterface(cfg.ws)],
      password: cfg.password,
      contact_uri: `sip:${cfg.username}-${id}@${id}.invalid`,
      display_name: cfg.username,
      register_expires: 100,
      extra_headers: [`XGRemoteIP: ${ip}`],
    });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Сервер Exolve не ответил за 15 секунд.")), 15000);
      this.ua.on("registered", () => { clearTimeout(timer); resolve(); });
      this.ua.on("registrationFailed", (e) => {
        clearTimeout(timer);
        reject(new Error(e.cause === JsSIP.C.causes.AUTHENTICATION_ERROR
          ? "Exolve не принял логин и пароль SIP ID."
          : `Не удалось подключиться к Exolve: ${FAIL[e.cause] || e.cause}`));
      });
      this.ua.start();
    });
    // заготовки-заполнители: синтезируем один раз, пока клиент ещё не нажал «Позвонить»
    Promise.all(FILLERS.map((t) => this.#synth(t).catch(() => null)))
      .then((list) => { this.fillers = list.filter(Boolean); });
  }

  /** Звонит. Promise завершается, когда трубку взяли. */
  async dial(number, onState = () => {}) {
    onState("connecting");
    try {
      await this.connect();
    } catch (e) {
      this.hangup();
      throw e;
    }
    await this.ctx.resume(); // dial вызывается по клику — браузер разрешит звук
    onState("dialing");
    const t0 = performance.now();
    const mark = (what) => console.info(`[звонок] ${what}: ${Math.round(performance.now() - t0)} мс`);
    let iceTimer = null;
    return new Promise((resolve, reject) => {
      this.session = this.ua.call(number, {
        mediaStream: this.out.stream,
        pcConfig: { iceServers: [{ urls: STUN }] },
        rtcOfferConstraints: { offerToReceiveAudio: true, offerToReceiveVideo: false },
        sessionTimersExpires: 500,
        eventHandlers: {
          peerconnection: (e) => e.peerconnection.addEventListener("track", (t) => {
            if (t.track.kind === "audio") this.#attachRemote(t.streams[0] || new MediaStream([t.track]));
          }),
          // JsSIP ждёт, пока браузер переберёт все сетевые адреса, — с VPN/WSL это 10–40 с.
          // Звонку хватает внешнего адреса от STUN: как только он есть (или через 1 с) — отправляем вызов.
          icecandidate: (e) => {
            if (e.candidate.type === "srflx") { clearTimeout(iceTimer); mark("адрес от STUN"); e.ready(); return; }
            iceTimer ??= setTimeout(() => { mark("адреса собраны по таймеру"); e.ready(); }, 1000);
          },
          sending: () => mark("вызов отправлен"),
          progress: () => { mark("идут гудки"); onState("ringing"); },
          confirmed: () => { mark("трубку взяли"); onState("answered"); resolve(); },
          failed: (e) => {
            clearTimeout(iceTimer);
            this.hangup();
            reject(new Error(FAIL[e.cause] || `Звонок не прошёл: ${e.cause}`));
          },
          ended: () => { this.hangup(); this.onEnded(); },
        },
      });
    });
  }

  #attachRemote(stream) {
    this.remote = stream;
    // Chrome отдаёт звук WebRTC в запись только если поток подключён к элементу; на сайте разговор не слышен
    this.player = new Audio();
    this.player.muted = true;
    this.player.srcObject = stream;
    this.player.play().catch(() => {});
  }

  async #synth(text) {
    const res = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json", ...pinHeaders() }, body: JSON.stringify({ text }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Синтез речи не удался (${res.status})`);
    return this.ctx.decodeAudioData(await res.arrayBuffer());
  }

  // Ставит фразу в очередь: в линию уходит только собеседнику, на сайте не звучит.
  #play(buffer) {
    this.queue = this.queue.then(() => new Promise((resolve) => {
      if (this.ended) return resolve();
      const src = this.ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(this.out);
      const done = () => { this.playing = null; resolve(); };
      src.onended = done;
      this.playing = { stop: () => { try { src.stop(); } catch { /* уже остановлен */ } done(); } };
      src.start();
    }));
    return this.queue;
  }

  /** Произносит фразу в линию. Завершается, когда фраза прозвучала. */
  async say(text) {
    if (this.ended) return;
    const audio = await this.#synth(text);
    if (!this.ended) await this.#play(audio);
  }

  /** Короткое «Так, понял.» — пока распознаём ответ и думаем. */
  filler() {
    if (this.ended || !this.fillers.length) return;
    this.#play(this.fillers[this.fillerNo++ % this.fillers.length]);
  }

  /**
   * Слушает ответ продавца до паузы и распознаёт его.
   * -> {text, error, ended}: ended — продавец положил трубку.
   */
  async listen(onState = () => {}) {
    for (let attempt = 0; attempt < 4 && !this.ended; attempt++) {
      if (!this.remote) return { text: "", error: "Нет звука от собеседника." };
      onState("listening");
      this.capture = capturePhrase(this.remote, { threshold: 0.01, silenceMs: 900, maxMs: 25000, waitMs: 12000 });
      const { blob, heard } = await this.capture.result;
      this.capture = null;
      if (this.ended) break;
      if (!heard) continue; // тишина — слушаем дальше
      onState("processing");
      this.filler();
      const r = await transcribe(blob);
      if (r.text || !/Не разобрал/.test(r.error || "")) return r;
    }
    return this.ended ? { text: "", error: null, ended: true } : { text: "", error: "Продавец молчит." };
  }

  hangup() {
    if (this.ended) return;
    this.ended = true;
    this.playing?.stop();
    this.capture?.stop();
    try { if (this.session && !this.session.isEnded()) this.session.terminate(); } catch { /* уже завершён */ }
    try { this.ua?.stop(); } catch { /* уже остановлен */ }
    if (this.player) this.player.srcObject = null;
    this.ctx.close().catch(() => {});
  }
}
