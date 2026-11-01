// Озвучка реплик (speechSynthesis) и распознавание речи продавца (webkitSpeechRecognition).

let enabled = localStorageGet("voice", "1") === "1";
let voices = [];

function localStorageGet(k, d) {
  try { return localStorage.getItem(k) ?? d; } catch { return d; }
}
function localStorageSet(k, v) {
  try { localStorage.setItem(k, v); } catch { /* приватный режим */ }
}

function loadVoices() {
  if (!("speechSynthesis" in window)) return;
  voices = speechSynthesis.getVoices().filter((v) => v.lang?.toLowerCase().startsWith("ru"));
}
if ("speechSynthesis" in window) {
  loadVoices();
  speechSynthesis.onvoiceschanged = loadVoices;
}

export const voice = {
  get enabled() { return enabled && "speechSynthesis" in window; },
  set enabled(v) {
    enabled = v;
    localStorageSet("voice", v ? "1" : "0");
    if (!v && "speechSynthesis" in window) speechSynthesis.cancel();
  },
  supported: "speechSynthesis" in window,

  // Возвращает Promise, который завершается, когда реплика прозвучала.
  // force — говорить, даже если озвучка звонка выключена (голосовой режим чата).
  speak(text, speaker = "agent", force = false) {
    if (!(force ? this.supported : this.enabled)) return Promise.resolve();
    return new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(text.replace(/₽/g, "рублей"));
      u.lang = "ru-RU";
      const pick = speaker === "agent" ? voices[0] : voices[1] || voices[0];
      if (pick) u.voice = pick;
      u.rate = speaker === "agent" ? 1.12 : 1.05;
      u.pitch = speaker === "agent" ? 1.05 : voices[1] ? 1 : 0.75;
      const done = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(done, 2500 + text.length * 95); // страховка, если onend не придёт
      u.onend = done;
      u.onerror = done;
      speechSynthesis.speak(u);
    });
  },
  stop() { if ("speechSynthesis" in window) speechSynthesis.cancel(); },
};

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

// Понятные причины ошибок Web Speech API.
const MIC_ERRORS = {
  "not-allowed": "Браузер не дал доступ к микрофону. Разрешите микрофон для этой страницы (значок слева в адресной строке) и попробуйте снова.",
  "service-not-allowed": "Браузер запретил распознавание речи на этой странице. Разрешите микрофон в настройках сайта.",
  "audio-capture": "Микрофон не найден или занят другой программой.",
  "network": "Браузер не смог связаться с сервисом распознавания речи (Chrome отправляет звук на серверы Google). Проверьте интернет/VPN или откройте страницу в Microsoft Edge.",
  "no-speech": "Не услышал речь. Нажмите на микрофон и начните говорить сразу.",
  "language-not-supported": "Браузер не поддерживает распознавание русской речи.",
};

export const mic = {
  supported: Boolean(Recognition),
  /**
   * Слушает одну фразу.
   * onText(text, isFinal) — промежуточный и финальный текст; onStart() — микрофон включился.
   * result -> {text, error}: error — понятное сообщение или null.
   */
  listen(onText, onStart = () => {}) {
    if (!Recognition) {
      return { stop() {}, result: Promise.resolve({ text: "", error: "Этот браузер не умеет распознавать речь. Откройте страницу в Chrome или Edge." }) };
    }
    const r = new Recognition();
    r.lang = "ru-RU";
    r.interimResults = true;
    r.continuous = false;
    r.maxAlternatives = 1;
    let finalText = "";
    let lastInterim = "";
    let error = null;
    let started = false;
    let heard = false;
    // сторож: Chrome иногда молча висит, если сервис распознавания недоступен
    const watchdog = setTimeout(() => {
      if (!started) {
        error = "Микрофон не включился за 4 секунды: браузер не может связаться с сервисом распознавания речи. "
          + "Проверьте разрешение микрофона и интернет/VPN или откройте страницу в Microsoft Edge.";
        r.abort();
      }
    }, 4000);
    const cap = setTimeout(() => {
      if (!heard) error = "Сервис распознавания речи не ответил за 12 секунд. Проверьте интернет/VPN или откройте страницу в Microsoft Edge.";
      r.stop();
    }, 12000);
    const result = new Promise((resolve) => {
      r.onaudiostart = () => { started = true; onStart(); };
      r.onresult = (e) => {
        heard = true;
        let interim = "";
        for (const res of e.results) {
          if (res.isFinal) finalText += res[0].transcript;
          else interim += res[0].transcript;
        }
        lastInterim = interim;
        onText((finalText + " " + interim).trim(), !interim);
      };
      r.onerror = (e) => {
        if (e.error !== "aborted") error = MIC_ERRORS[e.error] || `Ошибка распознавания речи: ${e.error}`;
        console.warn("SpeechRecognition error:", e.error, e.message);
      };
      // если финальный результат не пришёл — отдаём то, что успели услышать
      r.onend = () => {
        clearTimeout(watchdog);
        clearTimeout(cap);
        resolve({ text: (finalText || lastInterim).trim(), error: finalText || lastInterim ? null : error });
      };
    });
    voice.stop();
    try {
      r.start();
    } catch (e) {
      return { stop() {}, result: Promise.resolve({ text: "", error: `Микрофон не запустился: ${e.message}` }) };
    }
    return { stop: () => r.stop(), result };
  },
};

