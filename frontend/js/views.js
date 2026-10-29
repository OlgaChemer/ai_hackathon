// Отрисовка панелей этапов (кроме звонка — он в call.js, и графика — chart.js).
import { html, rub, mln, pct, plural, shortDate } from "./format.js";

const EXAMPLES = [
  "Ищу 2-комнатную в Казани до 9 млн, у нас ребёнок — нужна школа рядом",
  "Двушка в Новосибирске до 7 млн, не первый этаж, кухня от 9 метров",
  "Ищу квартиру для семьи",
];

const ICON_UP = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M7 10v11H3V10h4zm2 11h8.3a2 2 0 0 0 2-1.6l1.4-7A2 2 0 0 0 18.7 10H14l.7-3.6a2 2 0 0 0-.5-1.8L13 3.4 8.6 8.6A2 2 0 0 0 8 10v9a2 2 0 0 0 1 2z" fill="currentColor"/></svg>';
const ICON_DOWN = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M17 14V3h4v11h-4zm-2-11H6.7a2 2 0 0 0-2 1.6l-1.4 7A2 2 0 0 0 5.3 14H10l-.7 3.6a2 2 0 0 0 .5 1.8l1.2 1.2 4.4-5.2A2 2 0 0 0 16 14V5a2 2 0 0 0-1-2z" fill="currentColor"/></svg>';

export function welcome(cities = []) {
  return html`
  <section class="panel welcome">
    <h1>Какую квартиру ищем?</h1>
    <p>Опишите словами или голосом: город, бюджет, сколько комнат и что для вас важно.
       Агент найдёт объявления, учтёт ваши отметки, позвонит продавцу, сторгуется и договорится о просмотре.
       А финансовый помощник подскажет, как собрать первоначальный взнос.</p>
    <div class="examples">
      ${html.raw(EXAMPLES.map((e) => html`<button class="example" data-example="${e}">${e}</button>`).join(""))}
    </div>
    <p class="hint">Объявления — с Циана${cities.length ? `: ${cities.join(", ")}` : ""}.</p>
  </section>`;
}

export function waiting(title, text) {
  return html`<section class="panel"><h2 class="panel-title">${title}</h2><p class="panel-lead">${text}</p></section>`;
}

function marketBlock(p) {
  const m = p.market;
  const lo = m.p25 * 0.8, hi = m.p75 * 1.15;
  const x = (v) => Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
  return html`
    <div class="market">
      <h3 class="sub">Цены рынка по запросу (${m.count} свежих объявлений Циана)</h3>
      <div class="market-scale" aria-label="Обычно от ${mln(m.p25)} до ${mln(m.p75)}, середина ${mln(m.median)}">
        <div class="track"></div>
        <div class="band" style="left:${x(m.p25)}%;width:${x(m.p75) - x(m.p25)}%"></div>
        <div class="tick" style="left:${x(m.median)}%"></div>
        ${html.raw(p.budget_max ? html`<div class="budget" style="left:${x(p.budget_max)}%"><span>ваш бюджет</span></div>` : "")}
      </div>
      <div class="market-legend num"><span>${mln(m.p25)}</span><span>середина ${mln(m.median)}</span><span>${mln(m.p75)}</span></div>
    </div>`;
}

export function profile(p) {
  const rooms = p.rooms === 0 ? "студия" : p.rooms;
  return html`
  <section class="panel">
    <h2 class="panel-title">Задача понята</h2>
    <p class="panel-lead">${p.summary}</p>
    <dl class="facts">
      <div><dt>Город</dt><dd>${p.city}${p.okrug ? `, ${p.okrug}` : ""}</dd></div>
      <div><dt>Комнат</dt><dd>${rooms}</dd></div>
      <div><dt>Бюджет до</dt><dd class="num">${rub(p.budget_max)}${html.raw(p.budget_source ? html`<small class="dd-note">${p.budget_source}</small>` : "")}</dd></div>
      <div><dt>Районы</dt><dd>${p.districts?.length ? p.districts.join(", ") : "любые"}</dd></div>
      <div><dt>Школа рядом</dt><dd>${p.near_school ? "важно" : "не важно"}</dd></div>
      <div><dt>Кухня</dt><dd>${p.kitchen_min ? `от ${p.kitchen_min} м²` : "любая"}</dd></div>
    </dl>
    ${html.raw(p.wishes?.length ? html`<p class="hint">Ещё учту: ${p.wishes.join("; ")}.</p>` : "")}
    ${html.raw(p.market ? marketBlock(p) : "")}
  </section>`;
}

