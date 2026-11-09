"""Финансовые продукты Сбера в сделке — по способу оплаты клиента.

Две точки касания плюс развилка для тех, кто копит на взнос:
- до звонков (ипотека, взнос копится): «покупать сейчас» или «сначала накопить» — fork();
  если копить, звонить продавцам незачем;
- после выбора квартиры: одна карточка «Деньги к сделке» — offer();
- в плане сделки: что дал пакет Сбера — package().

Стандартная ставка Сбера на уровне средней по рынку (ДОМ.РФ, 05.11.2026), поэтому выгода
считается от условий пакета (страхование жизни, зарплатная карта, доход на счёте до расчётов),
а не от выдуманной разницы с банками-конкурентами. Цифры и источники — calculation_policy.json,
раздел products.
"""
from __future__ import annotations

from ..calculation_policy import POLICY, metadata
from . import finance as F
from . import mortgage as M
from .numbers import fmt

P = POLICY['products']
YEARS = POLICY['mortgage']['years']
NOTE = ('Стандартная ставка Сбера — на уровне средней по ТОП-20 банков (ДОМ.РФ, 05.11.2026); '
        'выгода — от условий пакета. Демо-расчёт, итоговые условия подтверждает банк.')
INSURANCE_KEYS = ('Страхование (имущество, жизнь, титул), 1-й год', 'Страхование жизни, 1-й год')


def _r(value: float, step: int = 100) -> int:
    return int(round(value / step) * step)


def pct(rate: float) -> str:
    return f'{rate * 100:.2f}'.rstrip('0').rstrip('.').replace('.', ',') + '%'


def parking(amount: int, days: int) -> dict:
    """Деньги до расчётов на накопительном счёте: приветственная ставка действует ограниченный срок."""
    account = P['welcome_account']
    days = max(1, min(days, account['days']))
    return {'name': account['name'], 'rate': account['rate'], 'days': days, 'amount': int(amount),
            'income': _r(amount * account['rate'] * days / 365), 'market_rate': P['market_deposit_rate']}


def settlement_days(cash: bool, is_new: bool) -> int:
    days = P['days_to_settlement']
    return days['new'] if is_new else days['cash'] if cash else days['mortgage']


def rate_for(base: float, preferential: bool, *, life: bool, salary: bool) -> float:
    """Льготная ставка задана государством: страхование жизни для неё обязательно, скидок нет."""
    if preferential:
        return round(base, 4)
    rate = base + (0 if life else P['life_refusal_surcharge']) - (P['salary_discount'] if salary else 0)
    return round(rate, 4)


def variants(loan: int, base: float, preferential: bool, years: int = YEARS) -> dict:
    """Ставка и платёж для каждого сочетания «страхование жизни» × «зарплатная карта» — карточка пересчитывает на месте."""
    out = {}
    for life in (True, False):
        for salary in (False, True):
            rate = rate_for(base, preferential, life=life, salary=salary)
            out[f'{int(life)}{int(salary)}'] = {'rate': rate, 'monthly': _r(M.annuity(loan, rate, years))}
    return out


def _cash_offer(listing: dict, price: int) -> dict:
    is_new = bool(listing.get('is_new'))
    park = parking(price, settlement_days(True, is_new))
    items = [{'key': 'parking', 'title': park['name'] + ' до расчётов',
              'why': f"{fmt(price)} под {pct(park['rate'])} ~{park['days']} дн., пока идёт сделка; снять можно в любой момент",
              'value': park['income'], 'unit': 'доход', 'on': True}]
    if not is_new:
        items.append({'key': 'title', 'title': 'Титульное страхование',
                      'why': 'защита, если сделку оспорят прежние владельцы или наследники',
                      'value': _r(price * P['title_insurance_rate']), 'unit': '₽ в год', 'on': False})
    return {'chain': 'cash', 'price': price, 'items': items, 'parking': park}


