"""Критерии подбора: 5 обязательных и до 5 дополнительных.

Клиент называет критерии сам, остальные агент доспрашивает. Дальше каждое
объявление проверяется по этим критериям — считает код, а не модель:

- проверяем только то, что клиент назвал И что видно в объявлении;
- если данных в объявлении нет (например, не указан ремонт), критерий
  не считается ни выполненным, ни проваленным — он не портит процент;
- совпадение = выполнено / проверено, порог 0.7 — объявление подходит;
- если проверить удалось меньше MIN_CHECKED критериев, процент ненадёжен:
  решают жёсткие фильтры (город, комнаты, бюджет).

Дополнительные критерии «инфраструктура» и «сроки» в объявлении не проверить —
они влияют на ранжирование и на темп работы, но не на процент совпадения.
"""
from __future__ import annotations

import re

THRESHOLD = 0.7
MIN_CHECKED = 3
TOP = 10

CORE = ("city", "budget_max", "rooms", "condition", "metro_max_minutes")
EXTRA = ("floor", "area", "house", "infra", "timeline")
EXTRA_MIN, EXTRA_MAX = 3, 5

# «Состояние не хуже ожидаемого»: ранг объявления >= ранга запроса.
CONDITION_RANK = {
    "без отделки": 0, "требует ремонта": 0, "предчистовая": 1,
    "косметика": 2, "чистовая отделка": 3, "евроремонт": 3, "дизайнерский": 3,
}
CONDITION_NEED = {"ready": 3, "cosmetic": 2, "repair": 0, "any": 0}
CONDITION_LABEL = {"ready": "заехать сразу", "cosmetic": "косметический", "repair": "под ремонт", "any": "не важно"}


# ------------------------------------------------------------------ что уже известно

def is_set(profile: dict, key: str) -> bool:
    """Критерий задан, если клиент ответил — в том числе «не важно»."""
    p = profile
    if key == "city":
        return bool(p.get("city"))
    if key == "budget_max":
        return bool(p.get("budget_max"))
    if key == "rooms":
        return p.get("rooms") is not None
    if key == "condition":
        return bool(p.get("condition"))
    if key == "metro_max_minutes":
        return bool(p.get("metro_max_minutes")) or bool(p.get("metro_any"))
    if key == "floor":
        return bool(p.get("not_first_floor") or p.get("not_last_floor") or p.get("floor_any"))
    if key == "area":
        return bool(p.get("area_min") or p.get("kitchen_min") or p.get("area_any"))
    if key == "house":
        return bool(p.get("material") or p.get("year_min") or p.get("house_any"))
    if key == "infra":
        return bool(p.get("infra") or p.get("infra_any"))
    if key == "timeline":
        return bool(p.get("timeline") or p.get("timeline_any"))
    return False


def missing(profile: dict, keys) -> list[str]:
    return [k for k in keys if not is_set(profile, k)]


def missing_core(profile: dict) -> list[str]:
    return missing(profile, CORE)


def missing_extra(profile: dict) -> list[str]:
    return missing(profile, EXTRA)


def core_ready(profile: dict) -> bool:
    return not missing_core(profile)


def extra_count(profile: dict) -> int:
    return len(EXTRA) - len(missing_extra(profile))


# ------------------------------------------------------------------ вопросы

QUESTIONS = {
    "city": "В каком городе ищем? Если есть предпочтения по району — назовите.",
    "budget_max": "Какой максимум по бюджету? Если не знаете — посчитаю по вашим доходам.",
    "rooms": "Сколько комнат минимум — студия, одна, две, три?",
    "condition": "Какое состояние квартиры устроит: заехать сразу, косметический ремонт или готовы делать ремонт с нуля?",
    "metro_max_minutes": "Насколько важно метро: пешком до 10 минут или готовы добираться транспортом?",
    "floor": "Какие этажи не рассматриваете — первый, последний?",
    "area": "Есть рамки по площади: общая от скольких м² и кухня от скольких?",
    "house": "Есть предпочтения по дому — кирпич, монолит, панель? И насколько старый дом допустим?",
    "infra": "Что должно быть рядом — школа, садик, парк?",
    "timeline": "К какому сроку нужна квартира — в течение месяца, 2–3 месяца или пока присматриваетесь?",
}

