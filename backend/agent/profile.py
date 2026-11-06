"""Запрос клиента: профиль (город, комнаты, бюджет, пожелания), его нормализация и жёсткие фильтры."""
from __future__ import annotations

import re

from .. import geo
from ..sources import normalize_city
from .features import FEATURE_WITH, FEATURES, merge_features
from .numbers import fmt


REQUIRED = {"city": "город", "budget_max": "бюджет", "rooms": "число комнат"}


EMPTY_PROFILE = {
    "city": None,
    "rooms": None,
    "rooms_options": [],       # все допустимые варианты, например [1, 2]
    "cities": [],              # дополнительные явно названные территории
    "prefer_center": False,    # близость к центру — пожелание, если не указано иначе
    "center_required": False,
    "filters": [],
    "seller_questions": [],
    "budget_max": None,
    "payment": None,          # cash | mortgage_ready | mortgage_saving
    "okrug": None,
    "districts": [],          # районы, которые клиент назвал (ищем в них)
    "district_type": None,    # sleeping | quiet | prestige — тип района (agent/districts.py)
    "avoid_districts": [],    # районы, которые не показывать
    "not_first_floor": False,
    "not_last_floor": False,
    "condition": None,        # ready | cosmetic | repair | any
    "material": [],
    "year_min": None,
    "timeline": None,         # month | quarter | exploring
    "soft_criteria": [],      # ключи условий, которые клиент назвал желательными, не обязательными

    "kitchen_min": None,
    "area_min": None,
    "metro_max_minutes": None,
    "children": None,         # сколько детей (0 — нет) — для семейной ипотеки
    "youngest_age": None,     # возраст младшего ребёнка, полных лет
    "matcap": None,           # есть неизрасходованный маткапитал: True / сумма
    "housing": None,          # secondary | new | both | None (не сказал)
    "features": [],           # удобства, которые нужны обязательно (features.FEATURES)
    "poi": [],                # что должно быть в 1 км (geo.POI)
    "wishes": [],
    "field_states": {},  # unknown / unrestricted / constrained; отсутствие не равно ответу «любой»
}


# Округа Москвы и как их называют люди
OKRUG_ALIASES = [
    (r"юго-?запад|\bюзао\b", "ЮЗАО"), (r"юго-?восток|\bювао\b", "ЮВАО"), (r"северо-?запад|\bсзао\b", "СЗАО"),
    (r"северо-?восток|\bсвао\b", "СВАО"), (r"\bцентр\w*|\bцао\b", "ЦАО"), (r"\bсевер\w*|\bсао\b", "САО"),
    (r"\bвосток\w*|\bвао\b", "ВАО"), (r"\bюг\b|\bюге\b|\bюаo\b|\bюао\b", "ЮАО"), (r"\bзапад\w*|\bзао\b", "ЗАО"),
]


def okrug_from_text(text: str | None) -> str | None:
    t = (text or "").lower().replace("ё", "е")
    return next((name for pattern, name in OKRUG_ALIASES if re.search(pattern, t)), None)