def offer(listing: dict, price: int, profile: dict) -> dict | None:
    """Пакет к выбранной квартире: 1–3 позиции под способ оплаты. None — для ипотеки ещё неизвестен взнос."""
    base = {'assumptions': metadata(), 'listing_id': listing['id'], 'note': NOTE}
    if profile.get('payment') == 'cash':
        return {**base, **_cash_offer(listing, price)}
    pay = M.listing_payment(listing, profile, price=price)
    if pay is None:
        return None
    loan = max(0, price - pay['down'])
    if not loan:  # своих денег хватает на всю цену — ипотека не нужна
        return {**base, **_cash_offer(listing, price)}
    preferential = pay['program'] != 'рыночная'
    table = variants(loan, pay['rate'], preferential)
    life_cost = _r(loan * P['life_insurance_rate'])
    extra = table['00']['monthly'] - table['10']['monthly']
    life = {'key': 'life', 'title': 'Страхование жизни',
            'value': life_cost, 'unit': '₽ в год', 'on': True, 'locked': preferential,
            'why': ('обязательно для льготной ставки программы' if preferential else
                    f"без него ставка выше на {pct(P['life_refusal_surcharge'])[:-1]} п.п. — платёж +{fmt(extra)} в месяц, "
                    f"это {fmt(extra * 12)} в год против полиса за {fmt(life_cost)}")}
    items = [life,
             {'key': 'property', 'title': 'Страхование квартиры в залоге',
              'why': 'обязательно для ипотеки по закону — уже в плане сделки',
              'value': _r(loan * P['property_insurance_rate']), 'unit': '₽ в год', 'on': True, 'locked': True}]
    park = None
    if pay['down'] > 0:
        park = parking(pay['down'], settlement_days(False, bool(listing.get('is_new'))))
        items.append({'key': 'parking', 'title': park['name'] + ' для взноса',
                      'why': f"взнос {fmt(pay['down'])} под {pct(park['rate'])} ~{park['days']} дн. до выдачи кредита",
                      'value': park['income'], 'unit': 'доход', 'on': True})
    return {**base, 'chain': 'mortgage', 'price': price, 'program': pay['title'], 'preferential': preferential,
            'loan': loan, 'down': pay['down'], 'years': YEARS, 'base_rate': pay['rate'], 'variants': table,
            'salary_available': not preferential, 'salary_discount': P['salary_discount'],
            'items': items, 'parking': park, 'life_cost': life_cost}


def choose(o: dict, approved: bool, data: dict) -> dict:
    """Решение клиента по карточке. «Не нужно» снимает всё необязательное."""
    picked = {item['key']: bool(item.get('locked') or (approved and data.get(item['key'], item['on'])))
              for item in o['items']}
    choice = {'listing_id': o['listing_id'], 'chain': o['chain'], 'picked': picked,
              'salary': bool(approved and o.get('salary_available') and data.get('salary'))}
    if o['chain'] == 'mortgage':
        row = o['variants'][f"{int(picked['life'])}{int(choice['salary'])}"]
        choice.update(rate=row['rate'], monthly=row['monthly'])
    return choice


def package(o: dict, choice: dict) -> dict:
    """Итог пакета для плана сделки: строки «что дало» и одна главная цифра."""
    picked, rows = choice['picked'], []
    headline, monthly_saving, term_saving = None, 0, 0
    if o['chain'] == 'mortgage':
        table = o['variants']
        chosen = table[f"{int(picked['life'])}{int(choice['salary'])}"]
        bare = table['00']
        if o['preferential']:
            rows.append({'title': 'Ставка программы', 'text': f"{pct(chosen['rate'])} — {o['program'].lower()}, одинаковая во всех банках"})
        else:
            if picked['life']:
                gain = table['00']['monthly'] - table['10']['monthly']
                rows.append({'title': 'Страхование жизни',
                             'text': f"ставка ниже на {pct(P['life_refusal_surcharge'])[:-1]} п.п.: −{fmt(gain)} в месяц при полисе {fmt(o['life_cost'])} в год"})
            if choice['salary']:
                gain = table[f"{int(picked['life'])}0"]['monthly'] - chosen['monthly']
                rows.append({'title': 'Зарплатная карта Сбера',
                             'text': f"скидка {pct(P['salary_discount'])[:-1]} п.п.: −{fmt(gain)} в месяц"})
            monthly_saving = bare['monthly'] - chosen['monthly']
            term_saving = monthly_saving * 12 * o['years']
            if monthly_saving > 0:
                headline = (f"Условия Сбера снижают платёж на {fmt(monthly_saving)} в месяц — "
                            f"{fmt(term_saving)} за {o['years']} лет")
    park = o.get('parking')
    if park and picked.get('parking'):
        rows.append({'title': park['name'],
                     'text': f"+{fmt(park['income'])} за {park['days']} дн. до расчётов под {pct(park['rate'])} "
                             f"(средняя максимальная ставка вкладов ТОП-10 банков — {pct(park['market_rate'])})"})
        headline = headline or f"Пока идёт сделка, деньги принесут {fmt(park['income'])}"
    if picked.get('title'):
        rows.append({'title': 'Титульное страхование', 'text': 'защита права собственности на случай оспаривания сделки'})
    year_saving = monthly_saving * 12 - (o.get('life_cost', 0) if picked.get('life') and not o.get('preferential') else 0)
    return {'headline': headline, 'rows': rows, 'rate': choice.get('rate'), 'monthly': choice.get('monthly'),
            'monthly_saving': monthly_saving, 'term_saving': term_saving, 'year_saving': year_saving,
            'income': park['income'] if park and picked.get('parking') else 0, 'note': o['note']}


