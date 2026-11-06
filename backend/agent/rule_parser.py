"""Разбор простого сообщения клиента кодом, без модели.

«Двушка в Казани до 9 млн, наличными», «хочу квартиру в Москве», «в Хамовниках, вторичка» — такие сообщения
разбираются здесь, и модели не нужно делать два запроса (тип сообщения и извлечение условий).

Принцип — «уверен или отдай модели». Разбор принимает сообщение, только если:
  * каждое слово либо распознано (город, район, комнаты, бюджет, рынок, оплата, центр, тип района),
    либо служебное («хочу», «ищу», «квартиру», «в», «до»…);
  * нет отрицаний и исправлений («не», «кроме», «а нет», «передумал», «лучше», «вместо»…), вопроса,
    упоминаний места не про квартиру («работаю в», «живу в», «как в», «из»), команд и вопросов о выдаче;
  * места однозначны: один город (или «X или Y»), районы — из одного города и согласны с городом профиля;
  * нет открытой карточки, на которую сообщение может быть ответом.
Иначе — None, и сообщение разбирает модель, как раньше.

Слова приводятся к начальной форме pymorphy3 (все варианты разбора), названия ищутся в справочнике
backend.geodata (города, районы, неофициальные районы из OpenStreetMap и синонимы).
"""
from __future__ import annotations

import re
from functools import lru_cache
from itertools import product

from .. import geodata
from ..sources import SUPPORTED_CITIES
from .numbers import parse_money

MAX_TOKENS = 30

# Слова, которые ничего не меняют в условиях
FILLER = set("""хочу хотим хотел хотела хотелось ищу ищем ищет искать нужна нужен нужно нужны надо купить куплю купим
покупаю покупаем подобрать подберите подбери найди найдите найти квартиру квартира квартиры квартир квартирку жилье жилья
в во на у около рядом с со до за и пожалуйста мне нам привет здравствуйте добрый день вечер утро район районе районы
районах районов город городе бюджет бюджетом рублей руб р млн миллионов миллиона миллион тыс тысяч комнат комнаты
комнатах комнатная комнатную рынок рынке рынка можно вариант варианты интересует интересуют посмотреть
""".split())
# Слова, после которых решение принимает модель
STOP = set("""не нет кроме а но передумал передумала передумали лучше вместо точнее хотя или либо как работаю работаем
работает живу живем живём живут учусь учимся родители переезжаем переезжаю переехать из если сколько почему что какой
какая какие какую где когда звони позвони позвонить звонить покажи покажите показать еще ещё больше меньше дешевле
дороже первый первую второй вторую третий третью этот эту эта эти тот ту та те самый самые самую сравни сравнить
""".split())
LOCATIVE = {"в", "во", "на", "у", "около", "возле", "рядом", "район", "районе", "микрорайон", "микрорайоне", "мкр"}

ROOM_WORDS = {
    "студия": 0, "студию": 0, "студии": 0,
    "однушка": 1, "однушку": 1, "однушки": 1, "однокомнатная": 1, "однокомнатную": 1, "однокомнатной": 1,
    "двушка": 2, "двушку": 2, "двушки": 2, "двухкомнатная": 2, "двухкомнатную": 2, "двухкомнатной": 2,
    "трешка": 3, "трешку": 3, "трешки": 3, "трехкомнатная": 3, "трехкомнатную": 3, "трехкомнатной": 3,
    "четырехкомнатная": 4, "четырехкомнатную": 4, "пятикомнатная": 5, "пятикомнатную": 5,
}
NUM_WORDS = {"одна": 1, "одну": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5}

DISTRICT_TYPES = {  # тип района -> регулярное выражение по нормализованному тексту
    "sleeping": r"спальн\w*\s+район\w*",
    "quiet": r"тих\w*\s+(?:район\w*|мест\w*)",
    "prestige": r"(?:престижн|элитн)\w*\s+район\w*",
}


