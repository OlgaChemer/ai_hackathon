"""Действия сделки: звонок продавцу (с рамками торга в коде), подтверждение цены, просмотр, план сделки и подписание."""
from __future__ import annotations

from datetime import date,datetime,timezone

from ... import economics, sources
from ...calculation_policy import POLICY, metadata
from .. import criteria as C
from .. import finance as F
from .. import mortgage as M
from .. import negotiation as N
from .. import seller_sim
from ..deal import client_slots, deal_plan, legal_check, viewing_checklist
from ..features import FEATURES, missing_info
from ..numbers import fmt
from ..pricing import bargaining_arguments, fair_price
from . import ActionError, action
from .search import card

ID = {"listing_id": {"type": "string", "description": "id квартиры из подборки, например cian-335448927"}}
CORRECTION_LABELS = {"kitchen": ("кухня", " м²"), "area": ("площадь", " м²"), "floor": ("этаж", ""), "floors": ("этажей в доме", ""),
                     "year": ("год постройки", ""), "rooms": ("комнат", ""), "condition": ("ремонт", "")}


def _listing(s, listing_id: str) -> dict:
    x = next((x for x in (s.search or {}).get("listings", []) if x["id"] == listing_id), None)
    if x is None:
        raise ActionError(f"Квартиры {listing_id} нет в найденных — возьми id из подборки (show_selection).")
    verdict = C.evaluate(s.profile, x)
    if verdict['status'] == 'rejected':
        raise ActionError("Квартира не выполняет обязательные условия: " + ", ".join(verdict['failed']))
    return x


def _meta(s, x: dict) -> dict:
    meta = s.search["meta"]
    if x["id"] not in meta:
        meta[x["id"]] = {"fair": fair_price(x, s.search["comps"]), "score": 0, "pay": None}
    return meta[x["id"]]


