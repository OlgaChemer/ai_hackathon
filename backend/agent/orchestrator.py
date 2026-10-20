"""Конвейер агента из 9 шагов с 5 подтверждениями клиента.

Сессия выполняется фоновой задачей и пишет события в очередь; HTTP-слой отдаёт
их браузеру через SSE. На шагах, где решение за клиентом, конвейер ждёт
POST /confirm. В режиме «вы — продавец» реплики продавца приходят через
POST /seller.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import economics
from ..llm import LLMClient, LLMError
from . import data as D
from . import negotiation as N
from . import prompts
from . import tools as T

log = logging.getLogger(__name__)

STEPS = [
    "Понимаю запрос",
    "Финансы и ипотека",
    "Поиск и ранжирование",
    "Справедливая цена",
    "Проверка объекта и продавца",
    "Звонок и торг",
    "Просмотр",
    "Оформление сделки",
    "Сопровождение",
]


class Cancelled(Exception):
    pass


@dataclass
class Session:
    id: str
    query: str
    llm: LLMClient
    client_name: str = "Анна"
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    seller_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    profile: dict = field(default_factory=dict)
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


SESSIONS: dict[str, Session] = {}


def create_session(query: str, llm: LLMClient) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], query=query.strip(), llm=llm)
    SESSIONS[s.id] = s
    return s


# ------------------------------------------------------------- шаги


async def parse_request(s: Session) -> dict:
    try:
        raw = await s.llm.chat_json(
            [{"role": "system", "content": prompts.PARSE_REQUEST}, {"role": "user", "content": s.query}],
            tag="parse_request",
        )
        profile = T.merge_profile(raw)
        profile["summary"] = raw.get("summary") or ""
        profile["source"] = s.llm.name
    except (LLMError, KeyError, ValueError, TypeError) as exc:
        log.warning("parse_request fallback: %s", exc)
        await mark_degraded(s, exc)
        profile = T.parse_request_rules(s.query)
        profile["summary"] = ""
        profile["source"] = "rules"
    if not profile["summary"]:
        profile["summary"] = (
            f"Ищу {profile['rooms']}-комнатную квартиру в г. {profile['city']} до {T.fmt(profile['budget_max'])}"
            + (", рядом со школой" if profile["near_school"] else "")
            + "."
        )
    return profile


async def mark_degraded(s: Session, exc: Exception) -> None:
    if not s.llm_degraded and s.llm.name != "offline":
        s.llm_degraded = True
        await s.emit("provider", provider=s.llm.name, model=s.llm.model, degraded=True, reason=str(exc)[:160])


def viewing_checklist(listing: dict) -> list[str]:
    items = [
        "Сверить паспорт продавца с выпиской ЕГРН",
        "Проверить окна, батареи и стояки",
        "Посмотреть потолки и стены на следы протечек",
        "Сверить планировку с техпланом",
        "Узнать, кто прописан и когда выпишутся",
        "Проверить напор воды и электрику на кухне",
    ]
    for d in listing.get("defects", []):
        items.insert(1, f"Оценить: {d}")
    return items


async def pipeline(s: Session) -> None:
    await s.emit("provider", provider=s.llm.name, model=s.llm.model, degraded=False)
    await s.emit("message", role="user", text=s.query)

    # 1. Понять запрос
    await s.step(1, "active")
    profile = await parse_request(s)
    s.profile = profile
    await s.emit("result", step=1, kind="profile", data=profile)
    await s.say(profile["summary"] + " Проверьте «красные линии» — в их пределах я действую сам.")
    r = await s.ask(
        "red_lines",
        "Красные линии",
        "Агент сам ищет, звонит и торгуется, но не выходит за эти рамки. Любую итоговую цену подтверждаете вы.",
        payload={
            "budget_max": profile["budget_max"],
            "rooms": profile["rooms"],
            "near_school": profile["near_school"],
            "authority_rule": "не выше верхней границы справедливой цены и бюджета",
        },
    )
    if not r["approved"]:
        await s.say("Хорошо, остановился. Поправьте запрос, и начнём заново.")
        await s.emit("done", status="cancelled")
        return
    if r["data"].get("budget_max"):
        profile["budget_max"] = int(r["data"]["budget_max"])
    await s.step(1, "done")

    # 2. Финансы
    await s.step(2, "active")
    r = await s.ask(
        "consent",
        "Согласие на данные",
        "Разрешите использовать данные Сбер ID для предодобрения ипотеки и расчёта реального бюджета.",
    )
    mortgage = T.mortgage_scenarios(profile)
    mortgage["consent"] = r["approved"]
    await s.emit("result", step=2, kind="mortgage", data=mortgage)
    if r["approved"]:
        rec = next(x for x in mortgage["scenarios"] if x["title"] == mortgage["recommended"])
        await s.say(
            f"Ипотека предодобрена на {T.fmt(mortgage['approved'])}. Рекомендую сценарий «{rec['title']}»: "
            f"платёж ~{T.fmt(rec['monthly'])} в месяц. Каждые 100 000 ₽, которые я сторгую, — это минус "
            f"{T.fmt(mortgage['per_100k_discount'])} в месяц."
        )
    else:
        await s.say("Без согласия считаю бюджет по вашему запросу, без предодобрения.")
    await s.step(2, "done")

    # 3. Поиск
    await s.step(3, "active")
    found = T.search(profile)
    shortlist = found["shortlist"]
    await s.emit("result", step=3, kind="search", data=found)
    await s.say(
        f"Просмотрел {found['total']} объявлений, по параметрам подходят {found['matched']}. "
        f"В коротком списке {len(shortlist)} — отсортировал по цене к рынку, школе, состоянию и потенциалу торга."
    )
    await s.step(3, "done")
    if not shortlist:
        await s.say("Подходящих объектов нет. Попробуйте расширить бюджет или районы.")
        await s.emit("done", status="empty")
        return

    # 4. Справедливая цена
    await s.step(4, "active")
    fair_rows = [
        {"id": x["listing"]["id"], "price": x["listing"]["price"], **x["fair"]} for x in shortlist
    ]
    await s.emit("result", step=4, kind="fair", data=fair_rows)
    over = [x for x in fair_rows if x["overprice_pct"] > 1]
    await s.say(
        f"Посчитал справедливую цену по закрытым сделкам-аналогам. {len(over)} из {len(fair_rows)} объектов "
        "выставлены дороже рынка — там есть пространство для торга."
    )
    await s.step(4, "done")

    # 5. Юридическая проверка
    await s.step(5, "active")
    checks = {x["listing"]["id"]: T.legal_check(x["listing"]["id"]) for x in shortlist}
    await s.emit("result", step=5, kind="legal", data=list(checks.values()))
    blocked = [x for x in shortlist if checks[x["listing"]["id"]]["status"] == "block"]
    for b in blocked:
        reasons = ", ".join(c["title"].lower() for c in checks[b["listing"]["id"]]["checks"] if c["status"] == "block")
        await s.say(
            f"Исключил {b['listing']['address']} за {T.fmt(b['listing']['price'])}: {reasons}. "
            "Дёшево, но рискованно — деньги можно потерять."
        )
    order = {"ok": 0, "warn": 1}
    candidates = sorted(
        (x for x in shortlist if checks[x["listing"]["id"]]["status"] != "block"),
        key=lambda x: (order[checks[x["listing"]["id"]]["status"]], -x["score"]),
    )
    await s.step(5, "done")

    # 6–9 по кандидатам (если цена не устроила — следующий объект)
    for cand in candidates[:3]:
        listing = D.listing_by_id(cand["listing"]["id"])
        fp = cand["fair"]
        authority = min(profile["budget_max"], fp["high"])
        if authority < fp["low"]:
            continue
        await s.step(6, "active")
        r = await s.ask(
            "call",
            "Звонок продавцу",
            f"Звоню по объекту {listing['address']}. Буду торговаться до {T.fmt(fp['target'])}, "
            f"без вашего согласия не выше {T.fmt(authority)}.",
            listing=T.public_listing(listing),
            fair=fp,
            authority=authority,
            arguments=T.bargaining_arguments(listing, fp),
            legal=checks[listing["id"]],
        )
        if not r["approved"]:
            await s.say("Пропускаю этот объект, перехожу к следующему.")
            continue
        mode = "human" if r["data"].get("mode") == "human" else "auto"
        neg = N.Negotiation(listing=listing, fair=fp, authority=authority, client_name=s.client_name, mode=mode)
        s.journal = {"listing_id": listing["id"], "base_price": listing["price"], "fixed_at": D.TODAY.isoformat()}
        end = None
        human = (lambda: N.queue_input(s.seller_queue)) if mode == "human" else None
        async for ev in N.run(s.llm, neg, human_input=human):
            if ev["type"] == "fallback":
                await mark_degraded(s, LLMError(ev["text"]))
            await s.events.put(ev)
            if ev["type"] == "call_end":
                end = ev
        price = end["price"]
        savings = listing["price"] - price
        over_budget = price > profile["budget_max"]
        if end["status"] == "agreed":
            text = f"Продавец согласен на {T.fmt(price)} (было {T.fmt(listing['price'])}). Подтвердить предложение?"
        else:
            text = f"Продавец остановился на {T.fmt(price)} — это выше лимита, который вы мне дали. Согласиться всё равно?"
        if over_budget:
            text += f" Внимание: цена выше вашего бюджета {T.fmt(profile['budget_max'])}."
        r = await s.ask(
            "price", "Подтвердите цену", text,
            price=price, savings=savings, base=listing["price"],
            status=end["status"], over_budget=over_budget,
        )
        if not r["approved"]:
            await s.step(6, "pending")
            await s.say("Понял, эта цена не подходит. Возвращаюсь к поиску и звоню по следующему объекту.")
            continue
        s.journal.update(final_price=price, savings=savings)
        await s.step(6, "done")

        # 7. Просмотр
        await s.step(7, "active")
        await s.emit("result", step=7, kind="viewing", data={
            "listing_id": listing["id"],
            "slots": ["Чт, 23 окт · 18:30", "Сб, 25 окт · 11:00", "Видеопросмотр сегодня · 20:00"],
            "checklist": viewing_checklist(listing),
        })
        await s.say("Договорился о просмотре: продавец ждёт в четверг в 18:30. Подготовил чек-лист осмотра.")
        r = await s.ask("object", "Выбор объекта", "После просмотра: выбираете эту квартиру?")
        if not r["approved"]:
            await s.step(7, "pending")
            await s.say("Хорошо, ищу дальше.")
            continue
        await s.step(7, "done")

        # 8. Оформление
        await s.step(8, "active")
        plan = T.deal_plan(listing, price, profile)
        await s.emit("result", step=8, kind="deal", data=plan)
        await s.say(
            f"Собрал план сделки: ипотека {T.fmt(plan['loan'])}, платёж {T.fmt(plan['monthly'])} в месяц "
            f"(на {T.fmt(plan['monthly_saved'])} меньше благодаря торгу). Регистрация — электронно, деньги через СБР."
        )
        r = await s.ask("sign", "Подписание договора", "Подписать договор купли-продажи электронной подписью?")
        if not r["approved"]:
            await s.say("Подписание отложено. Все документы сохранены в чате — вернёмся, когда будете готовы.")
            await s.emit("done", status="paused")
            return
        await s.step(8, "done")

        # 9. Итог
        await s.step(9, "active")
        econ = economics.unit(economics.Inputs(
            price=listing["price"],
            savings_pct=savings / listing["price"],
        ))
        summary = {
            "listing": T.public_listing(listing),
            "base_price": listing["price"],
            "final_price": price,
            "savings": savings,
            "fee": round(savings * 0.15),
            "net": round(savings * 0.85),
            "monthly_saved": plan["monthly_saved"],
            "hours_saved": "60–70 ч",
            "sber": {"revenue": econ["revenue"], "contribution": econ["contribution"], "lines": econ["lines"]},
            "next_events": ["Ремонт и обустройство", "Переезд", "Страхование на следующий год", "Рефинансирование"],
        }
        await s.emit("result", step=9, kind="summary", data=summary)
        await s.say(
            f"Готово! Экономия {T.fmt(savings)}, плата агенту {T.fmt(summary['fee'])} (15% от экономии), "
            f"вам остаётся {T.fmt(summary['net'])}. Когда получите ключи — помогу с ремонтом и переездом."
        )
        await s.step(9, "done")
        await s.emit("done", status="completed")
        return

    await s.say("Подходящие объекты закончились. Расширим поиск?")
    await s.emit("done", status="exhausted")


async def run_session(s: Session) -> None:
    try:
        await pipeline(s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # показать ошибку в UI, а не молча упасть
        log.exception("pipeline failed")
        await s.emit("error", text=f"Ошибка агента: {exc}")
