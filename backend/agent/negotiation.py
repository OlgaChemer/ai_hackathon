"""Звонок агента продавцу: уточнение деталей -> торг -> время просмотра.

Модель формулирует реплики, а рамки держит код:
- агент не предлагает больше лимита клиента и не откатывает своё предложение назад;
- ИИ-продавец не опускается ниже своей скрытой минимальной цены;
- итоговая цена — только «согласована предварительно», финал подтверждает клиент.

Если LLM недоступен или ответил мусором, после повторной попытки звонок завершается с ошибкой.
Продавцом может быть ИИ (mode="auto") или человек: в браузере (mode="human")
или на настоящем телефоне через МТС Exolve (mode="phone").
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import AsyncIterator, Awaitable, Callable

from ..llm import LLMClient, LLMError
from . import guard, prompts
from .features import FEATURES, feature_status
from .numbers import fmt, parse_money, parse_price_near
from .pricing import bargaining_arguments

log = logging.getLogger(__name__)

# сбои ответа модели: сеть, не тот JSON
LLM_FAIL = (LLMError, KeyError, IndexError, TypeError, ValueError)


class CallError(RuntimeError):
    """Звонок не может продолжаться: модель не ответила. Сценарных подмен нет — агент честно говорит об этом."""

MAX_ROUNDS = 6
LIVE_MODES = {"human", "phone"}  # продавец отвечает сам: в браузере или по телефону
HANGUP = "(положил трубку)"  # так браузер сообщает, что продавец завершил звонок
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
    viewing: str | None = None

    @property
    def live(self) -> bool:
        """Продавец — человек: в браузере (human) или по телефону (phone)."""
        return self.mode in LIVE_MODES

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


# ------------------------------------------------------------------ LLM-ходы


async def llm_buyer(llm: LLMClient, n: Negotiation, round_no: int, phase: str) -> dict:
    system = prompts.BUYER_SYSTEM.format(
        authority=n.authority, target=n.target,
        price=n.listing["price"], fair=n.fair["fair"], low=n.fair["low"], high=n.fair["high"],
    )
    stage = prompts.CALL_PHASES[phase].format(questions=n.question_text(), slots=" или ".join(n.slots))
    user = (
        f"{stage}\nТекущая цена продавца: {n.asking} ₽. Твоё последнее предложение: {n.offer or 'ещё не было'}.\n"
        f"{'Продавец назвал последнее слово. ' if n.seller_final else ''}"
        f"Разговор:\n{n.transcript()}\n\nТвоя следующая реплика (JSON):"
    )
    data = await llm.chat_json([{"role": "system", "content": system + "\n" + guard.POLICY},
                                guard.data_message("negotiation", {"listing": n.listing_title(), "arguments": n.args,
                                                                  "conversation": user})],
                               temperature=0.6, tag="buyer_turn")
    return {"utterance": str(data.get("utterance") or "").strip(), "offer": _int_or_none(data.get("offer")),
            "accept": bool(data.get("accept"))}


async def llm_seller(llm: LLMClient, n: Negotiation, phase: str) -> dict:
    s = n.seller
    system = prompts.SELLER_SYSTEM.format(
        price=n.listing["price"], min_price=s["min_price"],
    )
    user = (f"Твоя текущая цена: {n.asking} ₽. Последнее предложение покупателя: {n.offer or 'не было'}.\n"
            f"Разговор:\n{n.transcript()}\n\nТвоя реплика (JSON):")
    data = await llm.chat_json([{"role": "system", "content": system + "\n" + guard.POLICY},
                                guard.data_message("seller_simulation", {"listing": n.listing_title(), "persona": s,
                                                                        "conversation": user})],
                               temperature=0.7, tag="seller_turn")
    return {"utterance": str(data.get("utterance") or "").strip(), "ask": _int_or_none(data.get("ask")),
            "accept": bool(data.get("accept"))}


def _agrees(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in AGREE_WORDS) and not re.search(r"\bне\s+(?:со)?глас|\bне\s+устраива", t)


async def parse_human_seller(llm: LLMClient, n: Negotiation, text: str) -> dict:
    """Ответ продавца -> {ask, accept}. Назвал цену выше нашего предложения — это встречная цена,
    а не согласие, даже со словами «давай» или «хорошо»."""
    # быстрый путь без модели: в ответе есть правдоподобная цена или явное «согласен» — экономим секунды в звонке
    ask = parse_price_near(text, n.listing["price"])
    if ask:
        return {"utterance": text, "ask": ask, "accept": n.offer is not None and ask <= n.offer}
    if _agrees(text) and not re.search(r"\d", text):
        return {"utterance": text, "ask": None, "accept": True}
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": prompts.PARSE_SELLER.replace("{offer}", str(n.offer)).replace("{ask}", str(n.asking)) + "\n" + guard.POLICY},
             guard.data_message("seller_utterance", text)],
            temperature=0.0, tag="parse_seller",
        )
        ask = normalize_price(data.get("ask"))
        accept = bool(data.get("accept"))
    except LLM_FAIL as exc:
        raise CallError("модель не разобрала ответ продавца") from exc
    if ask is not None and n.offer is not None and ask > n.offer:
        accept = False  # встречная цена, а не согласие
    return {"utterance": text, "ask": ask, "accept": accept}


CORRECTION_RANGES = {"kitchen": (2, 60), "area": (10, 500), "floor": (-1, 120), "floors": (1, 120),
                     "year": (1800, date.today().year + 6), "rooms": (0, 10)}


def _listing_brief(x: dict) -> str:
    """Что сказано в объявлении — чтобы модель отличала уточнение от повтора."""
    parts = [f"{x.get('rooms')}-комн." if x.get("rooms") else "студия", f"площадь {x.get('area')} м²",
             f"кухня {x['kitchen']} м²" if x.get("kitchen") else "кухня не указана",
             f"этаж {x.get('floor')}/{x.get('floors')}" if x.get("floor") else "этаж не указан",
             f"год {x['year']}" if x.get("year") else "год не указан", f"ремонт: {x.get('condition') or 'не указан'}"]
    parts += [f"{FEATURES[k][0]}: {'да' if feature_status(x, k) else 'нет' if feature_status(x, k) is False else 'не указано'}"
              for k in ("balcony", "separate_wc")]
    return "; ".join(parts)


def clean_corrections(raw: dict | None, x: dict) -> dict:
    """Уточнения продавца -> только правдоподобные и новые: в диапазоне, не совпадают с объявлением."""
    out: dict = {}
    for key, value in (raw or {}).items():
        if value is None or value == "":
            continue
        if key in CORRECTION_RANGES:
            try:
                v = float(str(value).replace(",", "."))
            except ValueError:
                continue
            lo, hi = CORRECTION_RANGES[key]
            if not lo <= v <= hi:
                continue
            v = round(v, 1) if key in ("kitchen", "area") else int(v)
            if x.get(key) is None or abs(float(x[key]) - v) > 0.05:
                out[key] = v
        elif key == "condition" and isinstance(value, str) and 0 < len(value.strip()) <= 40:
            if value.strip().lower() != (x.get("condition") or "").lower():
                out[key] = value.strip()
        elif key in FEATURES and isinstance(value, bool) and feature_status(x, key) is not value:
            out[key] = value
    floors = out.get("floors", x.get("floors"))
    if "floor" in out and floors and out["floor"] > floors:
        out.pop("floor")
    return out


async def extract_learned(llm: LLMClient, n: Negotiation) -> dict:
    """Что агент узнал за звонок, на какое время договорились и какие параметры продавец уточнил."""
    try:
        system = prompts.EXTRACT_CALL + "\n" + guard.POLICY
        data = await llm.chat_json([{"role": "system", "content": system},
                                    guard.data_message("call_transcript", {"labels": [q["label"] for q in n.questions],
                                                                         "listing": _listing_brief(n.listing), "transcript": n.transcript()})],
                                   temperature=0.0, tag="extract_call")
    except LLM_FAIL as exc:  # итог звонка не разобран — расшифровка остаётся на экране, факты не выдумываем
        log.warning("итог звонка не разобран: %s", type(exc).__name__)
        return {"facts": {}, "viewing": n.viewing, "corrections": {}}
    raw = data.get("facts")
    labels = {q["label"] for q in n.questions}
    facts = {k: guard.output(v) for k, v in raw.items() if k in labels and isinstance(v, str) and v} if isinstance(raw, dict) else {}
    corrections = clean_corrections(data.get("corrections") if isinstance(data.get("corrections"), dict) else None, n.listing)
    return {"facts": facts, "viewing": n.viewing or data.get("viewing"), "corrections": corrections}


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
    # сказанная вслух цена должна совпадать с той, что агент на самом деле принимает или предлагает
    said = parse_price_near(move["utterance"], n.listing["price"])
    if move["accept"] and said and said != n.asking:
        notes.append(f"Агент назвал {fmt(said)}, но принимает цену продавца {fmt(n.asking)} — реплика исправлена")
        move["utterance"] = f"Хорошо, {fmt(n.asking)} — договорились. Подтвержу у клиента и пришлю итоги письменно."
    elif not move["accept"] and move["offer"] and said and said > move["offer"]:
        notes.append(f"Агент назвал {fmt(said)} — выше своего предложения {fmt(move['offer'])}, реплика исправлена")
        move["utterance"] = f"Клиент готов на {fmt(move['offer'])}. Сделка быстрая, через безопасные расчёты Сбера. Что скажете?"
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


async def _model(step, what: str):
    """Ход модели с одним повтором; не ответила — звонок останавливается (CallError)."""
    for attempt in range(2):
        try:
            return await step()
        except LLM_FAIL as exc:
            log.warning("%s: %s", what, type(exc).__name__)
            if attempt:
                raise CallError(f"модель не ответила ({what})") from exc


async def run(llm: LLMClient, n: Negotiation, human_input: SellerInput | None = None) -> AsyncIterator[Event]:
    """Асинхронный генератор событий звонка: call_phase / turn / guard / price / call_end. Бросает CallError."""

    def price_event() -> Event:
        return {"type": "price", "ask": n.asking, "offer": n.offer, "savings": n.listing["price"] - n.asking,
                "base": n.listing["price"]}

    async def agent_turn(round_no: int, phase: str):
        async def step():
            move = await llm_buyer(llm, n, round_no, phase)
            return move, guard_buyer(n, move, phase, first=not n.history)
        move, notes = await _model(step, "реплика ассистента")
        move["utterance"] = guard.output(move["utterance"])
        if move["offer"] is not None:
            n.offer = move["offer"]
        n.history.append({"speaker": "agent", "text": move["utterance"], "offer": move["offer"]})
        return move, notes

    async def seller_turn(round_no: int, phase: str):
        if n.live and human_input is not None:
            text = await human_input()
            if text is None or text == HANGUP:
                return None
            if phase == "bargain":
                smove = await parse_human_seller(llm, n, text)
                said = smove["ask"]
                smove["ask"] = r10k(min(said, n.asking)) if said else n.asking
                if smove["accept"] and n.offer is not None and (said is None or said <= n.offer):
                    smove["ask"] = n.offer  # «согласен» без своей цены — на наше предложение
            else:
                smove = {"utterance": text, "ask": n.asking, "accept": False}
        else:
            async def step():
                move = await llm_seller(llm, n, phase)
                guard_seller(n, move)
                return move
            smove = await _model(step, "реплика ИИ-продавца")
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
    if n.live:
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
            if n.live:
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
                text = (f"Понимаю. Моё предложение — {fmt(n.offer)}. "
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
        if n.live:
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
        "learned": learned["facts"],
        "viewing": learned["viewing"],
        "corrections": learned["corrections"],
        "transcript": n.history,
    }


async def queue_input(queue: "asyncio.Queue[str]", timeout: float = 180.0) -> str | None:
    from ..runtime import human_wait
    try:
        with human_wait():
            return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
