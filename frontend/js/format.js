// Форматирование и маленький DOM-хелпер.

export const rub = (v) => (v == null ? "—" : `${Math.round(v).toLocaleString("ru-RU")} ₽`);
export const mln = (v) => `${(v / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} млн ₽`;
export const pct = (v, digits = 1) => `${(v * 100).toLocaleString("ru-RU", { maximumFractionDigits: digits })}%`;

export function plural(n, one, few, many) {
  const m = Math.abs(n) % 100;
  if (m >= 11 && m <= 14) return many;
  const d = m % 10;
  return d === 1 ? one : d >= 2 && d <= 4 ? few : many;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// html`...` — шаблон с экранированием подстановок; html.raw() — без экранирования.
export function html(strings, ...values) {
  return strings.reduce((out, s, i) => {
    if (i === 0) return s;
    const v = values[i - 1];
    const str = v && v.__raw ? v.value : Array.isArray(v) ? v.map((x) => (x && x.__raw ? x.value : esc(x))).join("") : esc(v);
    return out + str + s;
  }, "");
}
html.raw = (value) => ({ __raw: true, value: String(value) });

export function el(markup) {
  const t = document.createElement("template");
  t.innerHTML = markup.trim();
  return t.content.firstElementChild;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function shortDate(iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}