CHIPS = {
    "city": [],  # города подставляет оркестратор
    "budget_max": [],
    "rooms": ["Студия", "1 комната", "2 комнаты", "3 комнаты"],
    "condition": ["Заехать сразу", "Косметический", "Под ремонт", "Не важно"],
    "metro_max_minutes": ["Пешком до 10 минут", "До 20 минут транспортом", "Метро не важно"],
    "floor": ["Не первый", "Не первый и не последний", "Любой этаж"],
    "area": ["Общая от 45 м²", "Общая от 55 м²", "Кухня от 10 м²", "Площадь не важна"],
    "house": ["Кирпич или монолит", "Не старше 1980 года", "Дом не важен"],
    "infra": ["Школа", "Садик", "Парк", "Ничего критично"],
    "timeline": ["В течение месяца", "2–3 месяца", "Пока присматриваюсь"],
}


def questions_text(profile: dict, keys: list[str]) -> str:
    """Нумерованный список вопросов — клиент может ответить одной строкой."""
    asks = [QUESTIONS[k] for k in keys]
    if len(asks) == 1:
        return asks[0]
    lines = " ".join(f"{i}) {q}" for i, q in enumerate(asks, 1))
    return f"Уточню {len(asks)} момента — можно ответить одной строкой: {lines}"


def progress(profile: dict) -> dict:
    return {
        "core_done": len(CORE) - len(missing_core(profile)),
        "core_total": len(CORE),
        "extra_done": extra_count(profile),
        "extra_min": EXTRA_MIN,
        "extra_max": EXTRA_MAX,
    }


# ------------------------------------------------------------------ проверка объявления

def _metro_walk(x: dict) -> int | None:
    m = x.get("metro") or {}
    minutes = m.get("minutes")
    if minutes is None:
        return None
    return int(minutes) if m.get("mode") == "пешком" else int(minutes) * 2


def _check_city(profile: dict, x: dict) -> bool | None:
    return (x.get("city") or "") == profile.get("city")


def _check_budget(profile: dict, x: dict) -> bool | None:
    budget = profile.get("budget_max")
    if not budget:
        return None
    return x.get("price", 0) <= budget * 1.05  # небольшой перебор оставляем: можно сторговать


def _check_rooms(profile: dict, x: dict) -> bool | None:
    return x.get("rooms") == profile.get("rooms")


def _check_condition(profile: dict, x: dict) -> bool | None:
    need = CONDITION_NEED.get(profile.get("condition") or "any", 0)
    if need == 0:  # «под ремонт» или «не важно» — состояние не ограничивает
        return None
    rank = CONDITION_RANK.get((x.get("condition") or "").lower())
    if rank is None:
        return None
    return rank >= need


def _check_metro(profile: dict, x: dict) -> bool | None:
    limit = profile.get("metro_max_minutes")
    if not limit:
        return None
    walk = _metro_walk(x)
    if walk is None:
        return None
    return walk <= limit


def _check_floor(profile: dict, x: dict) -> bool | None:
    floor, floors = x.get("floor"), x.get("floors")
    if floor is None:
        return None
    if profile.get("not_first_floor") and floor == 1:
        return False
    if profile.get("not_last_floor") and floors and floor == floors:
        return False
    return True


def _check_area(profile: dict, x: dict) -> bool | None:
    area_min, kitchen_min = profile.get("area_min"), profile.get("kitchen_min")
    if not area_min and not kitchen_min:
        return None
    area, kitchen = x.get("area"), x.get("kitchen")
    if area_min:
        if area is None:
            return None
        if area < area_min:
            return False
    if kitchen_min:
        if kitchen is None:
            return None  # кухня не указана — не портим процент
        if kitchen < kitchen_min:
            return False
    return True


