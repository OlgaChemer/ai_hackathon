"""Деньги и числа: форматирование для людей и разбор сумм из речи («23 600», «9,5 млн»)."""
from __future__ import annotations

import re


def parse_money(text: str) -> int | None:
    """'9 млн' -> 9000000, '8,7 млн' -> 8700000, '8 700 000' -> 8700000, '8700 тыс' -> 8700000."""
    t = text.lower().replace(" ", " ")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:млн|миллион|лям)", t)
    if m:
        value = float(m.group(1).replace(",", "."))
        tail = re.search(r"(?:млн|миллион\w*)\s*(\d{1,3})\s*(?:тыс|тысяч)", t[m.start():])
        extra = int(tail.group(1)) * 1000 if tail else 0
        return int(round(value * 1_000_000)) + extra
    m = re.search(r"(\d[\d ]{2,})\s*(?:тыс|тысяч)", t)
    if m:
        return int(m.group(1).replace(" ", "")) * 1000
    m = re.search(r"\d{1,3}(?:[ .]\d{3}){2,}|\d{6,}", t)
    if m:
        return int(re.sub(r"\D", "", m.group(0)))
    return None


def parse_price_near(text: str, ref: int) -> int | None:
    """Цена в реплике, если она похожа на цену этой квартиры (0,5–1,2 от ref).

    В разговоре «млн» опускают: «давай 23 600», «23,6», «двадцать три шестьсот» (Whisper пишет «23 600»).
    Год, этаж, метры и минуты ценой не считаются.
    """
    lo, hi = 0.5 * ref, 1.2 * ref
    full = parse_money(text)
    if full and lo <= full <= hi:
        return full
    t = text.lower().replace(" ", " ")
    for m in re.finditer(r"(?<![\d.,])\d+(?: \d{3})*(?:[.,]\d+)?(?![\d.,]*\d)(?!\s*(?:год|г\.|этаж|кв|метр|м²|м2|минут|мин|лет|сот|%))", t):
        try:
            v = float(m.group(0).replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        for scale in (1, 1_000, 1_000_000):
            if lo <= v * scale <= hi:
                return int(round(v * scale))
    return None


def fmt(v: int | float) -> str:
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def mln(v: float) -> str:
    return f"{v / 1e6:.1f}".replace(".0", "").replace(".", ",") + " млн ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
