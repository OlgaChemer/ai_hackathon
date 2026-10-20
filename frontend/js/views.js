// Отрисовка панелей этапов (кроме звонка — он в call.js).
import { html, rub, mln, pct, plural, shortDate } from "./format.js";

export const EXAMPLES = [
  "Ищу 2-комнатную до 9 млн рядом со школой, у нас ребёнок",
  "Двушка в Казани до 9 млн, не первый этаж",
  "1-комнатная до 7 млн в Ново-Савиновском районе",
];

export function welcome() {
  return html`
  <section class="panel welcome">
    <h1>Какую квартиру ищем?</h1>
    <p>Агент найдёт варианты, проверит юридическую чистоту, сам позвонит продавцу и сторгуется.
       Вы только подтверждаете решения — и платите 15% от сэкономленного, если агент сэкономил.</p>
    <div class="examples">
      ${html.raw(EXAMPLES.map((e) => html`<button class="example" data-example="${e}">${e}</button>`).join(""))}
    </div>
    <div class="how">
      <div><h3>Ищет и проверяет</h3><p>Справедливая цена по закрытым сделкам, проверка по ЕГРН, ФССП и реестру банкротств.</p></div>
      <div><h3>Торгуется голосом</h3><p>Звонит продавцу как ИИ-ассистент покупателя, с аргументами и в ваших рамках.</p></div>
      <div><h3>Доводит до ключей</h3><p>Ипотека, страхование, безопасные расчёты и электронная регистрация — в одном плане.</p></div>
    </div>
  </section>`;
}

export function waiting(title, text) {
  return html`<section class="panel"><h2 class="panel-title">${title}</h2><p class="panel-lead">${text}</p></section>`;
}

export function profile(p) {
  return html`
  <section class="panel">
    <h2 class="panel-title">Задача понята</h2>
    <p class="panel-lead">${p.summary}</p>
    <dl class="facts">
      <div><dt>Город</dt><dd>${p.city}</dd></div>
      <div><dt>Комнат</dt><dd>${p.rooms}</dd></div>
      <div><dt>Бюджет до</dt><dd class="num">${rub(p.budget_max)}</dd></div>
      <div><dt>Районы</dt><dd>${p.districts?.length ? p.districts.join(", ") : "любые"}</dd></div>
      <div><dt>Школа рядом</dt><dd>${p.near_school ? "важно" : "не важно"}</dd></div>
      <div><dt>Первый этаж</dt><dd>${p.not_first_floor ? "не рассматриваем" : "можно"}</dd></div>
    </dl>
    <p class="hint">Разобрано: ${p.source === "rules" ? "правилами (без модели)" : "языковой моделью"}.</p>
  </section>`;
}

export function mortgage(m) {
  const cards = m.scenarios.map((s) => {
    const best = s.title === m.recommended;
    const status = s.savings_gap
      ? html`<span class="tag tag-amber">не хватает ${rub(s.savings_gap)}: копить ${s.months_to_save} мес.</span>`
      : s.fits_income ? html`<span class="tag tag-green">по силам</span>` : html`<span class="tag tag-red">высокий платёж</span>`;
    return html`
      <article class="scenario ${best ? "is-best" : ""}">
        <h3>${s.title}</h3>
        <div class="big num">${rub(s.monthly)}<small style="font-size:14px;font-weight:500"> / мес</small></div>
        <dl>
          <dt>Кредит</dt><dd class="num">${rub(s.loan)}</dd>
          <dt>Первый взнос</dt><dd class="num">${rub(s.down_payment)}</dd>
          <dt>Переплата</dt><dd class="num">${mln(s.overpay)}</dd>
          <dt>Нужен доход</dt><dd class="num">${rub(s.income_needed)}</dd>
        </dl>
        <p style="margin:12px 0 0">${html.raw(status)}</p>
      </article>`;
  });
  return html`
  <section class="panel">
    <h2 class="panel-title">${m.consent ? `Ипотека предодобрена на ${mln(m.approved)}` : "Бюджет без предодобрения"}</h2>
    <p class="panel-lead">Три сценария по ставке ${pct(m.rate)} (допущение для демо). Расходы на сделку — около ${rub(m.deal_costs)}.</p>
    <div class="scenarios">${html.raw(cards.join(""))}</div>
    <p class="hint">Каждые 100 000 ₽, которые агент сторгует, снижают платёж на <b class="num">${rub(m.per_100k_discount)}</b> в месяц.</p>
  </section>`;
}

