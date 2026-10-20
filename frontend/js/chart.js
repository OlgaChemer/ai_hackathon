// График накопления взноса: линия на каждую стратегию, коридор плохого/хорошего
// сценария для выбранной, пунктир цели. Подписи на концах линий + подсказка при наведении.
import { html } from "./format.js";

const M = { top: 16, right: 132, bottom: 30, left: 56 };
const short = (v) => `${(v / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} млн`;
const full = (v) => `${Math.round(v).toLocaleString("ru-RU")} ₽`;

export function savingsChart(container, fin, selectedKey) {
  const draw = () => render(container, fin, selectedKey);
  draw();
  if (!container._ro) {
    container._ro = new ResizeObserver(() => draw());
    container._ro.observe(container);
  }
}

function niceStep(max) {
  const raw = max / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw);
}

function render(container, fin, selectedKey) {
  const W = Math.max(300, container.clientWidth || 640);
  const H = 260;
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;
  const strategies = fin.strategies;
  const sel = strategies.find((s) => s.key === selectedKey) || strategies[0];
  const n = sel.series.length - 1;
  const maxV = Math.max(fin.need * 1.08, ...strategies.map((s) => s.series[n]), sel.series_high[n]);
  const minV = Math.min(...strategies.map((s) => s.series[0]), sel.series_low[n], fin.need) * 0.92;
  const step = niceStep(maxV - minV);
  const yMin = Math.max(0, Math.floor(minV / step) * step);
  const yMax = Math.ceil(maxV / step) * step;
  const x = (i) => M.left + (i / n) * iw;
  const y = (v) => M.top + ih - ((Math.max(yMin, v) - yMin) / (yMax - yMin)) * ih;
  const path = (arr) => arr.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const band = `${path(sel.series_high)}${sel.series_low.map((v, i) => `L${x(n - i).toFixed(1)},${y(sel.series_low[n - i]).toFixed(1)}`).join("")}Z`;

  const yTicks = [];
  for (let v = yMin; v <= yMax + 1; v += step) yTicks.push(v);
  const xEvery = n <= 12 ? 2 : n <= 24 ? 4 : 6;
  const xTicks = [];
  for (let i = 0; i <= n; i += xEvery) xTicks.push(i);

  // подписи на концах линий: разводим, если слишком близко
  const ends = strategies.map((s) => ({ key: s.key, title: s.title, yy: y(s.series[n]) })).sort((a, b) => a.yy - b.yy);
  for (let i = 1; i < ends.length; i++) if (ends[i].yy - ends[i - 1].yy < 16) ends[i].yy = ends[i - 1].yy + 16;

  container.innerHTML = html`
    <svg class="chart-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
         aria-label="Накопление первоначального взноса по трём стратегиям; цель ${full(fin.need)}">
      <g class="grid">
        ${html.raw(yTicks.map((v) => html`<line x1="${M.left}" x2="${M.left + iw}" y1="${y(v)}" y2="${y(v)}"/><text x="${M.left - 8}" y="${y(v) + 4}" text-anchor="end">${v ? short(v) : "0"}</text>`).join(""))}
        ${html.raw(xTicks.map((i) => html`<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${i === 0 ? "сейчас" : `${i} мес.`}</text>`).join(""))}
      </g>
      <path class="band s-${sel.key}" d="${band}"/>
      <line class="target" x1="${M.left}" x2="${M.left + iw}" y1="${y(fin.need)}" y2="${y(fin.need)}"/>
      <text class="target-label" x="${M.left + 6}" y="${y(fin.need) - 6}">Цель: ${full(fin.need)}</text>
      ${html.raw(strategies.map((s) => html`<path class="series s-${s.key} ${s.key === sel.key ? "is-sel" : ""}" d="${path(s.series)}"/>`).join(""))}
      ${html.raw(ends.map((e) => html`<text class="end-label s-${e.key}" x="${M.left + iw + 8}" y="${e.yy + 4}">${e.title}</text>`).join(""))}
      <g class="hover" visibility="hidden">
        <line class="crosshair" y1="${M.top}" y2="${M.top + ih}"/>
        ${html.raw(strategies.map((s) => html`<circle class="dot s-${s.key}" r="4.5"/>`).join(""))}
      </g>
      <rect class="hit" x="${M.left}" y="${M.top}" width="${iw}" height="${ih}"/>
    </svg>
    <div class="chart-tip" hidden></div>`;

  const svg = container.querySelector("svg");
  const hover = svg.querySelector(".hover");
  const tip = container.querySelector(".chart-tip");
  const dots = [...svg.querySelectorAll(".dot")];
  const move = (evt) => {
    const rect = svg.getBoundingClientRect();
    const px = ((evt.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(n, Math.round(((px - M.left) / iw) * n)));
    hover.setAttribute("visibility", "visible");
    hover.querySelector(".crosshair").setAttribute("x1", x(i));
    hover.querySelector(".crosshair").setAttribute("x2", x(i));
    strategies.forEach((s, k) => { dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(s.series[i])); });
    tip.hidden = false;
    tip.innerHTML = html`<strong>${i === 0 ? "Сейчас" : `Через ${i} мес.`}</strong>
      ${html.raw(strategies.map((s) => html`<div><span class="sw s-${s.key}"></span>${s.title}<b class="num">${full(s.series[i])}</b></div>`).join(""))}
      <div class="muted">Цель<b class="num">${full(fin.need)}</b></div>`;
    const left = (x(i) / W) * rect.width;
    tip.style.left = `${Math.min(rect.width - 210, Math.max(0, left + 12))}px`;
  };
  const hit = svg.querySelector(".hit");
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

