"""Симуляция продавца для звонка по реальному объявлению.

Настоящему продавцу прототип не звонит: продавца играет ИИ (или человек в режиме
«продавец я»). Персона детерминирована по id объявления: скрытая минимальная цена,
характер, ответы на вопросы, которых нет в объявлении, и удобное время показа.
Всё, что здесь придумано, в интерфейсе подписано как симуляция.
"""
from __future__ import annotations

import hashlib
import random

NAMES = ["Ильдар Рашитович", "Елена Викторовна", "Марат Ильдусович", "Ольга Сергеевна", "Айдар Ринатович",
         "Татьяна Николаевна", "Руслан Фаритович", "Наталья Андреевна", "Денис Олегович", "Гульнара Ахатовна"]
REASONS = ["переезжаем в другой город", "купили квартиру побольше", "разъезжаемся с родителями",
           "продаём, чтобы закрыть ипотеку на новостройку", "уезжаем ближе к детям"]
STYLES = ["спокойный, держится за цену, ценит быстрый выход на сделку", "дружелюбный, торгуется понемногу",
          "эмоциональный, но уступает за быструю сделку", "деловой, говорит коротко, уступает только на аргументы"]
CONDITIONS = ["косметический ремонт пять лет назад", "свежий евроремонт", "жилое состояние, нужен косметический ремонт",
              "ремонт от застройщика, всё аккуратно"]
AVAILABILITY = ["будни после 18:00 и суббота", "выходные днём", "любой день после 17:00"]


def _rng(listing_id: str) -> random.Random:
    return random.Random(int(hashlib.md5(listing_id.encode()).hexdigest()[:8], 16))


DEV_NAMES = ["Алина, отдел продаж", "Тимур, отдел продаж", "Екатерина, отдел продаж", "Артём, отдел продаж"]
DECORATION = ["предчистовая отделка", "чистовая отделка «под ключ»", "без отделки, свободная планировка"]


def build_new(listing: dict, rnd: random.Random) -> dict:
    """Новостройка: звоним в отдел продаж застройщика — цена жёстче, скидка небольшая."""
    price = listing["price"]
    flex = round(rnd.uniform(0.006, 0.022), 4)
    deadline = listing.get("deadline") or rnd.choice(["2 кв. 2027", "4 кв. 2027", "дом сдан"])
    developer = listing.get("developer") or "застройщик"
    facts = {
        "deadline": f"сдача: {deadline}" + ("" if deadline == "дом сдан" else ", переносов не было"),
        "condition": listing.get("condition") or rnd.choice(DECORATION),
        "contract": "ДДУ по 214-ФЗ, оплата через эскроу в Сбере; при ипотеке у банка-партнёра скидка до 1%",
        "floors": f"в доме {listing['floors']} этажей" if listing.get("floors") else f"в доме {rnd.choice([12, 16, 17, 20, 25])} этажей",
    }
    is_dev = listing.get("seller_role") == "застройщик"
    if not is_dev:  # переуступка: продаёт дольщик (часто через агента), а не застройщик
        facts["contract"] = "переуступка прав по ДДУ, застройщик согласие даёт, ДДУ оплачен полностью"
        flex = round(rnd.uniform(0.015, 0.04), 4)
    return {
        "name": rnd.choice(DEV_NAMES) if is_dev else rnd.choice(NAMES),
        "role": f"отдел продаж, {developer}" if is_dev else listing.get("seller_role") or "собственник",
        "min_price": int(round(price * (1 - flex), -4)),
        "motivation": "план продаж по корпусу" if is_dev else "переуступка: дольщик продаёт до сдачи дома",
        "style": "вежливый, держится за прайс, уступает за ипотеку с банком-партнёром или быстрое бронирование",
        "availability": rnd.choice(["офис продаж ежедневно 10:00–20:00", "будни и суббота, показ квартиры-образца"]),
        "facts": facts,
    }


def build(listing: dict) -> dict:
    rnd = _rng(listing["id"])
    if listing.get("is_new"):
        return build_new(listing, rnd)
    price = listing["price"]
    days = listing.get("days_on_market") or 30
    # чем дольше висит объявление, тем больше готовность уступить
    flex = min(0.065, 0.025 + days / 3000 + rnd.uniform(0, 0.015))
    year = listing.get("year") or rnd.choice([1978, 1986, 1995, 2004, 2009, 2013, 2017])
    owned_since = max(year, rnd.choice([2009, 2012, 2015, 2017, 2019, 2021]))
    facts = {
        "condition": listing.get("condition") or rnd.choice(CONDITIONS),
        "year": f"дом {year} года",
        "kitchen": f"кухня {listing['kitchen']:g} м²" if listing.get("kitchen") else f"кухня около {round(listing['area'] * 0.17)} м²",
        "floors": f"в доме {listing['floors']} этажей" if listing.get("floors") else f"в доме {rnd.choice([5, 9, 10, 12, 16])} этажей",
        "owners": rnd.choice([f"один собственник, в собственности с {owned_since} года",
                              f"двое собственников — я и супруг(а), с {owned_since} года"]),
        "residents": "прописанных нет" if rnd.random() < 0.6 else "прописан один человек, выпишется до сделки",
        "reason": rnd.choice(REASONS),
    }
    return {
        "name": rnd.choice(NAMES),
        "role": listing.get("seller_role") or "собственник",
        "min_price": int(round(price * (1 - flex), -4)),
        "motivation": facts["reason"],
        "style": rnd.choice(STYLES),
        "availability": rnd.choice(AVAILABILITY),
        "facts": facts,
    }