function listingRow(r, legal) {
  const l = r.listing;
  const f = r.fair;
  const delta = (l.price - f.fair) / f.fair;
  const tag = delta > 0.01
    ? html`<span class="tag tag-amber">дороже рынка на ${pct(delta)}</span>`
    : delta < -0.03 ? html`<span class="tag tag-green">ниже рынка на ${pct(-delta)}</span>` : html`<span class="tag tag-green">по рынку</span>`;
  const blocked = legal?.status === "block";
  return html`
    <article class="listing ${blocked ? "is-blocked" : ""}">
      <div class="thumb" aria-hidden="true"></div>
      <div>
        <h3>${l.rooms}-комн., ${l.area} м², ${l.address}</h3>
        <div class="meta">${l.district} р-н · ${l.floor}/${l.floors} эт. · ${l.year} г. · ${l.condition} · школа ${l.school.minutes} мин пешком</div>
        <div style="margin-top:6px">${html.raw(tag)} ${html.raw(blocked ? html`<span class="tag tag-red">исключён проверкой</span>` : "")}</div>
      </div>
      <div class="side">
        <div class="price num">${rub(l.price)}</div>
        <div class="score">${l.days_on_market} ${plural(l.days_on_market, "день", "дня", "дней")} в продаже · оценка ${r.score}</div>
      </div>
    </article>`;
}

export function search(found, legalById = {}) {
  return html`
  <section class="panel">
    <h2 class="panel-title">Короткий список</h2>
    <p class="panel-lead">Из ${found.total} объявлений по параметрам подходят ${found.matched}. Отсортировано по цене к рынку, школе, состоянию и потенциалу торга.</p>
    <div class="listings">${html.raw(found.shortlist.map((r) => listingRow(r, legalById[r.listing.id])).join(""))}</div>
  </section>`;
}

export function fair(rows, shortlist) {
  const lo = Math.min(...rows.map((r) => Math.min(r.low, r.price))) * 0.985;
  const hi = Math.max(...rows.map((r) => Math.max(r.high, r.price))) * 1.015;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const byId = Object.fromEntries(shortlist.map((s) => [s.listing.id, s.listing]));
  const items = rows.map((r) => {
    const l = byId[r.id];
    const d = r.price - r.fair;
    return html`
      <div class="fair-row">
        <div class="who">${l.address}<small>${l.district} р-н, ${l.area} м²</small></div>
        <div class="fair-scale" aria-label="Коридор ${rub(r.low)}–${rub(r.high)}, цена ${rub(r.price)}">
          <div class="track"></div>
          <div class="band" style="left:${x(r.low)}%;width:${x(r.high) - x(r.low)}%"></div>
          <div class="tick" style="left:${x(r.price)}%" title="Цена объявления ${rub(r.price)}"></div>
        </div>
        <div class="delta num" style="color:${d > 0 ? "var(--amber)" : "var(--green-strong)"}">${d > 0 ? "+" : "−"}${rub(Math.abs(d))}</div>
      </div>`;
  });
  return html`
  <section class="panel">
    <h2 class="panel-title">Справедливая цена</h2>
    <p class="panel-lead">Медиана цены м² по закрытым сделкам-аналогам за полгода с поправками на состояние, год дома и этаж. Если объявление правее коридора — есть что сторговать.</p>
    ${html.raw(items.join(""))}
    <div class="legend"><span class="l-band">коридор справедливой цены</span><span class="l-tick">цена в объявлении</span></div>
  </section>`;
}

