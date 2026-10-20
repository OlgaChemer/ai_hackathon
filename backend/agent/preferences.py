"""Обратная связь клиента: отметки «нравится / не нравится» и пожелания текстом.

Из отметок выводим простые правила (район, площадь, кухня, этаж, ремонт, цена),
из текста — то же через LLM (или регулярные выражения без модели).
Правила дают поправку к оценке объявления, а `learned` объясняет клиенту, что учтено.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from ..llm import LLMClient, LLMError
from .tools import fmt


@dataclass
class Prefs:
    liked: list[str] = field(default_factory=list)
    disliked: list[str] = field(default_factory=list)
    districts_plus: set[str] = field(default_factory=set)
    districts_minus: set[str] = field(default_factory=set)
    area_min: float | None = None
    kitchen_min: float | None = None
    avoid_first: bool = False
    avoid_last: bool = False
    conditions_plus: set[str] = field(default_factory=set)
    metro_max: int | None = None
    price_focus: int | None = None
    notes: list[str] = field(default_factory=list)

    def learned(self) -> list[str]:
        out = []
        if self.districts_plus:
            out.append("нравятся районы: " + ", ".join(sorted(self.districts_plus)))
        if self.districts_minus:
            out.append("не показываю: " + ", ".join(sorted(self.districts_minus)))
        if self.area_min:
            out.append(f"площадь от {self.area_min:g} м²")
        if self.kitchen_min:
            out.append(f"кухня от {self.kitchen_min:g} м²")
        if self.avoid_first:
            out.append("не первый этаж")
        if self.avoid_last:
            out.append("не последний этаж")
        if self.conditions_plus:
            out.append("ремонт: " + ", ".join(sorted(self.conditions_plus)))
        if self.metro_max:
            out.append(f"до метро до {self.metro_max} мин")
        if self.price_focus:
            out.append(f"цена ближе к {fmt(self.price_focus)}")
        return out


def learn(p: Prefs, by_id: dict[str, dict]) -> None:
    """Пересчитывает правила по всем отметкам."""
    liked = [by_id[i] for i in p.liked if i in by_id]
    disliked = [by_id[i] for i in p.disliked if i in by_id]
    liked_d = {x["district"] for x in liked if x.get("district")}
    p.districts_plus |= liked_d
    # район исключаем, только если клиент уже показал, какие районы ему нравятся
    if liked:
        counts: dict[str, int] = {}
        for x in disliked:
            if x.get("district") and x["district"] not in liked_d:
                counts[x["district"]] = counts.get(x["district"], 0) + 1
        p.districts_minus |= {d for d, c in counts.items() if c >= 2}  # один отказ — ещё не про район
    if liked and disliked:
        la = [x["area"] for x in liked]
        da = [x["area"] for x in disliked]
        if statistics.mean(da) < min(la) - 2:
            p.area_min = max(p.area_min or 0, round(min(la) - 2))
        lk = [x["kitchen"] for x in liked if x.get("kitchen")]
        dk = [x["kitchen"] for x in disliked if x.get("kitchen")]
        if lk and dk and statistics.mean(dk) < min(lk) - 0.5:
            p.kitchen_min = max(p.kitchen_min or 0, round(min(lk) - 0.5, 1))
        lp = statistics.mean(x["price"] for x in liked)
        dp = statistics.mean(x["price"] for x in disliked)
        if len(liked) >= 2 and len(disliked) >= 2 and dp > lp * 1.06:
            p.price_focus = int(round(lp, -5))
    if any(x.get("floor") == 1 for x in disliked) and not any(x.get("floor") == 1 for x in liked):
        p.avoid_first = True
    if any(x.get("floor") and x.get("floor") == x.get("floors") for x in disliked) and not any(
        x.get("floor") and x.get("floor") == x.get("floors") for x in liked
    ):
        p.avoid_last = True
    p.conditions_plus |= {x["condition"] for x in liked if x.get("condition") in ("евроремонт", "дизайнерский")}


def adjust(x: dict, p: Prefs) -> float:
    s = 0.0
    if x["id"] in p.liked:
        s += 25
    if x.get("district") in p.districts_plus:
        s += 8
    if x.get("district") in p.districts_minus:
        s -= 15
    if p.area_min and x["area"] < p.area_min:
        s -= 10
    if p.kitchen_min and x.get("kitchen") is not None and x["kitchen"] < p.kitchen_min:
        s -= 8
    if p.avoid_first and x.get("floor") == 1:
        s -= 12
    if p.avoid_last and x.get("floor") and x.get("floor") == x.get("floors"):
        s -= 6
    if x.get("condition") in p.conditions_plus:
        s += 5
    m = (x.get("metro") or {}).get("minutes")
    if p.metro_max and m is not None and m > p.metro_max:
        s -= 8
    if p.price_focus:
        s -= min(10, abs(x["price"] - p.price_focus) / p.price_focus * 50)
    return s


PARSE_FEEDBACK = """Клиент подбирает квартиру и пишет пожелание к подборке. Извлеки JSON:
{"area_min": число или null, "kitchen_min": число или null, "avoid_first_floor": true/false,
 "avoid_last_floor": true/false, "metro_max_minutes": число или null,
 "districts_plus": [районы, которые хочет], "districts_minus": [районы, которые не хочет],
 "budget_max": число в рублях или null, "condition": "евроремонт"|"косметика"|null}
