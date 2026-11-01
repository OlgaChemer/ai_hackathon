"""Проверка по объявлению, просмотр и план сделки по датам."""
from __future__ import annotations

from datetime import date, timedelta

from ..sources import description_flags
from .mortgage import DEMO_RATE, annuity, deal_costs
from .numbers import fmt


def legal_check(listing: dict) -> dict:
    """Что можно сказать по данным объявления. Выписки ЕГРН/ФССП в прототипе не запрашиваем."""
    checks = []

    def add(source, title, status, note):
        checks.append({"source": source, "title": title, "status": status, "note": note})

    rr = listing.get("rosreestr_checked")
    add(listing.get("source_name", "Площадка"), "Проверка площадкой",
        "ok" if rr else "info", "собственник проверен в Росреестре" if rr else "площадка собственника не проверяла")
    if listing.get("is_apartments"):
        add("Объявление", "Апартаменты", "warn", "не жильё: нельзя прописаться, ипотека сложнее")
    if listing.get("is_auction"):
        add("Объявление", "Продажа с торгов", "warn", "особый порядок сделки — нужен юрист")
    for note in description_flags(listing.get("description")):
        add("Описание", "Обратить внимание", "warn", note)
    add("ЕГРН, ФССП, ЕФРСБ", "Выписки по объекту и продавцу", "pending",
        "запросим после выбора объекта (в прототипе не подключено)")
    status = "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"listing_id": listing["id"], "status": status, "checks": checks}


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def human_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def client_slots(today: date | None = None) -> list[str]:
    """Удобное клиенту время: ближайший будний вечер и ближайшая суббота."""
    today = today or date.today()
    weekday = next(today + timedelta(days=i) for i in range(1, 8) if (today + timedelta(days=i)).weekday() < 5)
    saturday = next(today + timedelta(days=i) for i in range(1, 8) if (today + timedelta(days=i)).weekday() == 5)
    return [f"{human_date(weekday)}, 18:30", f"{human_date(saturday)}, 11:00"]


def viewing_checklist(listing: dict) -> list[str]:
    if listing.get("is_new"):
        items = [
            "Сверить квартиру с планировкой из ДДУ: площадь, окна, высота потолков",
            "Посмотреть шоурум или готовые квартиры застройщика — качество отделки",
            "Проверить двор, парковку, школу и сад рядом — что уже построено, а что в планах",
            "Уточнить, как будут принимать квартиру: акт осмотра и устранение недостатков",
        ]
        if not listing.get("finished"):
            items.insert(0, "Посмотреть стройку: идут ли работы, совпадает ли этаж с графиком")
        return items
    items = [
        "Сверить паспорт продавца с выпиской ЕГРН",
        "Проверить окна, батареи и стояки",
        "Посмотреть потолки и стены на следы протечек",
        "Сверить планировку с техпланом",
        "Узнать, кто прописан и когда выпишутся",
    ]
    if listing.get("condition") in (None, "требует ремонта", "без отделки"):
        items.insert(1, "Оценить объём ремонта — от него зависит итоговая цена")
    if listing.get("floor") == 1:
        items.insert(1, "Первый этаж: проверить сырость и запах из подвала")
    for note in description_flags(listing.get("description")):
        items.append(note)
    return items


