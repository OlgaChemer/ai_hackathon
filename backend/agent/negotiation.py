"""Звонок агента продавцу: уточнение деталей -> торг -> время просмотра.

Модель формулирует реплики, а рамки держит код:
- агент не предлагает больше лимита клиента и не откатывает своё предложение назад;
- ИИ-продавец не опускается ниже своей скрытой минимальной цены;
- итоговая цена — только «согласована предварительно», финал подтверждает клиент.

Если LLM недоступен или ответил мусором, ход делает сценарная стратегия.
Продавцом может быть ИИ (mode="auto") или человек (mode="human").
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from ..llm import LLMClient, LLMError
from . import prompts
from .tools import bargaining_arguments, fmt, parse_money

log = logging.getLogger(__name__)

# любые сбои ответа модели -> сценарный ход
LLM_FAIL = (LLMError, KeyError, IndexError, TypeError, ValueError)

MAX_ROUNDS = 6
STEP = 10_000
AGREE_WORDS = ("соглас", "договорились", "по рукам", "давайте так", "хорошо, давайте", "идёт", "идет", "устраивает")


def r10k(v: float) -> int:
    return int(round(v / STEP) * STEP)


@dataclass
class Negotiation:
    listing: dict
    seller: dict           # персона продавца (seller_sim.build)
    fair: dict
    authority: int
    questions: list[dict] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)
    client_name: str = "клиента"
    mode: str = "auto"
    args: list[str] = field(default_factory=list)
    asking: int = 0
    offer: int | None = None
    seller_final: bool = False
    history: list[dict] = field(default_factory=list)
    status: str = "talking"  # talking | agreed | no_deal
    agreed_price: int | None = None
    used_fallback: bool = False
    viewing: str | None = None

    def __post_init__(self):
        self.asking = self.listing["price"]
        self.args = bargaining_arguments(self.listing, self.fair)

    @property
    def target(self) -> int:
        return min(self.fair["target"], self.authority)

    def transcript(self) -> str:
        who = {"agent": "Ассистент покупателя", "seller": "Продавец"}
        return "\n".join(f"{who[h['speaker']]}: {h['text']}" for h in self.history) or "(разговор ещё не начат)"

    def listing_title(self) -> str:
        x = self.listing
        floor = f"{x['floor']}/{x['floors']} эт." if x.get("floor") and x.get("floors") else (f"{x['floor']} эт." if x.get("floor") else "")
        rooms = "студия" if x["rooms"] == 0 else f"{x['rooms']}-комн."
        return f"{rooms}, {x['area']:g} м², {floor} {x['address']} ({x.get('district') or ''}, {x['city']})".replace("  ", " ")

    def question_text(self) -> str:
        return "; ".join(q["question"] for q in self.questions) or "актуальна ли цена и когда готовы выйти на сделку"


# ------------------------------------------------------------------ сценарная стратегия


def scripted_buyer(n: Negotiation, round_no: int, phase: str) -> dict:
    if phase == "clarify":
        qs = n.questions
        ask = ""
        if qs:
            ask = " Подскажите, пожалуйста: " + "; ".join(q["question"] for q in qs) + "?"
        return {
            "utterance": (
                f"Здравствуйте! Я ИИ-ассистент покупателя из Домклика, звоню по вашей квартире: {n.listing['address']}. "
                f"Разговор записывается. Квартира ещё продаётся?{ask}"
            ),
            "offer": None, "accept": False,
        }
    if phase == "viewing":
        return {
            "utterance": f"Спасибо! Давайте договоримся о просмотре: клиенту удобно {n.slots[0]} или {n.slots[1]}. Как вам удобнее?",
            "offer": None, "accept": False,
        }
    a = n.args
    if round_no == 1:
        offer = r10k(n.target * 0.985)
        lead = a[0] if a else "мы посмотрели рынок"
        second = f", и {a[1]}" if len(a) > 2 else ""
        return {"utterance": f"Спасибо за ответы. Смотрите: {lead}{second}. Готовы предложить {fmt(offer)}.",
                "offer": offer, "accept": False}
    prev = n.offer or n.target
    gap = n.asking - prev
    if n.asking <= n.authority and (gap <= 40_000 or n.seller_final):
        return {"utterance": f"Хорошо, {fmt(n.asking)} нас устраивает. Я подтвержу цену у клиента и пришлю итоги письменно.",
                "offer": n.asking, "accept": True}
    if prev >= n.authority:
        return {"utterance": f"Понимаю вас, но {fmt(n.authority)} — наш максимум. Зато сделка быстрая: ипотека одобрена, деньги через безопасные расчёты.",
                "offer": n.authority, "accept": False}
    offer = min(n.authority, r10k(prev + gap * 0.45))
    extra = a[min(round_no, len(a) - 1)] if a else "покупатель готов к быстрой сделке"
    return {"utterance": f"Понимаю. Но учтите: {extra}. Можем подняться до {fmt(offer)}.", "offer": offer, "accept": False}


def scripted_seller(n: Negotiation, round_no: int, phase: str) -> dict:
    s, price = n.seller, n.listing["price"]
    if phase == "clarify":
        answers = [s["facts"].get(q["key"]) for q in n.questions if s["facts"].get(q["key"])]
        detail = (" " + ". ".join(a[0].upper() + a[1:] for a in answers) + ".") if answers else ""
        return {"utterance": f"Да, продаётся.{detail} Цена {fmt(price)}, небольшой торг возможен.", "ask": price, "accept": False}
    if phase == "viewing":
        slot = n.slots[1] if "выходн" in s["availability"] else n.slots[0]
        return {"utterance": f"Давайте {slot}. Позвоните за час, встречу у подъезда.", "ask": n.asking, "accept": False}
    min_p, offer = s["min_price"], n.offer
    if offer is None:
        return {"utterance": f"Цена {fmt(price)}, квартира хорошая.", "ask": price, "accept": False}
    if offer >= min_p and n.asking - offer <= 30_000:
        return {"utterance": f"Ладно, договорились на {fmt(offer)}.", "ask": offer, "accept": True}
    if n.seller_final:
        return {"utterance": f"Нет, {fmt(n.asking)} — это окончательно.", "ask": n.asking, "accept": False}
    if offer >= min_p - 60_000 and round_no >= 3:
        final = max(min_p, r10k((n.asking + offer) / 2))
        n.seller_final = True
        return {"utterance": f"Давайте так: {fmt(final)} — последнее слово, если выходим на сделку в ближайшие недели.",
                "ask": final, "accept": False}
    new_ask = max(min_p, r10k(n.asking - (n.asking - min_p) * 0.4))
    lines = [f"{fmt(offer)} — это слишком мало. Могу уступить до {fmt(new_ask)}.",
             f"Квартира ухоженная, это не повод столько сбрасывать. {fmt(new_ask)} — готов обсуждать.",
             f"Я и так иду навстречу. Хорошо, {fmt(new_ask)}, но ниже уже тяжело."]
    return {"utterance": lines[min(round_no - 1, len(lines) - 1)], "ask": new_ask, "accept": False}


# ------------------------------------------------------------------ LLM-ходы


async def llm_buyer(llm: LLMClient, n: Negotiation, round_no: int, phase: str) -> dict:
    system = prompts.BUYER_SYSTEM.format(
        client=n.client_name, authority=n.authority, target=n.target, listing=n.listing_title(),
        price=n.listing["price"], fair=n.fair["fair"], low=n.fair["low"], high=n.fair["high"],
        arguments="\n".join(f"- {x}" for x in n.args),
    )
    stage = prompts.CALL_PHASES[phase].format(questions=n.question_text(), slots=" или ".join(n.slots))
    user = (
        f"{stage}\nТекущая цена продавца: {n.asking} ₽. Твоё последнее предложение: {n.offer or 'ещё не было'}.\n"
        f"{'Продавец назвал последнее слово. ' if n.seller_final else ''}"
        f"Разговор:\n{n.transcript()}\n\nТвоя следующая реплика (JSON):"
    )
    data = await llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}],
                               temperature=0.6, tag="buyer_turn")
    return {"utterance": str(data.get("utterance") or "").strip(), "offer": _int_or_none(data.get("offer")),
            "accept": bool(data.get("accept"))}


async def llm_seller(llm: LLMClient, n: Negotiation, phase: str) -> dict:
    s = n.seller
    system = prompts.SELLER_SYSTEM.format(
        name=s["name"], role=s["role"], motivation=s["motivation"], style=s["style"], listing=n.listing_title(),
        price=n.listing["price"], facts="; ".join(s["facts"].values()), availability=s["availability"],
        min_price=s["min_price"],
    )
    user = (f"Твоя текущая цена: {n.asking} ₽. Последнее предложение покупателя: {n.offer or 'не было'}.\n"
            f"Разговор:\n{n.transcript()}\n\nТвоя реплика (JSON):")
    data = await llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}],
                               temperature=0.7, tag="seller_turn")
    return {"utterance": str(data.get("utterance") or "").strip(), "ask": _int_or_none(data.get("ask")),
            "accept": bool(data.get("accept"))}


async def parse_human_seller(llm: LLMClient, n: Negotiation, text: str) -> dict:
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": prompts.PARSE_SELLER.replace("{offer}", str(n.offer)).replace("{ask}", str(n.asking))},
             {"role": "user", "content": text}],
            temperature=0.0, tag="parse_seller",
        )
        ask = normalize_price(data.get("ask"))
        accept = bool(data.get("accept"))
    except LLM_FAIL:
        ask, accept = parse_money(text), any(w in text.lower() for w in AGREE_WORDS)
    if ask is None:
        bare = re.search(r"\b(\d{1,2}[.,]\d{1,3})\b", text)  # «8,7» без «млн»
        ask = normalize_price(bare.group(1)) if bare else None
    return {"utterance": text, "ask": ask, "accept": accept}


async def extract_learned(llm: LLMClient, n: Negotiation) -> dict:
    """Что агент узнал за звонок и на какое время договорились."""
    if llm.name != "offline":
        try:
            data = await llm.chat_json(
                [{"role": "system", "content": prompts.EXTRACT_CALL.replace("{labels}", ", ".join(f"«{q['label']}»" for q in n.questions))},
                 {"role": "user", "content": n.transcript()}],
                temperature=0.0, tag="extract_call",
            )
            facts = {str(k): str(v) for k, v in (data.get("facts") or {}).items() if v}
            return {"facts": facts, "viewing": n.viewing or data.get("viewing")}
        except LLM_FAIL as exc:
            log.warning("extract fallback: %s", exc)
    facts = {}
    if n.mode == "auto":
        facts = {q["label"]: n.seller["facts"][q["key"]] for q in n.questions if q["key"] in n.seller["facts"]}
    return {"facts": facts, "viewing": n.viewing}


def normalize_price(v) -> int | None:
    """8.7 -> 8 700 000; 8700 -> 8 700 000; 8700000 -> 8 700 000."""
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        return parse_money(str(v))
    if f < 100:
        f *= 1_000_000
    elif f < 100_000:
        f *= 1000
    return int(f)


def _int_or_none(v) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return parse_money(str(v)) or (int(re.sub(r"\D", "", str(v))) if re.search(r"\d", str(v)) else None)


def pick_slot(text: str, slots: list[str]) -> str | None:
    t = text.lower()
    for s in slots:
        day = s.split(",")[0]  # «чт», «сб»
        long = {"пн": "понедельн", "вт": "вторник", "ср": "сред", "чт": "четверг", "пт": "пятниц", "сб": "суббот", "вс": "воскрес"}[day]
        if long in t or re.search(rf"\b{day}\b", t) or s.split(", ")[1].split(" ")[0] in t:
            return s
    return None


# ------------------------------------------------------------------ ограничения (guards)


def guard_buyer(n: Negotiation, move: dict, phase: str, first: bool) -> list[str]:
    notes = []
    if not move["utterance"]:
        raise LLMError("пустая реплика")
    if first and "ассистент" not in move["utterance"].lower():
        move["utterance"] = "Здравствуйте, я ИИ-ассистент покупателя из Домклика, разговор записывается. " + move["utterance"]
        notes.append("Добавлено раскрытие: звонит ИИ-ассистент")
    if phase != "bargain":
        move["offer"], move["accept"] = None, False
        return notes
    offer = move["offer"]
    if offer is not None:
        if offer > n.authority:
            notes.append(f"Предложение {fmt(offer)} выше лимита клиента — снижено до {fmt(n.authority)}")
            offer = n.authority
        if n.offer is not None and offer < n.offer:
            offer = n.offer  # не откатываем своё предложение
        move["offer"] = r10k(offer)
    if move["accept"] and n.asking > n.authority:
        notes.append(f"Цена продавца {fmt(n.asking)} выше лимита клиента — агент не может согласиться")
        move["accept"] = False
        move["offer"] = n.authority
    return notes


def guard_seller(n: Negotiation, move: dict) -> None:
    if not move["utterance"]:
        raise LLMError("пустая реплика")
    min_p = n.seller["min_price"]
    ask = move["ask"] if move["ask"] is not None else n.asking
    ask = max(min_p, min(ask, n.asking))
    if n.offer is not None and ask < n.offer:
        ask, move["accept"] = n.offer, True  # назвал меньше предложения покупателя = согласие
    if move["accept"] and n.offer is not None and n.offer >= min_p:
        ask = n.offer
    elif move["accept"]:
        move["accept"] = False
    move["ask"] = r10k(ask)


# ------------------------------------------------------------------ цикл звонка

Event = dict
SellerInput = Callable[[], Awaitable[str | None]]


async def run(llm: LLMClient, n: Negotiation, human_input: SellerInput | None = None) -> AsyncIterator[Event]:
    """Асинхронный генератор событий звонка: call_phase / turn / guard / price / call_end."""
    use_llm = llm.name != "offline"

    def price_event() -> Event:
        return {"type": "price", "ask": n.asking, "offer": n.offer, "savings": n.listing["price"] - n.asking,
                "base": n.listing["price"]}

    async def agent_turn(round_no: int, phase: str):
        move, notes = None, []
        if use_llm:
            try:
                move = await llm_buyer(llm, n, round_no, phase)
                notes = guard_buyer(n, move, phase, first=not n.history)
            except LLM_FAIL as exc:
                log.warning("buyer LLM fallback: %s", exc)
                move, n.used_fallback = None, True
        if move is None:
            move = scripted_buyer(n, round_no, phase)
            notes = guard_buyer(n, move, phase, first=not n.history)
        if move["offer"] is not None:
            n.offer = move["offer"]
        n.history.append({"speaker": "agent", "text": move["utterance"], "offer": move["offer"]})
        return move, notes

    async def seller_turn(round_no: int, phase: str):
        if n.mode == "human" and human_input is not None:
            text = await human_input()
            if text is None:
                return None
            if phase == "bargain":
                smove = await parse_human_seller(llm, n, text)
                smove["ask"] = r10k(min(smove["ask"], n.asking)) if smove["ask"] else n.asking
                if smove["accept"] and n.offer is not None:
                    smove["ask"] = n.offer
            else:
                smove = {"utterance": text, "ask": n.asking, "accept": False}
        else:
            smove = None
            if use_llm:
                try:
                    smove = await llm_seller(llm, n, phase)
                    guard_seller(n, smove)
                except LLM_FAIL as exc:
                    log.warning("seller LLM fallback: %s", exc)
                    smove, n.used_fallback = None, True
            if smove is None:
                smove = scripted_seller(n, round_no, phase)
                guard_seller(n, smove)
            if phase != "bargain":
                smove["ask"], smove["accept"] = n.asking if phase == "viewing" else smove["ask"], False
        n.history.append({"speaker": "seller", "text": smove["utterance"], "ask": smove["ask"]})
        return smove

    yield {"type": "call_start", "seller": n.seller["name"], "role": n.seller["role"], "base": n.listing["price"],
           "authority": n.authority, "target": n.target, "mode": n.mode, "questions": n.questions}

    # 1) уточнение деталей
    yield {"type": "call_phase", "phase": "clarify"}
    move, notes = await agent_turn(0, "clarify")
    for note in notes:
        yield {"type": "guard", "text": note}
    yield {"type": "turn", "speaker": "agent", "text": move["utterance"], "offer": None, "phase": "clarify"}
    if n.mode == "human":
        yield {"type": "await_seller"}
    smove = await seller_turn(0, "clarify")
    if smove is None:
        yield {"type": "turn", "speaker": "seller", "text": "(продавец не ответил)", "ask": n.asking}
        n.status = "no_deal"
    else:
        n.asking = smove["ask"] or n.asking
        yield {"type": "turn", "speaker": "seller", "text": smove["utterance"], "ask": n.asking, "phase": "clarify"}

    # 2) торг
    if n.status == "talking":
        yield {"type": "call_phase", "phase": "bargain"}
        for round_no in range(1, MAX_ROUNDS + 1):
            move, notes = await agent_turn(round_no, "bargain")
            for note in notes:
                yield {"type": "guard", "text": note}
            yield {"type": "turn", "speaker": "agent", "text": move["utterance"], "offer": move["offer"], "phase": "bargain"}
            if move["accept"] or (n.offer is not None and n.offer >= n.asking):
                n.status, n.agreed_price = "agreed", n.asking
                break
            if round_no == MAX_ROUNDS:
                break
            if n.mode == "human":
                yield {"type": "await_seller"}
            prev_ask = n.asking
            smove = await seller_turn(round_no, "bargain")
            if smove is None:
                yield {"type": "turn", "speaker": "seller", "text": "(продавец не ответил)", "ask": n.asking}
                break
            n.asking = smove["ask"]
            yield {"type": "turn", "speaker": "seller", "text": smove["utterance"], "ask": n.asking, "phase": "bargain"}
            yield price_event()
            if smove["accept"] or (n.offer is not None and n.asking <= n.offer):
                n.status, n.agreed_price = "agreed", n.asking
                text = f"Отлично, фиксирую {fmt(n.asking)}. Подтвержу у клиента и пришлю итоги письменно."
                n.history.append({"speaker": "agent", "text": text, "offer": n.asking})
                yield {"type": "turn", "speaker": "agent", "text": text, "offer": n.asking, "phase": "bargain"}
                break
            if n.offer == n.authority and n.asking == prev_ask and round_no >= 2:
                text = (f"Понимаю. {fmt(n.authority)} — максимум, который клиент разрешил мне без согласования. "
                        f"Передам клиенту ваше предложение {fmt(n.asking)}.")
                n.history.append({"speaker": "agent", "text": text, "offer": n.offer})
                yield {"type": "turn", "speaker": "agent", "text": text, "offer": n.offer, "phase": "bargain"}
                break
        if n.status != "agreed":
            n.status = "no_deal"
        yield price_event()

        # 3) просмотр
        yield {"type": "call_phase", "phase": "viewing"}
        move, notes = await agent_turn(MAX_ROUNDS + 1, "viewing")
        yield {"type": "turn", "speaker": "agent", "text": move["utterance"], "offer": None, "phase": "viewing"}
        if n.mode == "human":
            yield {"type": "await_seller"}
        smove = await seller_turn(MAX_ROUNDS + 1, "viewing")
        if smove is not None:
            yield {"type": "turn", "speaker": "seller", "text": smove["utterance"], "ask": n.asking, "phase": "viewing"}
            n.viewing = pick_slot(smove["utterance"], n.slots)
            text = (f"Отлично, записал просмотр: {n.viewing}. Пришлю подтверждение клиенту. Спасибо, до свидания!"
                    if n.viewing else "Спасибо! Уточню время с клиентом и напишу вам. До свидания!")
            n.history.append({"speaker": "agent", "text": text, "offer": None})
            yield {"type": "turn", "speaker": "agent", "text": text, "offer": None, "phase": "viewing"}

    learned = await extract_learned(llm, n)
    yield {
        "type": "call_end",
        "status": n.status,
        "price": n.agreed_price or n.asking,
        "savings": n.listing["price"] - (n.agreed_price or n.asking),
        "within_authority": (n.agreed_price or n.asking) <= n.authority,
        "fallback": n.used_fallback,
        "learned": learned["facts"],
        "viewing": learned["viewing"],
        "transcript": n.history,
    }


async def queue_input(queue: "asyncio.Queue[str]", timeout: float = 180.0) -> str | None:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
