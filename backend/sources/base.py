"""Общий формат объявления и ошибки источников.

Любая площадка приводит объявление к одному словарю; агенту неважно, откуда
оно пришло. Поля: id, source, source_name, url, city, district, okrug, address,
rooms, area, kitchen, floor, floors, year, condition, material, price,
days_on_market, metro, photo, description, rosreestr_checked, is_apartments,
is_auction, seller_role, lat, lng. Неизвестные поля — None:
именно их агент уточняет у продавца по телефону.
"""
from __future__ import annotations

import re


class SourceUnavailable(RuntimeError):
    """Площадка не ответила данными (капча, блокировка, сеть, неизвестный город)."""


# Города, для которых знаем код региона площадки (Краснодар — код не проверен).
CITY_ALIASES = [
    (r"москв", "Москва"),
    (r"петербург|питер|спб", "Санкт-Петербург"),
    (r"казан", "Казань"),
    (r"новосиб", "Новосибирск"),
    (r"екатеринбург|екб", "Екатеринбург"),
    (r"нижн\w* новгород", "Нижний Новгород"),
    (r"краснодар", "Краснодар"),
]


def normalize_city(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower().replace("ё", "е")
    for pattern, name in CITY_ALIASES:
        if re.search(pattern, t):
            return name
    return None


# Слова в описании, на которые стоит обратить внимание юристу.
DESCRIPTION_FLAGS = [
    (r"\bдол(я|и|ю|ей)\b|долев", "В описании упомянута доля — нужна проверка собственников"),
    (r"несовершеннолет|детск\w* дол", "Есть доли детей — потребуется разрешение опеки"),
    (r"наследств", "Квартира получена по наследству — рекомендуем титульное страхование"),
    (r"перепланиров", "Упомянута перепланировка — проверить, узаконена ли"),
    (r"обремен|арест", "Упомянуто обременение — снимается до или в момент сделки"),
    (r"переуступ", "Переуступка прав — другой тип сделки"),
    (r"альтернатив", "Альтернативная сделка — сроки дольше"),
]


def description_flags(description: str | None) -> list[str]:
    text = (description or "").lower()
    return [note for pattern, note in DESCRIPTION_FLAGS if re.search(pattern, text)]