def deal_plan(listing: dict, final_price: int, down_payment: int | None, today: date | None = None,
              rate: float | None = None, program: str | None = None) -> dict:
    """План сделки по датам. down_payment=None — без ипотеки. rate и program — выбранная ипотечная программа.
    Новостройка — ДДУ и эскроу; переуступка — уступка прав по ДДУ через безопасные расчёты."""
    today = today or date.today()
    is_new = bool(listing.get("is_new"))
    assignment = is_new and "уступк" in (listing.get("sale_type") or "")
    cash = down_payment is None
    loan = 0 if cash else max(0, final_price - down_payment)
    years = 0 if cash else 30
    rate = None if cash else (DEMO_RATE if rate is None else rate)
    program = None if cash else (program or "Рыночная ипотека Сбера")
    credit = None if cash else f"{program}: кредит {fmt(loan)} на {years} лет под {rate * 100:.1f}%".replace(".", ",")

    if is_new and not assignment:
        steps = [
            (0, "Цена согласована с застройщиком", "Итог звонка и запись разговора — в журнале сделки"),
            (1, "Проверка застройщика и дома", "ЕИСЖС: разрешение, проектная декларация, эскроу, сроки"),
            *([] if cash else [(2, "Заявка на ипотеку по объекту", credit),
                               (5, "Страхование", "Жизнь заёмщика — СберСтрахование; имущество — после сдачи дома")]),
            (7, "Подписание ДДУ", "Электронная подпись, договор участия в долевом строительстве"),
            (7, "Эскроу-счёт", "Деньги лежат в банке до сдачи дома, застройщик получит их после ввода"),
            (14, "Регистрация ДДУ", "Росреестр электронно — без визитов"),
        ]
        costs = {"Электронная регистрация ДДУ": 2_000}
    elif assignment:
        steps = [
            (0, "Цена согласована с продавцом", "Итог звонка и запись разговора — в журнале сделки"),
            (1, "Проверка ДДУ и цепочки", "Оплачен ли ДДУ, согласие застройщика, ЕИСЖС: дом и сроки"),
            *([] if cash else [(2, "Заявка на ипотеку по объекту", credit),
                               (5, "Страхование", "Жизнь заёмщика — СберСтрахование; имущество — после сдачи дома")]),
            (7, "Подписание договора уступки", "Электронная подпись, уступка прав по ДДУ"),
            (7, "Безопасные расчёты", "Деньги уходят продавцу только после регистрации уступки"),
            (14, "Регистрация уступки", "Росреестр электронно — без визитов"),
        ]
        costs = {"Сервис безопасных расчётов": 6_900, "Электронная регистрация уступки": 2_000}
    elif cash:
        steps = [
            (0, "Цена согласована", "Итог звонка и запись разговора — в журнале сделки"),
            (2, "Выписки ЕГРН и проверка продавца", "Собственники, обременения, долги, банкротство"),
            (3, "Титульное страхование", "По желанию — защита от оспаривания сделки"),
            (5, "Подписание договора", "Электронная подпись, договор купли-продажи в Домклик"),
            (5, "Безопасные расчёты", "Деньги уходят продавцу только после регистрации"),
            (9, "Электронная регистрация", "Росреестр без визитов в МФЦ"),
        ]
        costs = {"Сервис безопасных расчётов": 6_900, "Электронная регистрация": 2_000}
    else:
        steps = [
            (0, "Цена согласована", "Итог звонка и запись разговора — в журнале сделки"),
            (1, "Заявка на ипотеку по объекту", credit),
            (3, "Оценка залога", "Аккредитованная оценочная компания, онлайн-заказ"),
            (4, "Выписки ЕГРН и проверка продавца", "Собственники, обременения, долги, банкротство"),
            (6, "Страхование", "Имущество, жизнь, титул — СберСтрахование"),
            (8, "Подписание договора", "Электронная подпись, договор купли-продажи в Домклик"),
            (8, "Безопасные расчёты", "Деньги уходят продавцу только после регистрации"),
            (12, "Электронная регистрация", "Росреестр без визитов в МФЦ"),
        ]
        costs = deal_costs(final_price, loan)
    if is_new and not cash:
        costs = {"Страхование жизни, 1-й год": int(round(loan * 0.004, -2)), **costs}
    pay = 0 if cash else annuity(loan, rate, years)
    pay_before = 0 if cash else annuity(max(0, listing["price"] - down_payment), rate, years)
    return {
        "cash": cash, "new": is_new, "assignment": assignment,
        "loan": loan, "down_payment": final_price if cash else down_payment, "years": years,
        "rate": rate, "program": program,
        "monthly": int(round(pay, -2)), "monthly_saved": int(round(pay_before - pay, -1)),
        "costs": costs,
        "timeline": [{"date": (today + timedelta(days=d)).isoformat(), "title": t, "note": n} for d, t, n in steps],
    }
