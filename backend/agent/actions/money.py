"""Действия шага «Финансы»: как платит клиент, семья, ипотечные программы, план накоплений, доступный бюджет."""
from __future__ import annotations

from .. import finance as F
from .. import mortgage as M
from ..workflow import programs_current
from ..numbers import fmt
from ..profile import profile_summary
from . import ActionError, action
from .request import emit_profile


def _need_budget(s) -> int:
    if not s.profile.get("budget_max"):
        raise ActionError("Сначала нужен бюджет: спроси клиента или посчитай affordable_budget.")
    return s.profile["budget_max"]


def _set(s, **fields) -> None:
    s.profile.update(fields)
    if s.profile.get("budget_max") and s.profile.get("city") and s.profile.get("rooms") is not None:
        s.profile["summary"] = profile_summary(s.profile)
    emit_profile(s)


def _finance_panel(s) -> None:
    """Экран «Финансы»: наличные — без ипотеки; иначе программы, сценарии и (если есть) план накоплений."""
    p = s.profile
    budget = p["budget_max"]
    if p.get("payment") == "cash":
        s.push("result", step=2, kind="finance", data={"mode": "cash", "price": budget, "savings_preview": F.cash_preview(budget)})
        return
    progs = s.mortgage
    chosen = next((c for c in progs["programs"] if c["key"] == progs["chosen"]), None) if progs else None
    m_kw = ({"rate": progs["rate"] if progs["chosen"] != "market" else None, "program": chosen["title"],
             "matcap": (progs["matcap"] or {}).get("amount", 0)} if progs else {})
    fin = s.finance
    mortgage = M.mortgage_scenarios(budget, fin["inputs"]["monthly_income"] if fin else None,
                                    fin["available"] if fin else None, **m_kw)
    data = {"mode": "saving" if fin else "mortgage", "mortgage": mortgage, "programs": progs}
    if fin:
        data["finance"] = fin
    s.push("result", step=2, kind="finance", data=data)


@action("ask_payment", 2, "Карточка: как клиент покупает — наличными, ипотека и взнос есть, или ипотека, а взнос ещё копит.")
async def ask_payment(s) -> dict:
    r = await s.ask("payment", "Как планируете покупать?",
                    "От этого зависит, что посчитать: ипотеку, план накопления на взнос — или ничего.")
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    _set(s, payment=r["data"].get("payment", "mortgage_ready"))
    return {"payment": s.profile["payment"]}


@action("ask_family", 2,
        "Карточка «Семья и маткапитал»: сколько детей, возраст младшего, есть ли маткапитал. От этого зависит семейная ипотека. "
        "Не вызывай, если клиент это уже сказал.")
async def ask_family(s) -> dict:
    known = M.normalize_family(s.profile)
    r = await s.ask(
        "family", "Семья и маткапитал",
        "От этого зависит расчёт семейной ипотеки и первоначального взноса с маткапиталом. Итоговые условия подтверждает банк.",
        defaults={"children": known["children"] if known["children"] is not None else 1,
                  "youngest_age": known["youngest_age"] if known["youngest_age"] is not None else "",
                  "matcap": known["matcap"] or (M.MATCAP_FIRST if s.profile.get("matcap") else 0),
                  "matcap_default": M.MATCAP_FIRST},
    )
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    fam = M.normalize_family(r["data"]) if r["approved"] else {"children": 0, "youngest_age": None, "matcap": 0}
    _set(s, children=fam["children"], youngest_age=fam["youngest_age"], matcap=fam["matcap"] or False)
    return {"children": fam["children"], "youngest_age": fam["youngest_age"], "matcap": fam["matcap"]}


@action("ask_deposit", 2,
        "Карточка «Первоначальный взнос»: сколько наличных у клиента. Спрашивается после семьи и маткапитала, "
        "до расчёта ипотеки: от суммы зависит ставка, платёж и подходящие программы.")