// ------------------------------------------------------------------ финансы

export function finance(d, opened = {}, selected) {
  if (d.mode === "cash") {
    return html`
    <section class="panel">
      <h2 class="panel-title">Покупка без ипотеки</h2>
      <p class="panel-lead">Ипотека и план накоплений не нужны. Деньги продавцу уйдут через сервис безопасных расчётов — только после регистрации права в Росреестре.</p>
      <div class="alloc">
        <div class="alloc-row">
          <div><strong>Накопительный счёт — пока ищем квартиру</strong><small>по желанию: деньги доступны в любой момент и приносят доход (${pct(0.10)} годовых — допущение)</small></div>
          <div class="num">${rub(d.price)}<small>до ${rub(Math.round((d.price * 0.10 / 12) * 2 / 100) * 100)} за 2 месяца</small></div>
          <button class="btn ${opened.savings_account ? "btn-ghost" : "btn-primary"} btn-small" data-open="savings_account" data-amount="${d.price}" ${opened.savings_account ? "disabled" : ""}>${opened.savings_account ? "Открыт" : "Открыть"}</button>
        </div>
      </div>
    </section>`;
  }
  if (d.mode === "mortgage") return mortgage(d.mortgage);
  const f = d.finance;
  const sel = f.strategies.find((s) => s.key === (selected || f.risk)) || f.strategies[0];
  const alloc = sel.mix.map((m) => ({ ...m, amount_now: Math.round((f.available * m.share) / 1000) * 1000, monthly: Math.round((f.monthly * m.share) / 1000) * 1000 }));
  const months = (m) => (m == null ? "дольше 10 лет" : m === 0 ? "уже хватает" : `${m} мес.`);
  return html`
  <section class="panel">
    <h2 class="panel-title">Финансовая подготовка</h2>
    <p class="panel-lead">${f.verdict}</p>
    <div class="fin-kpis">
      <div><span class="k">Нужно на взнос 20% и сделку</span><b class="num">${rub(f.need)}</b></div>
      <div><span class="k">Можно направить сейчас</span><b class="num">${rub(f.available)}</b><small>подушка ${rub(f.cushion)} остаётся</small></div>
      <div class="${f.gap ? "is-gap" : "is-ok"}"><span class="k">${f.gap ? "Не хватает" : "Хватает с запасом"}</span><b class="num">${rub(f.gap || f.available - f.need)}</b></div>
      <div><span class="k">Откладывать в месяц</span><b class="num">${rub(f.monthly)}</b><small>70% от свободных ${rub(f.free_monthly)}</small></div>
    </div>
    <div class="strategy-tabs" role="radiogroup" aria-label="Стратегия">
      ${html.raw(f.strategies.map((s) => html`
        <button class="strategy s-${s.key} ${s.key === sel.key ? "is-on" : ""}" role="radio" aria-checked="${s.key === sel.key}" data-strategy="${s.key}">
          <span class="swatch"></span><strong>${s.title}</strong>
          <span class="num">${pct(s.rate)} годовых · ${months(s.months)}</span>
          <small>${s.low === s.high ? "доход фиксирован" : `доходность от ${pct(s.low)} до ${pct(s.high)}`}${s.months_worst !== s.months ? ` · в плохом случае ${months(s.months_worst)}` : ""}</small>
        </button>`).join(""))}
    </div>
    <div class="chart" data-chart data-selected="${sel.key}"></div>
    ${html.raw(f.risk_note ? html`<p class="hint warn-text">${f.risk_note}</p>` : "")}
    <h3 class="sub" style="margin-top:22px">План: ${sel.title.toLowerCase()} стратегия</h3>
    <div class="alloc">
      ${html.raw(alloc.map((a) => html`
        <div class="alloc-row">
          <div><strong>${a.name}</strong><small>${a.note} · ${pct(a.rate)} годовых</small></div>
          <div class="num">${rub(a.amount_now)}<small>+ ${rub(a.monthly)} в мес.</small></div>
          <button class="btn ${opened[a.product] ? "btn-ghost" : "btn-primary"} btn-small" data-open="${a.product}" data-amount="${a.amount_now}" ${opened[a.product] ? "disabled" : ""}>${opened[a.product] ? "Открыт" : "Открыть"}</button>
        </div>`).join(""))}
    </div>
    <p class="hint">${f.assumption} Риск-профиль в проде подтвердит анкета брокера.</p>
  </section>
  ${html.raw(mortgage(d.mortgage))}`;
}