@lru_cache(maxsize=1)
def _morph():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=20000)
def lemmas(word: str) -> frozenset[str]:
    """Все начальные формы слова (pymorphy3) плюс само слово: «нижнем» -> {нижний, нижнем}."""
    out = {word}
    if word.isalpha():
        out |= {p.normal_form.replace("ё", "е") for p in _morph().parse(word)}
    return frozenset(out)


@lru_cache(maxsize=1)
def _name_index() -> dict[str, list[tuple[tuple[frozenset[str], ...], dict]]]:
    """Название из справочника -> по первой лемме: [(леммы каждого слова названия, сущность)]."""
    idx: dict[str, list] = {}
    for key in geodata.all_names():
        words = key.split()
        sets = tuple(lemmas(w) for w in words)
        for entity in geodata.lookup(key):
            for first in sets[0]:
                idx.setdefault(first, []).append((sets, entity))
    return idx


def reset() -> None:
    _name_index.cache_clear()


def tokens(text: str) -> list[str]:
    t = text.casefold().replace("ё", "е")
    return re.findall(r"\d+(?:[.,]\d+)?|[a-zа-я]+|\?", t)


def _common_word(word: str) -> bool:
    """Название совпадает с обычным словом («Сокол», «Мирный», «Центральный»): нужен явный контекст."""
    parses = _morph().parse(word)
    top = parses[0]
    return not any(tag in top.tag for tag in ("Geox", "Name", "Surn")) and top.score >= 0.4


def _places(toks: list[str], skip: set[int] = frozenset()) -> tuple[list[tuple[int, int, dict]], set[int]]:
    """Найденные названия: [(начало, конец, сущность)] — самое длинное совпадение; метро — только после «метро».
    skip — слова, уже понятые иначе («в центре» — не микрорайон «Центр»)."""
    idx = _name_index()
    found: list[tuple[int, int, dict]] = []
    used: set[int] = set()
    i = 0
    while i < len(toks):
        if i in skip:
            i += 1
            continue
        best: tuple[int, list[dict]] | None = None
        for first in lemmas(toks[i]):
            for sets, entity in idx.get(first, []):
                n = len(sets)
                if i + n > len(toks) or any(i + k in skip or not (lemmas(toks[i + k]) & sets[k]) for k in range(n)):
                    continue
                if best is None or n > best[0]:
                    best = (n, [entity])
                elif n == best[0] and entity not in best[1]:
                    best[1].append(entity)
        if best:
            n, entities = best
            before = toks[max(0, i - 2):i]
            metro_ctx = any(w in ("метро", "станции", "станция", "м") for w in before)
            if not metro_ctx:
                entities = [e for e in entities if e["type"] != "metro"]
            else:
                entities = [e for e in entities if e["type"] == "metro"] or entities
            if n == 1 and entities and _common_word(toks[i]) and not any(w in LOCATIVE for w in before):
                entities = []  # «советский» без «в/на/район» — не место
            if entities:
                found.append((i, i + n, {"options": entities}))
                used |= set(range(i, i + n))
                i += n
                continue
        i += 1
    return found, used


def _rooms(text: str, toks: list[str], used: set[int]) -> list[int] | None:
    rooms: set[int] = set()
    for k, w in enumerate(toks):
        if w in ROOM_WORDS:
            rooms.add(ROOM_WORDS[w])
            used.add(k)
    # «2-комнатная», «2 к», «2х комнатную», «1-2 комнаты», «две комнаты»
    for m in re.finditer(r"(?<![\d.,])([1-5])(?:\s*(?:-|или|–)\s*([1-5]))?\s*(?:-?\s*х)?\s*(?:-?\s*(?:комн\w*|к\b|кк\b))", text):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        rooms |= set(range(min(a, b), max(a, b) + 1))
        _mark(text, m.span(), toks, used)
    for m in re.finditer(r"\b(одна|одну|две|два|три|четыре|пять)\s+комнат\w*", text):
        rooms.add(NUM_WORDS[m.group(1)])
        _mark(text, m.span(), toks, used)
    if re.search(r"\b(?:или|либо)\b", text):
        # «однушку или двушку», «1 или 2 комнаты»: «или» между комнатностями — допустимо
        for m in re.finditer(r"(\w+)\s+(?:или|либо)\s+(\w+)", text):
            if all(_room_word(x) for x in (m.group(1), m.group(2))):
                _mark(text, m.span(), toks, used)
    return sorted(rooms) or None