Районы пиши в именительном падеже, как в списке: {districts}."""


async def apply_text(p: Prefs, text: str, llm: LLMClient, districts: list[str]) -> dict:
    """Разбирает пожелание текстом. Возвращает распознанные поля (budget_max — для профиля)."""
    p.notes.append(text)
    try:
        d = await llm.chat_json(
            [{"role": "system", "content": PARSE_FEEDBACK.replace("{districts}", ", ".join(districts) or "—")},
             {"role": "user", "content": text}],
            temperature=0.0, tag="feedback",
        )
    except (LLMError, KeyError, TypeError, ValueError):
        d = _rules(text, districts)
    if d.get("area_min"):
        p.area_min = float(d["area_min"])
    if d.get("kitchen_min"):
        p.kitchen_min = float(d["kitchen_min"])
    p.avoid_first |= bool(d.get("avoid_first_floor"))
    p.avoid_last |= bool(d.get("avoid_last_floor"))
    if d.get("metro_max_minutes"):
        p.metro_max = int(d["metro_max_minutes"])
    p.districts_plus |= {x for x in d.get("districts_plus") or [] if isinstance(x, str)}
    p.districts_minus |= {x for x in d.get("districts_minus") or [] if isinstance(x, str)}
    if d.get("condition") in ("евроремонт", "косметика"):
        p.conditions_plus.add(d["condition"])
    return d


def _rules(text: str, districts: list[str]) -> dict:
    t = text.lower()
    d: dict = {}
    if m := re.search(r"кухн\w*\D{0,12}(\d{1,2})", t):
        d["kitchen_min"] = float(m.group(1))
    elif "кухн" in t and ("больш" in t or "просторн" in t):
        d["kitchen_min"] = 10.0
    if (m := re.search(r"(?:от|больше|не меньше)\s*(\d{2,3})\s*(?:м|кв)", t)) and int(m.group(1)) >= 20 and "kitchen_min" not in d:
        d["area_min"] = float(m.group(1))
    d["avoid_first_floor"] = "не перв" in t or "без перв" in t
    d["avoid_last_floor"] = "не последн" in t
    if m := re.search(r"метро\D{0,15}(\d{1,2})", t):
        d["metro_max_minutes"] = int(m.group(1))
    if "евро" in t or re.search(r"свеж\w* ремонт", t) or "с ремонтом" in t:
        d["condition"] = "евроремонт"
    neg = re.findall(r"не\s+(?:в\s+)?(\w+)", t)
    d["districts_minus"] = [x for x in districts if any(n[:5] in x.lower() for n in neg)]
    d["districts_plus"] = [x for x in districts if x.lower()[:5] in t and x not in d["districts_minus"]]
    return d