async def apply_corrections(s, listing: dict, corrections: dict, persist: bool) -> list[str]:
    """Продавец уточнил параметры: применяем к квартире и, если продавец настоящий, — в базу поверх объявления.
    Слова ИИ-продавца (симуляция) действуют только в этом диалоге."""
    said = []
    for key, value in corrections.items():
        listing.setdefault('fact_sources',{})[key]={'source':'seller_call' if persist else 'seller_simulation',
            'observed_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
        if key in CORRECTION_LABELS:
            label, unit = CORRECTION_LABELS[key]
            was = listing.get(key)
            listing.setdefault("listed", {})[key] = was
            listing[key] = value
            said.append(f"{label} {value:g}{unit}" if isinstance(value, (int, float)) else f"{label}: {value}")
            if was is not None:
                said[-1] += f" (в объявлении — {was:g}{unit})" if isinstance(was, (int, float)) else f" (в объявлении — {was})"
        else:
            listing.setdefault("confirmed", {})[key] = value
            said.append(f"{FEATURES[key][0]} — {'есть' if value else 'нет'}")
    if persist:
        sources.store.correct(listing["id"], corrections)
    await s.say("Продавец уточнил параметры: " + "; ".join(said) + "."
                + (" Запомнил — в подборках у этой квартиры будет так." if persist else ""))
    return said


@action("call_seller", 6,
        "Позвонить продавцу квартиры: сначала проверю, что объявление не снято, и обновлю цену; клиент на карточке выбирает, "
        "кто играет продавца (ИИ, сам клиент в чате или звонок на телефон). Агент уточняет пробелы объявления, торгуется "
        "не выше лимита клиента (код не даст) и договаривается о просмотре. Возвращает итог: цена, что узнал, просмотр.",
        **ID)
async def call_seller(s, listing_id: str) -> dict:
    context=(s.active_operation or {}).get('call_context')
    if s._replaying_operation and context:return await _continue_call(s,context)
    authorization=f'{s.profile_revision}:{s.request_review_seq}:{s.selection_revision}'
    previous=s.calls.get(listing_id) or {}
    if previous.get('authorization')==authorization and previous.get('tool_result'):
        return {**previous['tool_result'],'already_completed':True}
    spent=(s.profile.get('_call_authorizations') or {}).get(listing_id) or {}
    if spent.get('authorization')==authorization:
        raise ActionError('Это разрешение на звонок уже использовано. Сначала проверьте итог или получите новую явную просьбу клиента.')
    if not s.profile.get("payment"):
        raise ActionError("Сначала выясни способ покупки — ask_payment, затем звонок продавцу.")
    if not s.red_lines_ok:
        raise ActionError("Рамки не подтверждены — сначала confirm_red_lines.")
    if not any(card['id'] == listing_id for card in s.shown) and listing_id not in s.prefs.liked:
        raise ActionError("Сначала show_selection: звонить можно только по квартире из текущей подборки.")
    if s.selection_profile_stale:
        raise ActionError('Сначала show_selection: обнови подборку под новые условия. Сохранённые на экране карточки относятся к прежним условиям.')
    if listing_id not in s.call_targets:
        raise ActionError('Клиент ещё не просил звонить по этой квартире. Уточнение фильтра не разрешает звонок; дождись выбора и просьбы звонить.')
    x = _listing(s, listing_id)
    check = legal_check(x)
    s.push("result", step=5, kind="legal", data={"checks": [check], "listings": [card(x, s.search["meta"], s.prefs)]
                                                  if x["id"] in s.search["meta"] else [x]})
    page = await sources.verify(x)  # объявление могли снять или поменять, пока клиент выбирал
    if page["alive"] is False:
        await s.say(f"Объявление {x['address']} сняли с публикации — звонить некуда.")
        return {"called": False, "reason": "объявление снято с публикации"}
    await s.finish_step(5)  # проверка объявления пройдена
    fresh = page.pop("listing", None)
    note = None
    if fresh:
        if fresh["price"] != x["price"]:
            note = f"цена в объявлении изменилась: было {fmt(x['price'])}, стало {fmt(fresh['price'])}"
            await s.say(f"Цена в объявлении изменилась: было {fmt(x['price'])}, стало {fmt(fresh['price'])}. Торгуюсь от новой.")
        x.update(fresh)
        s.search["meta"].pop(x["id"], None)
    verdict = C.evaluate(s.profile, x)
    if verdict['status'] == 'rejected':
        raise ActionError("После обновления квартира не выполняет обязательные условия: " + ", ".join(verdict['failed']))
    fp = _meta(s, x)["fair"]
    authority = min(s.profile["budget_max"], x["price"], fp["high"])
    seller = seller_sim.build(x)
    questions = missing_info(x, 3, s.prefs.features)
    questions += [{'key':f'client_{i}','label':q,'question':q} for i,q in enumerate(s.profile.get('seller_questions') or [])]
    slots = client_slots()
    context={'listing':x,'fair':fp,'authority':authority,'seller':seller,'questions':questions,
             'slots':slots,'page':page,'note':note,'legal':check,'authorization':authorization}
    s.checkpoint_operation('running',call_context=context)
    return await _continue_call(s,context)


async def _continue_call(s,context):
    x=next((x for x in (s.search or {}).get('listings',[]) if x['id']==context['listing']['id']),context['listing'])
    fp,authority,seller,questions,slots,page,note=(context[k] for k in ('fair','authority','seller','questions','slots','page','note'))
    check=context['legal']
    operation=s.active_operation or {}
    if operation.get('external_result'):
        return await _finish_call(s,x,slots,operation['external_result'],note,operation.get('call_mode','auto'))
    r = await s.ask(
        "call", "Звонок продавцу",
        f"Звоню по объекту {x['address']}: уточню {', '.join(q['label'] for q in questions)}, поторгуюсь до "
        f"{fmt(fp['target'])} (без вашего согласия — не выше {fmt(authority)}) и договорюсь о просмотре. "
        "Что ещё уточнить у продавца? Ниже можно изменить список вопросов до звонка.",
        listing=x, fair=fp, authority=authority, arguments=bargaining_arguments(x, fp), legal=check,
        questions=questions, seller_name=seller["name"], seller_role=seller["role"], simulated=True, page=page,
    )
    if not r["approved"]:
        s.call_targets=[i for i in s.call_targets if i!=x['id']]
        return {"called": False, "client_said": r["data"].get("text") or "клиент пропустил эту квартиру"}
    if 'questions' in r['data']:
        questions = [{'key':f'client_{i}','label':q,'question':q} for i,q in enumerate(r['data']['questions'])]
        s.profile['seller_questions'] = list(r['data']['questions'])
        s.remember()
    mode = r["data"].get("mode") if r["data"].get("mode") in N.LIVE_MODES else "auto"
    neg = N.Negotiation(listing=x, seller=seller, fair=fp, authority=authority, questions=questions, slots=slots, mode=mode)
    s.journal = {"listing_id": x["id"], "base_price": x["price"], "fixed_at": date.today().isoformat(), "url": x.get("url")}
    s.current = {"listing": x, "fair": fp}
    s.profile.setdefault('_call_authorizations',{})[x['id']]={'authorization':context['authorization'],'status':'started'}
    s.checkpoint_operation('external_started',call_mode=mode)
    while not s.seller_queue.empty():  # реплики от прошлого звонка не нужны
        s.seller_queue.get_nowait()
    human = (lambda: N.queue_input(s.seller_queue)) if neg.live else None
    end = None
    try:
        async for ev in N.run(s.llm, neg, human_input=human):
            s.push(ev["type"], **{k: v for k, v in ev.items() if k != "type"})
            if ev["type"] == "call_end":
                end = ev
    except N.CallError as exc:
        s.push("call_end", status="failed", price=neg.asking, learned={}, viewing=None, corrections={})
        raise ActionError(f"Звонок прервался: {exc}. Скажи клиенту и предложи повторить.") from exc
    return await _finish_call(s,x,slots,end,note,mode)


async def _finish_call(s,x,slots,end,note,mode):
    neg_live=mode in N.LIVE_MODES
    if end["learned"]:
        await s.say("Узнал у продавца: " + "; ".join(f"{k} — {v}" for k, v in end["learned"].items()) + ".")
    corrected = await apply_corrections(s, x, end["corrections"], persist=neg_live) if end.get("corrections") else []
    s.calls[x["id"]] = {"status": end["status"], "price": end["price"], "viewing": end.get("viewing"),
                        "learned": end["learned"], "slots": slots}
    result={"called": True, "status": "договорились" if end["status"] == "agreed" else "не договорились",
            "price": end["price"], "listing_price": x["price"], "savings": x["price"] - end["price"],
            "within_client_limit": end.get("within_authority",False), "over_budget": end["price"] > s.profile["budget_max"],
            "learned": end["learned"], "viewing": end.get("viewing"), "seller_corrected": corrected,
            "seller_was": "ИИ (симуляция)" if mode == "auto" else "живой человек", **({"note": note} if note else {})}
    authorization=((s.active_operation or {}).get('call_context') or {}).get('authorization')
    s.calls[x['id']].update(authorization=authorization,tool_result=result)
    if x['id'] in s.profile.get('_call_authorizations',{}):s.profile['_call_authorizations'][x['id']]['status']='completed'
    # Разрешение относится к одному звонку. Последующие вопросы/изменение
    # версии профиля не превращают его в согласие на повторный набор.
    s.call_targets=[i for i in s.call_targets if i!=x['id']]
    return result


@action("confirm_price", 6,
        "Карточка: клиент подтверждает цену, о которой агент договорился в звонке (или отказывается). Только после call_seller.",
        **ID)
async def confirm_price(s, listing_id: str) -> dict:
    if not s.red_lines_ok:
        raise ActionError("Рамки изменились — сначала confirm_red_lines и повторный выбор квартиры.")
    call = s.calls.get(listing_id)
    if not call:
        raise ActionError("По этой квартире ещё не звонили — сначала call_seller.")
    x = _listing(s, listing_id)
    price = call["price"]
    over_budget = price > s.profile["budget_max"]
    if call["status"] == "agreed":
        text = f"Продавец согласен на {fmt(price)} (было {fmt(x['price'])}). Подтвердить предложение?"
    else:
        text = f"Продавец остановился на {fmt(price)} — это выше лимита, который вы мне дали. Согласиться всё равно?"
    if over_budget:
        text += f" Внимание: цена выше вашего бюджета {fmt(s.profile['budget_max'])}."
    r = await s.ask("price", "Подтвердите цену", text, price=price, savings=x["price"] - price, base=x["price"],
                    status=call["status"], over_budget=over_budget)
    if not r["approved"]:
        return {"confirmed": False, "client_said": r["data"].get("text")}
    s.agreed[listing_id] = price
    s.journal.update(final_price=price, savings=x["price"] - price)
    await s.finish_step(6)
    return {"confirmed": True, "price": price}


@action("plan_viewing", 7,
        "Просмотр: показать время (из звонка) и чек-лист осмотра, затем карточка «выбираете эту квартиру?». "
        "Только после подтверждённой цены.", **ID)
async def plan_viewing(s, listing_id: str) -> dict:
    if not s.red_lines_ok:
        raise ActionError("Рамки изменились — сначала confirm_red_lines и повторный выбор квартиры.")
    if listing_id not in s.agreed:
        raise ActionError("Цена не подтверждена клиентом — сначала confirm_price.")
    x = _listing(s, listing_id)
    call = s.calls[listing_id]
    slot = call.get("viewing") or call["slots"][0]
    _meta(s, x)  # справедливая цена для карточки
    s.push("result", step=7, kind="viewing", data={
        "listing": card(x, s.search["meta"], s.prefs), "slot": slot,
        "slots": call["slots"], "learned": call["learned"], "checklist": viewing_checklist(x)})
    await s.say(f"Просмотр: {slot}. Подготовил чек-лист осмотра с учётом того, что узнал у продавца.")
    r = await s.ask("object", "Выбор объекта", "После просмотра: выбираете эту квартиру?")
    if not r["approved"]:
        return {"chosen": False, "client_said": r["data"].get("text")}
    s.chosen = listing_id
    await s.finish_step(7)
    return {"chosen": True, "viewing": slot}


@action("prepare_deal", 8,
        "Демонстрационный план покупки по датам, финансирование, затраты и экономия. "
        "Покупатель подтверждает только демо-план; юридическое подписание и банковские операции не выполняются. "
        "Только для квартиры, которую клиент выбрал (plan_viewing).", **ID)
async def prepare_deal(s, listing_id: str) -> dict:
    if not s.red_lines_ok:
        raise ActionError("Рамки изменились — сначала confirm_red_lines и повторный выбор квартиры.")
    if s.chosen != listing_id:
        raise ActionError("Клиент ещё не выбрал эту квартиру — сначала plan_viewing.")
    x = _listing(s, listing_id)
    price, p = s.agreed[listing_id], s.profile
    down = None if p.get("payment") == "cash" else (s.finance["down_payment"] if s.finance else int(p["budget_max"] * POLICY['mortgage']['default_down_share']))
    pay = M.listing_payment(x, p, price=price, down_share=down / price) if down else None
    plan = deal_plan(x, price, down, rate=pay["rate"] if pay else None, program=pay["title"] if pay else None)
    s.push("result", step=8, kind="deal", data=plan)
    contract = ("договор уступки прав по ДДУ" if plan["assignment"]
                else "договор участия в долевом строительстве (ДДУ)" if plan["new"] else "договор купли-продажи")
    if plan["cash"]:
        await s.say("Собрал план сделки без ипотеки: проверка продавца, договор с электронной подписью, "
                    "деньги через сервис безопасных расчётов, электронная регистрация.")
    else:
        await s.say(f"Собрал план сделки: {plan['program'].lower()} {fmt(plan['loan'])}, платёж {fmt(plan['monthly'])} в месяц "
                    f"(на {fmt(plan['monthly_saved'])} меньше благодаря торгу).")
    r = await s.ask("sign", "Демонстрационный план покупки", "Подтвердить демонстрационный план покупки? Юридическое подписание и банковские операции здесь не выполняются.")
    if not r["approved"]:
        return {"signed": False, "client_said": r["data"].get("text")}
    await s.finish_step(8)
    savings = x["price"] - price
    econ = economics.unit(economics.Inputs(price=x["price"], savings_pct=max(0, savings) / x["price"],
                                           ltv=plan["loan"] / price))
    fin_lines = F.bank_value(s.finance or {}, s.opened)
    summary = {
        "listing": x, "base_price": x["price"], "final_price": price, "savings": savings,
        "assumptions": metadata(), "fee_rate": POLICY['economics']['fee_pct'],
        "fee": round(max(0, savings) * POLICY['economics']['fee_pct']),
        "net": round(max(0, savings) * (1 - POLICY['economics']['fee_pct'])),
        "monthly_saved": plan["monthly_saved"], "hours_saved": "60–70 ч",
        "sber": {"revenue": econ["revenue"], "contribution": econ["contribution"], "lines": econ["lines"],
                 "finance": fin_lines, "finance_total": sum(v["value"] for v in fin_lines)},
        "next_events": ["Ремонт и обустройство", "Переезд", "Страхование на следующий год", "Рефинансирование"],
    }
    await s.step(9)
    s.push("result", step=9, kind="summary", data=summary)
    await s.finish_step(9)
    s.push("done", status="completed",demo=True,legal_signed=False)
    return {"signed": True,'demo':True,'legal_signed':False, "savings": savings, "agent_fee": summary["fee"], "client_keeps": summary["net"],
            "monthly_payment": plan["monthly"] or None}