async def ask_deposit(s) -> dict:
    p = s.profile
    if p.get("payment") == "cash":
        return {"deposit": None, "note": "покупка без ипотеки — взнос не нужен"}
    if p.get("deposit") is not None and s.requested_action != 'ask_deposit':
        return {"deposit": p["deposit"], "already_known": True}
    budget = _need_budget(s)
    share = M.DEFAULT_DOWN_SHARE
    need = int(round(budget * share, -3))
    r = await s.ask(
        "deposit", "Первоначальный взнос",
        f"Сколько наличных есть на первоначальный взнос? Для квартиры за {fmt(budget)} банк обычно просит "
        f"{int(share * 100)}% — это {fmt(need)}; маткапитал считаю отдельно.",
        payload={"budget_max": budget, "need": need, "share": share},
    )
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    if not r["approved"] or r["data"].get("deposit") is None:
        return {"skipped": True, "note": "сумма взноса неизвестна — посчитай ипотеку по 20% и скажи это допущение"}
    value = max(0, int(r["data"]["deposit"]))
    p["deposit"] = value
    emit_profile(s)
    return {"deposit": value, "share": round(value / budget, 3) if budget else None}


@action("ask_housing", 2,
        "Карточка «Где искать»: новостройки, вторичка или и там, и там. Особенно важно, если семейная ипотека проходит "
        "только в новостройке (mortgage_programs скажет). Не вызывай, если клиент уже сказал.")
async def ask_housing(s) -> dict:
    if s.profile.get("housing") and s.requested_action!='ask_housing':
        return {"housing": s.profile["housing"], "already_known": True}
    alt = next((c for c in (s.mortgage or {}).get("programs", []) if c["key"] == "family_new"), None)
    if alt:
        market = next(c for c in s.mortgage["programs"] if c["key"] == "market")
        cap = M.max_family_price(s.profile.get("city"), s.profile)
        limit = f" для квартир до {fmt(cap)} при взносе 20%" if cap and cap < (s.profile.get("budget_max") or 0) else ""
        text = (f"Семейная ипотека работает в новостройках{limit}: ставка {alt['rate'] * 100:.0f}%, платёж около "
                f"{fmt(alt['monthly'])} вместо {fmt(market['monthly'])} на вторичке. Искать и там, и там?")
    else:
        text = "Где искать: в новостройках, на вторичном рынке или и там, и там?"
    r = await s.ask("housing", "Где искать", text)
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    _set(s, housing=r["data"].get("housing", "secondary"))
    s.search = None  # рынок другой — искать заново
    return {"housing": s.profile["housing"]}


def remember_programs(s, result):
    s.programs_key, s.programs_result = F.programs_key(s.profile), result
    if s.profile.get('payment')=='cash':
        s.profile['_programs_cache'] = {'key':list(s.programs_key),'result':result}
    else:
        s.profile.pop('_programs_cache',None)
    return result


@action("mortgage_programs", 2,
        "Посчитать ипотечные программы для бюджета: рыночная и семейная по демо-политике расчёта, маткапитал во взнос. "
        "Учитывает семью из профиля (если неизвестна — семейная будет «нужно уточнить»). Показывает клиенту экран программ. "
        "Для покупки наличными — показывает, что ипотека не нужна.")
async def mortgage_programs(s) -> dict:
    if programs_current(s):
        return {**s.programs_result, "already_calculated": True}
    budget = _need_budget(s)
    p = s.profile
    if not p.get("payment"):
        raise ActionError("Неизвестно, как клиент платит — ask_payment.")
    if p["payment"] == "cash":
        s.mortgage = None
        _finance_panel(s)
        await s.say("Покупка без ипотеки: деньги уйдут продавцу через сервис безопасных расчётов только после "
                    "регистрации права. Пока ищем, сумму можно держать на накопительном счёте.")
        return remember_programs(s, {"mode": "cash"})
    if p.get("children") is None:
        raise ActionError("Семья неизвестна, а от неё зависит семейная ипотека — сначала ask_family.")
    if p.get("deposit") is None:
        raise ActionError("Неизвестно, сколько наличных на первоначальный взнос — сначала ask_deposit.")
    down_share = max(0.05, min(0.9, (p.get("deposit") or 0) / budget)) if budget else M.DEFAULT_DOWN_SHARE
    progs = M.programs(budget, p.get("city"), p, down_share=down_share, housing=p.get("housing"),
                       is_secondary=p.get("housing") != "new")
    s.mortgage = progs
    _finance_panel(s)
    need = int(round(budget * M.DEFAULT_DOWN_SHARE, -3))
    if p.get("deposit") and p["deposit"] < need:
        await s.say(f"Ваших {fmt(p['deposit'])} хватает на взнос {int(down_share * 100)}% — это меньше обычных "
                    f"{int(M.DEFAULT_DOWN_SHARE * 100)}% ({fmt(need)}): ставка будет выше. Не хватает {fmt(need - p['deposit'])} — "
                    "могу посчитать план накопления.")
    await s.say(progs["explanation"])
    by = {c["key"]: c for c in progs["programs"]}
    out = {"chosen": by[progs["chosen"]]["title"], "rate": progs["rate"], "monthly": by[progs["chosen"]]["monthly"],
           "family_status": by["family"]["status"], "family_known": M.normalize_family(p)["children"] is not None}
    if "family_new" in by:
        out["family_in_new_build"] = {"rate": by["family_new"]["rate"], "monthly": by["family_new"]["monthly"],
                                      "down_payment": by["family_new"]["down_payment"]}
    if progs["matcap"]:
        out["matcap_to_down"] = progs["matcap"]["amount"]
    return remember_programs(s, out)


