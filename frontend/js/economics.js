// Вкладка «Экономика для Сбера»: калькулятор юнит-экономики и ROI.
import { api } from "./api.js";
import { html, rub, pct } from "./format.js";

const CONTROLS = [
  { key: "savings_pct", label: "Экономия на торге", min: 0.005, max: 0.06, step: 0.005, value: 0.03, fmt: (v) => pct(v) },
  { key: "fee_pct", label: "Плата за результат", min: 0.05, max: 0.3, step: 0.01, value: 0.15, fmt: (v) => pct(v, 0) },
  { key: "incrementality", label: "Инкрементальность ипотеки", min: 0.05, max: 0.6, step: 0.05, value: 0.3, fmt: (v) => pct(v, 0) },
  { key: "conversion", label: "Конверсия кейс → сделка", min: 0.05, max: 0.5, step: 0.01, value: 0.25, fmt: (v) => pct(v, 0) },
  { key: "deals", label: "Сделок в год", min: 500, max: 30000, step: 500, value: 2000, fmt: (v) => Number(v).toLocaleString("ru-RU") },
  { key: "fixed_costs", label: "Постоянные затраты", min: 50e6, max: 300e6, step: 10e6, value: 150e6, fmt: (v) => `${v / 1e6} млн ₽` },
  { key: "fin_deposit_share", group: "fin", label: "Доля клиентов, открывших вклад", min: 0.05, max: 0.7, step: 0.05, value: 0.35, fmt: (v) => pct(v, 0) },
  { key: "fin_avg_deposit", group: "fin", label: "Средний остаток на вкладе", min: 200000, max: 3000000, step: 100000, value: 1200000, fmt: (v) => rub(v) },
  { key: "fin_deposit_margin", group: "fin", label: "Маржа банка на вкладах, годовых", min: 0.005, max: 0.04, step: 0.005, value: 0.02, fmt: (v) => pct(v) },
];

const fmtRoi = (r) => (r == null ? "—" : r >= 1 ? `${r.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}×` : pct(r, 0));

export function mountEconomics(root) {
  if (root.dataset.ready) return;
  root.dataset.ready = "1";
  const values = Object.fromEntries(CONTROLS.map((c) => [c.key, c.value]));
  values.fin_enabled = true;
  const control = (c) => html`
        <div class="control">
          <label for="ec-${c.key}">${c.label}<output id="out-${c.key}">${c.fmt(c.value)}</output></label>
          <input type="range" id="ec-${c.key}" min="${c.min}" max="${c.max}" step="${c.step}" value="${c.value}" data-key="${c.key}">
        </div>`;
  root.innerHTML = html`
    <aside class="controls">
      <h2>Допущения</h2>
      ${html.raw(CONTROLS.filter((c) => !c.group).map(control).join(""))}
      <label class="toggle"><input type="checkbox" data-fin checked> Учитывать финансового помощника</label>
      <div class="fin-controls">${html.raw(CONTROLS.filter((c) => c.group === "fin").map(control).join(""))}</div>
      <button class="btn btn-ghost btn-small" data-reset>Вернуть базовый сценарий</button>
    </aside>
    <div class="econ-main">
      <div class="kpis" data-kpis></div>
      <section class="panel"><h2 class="panel-title">Одна закрытая сделка</h2><p class="panel-lead">Квартира 9 млн ₽, ипотека 80%. Главный доход — не плата клиента, а ипотека и продукты вокруг сделки.</p><div class="bars" data-bars></div><p class="hint" data-conflict></p></section>
      <section class="panel"><h2 class="panel-title">Финансовый помощник</h2><p class="panel-lead">Клиенты, которые копят на взнос, открывают вклады и брокерские счета в Сбере — в том числе те, кто квартиру так и не купил. Банк зарабатывает маржу на привлечённых средствах и комиссии.</p><div data-fin-block></div></section>
      <section class="panel"><h2 class="panel-title">Сценарии</h2><p class="panel-lead">ROI = (сделки × вклад − постоянные затраты) / постоянные затраты. Сильнее всего ROI двигают инкрементальность ипотеки и конверсия.</p><div style="overflow-x:auto"><table class="scen" data-scen></table></div></section>
    </div>`;

  let timer;
  const refresh = async () => {
    const data = await api.economics(values);
    render(root, data);
  };
  const fill = (input) => input.style.setProperty("--fill", `${((input.value - input.min) / (input.max - input.min)) * 100}%`);
  root.querySelectorAll("input[type=range]").forEach((input) => {
    fill(input);
    input.addEventListener("input", () => {
      fill(input);
      const c = CONTROLS.find((x) => x.key === input.dataset.key);
      values[c.key] = Number(input.value);
      root.querySelector(`#out-${c.key}`).textContent = c.fmt(values[c.key]);
      clearTimeout(timer);
      timer = setTimeout(refresh, 120);
    });
  });
  root.querySelector("[data-fin]").addEventListener("change", (e) => {
    values.fin_enabled = e.target.checked;
    root.querySelector(".fin-controls").classList.toggle("is-off", !e.target.checked);
    refresh();
  });
  root.querySelector("[data-reset]").addEventListener("click", () => {
    CONTROLS.forEach((c) => {
      values[c.key] = c.value;
      root.querySelector(`#ec-${c.key}`).value = c.value;
      fill(root.querySelector(`#ec-${c.key}`));
      root.querySelector(`#out-${c.key}`).textContent = c.fmt(c.value);
    });
    refresh();
  });
  refresh();
}