export function legal(checks, shortlist) {
  const byId = Object.fromEntries(shortlist.map((s) => [s.listing.id, s.listing]));
  const tags = { ok: ["tag-green", "чисто"], warn: ["tag-amber", "есть оговорки"], block: ["tag-red", "исключён"] };
  const cards = checks.map((c) => {
    const [cls, label] = tags[c.status];
    return html`
      <article class="legal ${c.status === "block" ? "is-block" : ""}">
        <header><h3>${byId[c.listing_id].address}</h3><span class="tag ${cls}">${label}</span></header>
        <ul class="checks">
          ${html.raw(c.checks.map((k) => html`<li><span class="ic ic-${k.status}"></span><span class="src">${k.source}</span><span><b>${k.title}:</b> ${k.note}</span></li>`).join(""))}
        </ul>
      </article>`;
  });
  return html`
  <section class="panel">
    <h2 class="panel-title">Проверка объекта и продавца</h2>
    <p class="panel-lead">ЕГРН, ФССП, реестр банкротств, нотариат и БТИ. Объекты с арестом, банкротством или продажей по доверенности агент исключает сам.</p>
    <div class="legal-grid">${html.raw(cards.join(""))}</div>
  </section>`;
}

export function viewing(v) {
  return html`
  <section class="panel">
    <h2 class="panel-title">Просмотр</h2>
    <p class="panel-lead">Агент договорился с продавцом и подготовил чек-лист с учётом найденных недостатков.</p>
    <div class="two-col">
      <div><h3 class="sub">Время</h3><div class="slots">${html.raw(v.slots.map((s) => html`<div class="slot">${s}</div>`).join(""))}</div></div>
      <div><h3 class="sub">Что проверить на месте</h3><ul class="checklist">${html.raw(v.checklist.map((c) => html`<li>${c}</li>`).join(""))}</ul></div>
    </div>
  </section>`;
}

export function deal(d) {
  const costs = Object.entries(d.costs).map(([k, v]) => html`<dt>${k}</dt><dd class="num">${rub(v)}</dd>`).join("");
  return html`
  <section class="panel">
    <h2 class="panel-title">План сделки</h2>
    <p class="panel-lead">Ипотека ${rub(d.loan)} на ${d.years} лет, платёж ${rub(d.monthly)} в месяц — на ${rub(d.monthly_saved)} меньше благодаря торгу.</p>
    <div class="two-col">
      <ol class="timeline">
        ${html.raw(d.timeline.map((t) => html`<li><time>${shortDate(t.date)}</time><span class="pt"></span><div><strong>${t.title}</strong><span>${t.note}</span></div></li>`).join(""))}
      </ol>
      <div><h3 class="sub">Расходы на сделку</h3><dl class="costs">${html.raw(costs)}</dl></div>
    </div>
  </section>`;
}

export function summary(s) {
  const lines = s.sber.lines.map((l) => html`<dt>${l.name}</dt><dd class="num">${rub(l.value)}</dd>`).join("");
  return html`
  <section class="panel">
    <div class="receipt">
      <div>
        <h2 class="panel-title">Сделка проведена</h2>
        <p class="panel-lead">${s.listing.address}, ${s.listing.district} р-н</p>
        <div class="hero-num num">−${rub(s.savings)}</div>
        <dl class="ledger">
          <dt>Цена в объявлении (зафиксирована в журнале)</dt><dd class="num">${rub(s.base_price)}</dd>
          <dt>Итоговая цена</dt><dd class="num">${rub(s.final_price)}</dd>
          <dt>Плата агенту, 15% от экономии</dt><dd class="num">${rub(s.fee)}</dd>
          <dt class="total">Чистая выгода клиента</dt><dd class="total num">${rub(s.net)}</dd>
          <dt>Платёж по ипотеке ниже на</dt><dd class="num">${rub(s.monthly_saved)} / мес</dd>
          <dt>Сэкономлено времени</dt><dd>${s.hours_saved}</dd>
        </dl>
      </div>
      <div class="sber-box">
        <h3>Что получает Сбер с этой сделки</h3>
        <p class="hint" style="margin-top:0">Доход и экономия, ₽ — по допущениям бизнес-модели</p>
        <dl class="ledger">${html.raw(lines)}<dt class="total">Итого</dt><dd class="total num">${rub(s.sber.revenue)}</dd>
          <dt>Маржинальный вклад</dt><dd class="num">${rub(s.sber.contribution)}</dd></dl>
        <h3 style="margin-top:20px">Следующие события клиента</h3>
        <div class="next">${html.raw(s.next_events.map((e) => html`<span>${e}</span>`).join(""))}</div>
      </div>
    </div>
  </section>`;
}
