"""Конвейер агента: запрос -> финансы -> поиск -> подбор -> проверка -> звонок -> просмотр -> сделка -> итог.

Сессия выполняется фоновой задачей и пишет события в очередь; HTTP-слой отдаёт
их браузеру через SSE. Где решение за клиентом, конвейер ждёт POST /confirm.
Текст и голос клиента приходят через POST /message: ответ на уточняющий вопрос,
пожелание к подборке или просто заметка, которую агент учтёт дальше.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import economics, sources
from ..llm import LLMClient, LLMError
from . import criteria as C
from . import finance as F
from . import negotiation as N
from . import preferences as P
from . import prompts, seller_sim
from . import tools as T

log = logging.getLogger(__name__)

STEPS = ["Запрос", "Финансы", "Поиск", "Подбор", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог"]
BATCH = C.TOP          # клиенту показываем топ-10, а не 6
MAX_SELECTION_ROUNDS = 6


@dataclass
class Session:
    id: str
    query: str
    llm: LLMClient
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    seller_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    profile: dict = field(default_factory=dict)
    prefs: P.Prefs = field(default_factory=P.Prefs)
    notes: list[str] = field(default_factory=list)
    finance: dict | None = None
    opened: dict[str, int] = field(default_factory=dict)
    journal: dict = field(default_factory=dict)
    llm_degraded: bool = False

    # ------------------------------------------------------------- события
    async def emit(self, type_: str, **payload: Any) -> None:
        await self.events.put({"type": type_, **payload})

    async def step(self, n: int, status: str) -> None:
        await self.emit("step", step=n, title=STEPS[n - 1], status=status)

    async def say(self, text: str) -> None:
        await self.emit("message", role="agent", text=text)

    async def ask(self, kind: str, title: str, text: str, **payload: Any) -> dict:
        """Пауза до решения клиента. Возвращает {"approved": bool, "data": {...}}."""
        fut = asyncio.get_running_loop().create_future()
        self.pending[kind] = fut
        await self.emit("confirm", kind=kind, title=title, text=text, **payload)
        try:
            return await fut
        finally:
            self.pending.pop(kind, None)

    def resolve(self, kind: str, approved: bool, data: dict | None) -> bool:
        fut = self.pending.get(kind)
        if not fut or fut.done():
            return False
        fut.set_result({"approved": approved, "data": data or {}})
        return True

    async def receive_text(self, text: str, marks: dict | None = None) -> str:
        """Текст клиента: ответ на вопрос, пожелание к подборке или заметка на потом."""
        await self.emit("message", role="user", text=text)
        if self.resolve("clarify", True, {"text": text}):
            return "answer"
        marks = marks or {}
        if self.resolve("selection", True, {"action": "more", "note": text,
                                            "liked": marks.get("liked", []), "disliked": marks.get("disliked", [])}):
            return "feedback"
        self.notes.append(text)
        await self.say("Записал — учту при подборе и в разговоре с продавцом.")
        return "note"

    async def open_product(self, product: str, amount: int) -> None:
        if product not in F.PRODUCTS:
            return
        self.opened[product] = int(amount)
        name = F.PRODUCTS[product]["name"]
        await self.emit("product_opened", product=product, amount=int(amount))
        await self.say(f"Готово: {name} на {T.fmt(amount)} (демо — в проде откроется в СберБанк Онлайн в один клик).")


SESSIONS: dict[str, Session] = {}


def create_session(query: str, llm: LLMClient) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], query=query.strip(), llm=llm)
    SESSIONS[s.id] = s
    return s


# ------------------------------------------------------------- помощники


async def mark_degraded(s: Session, exc: Exception) -> None:
    if not s.llm_degraded and s.llm.name != "offline":
        s.llm_degraded = True
        await s.emit("provider", provider=s.llm.name, model=s.llm.model, degraded=True, reason=str(exc)[:160])


async def parse_request(s: Session, text: str, base: dict | None = None) -> dict:
    try:
        raw = await s.llm.chat_json(
            [{"role": "system", "content": prompts.PARSE_REQUEST}, {"role": "user", "content": text}],
            tag="parse_request",
        )
        profile = T.merge_profile(raw, base)
        profile["parsed_by"] = s.llm.name
    except (LLMError, KeyError, ValueError, TypeError) as exc:
        log.warning("parse_request fallback: %s", exc)
        await mark_degraded(s, exc)
        profile = T.merge_profile(T.parse_request_rules(text), base)
        profile["parsed_by"] = "rules"
    return profile


def ranked(pool: list[dict], meta: dict[str, dict], prefs: P.Prefs) -> list[dict]:
    """Сначала доля совпавших критериев (клиент просил 70%+), внутри — цена и пожелания."""
    for x in pool:
        meta[x["id"]]["total"] = round(meta[x["id"]]["score"] + P.adjust(x, prefs), 1)
    return sorted(pool, key=lambda x: (-((meta[x["id"]].get("match") or {}).get("pct") or 0),
                                       -meta[x["id"]]["total"]))


def card(x: dict, meta: dict) -> dict:
    m = meta[x["id"]]
    return {**x, "fair": m["fair"], "score": m["total"], "match": match_payload(m.get("match")),
            "missing": [q["label"] for q in T.missing_info(x, 4) if q["key"] != "owners"]}


def match_payload(m: dict | None) -> dict | None:
    """Процент совпадения для карточки: проваленные критерии — человеческими словами."""
    if not m:
        return None
    return {**m, "failed": [C.LABEL.get(k, k) for k in m.get("failed", [])]}


async def score_into(pool: list[dict], comps: list[dict], profile: dict, meta: dict) -> None:
    for x in pool:
        if x["id"] not in meta:
            fp = T.fair_price(x, comps)
            meta[x["id"]] = {"fair": fp, "score": T.score_listing(x, profile, fp)}


async def ask_affordability(s: Session, profile: dict, market: dict | None) -> tuple[dict, dict]:
    """Клиент не знает бюджет: считаем, на какую квартиру хватит с ипотекой."""
    r = await s.ask(
        "finance", "Сколько вы можете себе позволить",
        "Посчитаю бюджет по доходам, расходам и накоплениям (в демо — пример из СберБанк Онлайн, можно поправить): "
        "платёж по ипотеке — не больше половины дохода, первый взнос 20% — из накоплений к сроку покупки.",
        defaults=F.DEFAULT_INPUTS, price=None,
    )
    inputs = r["data"] if r["approved"] else {}
    aff = F.affordable(inputs, T.DEMO_RATE)
    why = ("ограничивает платёж" if aff["limited_by"] == "payment" else "ограничивает первый взнос")
    text = (f"Комфортный бюджет — до {T.fmt(aff['budget'])} ({why}): платёж до {T.fmt(aff['payment_limit'])} в месяц "
            f"даёт до {T.fmt(aff['by_payment'])}, а накопления к сроку покупки ({aff['horizon_months']} мес.) — "
            f"взнос на квартиру до {T.fmt(aff['by_down'])}.")
    if market and aff["budget"] < market["p25"]:
        text += (f" Это ниже большинства предложений: такие квартиры обычно стоят от {T.fmt(market['p25'])}. "
                 "Поищу в пределах бюджета; если вариантов мало — стоит рассмотреть меньше комнат или другой район.")
    await s.say(text)
    return inputs, aff


# ------------------------------------------------------------- конвейер


async def pipeline(s: Session) -> None:
    await s.emit("provider", provider=s.llm.name, model=s.llm.model, degraded=False)
    await s.emit("message", role="user", text=s.query)

    # 1. Запрос и уточнения: сначала город и комнаты, потом бюджет — с ценами рынка перед глазами
    await s.step(1, "active")
    profile = await parse_request(s, s.query)
    market = None
    fin_inputs = None
    for _ in range(5):
        unsupported = profile.get("city") and profile["city"] not in sources.SUPPORTED_CITIES
        missing = [m for m in T.missing_required(profile) if m != "budget_max"]
        chips: list[str] = []
        if unsupported:
            question = (f"Пока ищу объявления в городах: {', '.join(sources.SUPPORTED_CITIES)}. "
                        f"Город «{profile['city']}» ещё не подключён — какой из этих подойдёт?")
            profile["city"] = None
            chips = sources.SUPPORTED_CITIES[:4]
        elif missing:
            question = T.clarify_question(missing)
            chips = sources.SUPPORTED_CITIES[:4] if "city" in missing else ["1 комната", "2 комнаты", "3 комнаты"]
        elif profile.get("budget_max") is None and profile.get("budget_unknown"):
            fin_inputs, aff = await ask_affordability(s, profile, market)
            profile["budget_max"] = aff["budget"]
            profile["budget_source"] = "по доходам и накоплениям"
            continue
        elif profile.get("budget_max") is None:
            if market is None:
                market = await sources.market(profile)
            question = T.budget_question(profile, market)
            chips = T.budget_chips(market) + ["Не знаю — посчитай по моим доходам"]
        else:
            break
        if market and profile.get("budget_max") is None:  # показать цены рынка на экране, пока клиент выбирает бюджет
            await s.emit("result", step=1, kind="profile",
                         data={**profile, "market": market, "summary": "Осталось выбрать бюджет — ниже цены рынка по вашему запросу."})
        r = await s.ask("clarify", "Уточнение", question, chips=chips, market=market)
        text = r["data"].get("text", "")
        profile = await parse_request(s, text, base=profile)
        if profile.get("budget_max") is None and T.wants_budget_help(text):
            profile["budget_unknown"] = True
    profile["market"] = market
    if T.missing_required(profile):
        await s.say("Без города, бюджета и числа комнат искать не получится. Напишите их — и начнём заново.")
        await s.emit("done", status="cancelled")
        return

    # 1b. Пять обязательных критериев: добираем то, чего не назвал клиент
    for _ in range(3):
        miss = C.missing_core(profile)
        if not miss:
            break
        chips = [c for k in miss for c in (C.CHIPS.get(k) or [])][:6]
        r = await s.ask("clarify", "Критерии подбора", C.questions_text(profile, miss),
                        chips=chips, progress=C.progress(profile))
        profile = await parse_request(s, r["data"].get("text", ""), base=profile)
    for key in C.missing_core(profile):  # не ответил — не блокируем поиск, фиксируем «не важно»
        if key == "condition":
            profile["condition"] = "any"
        elif key == "metro_max_minutes":
            profile["metro_any"] = True
    if C.missing_core(profile):
        await s.say("Без города, бюджета, комнат, состояния и метро подобрать не смогу — уточните их, пожалуйста.")
        await s.emit("done", status="cancelled")
        return

    # 1c. Дополнительные критерии: спрашиваем до пяти, минимум три
    for _ in range(3):
        if C.extra_count(profile) >= C.EXTRA_MIN:
            break
        keys = C.missing_extra(profile)[:C.EXTRA_MAX]
        if not keys:
            break
        chips = [c for k in keys for c in (C.CHIPS.get(k) or [])][:6]
        r = await s.ask("clarify", "Дополнительные критерии", C.questions_text(profile, keys),
                        chips=chips, progress=C.progress(profile))
        profile = await parse_request(s, r["data"].get("text", ""), base=profile)
    for key in C.missing_extra(profile)[:C.EXTRA_MAX]:  # «не важно» засчитываем как ответ
        profile[f"{key}_any"] = True
    profile["summary"] = T.profile_summary(profile)
    s.profile = profile
    s.prefs = P.Prefs(area_min=profile.get("area_min"), kitchen_min=profile.get("kitchen_min"),
                      avoid_first=bool(profile.get("not_first_floor")), metro_max=profile.get("metro_max_minutes"))
    await s.emit("result", step=1, kind="profile", data=profile)
    await s.say(profile["summary"] + " Проверьте «красные линии» — в их пределах я действую сам.")
    r = await s.ask(
        "red_lines", "Красные линии",
        "Агент сам ищет, звонит и торгуется, но не выходит за эти рамки. Любую итоговую цену подтверждаете вы.",
        payload={"budget_max": profile["budget_max"], "rooms": profile["rooms"], "city": profile["city"],
                 "authority_rule": "не выше верхней границы справедливой цены и бюджета"},
    )
    if not r["approved"]:
        await s.say("Хорошо, остановился. Поправьте запрос, и начнём заново.")
        await s.emit("done", status="cancelled")
        return
    if r["data"].get("budget_max"):
        profile["budget_max"] = int(r["data"]["budget_max"])
    await s.step(1, "done")

    # 2. Финансы — только то, что нужно этому клиенту: наличные / ипотека / накопить на взнос
    await s.step(2, "active")
    budget = profile["budget_max"]
    payment = profile.get("payment")
    fin = F.plan(budget, fin_inputs) if fin_inputs is not None else None
    if fin is not None and payment in (None, "mortgage_ready"):  # бюджет считали по доходам — значит, ипотека
        payment = "mortgage_saving" if fin["gap"] else "mortgage_ready"  # верим цифрам: не хватает — нужен план
    if payment is None:
        r = await s.ask("payment", "Как планируете покупать?",
                        "От этого зависит, что посчитать: ипотеку, план накопления на взнос — или ничего.")
        payment = r["data"].get("payment", "mortgage_ready") if r["approved"] else "mortgage_ready"
    profile["payment"] = payment
    s.finance = None
    if payment == "cash":
        await s.emit("result", step=2, kind="finance", data={"mode": "cash", "price": budget})
        await s.say("Покупка без ипотеки: деньги уйдут продавцу через сервис безопасных расчётов только после регистрации права. "
                    "Пока ищем квартиру, сумму можно держать на накопительном счёте — по желанию.")
    elif payment == "mortgage_ready":
        mortgage = T.mortgage_scenarios(budget, (fin_inputs or {}).get("monthly_income"))
        rec = next(x for x in mortgage["scenarios"] if x["title"] == mortgage["recommended"])
        await s.emit("result", step=2, kind="finance", data={"mode": "mortgage", "mortgage": mortgage})
        await s.say(f"Ипотека: при взносе {T.fmt(rec['down_payment'])} и сроке {rec['years']} лет платёж около {T.fmt(rec['monthly'])} в месяц. "
                    f"Каждые 100 000 ₽, которые я сторгую, — минус {T.fmt(mortgage['per_100k_discount'])} в месяц.")
    else:  # ипотека, взнос ещё копим — тут и нужен финансовый помощник
        if fin is None:
            r = await s.ask(
                "finance", "Финансовая подготовка",
                "Подтяну доходы, расходы и накопления из СберБанк Онлайн (в демо — пример, можно поправить) "
                "и посчитаю, как собрать первоначальный взнос.",
                defaults=F.DEFAULT_INPUTS, price=budget,
            )
            fin_inputs = r["data"] if r["approved"] else {}
            fin = F.plan(budget, fin_inputs)
        s.finance = fin
        mortgage = T.mortgage_scenarios(budget, fin["inputs"]["monthly_income"], fin["available"])
        await s.emit("result", step=2, kind="finance", data={"mode": "saving", "finance": fin, "mortgage": mortgage})
        rec = ", ".join(f"{a['name'].lower()} — {T.fmt(a['amount_now'])}" for a in fin["allocation"] if a["amount_now"])
        await s.say(f"{fin['verdict']} Рекомендую разложить накопления: {rec}." + (f" {fin['risk_note']}" if fin["risk_note"] else ""))
        if fin["gap"] and not fin["fits_horizon"]:
            text = (f"По выбранной стратегии взнос накопится {'за ' + str(fin['months']) + ' мес.' if fin['months'] else 'нескоро'}, "
                    f"а вы хотели за {fin['inputs']['horizon_months']} мес. Можно искать уже сейчас: торг уменьшит нужный взнос, "
                    "а я буду следить за ценами.")
        elif fin["gap"]:
            text = "План накоплений готов: продукты из плана можно открыть на экране справа. Ищем квартиру сейчас — к сделке взнос накопится."
        else:
            text = "На взнос хватает. Откройте продукты из плана, чтобы деньги работали, пока ищем квартиру."
        r = await s.ask("finance_choice", "Финансовый план", text, gap=fin["gap"])
        if not r["approved"] and fin["gap"]:
            await s.say("Договорились: сначала копим. Откройте продукты из плана — я напомню, когда взнос будет готов, и начну подбор.")
            await s.step(2, "done")
            await s.emit("done", status="saving")
            return
    await s.step(2, "done")

    # 3. Поиск (если ничего нет — предлагаем, что поменять, и ищем снова)
    await s.step(3, "active")
    for attempt in range(4):
        where = profile["city"] + (f", {profile['okrug']}" if profile.get("okrug") else "")
        await s.say(f"Ищу на Циане: {where}, {T.rooms_text(profile['rooms'])}, до {T.fmt(profile['budget_max'])}.")
        listings, report = await sources.collect(profile)
        pool = [x for x in listings if T.passes_hard_filters(x, profile)]
        if pool:
            fits, sel = C.select(profile, pool, limit=BATCH)
            if fits:
                pool = fits
                break
            # до 50% не дотянуло ни одно — показываем ближайшие, но честно об этом говорим
            pool = C.rank(profile, pool)[:BATCH]
            sel = {"threshold": 0.5, "passed": len(pool), "total": len(pool),
                   "relaxed": "ни одно объявление не совпало даже на 50% — показываю ближайшие"}
            break
        await s.emit("result", step=3, kind="search", data={"report": report, "total": len(listings), "matched": 0, "rows": []})
        if report and report[0]["status"] == "unavailable":
            await s.say(f"Циан сейчас не отдаёт объявления ({report[0]['note']}). Попробуйте чуть позже.")
            await s.emit("done", status="empty")
            return
        m = await sources.market(profile)
        question = (f"Целых квартир ({T.rooms_text(profile['rooms'])}, {where}) до {T.fmt(profile['budget_max'])} сейчас нет — "
                    "доли и комнаты я не показываю.")
        chips = []
        if m:
            cheapest = int(-(-m["min"] // 100_000) * 100_000)
            question += f" Самые доступные — от {T.mln(m['min'])}, обычно {T.mln(m['p25'])}–{T.mln(m['p75'])}."
            chips.append(f"Бюджет до {T.mln(cheapest)}")
        if profile["rooms"] and profile["rooms"] > 1:
            chips.append(f"{profile['rooms'] - 1}-комнатные")
        if profile.get("okrug"):
            chips.append(f"Искать по всему городу ({profile['city']})")
        question += " Что меняем?"
        r = await s.ask("clarify", "Ничего не нашлось", question, chips=chips)
        text = r["data"].get("text", "")
        if T.wants_whole_city(text):
            profile["okrug"] = None
        profile = await parse_request(s, text, base=profile)
        profile["summary"] = T.profile_summary(profile)
        await s.emit("result", step=1, kind="profile", data=profile)
    else:
        await s.say("Так и не нашёл подходящих объявлений. Начните новый запрос с другими параметрами.")
        await s.emit("done", status="empty")
        return
    meta: dict[str, dict] = {}
    comps = list({x["id"]: x for x in [*sources.comparables(profile), *listings]}.values())
    await score_into(pool, comps, profile, meta)
    for x in pool:
        meta[x["id"]]["match"] = C.check(profile, x)
    order = ranked(pool, meta, s.prefs)
    rows = [{"id": x["id"], "address": x["address"], "district": x.get("district"), "area": x["area"],
             "price": x["price"], "match": match_payload(meta[x["id"]].get("match")),
             **meta[x["id"]]["fair"]} for x in order[:8]]
    await s.emit("result", step=3, kind="search",
                 data={"report": report, "total": len(listings), "matched": len(pool), "rows": rows,
                       "threshold": sel["threshold"], "relaxed": sel["relaxed"]})
    src = ", ".join(f"{r['source']} — {r['count']}" + (" (сохранённый срез)" if r["status"] == "snapshot" else "") for r in report)
    over = sum(1 for x in pool if meta[x["id"]]["fair"]["overprice_pct"] > 1)
    await s.say(f"Нашёл объявлений: {src}. Проверил {sel['total']} по вашим критериям — "
                f"совпало на {round(sel['threshold'] * 100)}% и больше: {len(pool)}; "
                f"{over} из них дороже справедливой цены — там есть что сторговать."
                + (f" {sel['relaxed']}." if sel["relaxed"] else ""))
    await s.step(3, "done")

    # 4. Подбор с обратной связью
    await s.step(4, "active")
    by_id = {x["id"]: x for x in pool}
    shown: set[str] = set()
    page = 1
    districts = sorted({x["district"] for x in pool if x.get("district")})
    for round_no in range(1, MAX_SELECTION_ROUNDS + 1):
        for note in s.notes:
            await P.apply_text(s.prefs, note, s.llm, districts)
        s.notes.clear()
        order = ranked(pool, meta, s.prefs)
        liked = [x for x in order if x["id"] in s.prefs.liked]
        fresh = [x for x in order if x["id"] not in shown and x["id"] not in s.prefs.disliked and x["id"] not in s.prefs.liked
                 and x.get("district") not in s.prefs.districts_minus]
        if len(fresh) < BATCH and page < 3:
            page += 1
            extra = [x for x in await sources.more(profile, page)
                     if x["id"] not in by_id and T.passes_hard_filters(x, profile) and C.matches(profile, x)]
            if extra:
                pool += extra
                comps += extra
                by_id.update({x["id"]: x for x in extra})
                await score_into(extra, comps, profile, meta)
                for x in extra:
                    meta[x["id"]]["match"] = C.check(profile, x)
                order = ranked(pool, meta, s.prefs)
                fresh = [x for x in order if x["id"] not in shown and x["id"] not in s.prefs.disliked and x["id"] not in s.prefs.liked
                 and x.get("district") not in s.prefs.districts_minus]
        batch = liked + fresh[:BATCH]
        shown |= {x["id"] for x in batch}
        await s.emit("result", step=4, kind="selection", data={
            "round": round_no, "cards": [card(x, meta) for x in batch], "liked": s.prefs.liked,
            "disliked": s.prefs.disliked, "learned": s.prefs.learned(), "remaining": len(fresh) - BATCH,
        })
        if round_no == 1:
            await s.say("Вот лучшие варианты. Отметьте «Нравится» или «Не то» — я учту это и подберу ещё. "
                        "Пожелания можно написать или сказать голосом. Когда будете готовы — позвоню по отмеченным.")
        r = await s.ask("selection", "Подборка", "Отметьте квартиры на экране подбора.")
        d = r["data"]
        s.prefs.liked = [i for i in d.get("liked", []) if i in by_id]
        s.prefs.disliked = [i for i in d.get("disliked", []) if i in by_id]
        if d.get("note"):
            parsed = await P.apply_text(s.prefs, d["note"], s.llm, districts)
            if parsed.get("budget_max"):
                await s.say("Бюджет меняю только через «красные линии» — начните новый запрос, если он другой.")
        P.learn(s.prefs, by_id)
        if d.get("action") == "proceed":
            break
        learned = s.prefs.learned()
        await s.say(("Учёл: " + "; ".join(learned) + ". " if learned else "") + "Подобрал ещё варианты.")
    candidates = [by_id[i] for i in s.prefs.liked] or ranked(pool, meta, s.prefs)[:3]
    candidates.sort(key=lambda x: meta[x["id"]]["total"], reverse=True)
    await s.step(4, "done")

    # 5. Проверка
    await s.step(5, "active")
    checks = {x["id"]: T.legal_check(x) for x in candidates}
    await s.emit("result", step=5, kind="legal", data={"checks": list(checks.values()),
                                                         "listings": [card(x, meta) for x in candidates]})
    warned = [x for x in candidates if checks[x["id"]]["status"] == "warn"]
    await s.say(f"Проверил {len(candidates)} {T.plural(len(candidates), 'объект', 'объекта', 'объектов')} по данным объявлений"
                + (f"; есть оговорки у {len(warned)} — покажу юристу." if warned else "; явных рисков не видно.")
                + " Выписки ЕГРН и проверку продавца закажу после выбора квартиры.")
    await s.step(5, "done")

    # 6–9 по кандидатам (если цена не устроила — следующий)
    for listing in candidates[:3]:
        fp = meta[listing["id"]]["fair"]
        authority = min(profile["budget_max"], listing["price"], fp["high"])
        seller = seller_sim.build(listing)
        questions = T.missing_info(listing)
        slots = T.client_slots()
        await s.step(6, "active")
        r = await s.ask(
            "call", "Звонок продавцу",
            f"Звоню по объекту {listing['address']}: уточню {', '.join(q['label'] for q in questions)}, "
            f"поторгуюсь до {T.fmt(fp['target'])} (без вашего согласия — не выше {T.fmt(authority)}) и договорюсь о просмотре.",
            listing=listing, fair=fp, authority=authority, arguments=T.bargaining_arguments(listing, fp),
            legal=checks[listing["id"]], questions=questions, seller_name=seller["name"], seller_role=seller["role"],
            simulated=True,
        )
        if not r["approved"]:
            await s.say("Пропускаю этот объект, перехожу к следующему.")
            continue
        mode = "human" if r["data"].get("mode") == "human" else "auto"
        neg = N.Negotiation(listing=listing, seller=seller, fair=fp, authority=authority, questions=questions,
                            slots=slots, mode=mode)
        s.journal = {"listing_id": listing["id"], "base_price": listing["price"], "fixed_at": date.today().isoformat(),
                     "url": listing.get("url")}
        end = None
        human = (lambda: N.queue_input(s.seller_queue)) if mode == "human" else None
        async for ev in N.run(s.llm, neg, human_input=human):
            await s.events.put(ev)
            if ev["type"] == "call_end":
                end = ev
        if neg.used_fallback:
            await mark_degraded(s, LLMError("модель не ответила в звонке"))
        if end["learned"]:
            await s.say("Узнал у продавца: " + "; ".join(f"{k} — {v}" for k, v in end["learned"].items()) + ".")
        price = end["price"]
        savings = listing["price"] - price
        over_budget = price > profile["budget_max"]
        if end["status"] == "agreed":
            text = f"Продавец согласен на {T.fmt(price)} (было {T.fmt(listing['price'])}). Подтвердить предложение?"
        else:
            text = f"Продавец остановился на {T.fmt(price)} — это выше лимита, который вы мне дали. Согласиться всё равно?"
        if over_budget:
            text += f" Внимание: цена выше вашего бюджета {T.fmt(profile['budget_max'])}."
        r = await s.ask("price", "Подтвердите цену", text, price=price, savings=savings, base=listing["price"],
                        status=end["status"], over_budget=over_budget)
        if not r["approved"]:
            await s.step(6, "pending")
            await s.say("Понял, эта цена не подходит. Звоню по следующему объекту.")
            continue
        s.journal.update(final_price=price, savings=savings)
        await s.step(6, "done")

        # 7. Просмотр
        await s.step(7, "active")
        slot = end.get("viewing") or slots[0]
        await s.emit("result", step=7, kind="viewing", data={
            "listing": card(listing, meta), "slot": slot, "slots": slots, "learned": end["learned"],
            "checklist": T.viewing_checklist(listing),
        })
        await s.say(f"Просмотр: {slot}. Подготовил чек-лист осмотра с учётом того, что узнал у продавца.")
        r = await s.ask("object", "Выбор объекта", "После просмотра: выбираете эту квартиру?")
        if not r["approved"]:
            await s.step(7, "pending")
            await s.say("Хорошо, ищу дальше.")
            continue
        await s.step(7, "done")

        # 8. Оформление
        await s.step(8, "active")
        if profile.get("payment") == "cash":
            down = None
        else:
            down = s.finance["down_payment"] if s.finance else int(profile["budget_max"] * 0.2)
        plan = T.deal_plan(listing, price, down)
        await s.emit("result", step=8, kind="deal", data=plan)
        if plan["cash"]:
            await s.say("Собрал план сделки без ипотеки: проверка продавца, договор с электронной подписью, "
                        "деньги через сервис безопасных расчётов, электронная регистрация — около 9 дней.")
        else:
            await s.say(f"Собрал план сделки: ипотека {T.fmt(plan['loan'])}, платёж {T.fmt(plan['monthly'])} в месяц "
                        f"(на {T.fmt(plan['monthly_saved'])} меньше благодаря торгу). Регистрация — электронно, деньги через СБР.")
        r = await s.ask("sign", "Подписание договора", "Подписать договор купли-продажи электронной подписью?")
        if not r["approved"]:
            await s.say("Подписание отложено. Все документы сохранены — вернёмся, когда будете готовы.")
            await s.emit("done", status="paused")
            return
        await s.step(8, "done")

        # 9. Итог
        await s.step(9, "active")
        econ = economics.unit(economics.Inputs(price=listing["price"], savings_pct=max(0, savings) / listing["price"],
                                               ltv=0 if plan["cash"] else 0.8))
        fin_lines = F.bank_value(s.finance or {}, s.opened)
        summary = {
            "listing": listing,
            "base_price": listing["price"], "final_price": price, "savings": savings,
            "fee": round(max(0, savings) * 0.15), "net": round(max(0, savings) * 0.85),
            "monthly_saved": plan["monthly_saved"], "hours_saved": "60–70 ч",
            "sber": {"revenue": econ["revenue"], "contribution": econ["contribution"], "lines": econ["lines"],
                     "finance": fin_lines, "finance_total": sum(x["value"] for x in fin_lines)},
            "next_events": ["Ремонт и обустройство", "Переезд", "Страхование на следующий год", "Рефинансирование"],
        }
        await s.emit("result", step=9, kind="summary", data=summary)
        await s.say(f"Готово! Экономия {T.fmt(savings)}, плата агенту {T.fmt(summary['fee'])} (15% от экономии), "
                    f"вам остаётся {T.fmt(summary['net'])}. Когда получите ключи — помогу с ремонтом и переездом.")
        await s.step(9, "done")
        await s.emit("done", status="completed")
        return

    await s.say("Отмеченные объекты закончились. Вернёмся к подбору?")
    await s.emit("done", status="exhausted")


async def run_session(s: Session) -> None:
    try:
        await pipeline(s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # показать ошибку в UI, а не молча упасть
        log.exception("pipeline failed")
        await s.emit("error", text=f"Ошибка агента: {exc}")