def _room_word(w: str) -> bool:
    return w in ROOM_WORDS or bool(re.fullmatch(r"[1-5]", w))


def _mark(text: str, span: tuple[int, int], toks: list[str], used: set[int]) -> None:
    """Пометить токены, попавшие в отрезок текста (по порядку слов)."""
    before = len(tokens(text[:span[0]]))
    inside_n = len(tokens(text[span[0]:span[1]]))
    used |= set(range(before, before + inside_n))


def _budget(text: str, toks: list[str], used: set[int]) -> int | None:
    m = re.search(r"\b(?:до|за|не дороже|в пределах|бюджет(?:ом)?|максимум|не больше)\s+(\d+(?:[.,]\d+)?(?:\s\d{3})*)\s*"
                  r"(млн\w*|миллион\w*|лям\w*|тыс\w*|т\.?р|руб\w*|р\b)?", text)
    if not m:
        return None
    unit = m.group(2) or ""
    raw = m.group(1)
    if not unit and len(raw.replace(" ", "")) < 6:
        return None  # «до 9» — без единиц непонятно: отдадим модели
    value = parse_money(m.group(0))
    if not value:
        return None
    _mark(text, m.span(), toks, used)
    return value


PAYMENT = [(r"\bналичн\w*|\bналичк\w*|\bза свои\b|\bбез ипотеки\b|\bсвоими деньгами\b", "cash"),
           (r"\bипотек\w*.{0,30}\b(?:взнос\w* есть|есть (?:первоначальн\w* )?взнос\w*)|\b(?:взнос\w* есть|есть (?:первоначальн\w* )?взнос\w*).{0,30}\bипотек\w*", "mortgage_ready"),
           (r"\bипотек\w*.{0,30}\bкоп\w*|\bкоп\w*.{0,30}\bипотек\w*", "mortgage_saving")]
HOUSING = [(r"\bновостро\w*.{0,20}\bвторич\w*|\bвторич\w*.{0,20}\bновостро\w*|\bлюбой рын\w*", "both"),
           (r"\bвторич\w*(?:\s+рын\w*)?", "secondary"), (r"\bновостро\w*|\bновый дом\w*", "new")]
CENTER = [(r"\bв (?:самом )?центре\b|\bв центр\w*\b", "required"),
          (r"\b(?:поближе|ближе|недалеко от|рядом с|около|не далеко от)\s+(?:к\s+)?центр\w*", "preferred")]


def _first(patterns, text, toks, used):
    for pat, value in patterns:
        if m := re.search(pat, text):
            _mark(text, m.span(), toks, used)
            return value
    return None