def merge_profile(raw: dict, base: dict | None = None) -> dict:
    """Изменения запроса поверх известного профиля: пустые поля не затирают сказанное раньше,
    списки (features, poi, districts, wishes) заменяются целиком — так агент может и добавить, и снять условие."""
    p = {**EMPTY_PROFILE, **(base or {})}
    for key in EMPTY_PROFILE:
        if key not in raw:
            continue
        v = raw[key]
        if v is None or v == "":
            if key in ("area_min", "kitchen_min", "metro_max_minutes", "year_min", "condition", "timeline"):
                p[key] = None
            continue
        p[key] = v
    if raw.get("city") and "cities" not in raw and normalize_city(raw["city"]) != normalize_city((base or {}).get("city")):
        p["cities"] = []
    if raw.get("rooms") not in (None, "") and "rooms_options" not in raw:
        p["rooms_options"] = []
    p["rooms_options"] = sorted(set(int(n) for n in p.get("rooms_options") or [] if type(n) is int and 0 <= n <= 5))
    if p["rooms_options"]:
        p["rooms"] = p["rooms_options"][0]
    elif raw.get('rooms_options')==[] and 'rooms' not in raw:p['rooms']=None
    p["cities"] = list(dict.fromkeys(normalize_city(c) or c for c in p.get("cities") or []))[:3]
    p["city"] = normalize_city(p["city"]) or p["city"]
    for key, cast in (("rooms", int), ("budget_max", int), ("kitchen_min", float), ("area_min", float), ("metro_max_minutes", int),
                      ("children", int), ("youngest_age", int), ("year_min", int)):
        try:
            p[key] = cast(p[key]) if p[key] is not None else None
        except (TypeError, ValueError):
            p[key] = None
    p["districts"] = [str(d) for d in p.get("districts") or []]
    p["avoid_districts"] = [str(d) for d in p.get("avoid_districts") or []]
    if p.get("okrug"):  # «юго-запад» -> ЮЗАО; не Москва — округов нет
        p["okrug"] = okrug_from_text(p["okrug"]) or (p["okrug"] if p["okrug"] in ("ЦАО", "САО", "СВАО", "ВАО", "ЮВАО", "ЮАО", "ЮЗАО", "ЗАО", "СЗАО") else None)
    if p.get("city") != "Москва":
        p["okrug"] = None
    if p.get("payment") not in (None, "cash", "mortgage_ready", "mortgage_saving"):
        p["payment"] = None
    if p.get("housing") not in (None, "secondary", "new", "both"):
        p["housing"] = None
    p["features"] = merge_features([k for k in p.get("features") or [] if k in FEATURES])
    p["poi"] = list(dict.fromkeys(k for k in p.get("poi") or [] if k in geo.POI))
    if p.get("condition") not in (None, "ready", "cosmetic", "repair", "any"):
        p["condition"] = None
    if p.get("timeline") not in (None, "month", "quarter", "exploring"):
        p["timeline"] = None
    p["material"] = [m for m in p.get("material") or [] if m in ("кирпич", "монолит", "панель", "блочный", "дерево")]
    from .criteria import CRITERIA_KEYS
    p["soft_criteria"] = [k for k in p.get("soft_criteria") or [] if k in CRITERIA_KEYS]
    p["wishes"] = [str(w) for w in (p.get("wishes") or [])][:6]
    states={**(base or {}).get('field_states',{}),**(raw.get('field_states') or {})}
    for key in ('budget_max','rooms','districts','okrug','housing'):
        concrete='rooms_options' if key=='rooms' and raw.get('rooms_options') else key
        if raw.get(concrete) not in (None,'',[]):states[key]='constrained'
        if (raw.get('field_states') or {}).get(key) in ('unknown','unrestricted'):
            p[key]=[] if key=='districts' else None
            if key=='rooms':p['rooms_options']=[]
    p['field_states']=states
    return p


def missing_required(p: dict) -> list[str]:
    return [k for k in REQUIRED if p.get(k) in (None, "")]


def missing_search(p: dict) -> list[str]:
    """Область поиска нужна; неназванные комнаты и цена не ограничивают чтение."""
    return [] if p.get('city') else ['city']


def rooms_text(rooms: int | None) -> str:
    if rooms is None:return 'любая комнатность'
    return "студия" if rooms == 0 else f"{rooms}-комнатные"