def _check_house(profile: dict, x: dict) -> bool | None:
    material, year_min = profile.get("material") or [], profile.get("year_min")
    if not material and not year_min:
        return None
    if material:
        have = (x.get("material") or "").lower()
        if not have:
            return None
        if not any(m in have or have in m for m in material):
            return False
    if year_min:
        year = x.get("year")
        if not year:
            return None
        if year < year_min:
            return False
    return True


# Критерии, которые нельзя проверить по объявлению: они про сценарий и ранжирование.
NOT_CHECKABLE = {"infra", "timeline"}

CHECKS = {
    "city": _check_city,
    "budget_max": _check_budget,
    "rooms": _check_rooms,
    "condition": _check_condition,
    "metro_max_minutes": _check_metro,
    "floor": _check_floor,
    "area": _check_area,
    "house": _check_house,
}

LABEL = {
    "city": "город",
    "budget_max": "бюджет",
    "rooms": "комнаты",
    "condition": "состояние",
    "metro_max_minutes": "метро",
    "floor": "этаж",
    "area": "площадь",
    "house": "дом",
    "infra": "инфраструктура",
    "timeline": "сроки",
}


def check(profile: dict, x: dict) -> dict:
    """Сколько критериев клиента объявление выполняет.

    Возвращает {"ok", "checked", "pct", "failed": [ключи], "unknown": [ключи]}.
    Критерии без данных в объявлении попадают в unknown и не влияют на pct.
    """
    ok = checked = 0
    failed: list[str] = []
    unknown: list[str] = []
    for key, fn in CHECKS.items():
        if key not in CORE and key not in EXTRA:
            continue
        if not is_set(profile, key):
            continue
        if key in NOT_CHECKABLE:
            continue
        verdict = fn(profile, x)
        if verdict is None:
            unknown.append(key)
            continue
        checked += 1
        if verdict:
            ok += 1
        else:
            failed.append(key)
    pct = round(ok / checked, 3) if checked else None
    return {"ok": ok, "checked": checked, "pct": pct, "failed": failed, "unknown": unknown}


def matches(profile: dict, x: dict, threshold: float = THRESHOLD) -> bool:
    """Объявление подходит, если совпало не меньше 70% проверяемых критериев."""
    res = check(profile, x)
    if res["pct"] is None:  # проверить нечего — решают жёсткие фильтры
        return True
    if res["checked"] < MIN_CHECKED:  # слишком мало данных, чтобы судить по проценту
        return True
    return res["pct"] >= threshold


def failed_labels(profile: dict, x: dict) -> list[str]:
    return [LABEL.get(k, k) for k in check(profile, x)["failed"]]


def summary_line(profile: dict, x: dict) -> str:
    res = check(profile, x)
    if res["pct"] is None:
        return "проверить нечего"
    tail = f", не совпало: {', '.join(failed_labels(profile, x))}" if res["failed"] else ""
    return f"совпало {res['ok']} из {res['checked']} ({round(res['pct'] * 100)}%){tail}"


# ------------------------------------------------------------------ отбор

def rank(profile: dict, listings: list[dict]) -> list[dict]:
    """Сортировка: сначала те, у кого выше доля совпавших критериев."""
    def key(x: dict) -> tuple:
        res = check(profile, x)
        pct = res["pct"] if res["pct"] is not None else -1.0
        return (-pct, -res["ok"])

    return sorted(listings, key=key)


def select(profile: dict, listings: list[dict], limit: int = TOP) -> tuple[list[dict], dict]:
    """Прошедшие порог объявления, отсортированные по доле совпадения.

    Если вариантов меньше limit, порог честно ослабляется до 60% и 50% — и это
    видно в отчёте, чтобы агент сказал клиенту правду, а не выдал подборку молча.
    """
    info = {"threshold": THRESHOLD, "passed": 0, "total": len(listings), "relaxed": None}
    for threshold, note in ((THRESHOLD, None), (0.6, "снизил порог до 60%"), (0.5, "снизил порог до 50%")):
        passed = [x for x in listings if matches(profile, x, threshold)]
        info = {"threshold": threshold, "passed": len(passed), "total": len(listings), "relaxed": note}
        if len(passed) >= limit:
            return rank(profile, passed), info
    return rank(profile, passed), info
