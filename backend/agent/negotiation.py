"""Голосовой торг агента с продавцом.

Модель формулирует реплики, а рамки держит код:
- агент не предлагает больше лимита клиента и не откатывает своё предложение назад;
- ИИ-продавец не опускается ниже своей скрытой минимальной цены;
- итоговая цена — только «согласована предварительно», финал подтверждает клиент.

Если LLM недоступен или ответил мусором, ход делает сценарная стратегия.
Продавцом может быть LLM (mode="auto") или человек (mode="human").
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from ..llm import LLMClient, LLMError

# любые сбои ответа модели -> сценарный ход
LLM_FAIL = (LLMError, KeyError, IndexError, TypeError, ValueError)
from . import prompts
from .tools import bargaining_arguments, fmt, parse_money

log = logging.getLogger(__name__)

MAX_ROUNDS = 6
STEP = 10_000

AGREE_WORDS = ("соглас", "договорились", "по рукам", "давайте так", "хорошо, давайте", "идёт", "идет", "устраивает")


def r10k(v: float) -> int:
    return int(round(v / STEP) * STEP)


@dataclass
class Negotiation:
    listing: dict
    fair: dict
    authority: int
    client_name: str = "Анна"
    mode: str = "auto"
    args: list[str] = field(default_factory=list)
    asking: int = 0
    offer: int | None = None
    seller_final: bool = False
    history: list[dict] = field(default_factory=list)
    status: str = "talking"  # talking | agreed | no_deal
    agreed_price: int | None = None
    used_fallback: bool = False

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
        return f"{x['rooms']}-комн., {x['area']} м², {x['floor']}/{x['floors']} эт., {x['address']} ({x['district']} р-н, Казань)"


# ------------------------------------------------------------------ сценарная стратегия


def scripted_buyer(n: Negotiation, round_no: int) -> dict:
    a = n.args
    if round_no == 0:
        return {
            "utterance": (
                f"Здравствуйте! Я ИИ-ассистент покупателя из Домклика, звоню по вашей квартире на {n.listing['address']}. "
                "Разговор записывается. Квартира ещё продаётся? У моего клиента одобрена ипотека Сбера, готовы выйти на сделку быстро."
            ),
            "offer": None,
            "accept": False,
        }
    if round_no == 1:
        offer = r10k(n.target * 0.985)
        return {
            "utterance": (
                f"Спасибо. Мы изучили объект и рынок: {a[0]}, и {a[1] if len(a) > 1 else a[-1]}. "
                f"Готовы предложить {fmt(offer)}."
            ),
            "offer": offer,
            "accept": False,
        }
    prev = n.offer or n.target
    gap = n.asking - prev
    if n.asking <= n.authority and (gap <= 40_000 or n.seller_final):
        return {
            "utterance": f"Хорошо, {fmt(n.asking)} нас устраивает. Я подтвержу цену у клиента в течение часа и пришлю вам итоги письменно.",
            "offer": n.asking,
            "accept": True,
        }
    if prev >= n.authority:
        return {
            "utterance": f"Я вас понимаю, но {fmt(n.authority)} — наш максимум. Зато сделка быстрая: ипотека одобрена, деньги через безопасные расчёты.",
            "offer": n.authority,
            "accept": False,
        }
    offer = min(n.authority, r10k(prev + gap * 0.45))
    extra = a[min(round_no, len(a) - 1)]
    return {
        "utterance": f"Понимаю вас. Но учтите: {extra}. Можем подняться до {fmt(offer)}.",
        "offer": offer,
        "accept": False,
    }


def scripted_seller(n: Negotiation, round_no: int) -> dict:
    s = n.listing["seller"]
    min_p, price = s["min_price"], n.listing["price"]
    offer = n.offer
    if offer is None:
        return {
            "utterance": f"Да, продаётся. Цена {fmt(price)}, квартира хорошая, небольшой торг возможен при живом покупателе.",
            "ask": price,
            "accept": False,
        }
    if offer >= min_p and n.asking - offer <= 30_000:
        return {"utterance": f"Ладно, договорились на {fmt(offer)}.", "ask": offer, "accept": True}
    if n.seller_final:
        return {"utterance": f"Нет, {fmt(n.asking)} — это окончательно.", "ask": n.asking, "accept": False}
    if offer >= min_p - 60_000 and round_no >= 3:
        final = max(min_p, r10k((n.asking + offer) / 2))
        n.seller_final = True
        return {
            "utterance": f"Давайте так: {fmt(final)} — это моё последнее слово, если выходим на сделку в ближайшие две недели.",
            "ask": final,
            "accept": False,
        }
    new_ask = max(min_p, r10k(n.asking - (n.asking - min_p) * 0.4))
    lines = [
        f"Ну, {fmt(offer)} — это слишком мало. Могу уступить до {fmt(new_ask)}.",
        f"Квартира ухоженная, это не повод столько сбрасывать. {fmt(new_ask)} — готов обсуждать.",
        f"Я и так снижал цену. Хорошо, {fmt(new_ask)}, но ниже уже тяжело.",
    ]
    return {"utterance": lines[min(round_no - 1, len(lines) - 1)], "ask": new_ask, "accept": False}


# ------------------------------------------------------------------ LLM-ходы


async def llm_buyer(llm: LLMClient, n: Negotiation, round_no: int) -> dict:
    system = prompts.BUYER_SYSTEM.format(
        client=n.client_name,
        authority=n.authority,
        target=n.target,
        listing=n.listing_title(),
        price=n.listing["price"],
        fair=n.fair["fair"],
        low=n.fair["low"],
        high=n.fair["high"],
        arguments="\n".join(f"- {x}" for x in n.args),
    )
    user = (
        f"Ход {round_no + 1} из {MAX_ROUNDS + 1}. Текущая цена продавца: {n.asking} ₽. "
        f"Твоё последнее предложение: {n.offer or 'ещё не было'}.\n"
        f"{'Продавец назвал последнее слово. ' if n.seller_final else ''}"
        f"Разговор:\n{n.transcript()}\n\nТвоя следующая реплика (JSON):"
    )
    data = await llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.6,
        tag="buyer_turn",
    )
    return {
        "utterance": str(data.get("utterance") or "").strip(),
        "offer": _int_or_none(data.get("offer")),
        "accept": bool(data.get("accept")),
    }


async def llm_seller(llm: LLMClient, n: Negotiation, round_no: int) -> dict:
    s = n.listing["seller"]
    system = prompts.SELLER_SYSTEM.format(
        name=s["name"],
        role=s["role"],
        motivation=s["motivation"],
        style=s["style"],
        listing=n.listing_title(),
        price=n.listing["price"],
        min_price=s["min_price"],
    )
    user = (
        f"Твоя текущая цена: {n.asking} ₽. Последнее предложение покупателя: {n.offer or 'не было'}.\n"
        f"Разговор:\n{n.transcript()}\n\nТвоя реплика (JSON):"
    )
    data = await llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.7,
        tag="seller_turn",
    )
    return {
        "utterance": str(data.get("utterance") or "").strip(),
        "ask": _int_or_none(data.get("ask")),
        "accept": bool(data.get("accept")),
    }


async def parse_human_seller(llm: LLMClient, n: Negotiation, text: str) -> dict:
    try:
        data = await llm.chat_json(
            [
                {"role": "system", "content": prompts.PARSE_SELLER.replace("{offer}", str(n.offer)).replace("{ask}", str(n.asking))},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            tag="parse_seller",
        )
        ask = normalize_price(data.get("ask"))
        accept = bool(data.get("accept"))
    except LLM_FAIL:
        ask, accept = parse_money(text), any(w in text.lower() for w in AGREE_WORDS)
    if ask is None:
        bare = re.search(r"\b(\d{1,2}[.,]\d{1,3})\b", text)  # «8,7» без «млн»
        ask = normalize_price(bare.group(1)) if bare else None
    return {"utterance": text, "ask": ask, "accept": accept}


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


# ------------------------------------------------------------------ ограничения (guards)


def guard_buyer(n: Negotiation, move: dict, round_no: int) -> list[str]:
    notes = []
    if not move["utterance"]:
        raise LLMError("пустая реплика")
    if round_no == 0 and "ассистент" not in move["utterance"].lower():
        move["utterance"] = "Здравствуйте, я ИИ-ассистент покупателя из Домклика, разговор записывается. " + move["utterance"]
        notes.append("Добавлено раскрытие: звонит ИИ-ассистент")
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
    min_p = n.listing["seller"]["min_price"]
    ask = move["ask"] if move["ask"] is not None else n.asking
    ask = max(min_p, min(ask, n.asking))
    if move["accept"] and n.offer is not None and n.offer >= min_p:
        ask = n.offer
    elif move["accept"]:
        move["accept"] = False
    move["ask"] = r10k(ask)


# ------------------------------------------------------------------ цикл переговоров


Event = dict
SellerInput = Callable[[], Awaitable[str | None]]


async def run(
    llm: LLMClient, n: Negotiation, human_input: SellerInput | None = None
) -> AsyncIterator[Event]:
    """Асинхронный генератор событий звонка: turn / guard / price / end."""
    use_llm = llm.name != "offline"

    def price_event() -> Event:
        return {
            "type": "price",
            "ask": n.asking,
            "offer": n.offer,
            "savings": n.listing["price"] - n.asking,
            "base": n.listing["price"],
        }

    yield {"type": "call_start", "seller": n.listing["seller"]["name"], "role": n.listing["seller"]["role"],
           "base": n.listing["price"], "authority": n.authority, "target": n.target, "mode": n.mode}

    for round_no in range(MAX_ROUNDS + 1):
        # --- ход агента
        move = None
        if use_llm:
            try:
                move = await llm_buyer(llm, n, round_no)
                notes = guard_buyer(n, move, round_no)
            except LLM_FAIL as exc:
                log.warning("buyer LLM fallback: %s", exc)
                move, n.used_fallback = None, True
                yield {"type": "fallback", "text": "LLM недоступен — ход по сценарной стратегии"}
        if move is None:
            move = scripted_buyer(n, round_no)
            notes = guard_buyer(n, move, round_no)
        for note in notes:
            yield {"type": "guard", "text": note}
        if move["offer"] is not None:
            n.offer = move["offer"]
        n.history.append({"speaker": "agent", "text": move["utterance"], "offer": move["offer"]})
        yield {"type": "turn", "speaker": "agent", "text": move["utterance"], "offer": move["offer"]}

        if move["accept"]:
            n.status, n.agreed_price = "agreed", n.asking
            break
        if n.offer is not None and n.offer >= n.asking:
            n.status, n.agreed_price = "agreed", n.asking
            break
        if round_no == MAX_ROUNDS:
            break

        # --- ход продавца
        if n.mode == "human" and human_input is not None:
            yield {"type": "await_seller"}
            text = await human_input()
            if text is None:
                yield {"type": "turn", "speaker": "seller", "text": "(продавец не ответил)", "ask": n.asking}
                break
            smove = await parse_human_seller(llm, n, text)
            if smove["ask"] is not None:
                smove["ask"] = r10k(min(smove["ask"], n.asking))
            else:
                smove["ask"] = n.asking
            if smove["accept"] and n.offer is not None:
                smove["ask"] = n.offer
        else:
            smove = None
            if use_llm:
                try:
                    smove = await llm_seller(llm, n, round_no)
                    guard_seller(n, smove)
                except LLM_FAIL as exc:
                    log.warning("seller LLM fallback: %s", exc)
                    smove, n.used_fallback = None, True
            if smove is None:
                smove = scripted_seller(n, round_no)
                guard_seller(n, smove)

        prev_ask, n.asking = n.asking, smove["ask"]
        n.history.append({"speaker": "seller", "text": smove["utterance"], "ask": n.asking})
        yield {"type": "turn", "speaker": "seller", "text": smove["utterance"], "ask": n.asking}
        yield price_event()
        if smove["accept"] or (n.offer is not None and n.asking <= n.offer):
            n.status, n.agreed_price = "agreed", n.asking
            closing = f"Отлично, фиксирую {fmt(n.asking)}. Подтвержу у клиента в течение часа и пришлю итоги письменно. Спасибо!"
            n.history.append({"speaker": "agent", "text": closing, "offer": n.asking})
            yield {"type": "turn", "speaker": "agent", "text": closing, "offer": n.asking}
            break
        if n.offer == n.authority and n.asking == prev_ask and round_no >= 2:
            closing = (
                f"Понимаю. {fmt(n.authority)} — максимум, который клиент разрешил мне без согласования. "
                f"Передам клиенту ваше предложение {fmt(n.asking)} и вернусь с ответом. Спасибо!"
            )
            n.history.append({"speaker": "agent", "text": closing, "offer": n.offer})
            yield {"type": "turn", "speaker": "agent", "text": closing, "offer": n.offer}
            break

    if n.status != "agreed":
        n.status = "no_deal"
    yield price_event()
    yield {
        "type": "call_end",
        "status": n.status,
        "price": n.agreed_price or n.asking,
        "savings": n.listing["price"] - (n.agreed_price or n.asking),
        "within_authority": (n.agreed_price or n.asking) <= n.authority,
        "fallback": n.used_fallback,
        "transcript": n.history,
    }


async def queue_input(queue: "asyncio.Queue[str]", timeout: float = 180.0) -> str | None:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def dumps(event: Event) -> str:
    return json.dumps(event, ensure_ascii=False)