def profile_summary(p: dict) -> str:
    rooms = "квартиру без ограничения комнат" if p['rooms'] is None else "студию" if p["rooms"] == 0 else f"{p['rooms']}-комнатную квартиру"
    if len(p.get("rooms_options") or []) > 1:
        rooms = "/".join(map(str, p["rooms_options"])) + "-комнатную квартиру"
    where = ", ".join([p["city"], *(c for c in p.get("cities") or [] if c != p["city"])]) + (f", {p['okrug']}" if p.get("okrug") else "")
    if p.get("districts"):
        where += f" (район {', '.join(p['districts'])})"
    extra = (["центр: до 5 км по прямой от опорной точки"] if p.get("center_required")
             else ["желательно ближе к центру"] if p.get("prefer_center") else [])
    from .listing_query import describe
    extra += [describe(f) for f in p.get('filters') or []]
    if p.get("features"):
        extra.append("с " + ", ".join(FEATURE_WITH[k] for k in p["features"]))
    near = [geo.POI[k][0] for k in p.get("poi") or [] if k in geo.POI]
    if near:
        extra.append("рядом: " + ", ".join(near))
    if p.get("not_first_floor"):
        extra.append("не на первом этаже")
    if p.get("not_last_floor"):
        extra.append("не на последнем этаже")
    if p.get("condition") not in (None, "any"):
        extra.append({"ready": "заехать сразу", "cosmetic": "косметический ремонт", "repair": "под ремонт"}[p["condition"]])
    if p.get("material"):
        extra.append("дом: " + ", ".join(p["material"]))
    if p.get("year_min"):
        extra.append(f"дом от {p['year_min']} года")
    if p.get("kitchen_min"):
        extra.append(f"кухня от {p['kitchen_min']:g} м²")
    if p.get("area_min"):
        extra.append(f"площадь от {p['area_min']:g} м²")
    if p.get("metro_max_minutes"):
        extra.append(f"до метро до {p['metro_max_minutes']} мин")
    if p.get("soft_criteria"):
        from .criteria import SCALARS
        labels = {**SCALARS, **{f"feature:{k}": v[0] for k, v in FEATURES.items()}, **{f"poi:{k}": v[0] for k, v in geo.POI.items()}}
        extra.append("желательно, не обязательно: " + ", ".join(labels[k] for k in p["soft_criteria"]))
    tail = (", " + ", ".join(extra)) if extra else ""
    market = {"new": " в новостройке", "both": " в новостройке или на вторичке", "secondary": " на вторичном рынке"}.get(p.get("housing"), "")
    budget=f"до {fmt(p['budget_max'])}" if p.get('budget_max') else 'без ограничения цены'
    return f"Ищу {rooms}{market} в {where} {budget}{tail}."


def _stem(name: str) -> str:
    return name.lower().replace("ё", "е")[:5]


def in_districts(x: dict, districts: list[str] | None) -> bool:
    """Объявление в районе, который назвал клиент («черемушек» ~ «Черёмушки»). Учитываем и микрорайоны
    из выдачи Циана (cian_districts: «Сипайлово» внутри Октябрьского района Уфы)."""
    names = [x.get("district") or "", *(row[1] for row in x.get("cian_districts") or [] if len(row) > 1 and row[1])]
    names = [n.lower().replace("ё", "е") for n in names if n]
    return bool(names and districts and any(_stem(n) in d for n in districts for d in names))


def passes_hard_filters(x: dict, p: dict) -> bool:
    if p.get("city") and x.get("city") not in [p["city"], *(p.get("cities") or [])]:
        return False
    if p.get("rooms") is not None and x["rooms"] not in (p.get("rooms_options") or [p["rooms"]]):
        return False
    if p.get('budget_max') is not None and x["price"] > p["budget_max"] * 1.05:
        return False
    if p.get("not_first_floor") and "not_first_floor" not in p.get("soft_criteria", []) and x.get("floor") == 1:
        return False
    if p.get('okrug') and x.get('okrug') and x['okrug'] != p['okrug']:
        return False
    if p.get('center_required'):
        from .pricing import center_distance
        distance = center_distance(x)
        if distance is not None and distance > 5:
            return False
    from .conditions import compile_profile,evaluate
    # Инфраструктуру проверяем после обращения к карте. До обогащения отсутствие
    # nearby не означает отсутствие объекта: иначе exists отсечёт весь пул ещё
    # до вызова провайдера. Итоговые обязательные фильтры проверяет selectable.
    if any(evaluate(c,x)['status'] is False for c in compile_profile(p)
           if c['priority']=='required' and not c['field'].startswith('nearby:')):
        return False
    return True