def parse(text: str, profile: dict | None = None) -> dict | None:
    """Изменения профиля в формате CHANGE_SCHEMA или None — «не уверен, пусть разбирает модель»."""
    profile = profile or {}
    norm = " ".join(text.casefold().replace("ё", "е").split())
    toks = tokens(norm)
    if not toks or len(toks) > MAX_TOKENS or "?" in toks:
        return None
    used: set[int] = set()
    changes: dict = {}
    # «без ипотеки», «не первый этаж» и т. п. разбираем раньше, чем проверяем стоп-слова
    if pay := _first(PAYMENT, norm, toks, used):
        changes["payment"] = pay
    elif re.search(r"\bипотек", norm):
        return None  # ипотека без ясности со взносом — уточняет модель
    if housing := _first(HOUSING, norm, toks, used):
        changes["housing"] = housing
    if center := _first(CENTER, norm, toks, used):
        changes["prefer_center"] = True
        if center == "required":
            changes["center_required"] = True
    for kind, pat in DISTRICT_TYPES.items():
        if m := re.search(pat, norm):
            if "district_type" in changes:
                return None  # «спальный или престижный» — решит модель
            changes["district_type"] = kind
            _mark(norm, m.span(), toks, used)
    rooms = _rooms(norm, toks, used)
    if rooms is not None:
        changes["rooms_options"] = rooms
    budget = _budget(norm, toks, used)
    if budget is not None:
        changes["budget_max"] = budget
    found, place_used = _places(toks, skip=used)
    used |= place_used
    # «или» между двумя местами одного типа допустимо: «в Москве или в Питере», «в Хамовниках или на Арбате»
    for k, w in enumerate(toks):
        if w in ("или", "либо") and k not in used:
            left = [f for f in found if f[1] <= k]
            right = [f for f in found if f[0] > k]
            if left and right and right[0][0] - k <= 3 and k - left[-1][1] <= 1:
                used.add(k)
    leftover = [w for k, w in enumerate(toks) if k not in used and w not in FILLER]
    if leftover or any(w in STOP for k, w in enumerate(toks) if k not in used):
        return None
    if found:
        resolved = _resolve_places(found, profile)
        if resolved is None:
            return None
        changes.update(resolved)
    changes = {k: v for k, v in changes.items() if v != profile.get(k)}
    return changes or None


def _resolve_places(found: list, profile: dict) -> dict | None:
    """Места -> city/cities/districts/okrug. Неоднозначность или конфликт с профилем — None."""
    cities: list[str] = []
    districts: list[tuple[str, str, str | None]] = []  # (город, район, округ)
    mentioned_cities = {e["city"] for _, _, f in found for e in f["options"] if e["type"] == "city"}
    for _, _, f in found:
        opts = f["options"]
        if any(e["type"] == "city" for e in opts):
            names = {e["city"] for e in opts if e["type"] == "city"}
            if len(names) != 1:
                return None
            cities.append(names.pop())
            continue
        anchor = mentioned_cities or ({profile["city"]} if profile.get("city") else set())
        if anchor:
            opts = [e for e in opts if e["city"] in anchor]
        if not opts or len({e["city"] for e in opts}) != 1:
            return None  # «Советский» есть в десятке городов; город не назван — пусть уточнит модель
        e = opts[0]
        if e["type"] == "metro":
            return None  # станции метро в профиле нет — модель решит, как это учесть
        if e["type"] == "area":
            districts += [(e["city"], d, None) for d in e["districts"]]
        elif e["type"] == "district":
            districts.append((e["city"], e["name"], e.get("okrug")))
        elif e["type"] == "place":
            from ..sources import cian
            if cian.district_ids(e["city"], [e["name"]]):  # микрорайон есть у Циана — ищем точно по нему
                districts.append((e["city"], e["name"], _okrug(e["city"], e.get("district") or "")))
            elif e.get("district"):
                districts.append((e["city"], e["district"], _okrug(e["city"], e["district"])))
            else:
                return None
    cities = list(dict.fromkeys(cities))
    out: dict = {}
    if cities:
        if any(c not in SUPPORTED_CITIES for c in cities) or len(cities) > 2:
            return None
        out["city"] = cities[0]
        out["cities"] = cities[1:]
    if districts:
        d_cities = {c for c, _, _ in districts}
        target = out.get("city") or profile.get("city")
        if len(d_cities) != 1 or (target and target not in d_cities) or len(cities) > 1:
            return None
        city_name = d_cities.pop()
        if city_name not in SUPPORTED_CITIES:
            return None
        if not target and profile.get("city") is None:
            out["city"] = city_name
        names = list(dict.fromkeys(name for _, name, _ in districts))
        out["districts"] = names
        okrugs = {_okrug(city_name, name) for name in names}
        if city_name == "Москва" and len(okrugs) == 1 and None not in okrugs:
            out["okrug"] = okrugs.pop()
    return out


def _okrug(city_name: str, district: str) -> str | None:
    c = geodata.city(city_name) or {}
    return next((d.get("okrug") for d in c.get("districts", []) if d["name"] == district), None)