def adjust_costs(costs: dict, o: dict | None, choice: dict | None) -> dict:
    """Расходы на сделку — по выбранным полисам вместо общей строки «страхование»."""
    if not o or not choice:
        return costs
    out = {k: v for k, v in costs.items() if k not in INSURANCE_KEYS}
    by_key = {item['key']: item for item in o['items']}
    names = {'property': 'Страхование квартиры в залоге, 1-й год', 'life': 'Страхование жизни, 1-й год',
             'title': 'Титульное страхование, 1-й год'}
    for key, name in names.items():
        if key in by_key and choice['picked'].get(key):
            out[name] = by_key[key]['value']
    return out


# ------------------------------------------------------------------ развилка до звонков

def _lookup(s, listing_id: str) -> dict | None:
    pool = (s.search or {}).get('listings') or []
    return next((x for x in [*pool, *s.shown] if x.get('id') == listing_id
                 and type(x.get('price')) in (int, float) and x['price'] > 0), None)


def fork_targets(s) -> list[dict]:
    return [x for x in (_lookup(s, lid) for lid in s.call_targets) if x]


def fork(s) -> dict | None:
    """Хватит ли денег на взнос по квартирам, которые клиент просит обзвонить. None — развилка не нужна."""
    if s.profile.get('payment') != 'mortgage_saving' or not s.finance:
        return None
    targets = fork_targets(s)
    if not targets:
        return None
    inputs = s.finance['inputs']
    matcap = s.finance.get('matcap') or 0
    plans = [(x, F.plan(x['price'], inputs, matcap=matcap)) for x in targets]
    if all(plan['gap'] == 0 for _, plan in plans):
        return None
    top, plan = max(plans, key=lambda pair: pair[0]['price'])
    savings, expenses = inputs['savings'], inputs['monthly_expenses']
    usable = max(0, savings - expenses * P['min_cushion_months'])
    need_now = max(0, int(top['price'] * P['min_down_share']) - matcap) + plan['deal_costs']
    now_ok = usable >= need_now
    now = {'ok': now_ok, 'short': max(0, need_now - usable)}
    if now_ok:
        own = max(0, usable - plan['deal_costs'])
        pay = M.listing_payment(top, {**s.profile, 'deposit': own}, price=top['price'])
        now.update(down=pay['down'], rate=pay['rate'], monthly=pay['monthly'], program=pay['title'],
                   cushion_left=max(0, savings - own - plan['deal_costs']), cushion=plan['cushion'])
    gaps = [p['gap'] for _, p in plans]
    deposit = F.PRODUCTS['deposit']
    return {'assumptions': metadata(), 'targets': sorted(x['id'] for x in targets),
            'price_min': min(x['price'] for x in targets), 'price_max': top['price'],
            'gap_min': min(gaps), 'gap_max': max(gaps), 'now': now,
            'save': {'months': plan['months'], 'monthly': plan['monthly'], 'ready_date': plan['ready_date'],
                     'available': plan['available'], 'product': deposit['name'], 'rate': deposit['rate']},
            'recommended': 'now' if now_ok else 'save', 'note': NOTE}


def fork_resolved(s) -> bool:
    """Звонить можно: оплата не «копим», нехватки нет, или клиент выбрал «покупать сейчас» для этих квартир."""
    if s.profile.get('payment') != 'mortgage_saving':
        return True
    chosen = (s.products or {}).get('fork')
    if chosen and chosen.get('choice') == 'now' and set(s.call_targets) <= set(chosen.get('targets') or []):
        return True
    return fork(s) is None