// ------------------------------------------------------------------ распознавание на сервере (Whisper)

const REC_ERRORS = {
  NotAllowedError: "Браузер не дал доступ к микрофону. Разрешите микрофон для этой страницы (значок слева в адресной строке).",
  NotFoundError: "Микрофон не найден.",
  NotReadableError: "Микрофон занят другой программой.",
};

/**
 * Пишет одну фразу из аудиопотока (микрофон или голос собеседника в звонке).
 * Останавливается после тишины, по stop() или по лимиту длины.
 * result -> {blob, heard, ms}: heard — детектор громкости заметил речь.
 * onLevel(rms) — громкость 10 раз в секунду (для анимации); heard() — человек уже начал говорить.
 */
export function capturePhrase(stream, { threshold = 0.006, silenceMs = 1500, maxMs = 20000, waitMs = 8000, onLevel } = {}) {
  const mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);

  // детектор тишины
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  let heard = false;
  let silentSince = performance.now();
  const t0 = performance.now();

  const stopped = new Promise((resolve) => { rec.onstop = resolve; });
  const stop = () => { if (rec.state !== "inactive") rec.stop(); };
  const timer = setInterval(() => {
    analyser.getFloatTimeDomainData(buf);
    const rms = Math.sqrt(buf.reduce((a, v) => a + v * v, 0) / buf.length);
    const now = performance.now();
    onLevel?.(rms);
    if (rms > threshold) { heard = true; silentSince = now; }
    if ((heard && now - silentSince > silenceMs) || now - t0 > maxMs || (!heard && now - t0 > waitMs)) stop();
  }, 100);
  rec.start(250);

  const result = stopped.then(() => {
    clearInterval(timer);
    ctx.close();
    return { blob: new Blob(chunks, { type: rec.mimeType || "audio/webm" }), heard, ms: performance.now() - t0 };
  });
  return { stop, result, heard: () => heard };
}

/** Распознаёт запись на сервере (Whisper). -> {text, error} */
export async function transcribe(blob) {
  try {
    const res = await fetch("/api/stt", { method: "POST", headers: { "Content-Type": blob.type }, body: blob });
    const data = await res.json();
    if (!res.ok) return { text: "", error: data.detail || `Сервер не распознал запись (${res.status})` };
    return { text: (data.text || "").trim(), error: data.text ? null : "Не разобрал слова — попробуйте ещё раз." };
  } catch (e) {
    return { text: "", error: `Не удалось отправить запись: ${e.message}` };
  }
}

export const recorder = {
  // включается, если сервер сообщил, что Whisper установлен (см. app.js)
  available: false,
  supported: Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder),

  /**
   * Записывает одну фразу с микрофона и распознаёт её на сервере.
   * Останавливается сам после 1,5 с тишины (или по stop()); максимум 20 с.
   * onState("listening" | "processing").
   */
  listen(onState = () => {}) {
    let stopFn = () => {};
    const result = (async () => {
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } catch (e) {
        return { text: "", error: REC_ERRORS[e.name] || `Микрофон не включился: ${e.message}` };
      }
      voice.stop();
      const phrase = capturePhrase(stream);
      stopFn = phrase.stop;
      onState("listening");
      const { blob, ms } = await phrase.result;
      stream.getTracks().forEach((t) => t.stop());
      // даже если детектор громкости не сработал (тихий микрофон), отдаём запись Whisper — у него свой детектор речи
      if (ms < 400) return { text: "", error: "Запись слишком короткая — нажмите и говорите." };
      onState("processing");
      return transcribe(blob);
    })();
    return { stop: () => stopFn(), result };
  },
};

/**
 * Подключает кнопку микрофона к полю ввода. Нажал — слушаем (кнопка красная),
 * нажал ещё раз — стоп. Если на сервере есть Whisper — пишем звук и распознаём там,
 * иначе — распознаванием браузера.
 * onFinal(text) — распознанная фраза; onError(message) — понятная причина сбоя.
 */
export function bindMic(button, input, { onFinal, onError }) {
  let session = null;
  button.addEventListener("click", async () => {
    if (session) { session.stop(); return; }
    const placeholder = input.placeholder;
    button.classList.add("is-on");
    button.setAttribute("aria-pressed", "true");
    if (recorder.available && recorder.supported) {
      input.placeholder = "Включаю микрофон…";
      session = recorder.listen((state) => {
        if (state === "listening") input.placeholder = "Слушаю… говорите — остановлюсь, когда замолчите";
        if (state === "processing") {
          input.placeholder = "Распознаю…";
          button.classList.remove("is-on");
        }
      });
    } else {
      input.placeholder = "Включаю микрофон…";
      session = mic.listen(
        (text) => { input.value = text; },
        () => { input.placeholder = "Слушаю… говорите, нажмите ещё раз, чтобы закончить"; },
      );
    }
    const { text, error } = await session.result;
    session = null;
    button.classList.remove("is-on");
    button.setAttribute("aria-pressed", "false");
    input.placeholder = placeholder;
    if (text) { input.value = text; onFinal(text); }
    else if (error) onError(error);
  });
}