function mortgage(m) {
  const cards = m.scenarios.map((s) => {
    const best = s.title === m.recommended;
    const status = s.savings_gap
      ? html`<span class="tag tag-amber">на взнос не хватает ${rub(s.savings_gap)}</span>`
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
    <h2 class="panel-title">Ипотека</h2>
    <p class="panel-lead">Три сценария по ставке ${pct(m.rate)} (допущение). Каждые 100 000 ₽, которые агент сторгует, — это минус ${rub(m.per_100k_discount)} в месяц.</p>
    <div class="scenarios">${html.raw(cards.join(""))}</div>
  </section>`;
}

// ------------------------------------------------------------------ поиск и цена

function sourceChip(r) {
  const label = { live: "живые данные", cached: "кеш сессии", snapshot: "сохранённый срез", unavailable: "недоступен" }[r.status] || r.status;
  const cls = r.status === "unavailable" ? "tag-red" : r.status === "snapshot" ? "tag-amber" : "tag-green";
  return html`<span class="source-chip"><b>${r.source}</b> ${r.count} ${plural(r.count, "объявление", "объявления", "объявлений")} <span class="tag ${cls}">${label}</span></span>`;
}

export function search(d) {
  const rows = d.rows || [];
  if (!rows.length) {
    return html`<section class="panel"><h2 class="panel-title">Поиск</h2><div class="sources">${html.raw(d.report.map(sourceChip).join(""))}</div>
      <p class="panel-lead">${d.report.map((r) => `${r.source}: ${r.note}`).join("; ")}</p></section>`;
  }
  const lo = Math.min(...rows.map((r) => Math.min(r.low, r.price))) * 0.985;
  const hi = Math.max(...rows.map((r) => Math.max(r.high, r.price))) * 1.015;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const items = rows.map((r) => {
    const dlt = r.price - r.fair;
    return html`
      <div class="fair-row">
        <div class="who">${r.address}<small>${r.district || "район не указан"}, ${r.area} м²</small></div>
        <div class="fair-scale" aria-label="Коридор ${rub(r.low)}–${rub(r.high)}, цена ${rub(r.price)}">
          <div class="track"></div>
          <div class="band" style="left:${x(r.low)}%;width:${x(r.high) - x(r.low)}%"></div>
          <div class="tick" style="left:${x(r.price)}%" title="Цена объявления ${rub(r.price)}"></div>
        </div>
        <div class="delta num" style="color:${dlt > 0 ? "var(--amber)" : "var(--green-strong)"}">${dlt > 0 ? "+" : "−"}${rub(Math.abs(dlt))}</div>
      </div>`;
  });
  return html`
  <section class="panel">
    <h2 class="panel-title">Поиск и справедливая цена</h2>
    <div class="sources">${html.raw(d.report.map(sourceChip).join(""))}</div>
    <p class="panel-lead">Прошли по вашим критериям (порог ${d.threshold ? Math.round(d.threshold * 100) : 70}%): ${d.matched} из ${d.total}. Справедливая цена — медиана цены м² ближайших похожих квартир (по координатам, площадь ±30%) с поправками на ремонт, год дома и этаж, минус 3% на торг. Коридор — разброс цен аналогов. Если объявление правее коридора — есть что сторговать.</p>
    ${html.raw(items.join(""))}
    <div class="legend"><span class="l-band">коридор справедливой цены</span><span class="l-tick">цена в объявлении</span></div>
  </section>`;
}

// ------------------------------------------------------------------ подбор

function priceTag(c) {
  const f = c.fair;
  if (c.price > f.high) return html`<span class="tag tag-amber" title="${f.basis}">дороже аналогов на ${pct((c.price - f.fair) / f.fair)}</span>`;
  if (c.price < f.low) return html`<span class="tag tag-green" title="${f.basis}">дешевле аналогов на ${pct((f.fair - c.price) / f.fair)}</span>`;
  return html`<span class="tag tag-green" title="${f.basis}">в коридоре аналогов</span>`;
}

function metroText(m) {
  if (!m || !m.minutes) return "";
  return `м. ${m.name || ""} ${m.minutes} мин ${m.mode}`.replace("  ", " ");
}

function matchTag(m) {
  if (!m || m.pct === null || m.pct === undefined) return "";
  const good = m.pct >= 0.7;
  const label = `совпало ${m.ok} из ${m.checked} · ${Math.round(m.pct * 100)}%`;
  return `<span class="tag ${good ? "tag-green" : "tag-amber"}" title="Совпадение по вашим критериям">${label}</span>`;
}

function matchMissed(m) {
  if (!m || !m.failed || !m.failed.length) return "";
  return `<p class="meta">Не совпало: ${m.failed.join(", ")}</p>`;
}

function offerCard(c, vote) {
  const rooms = c.rooms === 0 ? "Студия" : `${c.rooms}-комн.`;
  const floor = c.floor ? `${c.floor}${c.floors ? `/${c.floors}` : ""} эт.` : "";
  const meta = [c.district, c.address, metroText(c.metro)].filter(Boolean).join(" · ");
  const params = [c.kitchen ? `кухня ${c.kitchen} м²` : null, c.year ? `${c.year} г.` : null, c.condition, c.material].filter(Boolean).join(" · ");
  return html`
  <article class="offer ${vote === "up" ? "is-liked" : vote === "down" ? "is-disliked" : ""}" data-id="${c.id}">
    <a class="offer-photo" ${c.url ? html.raw(html`href="${c.url}" target="_blank" rel="noopener noreferrer"`) : ""} aria-label="Открыть объявление на площадке">
      ${html.raw(c.photo ? html`<img src="${c.photo}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : "")}
      <span class="offer-src">${c.source_name}</span>
    </a>
    <div class="offer-body">
      <div class="offer-price"><span class="num">${rub(c.price)}</span> ${html.raw(priceTag(c))}</div>
      <h3>${rooms}, ${c.area} м²${floor ? `, ${floor}` : ""}</h3>
      ${html.raw(matchTag(c.match))}
      <p class="meta">${meta}</p>
      ${html.raw(params ? html`<p class="meta">${params}</p>` : "")}
      ${html.raw(matchMissed(c.match))}
      ${html.raw(c.missing?.length ? html`<p class="missing">Нет в объявлении: ${c.missing.join(", ")} — уточню по телефону</p>` : "")}
    </div>
    <div class="offer-actions">
      <button class="vote vote-up" data-vote="up" aria-pressed="${vote === "up"}" title="Нравится">${html.raw(ICON_UP)}<span>Нравится</span></button>
      <button class="vote vote-down" data-vote="down" aria-pressed="${vote === "down"}" title="Не подходит">${html.raw(ICON_DOWN)}<span>Не то</span></button>
    </div>
  </article>`;
}

export function selection(d, marks) {
  const vote = (id) => (marks.liked.has(id) ? "up" : marks.disliked.has(id) ? "down" : null);
  return html`
  <section class="panel">
    <div class="sel-head">
      <div>
        <h2 class="panel-title">Подборка${d.round > 1 ? ` · раунд ${d.round}` : ""}</h2>
        <p class="panel-lead">Отметьте, что нравится и что нет, — агент учтёт это в следующей подборке. Пожелание можно написать в чат или сказать голосом.</p>
      </div>
    </div>
    ${html.raw(d.learned?.length ? html`<div class="learned"><span>Учёл:</span>${html.raw(d.learned.map((l) => html`<span class="chip">${l}</span>`).join(""))}</div>` : "")}
    <div class="offers">${html.raw(d.cards.map((c) => offerCard(c, vote(c.id))).join(""))}</div>
    <div class="sel-actions">
      <button class="btn btn-ghost" data-selection="more">Подобрать ещё с учётом отметок</button>
      <button class="btn btn-primary" data-selection="proceed">Звонить по отмеченным <span data-liked-count>(${marks.liked.size})</span></button>
    </div>
  </section>`;
}

// ------------------------------------------------------------------ проверка, просмотр, сделка, итог

export function legal(d) {
  const byId = Object.fromEntries(d.listings.map((l) => [l.id, l]));
  const tags = { ok: ["tag-green", "по объявлению чисто"], warn: ["tag-amber", "есть оговорки"] };
  const cards = d.checks.map((c) => {
    const [cls, label] = tags[c.status] || tags.ok;
    return html`
      <article class="legal">
        <header><h3>${byId[c.listing_id].address}</h3><span class="tag ${cls}">${label}</span></header>
        <ul class="checks">
          ${html.raw(c.checks.map((k) => html`<li><span class="ic ic-${k.status}"></span><span class="src">${k.source}</span><span><b>${k.title}:</b> ${k.note}</span></li>`).join(""))}
        </ul>
      </article>`;
  });
  return html`
  <section class="panel">
    <h2 class="panel-title">Проверка рисков</h2>
    <p class="panel-lead">По данным объявления: проверка площадкой, апартаменты, торги и тревожные слова в описании (доли, наследство, перепланировка). Выписки ЕГРН, ФССП и реестра банкротств агент закажет после выбора квартиры.</p>
    <div class="legal-grid">${html.raw(cards.join(""))}</div>
  </section>`;
}

export function viewing(v) {
  const learned = Object.entries(v.learned || {});
  return html`
  <section class="panel">
    <h2 class="panel-title">Просмотр</h2>
    <p class="panel-lead">${v.listing.address}${v.listing.url ? "" : ""}. Время согласовано с продавцом во время звонка.</p>
    <div class="two-col">
      <div>
        <h3 class="sub">Когда</h3>
        <div class="slots">${html.raw(v.slots.map((s) => html`<div class="slot ${s === v.slot ? "is-on" : ""}">${s}${s === v.slot ? " — договорились" : ""}</div>`).join(""))}</div>
        ${html.raw(learned.length ? html`<h3 class="sub" style="margin-top:20px">Что агент узнал у продавца</h3><dl class="costs">${html.raw(learned.map(([k, val]) => html`<dt>${k}</dt><dd>${val}</dd>`).join(""))}</dl>` : "")}
      </div>
      <div><h3 class="sub">Что проверить на месте</h3><ul class="checklist">${html.raw(v.checklist.map((c) => html`<li>${c}</li>`).join(""))}</ul></div>
    </div>
  </section>`;
}

export function deal(d) {
  const costs = Object.entries(d.costs).map(([k, v]) => html`<dt>${k}</dt><dd class="num">${rub(v)}</dd>`).join("");
  return html`
  <section class="panel">
    <h2 class="panel-title">План сделки</h2>
    <p class="panel-lead">${d.cash ? "Без ипотеки: деньги через сервис безопасных расчётов, регистрация — электронно." : `Взнос ${rub(d.down_payment)}, ипотека ${rub(d.loan)} на ${d.years} лет, платёж ${rub(d.monthly)} в месяц — на ${rub(d.monthly_saved)} меньше благодаря торгу.`}</p>
    <div class="two-col">
      <ol class="timeline">
        ${html.raw(d.timeline.map((t) => html`<li><time>${shortDate(t.date)}</time><span class="pt"></span><div><strong>${t.title}</strong><span>${t.note}</span></div></li>`).join(""))}
      </ol>
      <div><h3 class="sub">Расходы на сделку</h3><dl class="costs">${html.raw(costs)}</dl></div>
    </div>
  </section>`;
}

export function summary(s) {
  const lines = s.sber.lines.filter((l) => l.value > 0).map((l) => html`<dt>${l.name}</dt><dd class="num">${rub(l.value)}</dd>`).join("");
  const fin = s.sber.finance.map((l) => html`<dt>${l.name} (${rub(l.amount)})</dt><dd class="num">${rub(l.value)} / год</dd>`).join("");
  return html`
  <section class="panel">
    <div class="receipt">
      <div>
        <h2 class="panel-title">Сделка проведена</h2>
        <p class="panel-lead">${s.listing.address}, ${s.listing.district || s.listing.city}${s.listing.url ? "" : ""}</p>
        <div class="hero-num num">−${rub(s.savings)}</div>
        <dl class="ledger">
          <dt>Цена в объявлении (зафиксирована в журнале)</dt><dd class="num">${rub(s.base_price)}</dd>
          <dt>Итоговая цена</dt><dd class="num">${rub(s.final_price)}</dd>
          <dt>Плата агенту, 15% от экономии</dt><dd class="num">${rub(s.fee)}</dd>
          <dt class="total">Чистая выгода клиента</dt><dd class="total num">${rub(s.net)}</dd>
          ${html.raw(s.monthly_saved ? html`<dt>Платёж по ипотеке ниже на</dt><dd class="num">${rub(s.monthly_saved)} / мес</dd>` : "")}
          <dt>Сэкономлено времени</dt><dd>${s.hours_saved}</dd>
        </dl>
      </div>
      <div class="sber-box">
        <h3>Что получает Сбер</h3>
        <p class="hint" style="margin-top:0">С этой сделки, ₽ — по допущениям бизнес-модели</p>
        <dl class="ledger">${html.raw(lines)}<dt class="total">Итого со сделки</dt><dd class="total num">${rub(s.sber.revenue)}</dd></dl>
        ${html.raw(fin ? html`<h3 style="margin-top:18px">Финансовый помощник</h3><dl class="ledger">${html.raw(fin)}</dl>` : "")}
        <h3 style="margin-top:20px">Следующие события клиента</h3>
        <div class="next">${html.raw(s.next_events.map((e) => html`<span>${e}</span>`).join(""))}</div>
      </div>
    </div>
  </section>`;
}