function render(root, { unit, scenarios }) {
  const bad = unit.roi != null && unit.roi < 0;
  root.querySelector("[data-kpis]").innerHTML = html`
    <div class="kpi is-key"><div class="v num">${rub(unit.contribution)}</div><div class="k">вклад Сбера на сделку</div></div>
    <div class="kpi ${bad ? "is-bad" : ""}"><div class="v num">${fmtRoi(unit.roi)}</div><div class="k">ROI за год${unit.fin.enabled ? ` · без помощника ${fmtRoi(unit.roi_core)}` : ""}</div></div>
    <div class="kpi"><div class="v num">${unit.breakeven_deals ? unit.breakeven_deals.toLocaleString("ru-RU") : "—"}</div><div class="k">сделок до безубыточности</div></div>
    <div class="kpi"><div class="v num">${rub(unit.client.net)}</div><div class="k">чистая выгода клиента</div></div>`;

  const f = unit.fin;
  root.querySelector("[data-fin-block]").innerHTML = f.enabled ? html`
    <dl class="ledger fin-ledger">
      <dt>Клиентов в воронке в год (сделки ÷ конверсия)</dt><dd class="num">${f.cases.toLocaleString("ru-RU")}</dd>
      <dt>Доход на одного клиента в воронке</dt><dd class="num">${rub(f.per_case)}</dd>
      <dt>В пересчёте на закрытую сделку</dt><dd class="num">${rub(f.per_deal)}</dd>
      <dt class="total">Доход за год</dt><dd class="total num">${(f.year / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн ₽</dd>
    </dl>` : html`<p class="hint">Выключено — ROI считается только по сделкам.</p>`;

  const items = [
    ...unit.lines.map((l) => ({ ...l, cost: false })),
    { name: "Затраты на закрытую сделку", note: `кейсы, не дошедшие до сделки, тоже оплачены`, value: unit.cost_per_deal, cost: true },
    { name: "Резерв на ошибки агента", note: "компенсации клиентам", value: unit.variable - unit.cost_per_deal, cost: true },
  ];
  const max = Math.max(...items.map((i) => i.value));
  root.querySelector("[data-bars]").innerHTML = items.map((i) => html`
    <div class="bar-row">
      <div>${i.name}<small>${i.note}</small></div>
      <div class="bar-wrap"><div class="bar ${i.cost ? "is-cost" : ""}" style="width:${(i.value / max) * 100}%"></div></div>
      <div class="v num">${i.cost ? "−" : ""}${rub(i.value)}</div>
    </div>`).join("");
  root.querySelector("[data-conflict]").innerHTML = html`Конфликт интересов: скидка уменьшает кредит на ${rub(unit.conflict.loan_reduction)},
    а маржу банка — на ${rub(unit.conflict.margin_loss)}. Это в ${unit.conflict.ratio?.toLocaleString("ru-RU") ?? "—"} раз меньше вклада сделки, поэтому банку выгодно играть на стороне покупателя.`;

  root.querySelector("[data-scen]").innerHTML = html`
    <thead><tr><th>Сценарий</th><th class="r">Экономия</th><th class="r">Инкр.</th><th class="r">Конверсия</th><th class="r">Вклад / сделка</th><th class="r">Сделок</th><th class="r">Пост. затраты</th><th class="r">ROI без помощника</th><th class="r">ROI</th></tr></thead>
    <tbody>${html.raw(scenarios.map((s) => html`
      <tr class="${s.roi < 0 ? "is-neg" : ""}">
        <td>${s.title}</td><td class="r num">${pct(s.inputs.savings_pct)}</td><td class="r num">${pct(s.inputs.incrementality, 0)}</td>
        <td class="r num">${pct(s.inputs.conversion, 0)}</td><td class="r num">${rub(s.contribution)}</td>
        <td class="r num">${s.inputs.deals.toLocaleString("ru-RU")}</td><td class="r num">${s.inputs.fixed_costs / 1e6} млн ₽</td>
        <td class="r num ${s.roi_core < 0 ? "neg" : ""}">${fmtRoi(s.roi_core)}</td>
        <td class="r num roi">${fmtRoi(s.roi)}</td>
      </tr>`).join(""))}</tbody>`;
}

