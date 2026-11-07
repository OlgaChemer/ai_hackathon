"""Подборка с обратной связью: отметки «нравится / не то» и пожелания клиента -> поправка к оценке квартиры.

Из отметок код выводит простые правила (район, площадь, кухня, этаж, ремонт, цена). Пожелания текстом разбирает
агент и передаёт их действием update_request — они попадают сюда через профиль (Prefs.sync).
`learned` объясняет клиенту, что учтено.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .. import geo
from .features import FEATURES, feature_status
from .numbers import fmt


@dataclass
class Prefs:
    profile: dict = field(default_factory=dict)
    liked: list[str] = field(default_factory=list)
    disliked: list[str] = field(default_factory=list)
    districts_plus: set[str] = field(default_factory=set)
    explicit_avoids: set[str] = field(default_factory=set)
    districts_minus: set[str] = field(default_factory=set)
    area_min: float | None = None
    kitchen_min: float | None = None
    avoid_first: bool = False
    avoid_last: bool = False
    conditions_plus: set[str] = field(default_factory=set)
    metro_max: int | None = None
    price_focus: int | None = None
    features: list[str] = field(default_factory=list)  # удобства, которые нужны обязательно (features.FEATURES)
    poi: list[str] = field(default_factory=list)       # что должно быть рядом с домом (geo.POI)

    def sync(self, profile: dict) -> None:
        """Пожелания из профиля запроса (их задаёт агент) -> правила подборки. Выученное из отметок остаётся."""
        self.profile = dict(profile)
        self.features = list(profile.get("features") or [])
        self.poi = list(profile.get("poi") or [])
        self.area_min = profile.get("area_min")
        self.kitchen_min = profile.get("kitchen_min")
        self.avoid_first = bool(profile.get("not_first_floor"))
        self.metro_max = profile.get("metro_max_minutes")
        learned = self.districts_minus - self.explicit_avoids
        self.explicit_avoids = set(profile.get("avoid_districts") or [])
        self.districts_minus = learned | self.explicit_avoids

    def learned(self) -> list[str]:
        out = []
        soft = set(self.profile.get("soft_criteria") or [])
        required_features = [k for k in self.features if f"feature:{k}" not in soft]
        preferred_features = [k for k in self.features if f"feature:{k}" in soft]
        required_poi = [k for k in self.poi if f"poi:{k}" not in soft]
        preferred_poi = [k for k in self.poi if f"poi:{k}" in soft]
        if preferred_features or preferred_poi:
            out.append("желательно: " + ", ".join([FEATURES[k][0] for k in preferred_features] + [geo.POI[k][0] for k in preferred_poi]))
        if required_features:
            out.append("важно: " + ", ".join(FEATURES[k][0] for k in required_features))
        if required_poi:
            out.append("обязательно рядом: " + ", ".join(geo.POI[k][0] for k in required_poi))
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
    for key in p.features:
        if f"feature:{key}" in p.profile.get("soft_criteria", []):
            continue
        status = feature_status(x, key)
        s += 12 if status else -8 if status is False else 0
    s += geo.score(x, [k for k in p.poi if f"poi:{k}" not in p.profile.get("soft_criteria", [])])
    m = (x.get("metro") or {}).get("minutes")
    if p.metro_max and m is not None and m > p.metro_max:
        s -= 8
    if p.price_focus:
        s -= min(10, abs(x["price"] - p.price_focus) / p.price_focus * 50)
    return s
