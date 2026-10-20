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
  speak(text, speaker = "agent") {
    if (!this.enabled) return Promise.resolve();
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
export const mic = {
  supported: Boolean(Recognition),
  // Слушает одну фразу; onText получает промежуточный и финальный текст.
  listen(onText) {
    if (!Recognition) return null;
    const r = new Recognition();
    r.lang = "ru-RU";
    r.interimResults = true;
    r.continuous = false;
    let finalText = "";
    const result = new Promise((resolve) => {
      r.onresult = (e) => {
        let interim = "";
        for (const res of e.results) (res.isFinal ? (finalText = res[0].transcript) : (interim += res[0].transcript));
        onText(finalText || interim, Boolean(finalText));
      };
      r.onend = () => resolve(finalText);
      r.onerror = () => resolve(finalText);
    });
    voice.stop();
    r.start();
    return { stop: () => r.stop(), result };
  },
};