@action("savings_plan", 2,
        "Карточка с доходами, расходами и накоплениями клиента (в демо — пример из СберБанк Онлайн, клиент правит) -> "
        "план накопления первоначального взноса на продуктах Сбера. Для клиента, который ещё копит на взнос.")
async def savings_plan(s) -> dict:
    budget = _need_budget(s)
    r = await s.ask("finance", "Финансовая подготовка",
                    "Подтяну доходы, расходы и накопления из СберБанк Онлайн (в демо — пример, можно поправить) "
                    "и посчитаю, как собрать первоначальный взнос.", defaults=F.DEFAULT_INPUTS, price=budget)
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    matcap = ((s.mortgage or {}).get("matcap") or {}).get("amount", 0)
    fin = F.plan(budget, r["data"] if r["approved"] else {}, matcap=matcap)
    s.finance = fin
    _finance_panel(s)
    rec = ", ".join(f"{a['name'].lower()} — {fmt(a['amount_now'])}" for a in fin["allocation"] if a["amount_now"])
    await s.say(f"{fin['verdict']} Рекомендую разложить накопления: {rec}." + (f" {fin['risk_note']}" if fin["risk_note"] else ""))
    return {"need": fin["need"], "available": fin["available"], "gap": fin["gap"], "monthly_saving": fin["monthly"],
            "months_to_goal": fin["months"], "fits_horizon": fin["fits_horizon"],
            "horizon_months": fin["inputs"]["horizon_months"]}


@action("affordable_budget", 2,
        "По ЯВНОЙ просьбе рассчитать личную платёжеспособность: карточка с доходами, расходами и накоплениями -> на какую квартиру хватит с ипотекой. "
        "Это НЕ средняя цена квартир; средние/типичные рыночные цены получает market_prices. "
        "(платёж не больше половины дохода, взнос 20% из накоплений к сроку покупки). Бюджет не записывает — "
        "предложи его клиенту и запиши через update_request.")
async def affordable_budget(s) -> dict:
    r = await s.ask("finance", "Сколько вы можете себе позволить",
                    "Посчитаю бюджет по доходам, расходам и накоплениям (в демо — пример из СберБанк Онлайн, можно поправить): "
                    "платёж по ипотеке — не больше половины дохода, первый взнос 20% — из накоплений к сроку покупки.",
                    defaults=F.DEFAULT_INPUTS, price=None)
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    aff = F.affordable(r["data"] if r["approved"] else {}, M.DEMO_RATE)
    out = {"budget": aff["budget"], "limited_by": "платёж" if aff["limited_by"] == "payment" else "первый взнос",
           "payment_limit": aff["payment_limit"], "by_payment": aff["by_payment"], "by_down_payment": aff["by_down"],
           "horizon_months": aff["horizon_months"]}
    if s.market and aff["budget"] < s.market["p25"]:
        out["note"] = f"ниже большинства предложений: такие квартиры обычно от {fmt(s.market['p25'])}"
    return out
