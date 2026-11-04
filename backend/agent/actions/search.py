"""Действия поиска и подбора: объявления с Циана и из базы агента, район, «что рядом» по карте,
пожелания (только подтверждённые), честная проверка «а дороже есть?», подборка с отметками клиента."""
from __future__ import annotations

import time

from ... import geo, sources
from ...config import settings
from .. import criteria as C
from ..criteria import match, selectable
from .. import mortgage as M
from .. import preferences as P
from ..features import FEATURE_WITH, FEATURES, feature_evidence, missing_info
from ..numbers import fmt
from ..pricing import fair_price, score_listing, value_rank
from ..profile import in_districts, passes_hard_filters, rooms_text
from . import ActionError, action
from .. import listing_query as Q

BATCH = 10       # квартир в подборке
MAX_PAGES = 3   # страниц выдачи Циана на один поиск


# ------------------------------------------------------------------ помощники подбора


def wishes_text(prefs: P.Prefs) -> str:
    """«с балконом или лоджией, рядом: школа, вода»."""
    parts = []
    if prefs.features:
        parts.append("с " + ", ".join(FEATURE_WITH[k] for k in prefs.features))
    if prefs.poi:
        parts.append("рядом: " + ", ".join(geo.POI[k][0] for k in prefs.poi))
    return ", ".join(parts)


def fits(x: dict, prefs: P.Prefs) -> bool:
    """Квартира выполняет все пожелания: удобства подтверждены (объявление или продавец), нужное — в 1 км по карте."""
    profile = {**prefs.profile, "features": prefs.features, "poi": prefs.poi}
    return C.evaluate(profile, x)['status'] == 'confirmed'


async def check_map(s, listings: list[dict], announce: bool = False, kinds=None) -> bool:
    """Что рядом с квартирами по карте. False — сервис карты не ответил (агент скажет об этом честно)."""
    kinds = kinds if kinds is not None else list(set(s.prefs.poi) | {
        f['field'].split(':',1)[1] for f in s.profile.get('filters') or [] if f['field'].startswith('nearby:')})
    if not kinds or not listings:
        return True
    if announce:
        await s.say("Проверяю по карте, что рядом: " + ", ".join(geo.POI[k][0] for k in s.prefs.poi) + ".")
    if s.search is not None and s.search.get("map_revision") != s.client_revision:
        s.search["map_revision"] = s.client_revision
        s.search["map_deadline"] = time.monotonic() + settings.places_budget_s
    deadline = (s.search or {}).get("map_deadline", time.monotonic() + settings.places_budget_s)
    # Сначала проверяем кандидатов первой выдачи; массовая проверка всего города
    # съедала бюджет до появления полезной карточки.
    score_into(listings, (s.search or {}).get('comps') or listings, s.profile, (s.search or {}).get('meta', {}))
    meta = (s.search or {}).get('meta', {})
    candidates = sorted(listings,key=lambda x:meta.get(x['id'],{}).get('score',0),reverse=True)
    ok = await geo.nearby(candidates[:30], kinds, budget_s=max(0, deadline - time.monotonic()))
    if not ok and not s.map_warned:
        s.map_warned = True
        await s.say("Карта не успела проверить часть адресов или не ответила. Отмечу пробелы на карточках — близость нужных объектов пока не подтверждена.")
    return ok


def score_into(pool: list[dict], comps: list[dict], profile: dict, meta: dict) -> None:
    """Справедливая цена, оценка и платёж по подходящей программе — для каждой новой квартиры."""
    for x in pool:
        if x["id"] in meta:
            continue
        fp = fair_price(x, comps)
        score = score_listing(x, profile, fp)
        pay = M.listing_payment(x, profile) if profile.get("payment") not in (None, "cash") else None
        if pay and pay["program"] == "семейная":
            score += 6  # платёж заметно ниже — для семьи это важнее пары метров
        meta[x["id"]] = {"fair": fp, "score": score, "pay": pay}


def ranked(pool: list[dict], meta: dict, prefs: P.Prefs) -> list[dict]:
    for x in pool:
        meta[x["id"]]["total"] = round(meta[x["id"]]["score"] + P.adjust(x, prefs) + match(x, prefs)["preference_score"], 1)
        meta[x["id"]]["ranking"] = value_rank(x, prefs.profile, meta[x["id"]]["fair"], match(x, prefs), P.adjust(x, prefs))
    return sorted(pool, key=lambda x: (match(x, prefs)["status"] == "confirmed", meta[x["id"]]["ranking"]["center_preferred"], meta[x["id"]]["ranking"]["value_score"], -x["price"], x["id"]), reverse=True)


def card(x: dict, meta: dict, prefs: P.Prefs) -> dict:
    """Карточка квартиры для экрана."""
    m = meta[x["id"]]
    return {**x, "fair": m["fair"], "score": m.get("total", m["score"]), "pay": m.get("pay"), "match": match(x, prefs), "ranking": m.get("ranking"),
            "features": [{"label": FEATURES[k][0], **feature_evidence(x, k)} for k in prefs.features],
            "nearby": geo.summary(x, prefs.poi),
            "unchecked": [geo.POI[k][0] for k in prefs.poi if k not in (x.get("nearby") or {})],
            "missing": [q["label"] for q in missing_info(x, 4, prefs.features) if q["key"] != "owners"]}


def brief(x: dict, meta: dict) -> dict:
    """Квартира для модели — коротко."""
    fp = meta[x["id"]]["fair"]
    out = {"id": x["id"], "address": x.get("address"), "district": x.get("district"), "rooms": x.get("rooms"),
           "area": x.get("area"), "floor": f"{x.get('floor')}/{x.get('floors')}", "price": x["price"],
           "fair_price": fp["fair"], "overprice_pct": fp["overprice_pct"], "new_build": bool(x.get("is_new"))}
    if meta[x["id"]].get("pay"):
        out["monthly"] = meta[x["id"]]["pay"]["monthly"]
    return out


async def _more_pages(s, until) -> None:
    """Догружает страницы выдачи (до MAX_PAGES), пока until() не станет True."""
    st, profile = s.search, s.profile
    while not until() and st["pages"] < MAX_PAGES:
        st["pages"] += 1
        seen = {x["id"] for x in st["listings"]}
        extra = [x for x in await sources.more(profile, st["pages"]) if x["id"] not in seen]
        if not extra:
            break
        st["listings"] += extra
        fresh = [x for x in extra if passes_hard_filters(x, profile) and (not st["strict"] or in_districts(x, st["named"]))]
        await check_map(s, fresh)
        st["pool"] += fresh
        st["comps"] += extra



# ------------------------------------------------------------------ действия


@action("search_listings", 3,
        "Найти объявления по запросу: Циан (вторичка и/или новостройки) + база агента; названный район; удобства "
        "и «что рядом» — только подтверждённые. Показывает клиенту экран поиска со справедливой ценой. "
        "Подтверждение рамок и ипотека для просмотра объявлений не нужны. Возвращает, сколько нашлось и сколько выполняют пожелания.")
async def search_listings(s) -> dict:
    p = s.profile
    where = ", ".join(dict.fromkeys([p["city"], *(p.get("cities") or [])])) + (f", {p['okrug']}" if p.get("okrug") else "")
    market_txt = {None: "новостройки и вторичка", "new": "новостройки", "both": "новостройки и вторичка"}.get(p.get("housing"), "вторичный рынок")
    room_label = "/".join(map(str, p["rooms_options"])) + "-комнатные" if len(p.get("rooms_options") or []) > 1 else rooms_text(p["rooms"])
    await s.say(f"Ищу на Циане: {where}, {room_label}, {market_txt}, до {fmt(p['budget_max'])}.")
    s.push("search_progress", on=True, text="Ищу объявления и сравниваю варианты…")
    listings, report = await sources.collect(p)
    known = sources.stored(p, exclude={x["id"] for x in listings})
    if known:
        listings += known
        report.append({"source": "База агента", "count": len(known), "status": "db",
                       "note": f"видели на Циане за {sources.store.SEEN_DAYS} дней"})
    pool = [x for x in listings if passes_hard_filters(x, p)]
    comps = list({x["id"]: x for x in [*sources.comparables(p), *listings]}.values())
    st = s.search = {"listings": listings, "pool": pool, "comps": comps, "report": report, "meta": {}, "pages": 1,
                     "named": p.get("districts") or [], "strict": False, "map_ok": True, "shown_ids": set(), "map_deadline": time.monotonic() + settings.places_budget_s, "map_revision": s.client_revision}
    if not pool:
        alternatives = ["Рассмотреть меньше комнат", "Изменить район"]
        if p.get("housing") in ("secondary", "new"):
            alternatives.insert(0, "Посмотреть новостройки" if p["housing"] == "secondary" else "Посмотреть вторичку")
        st["alternatives"] = alternatives
        s.push("result", step=3, kind="search", data={"report": report, "total": len(listings), "matched": 0, "rows": [], "alternatives": alternatives})
        s.push("search_progress", on=False)
        if report and all(r["status"] == "unavailable" for r in report):
            raise ActionError(f"Циан сейчас не отдаёт объявления ({report[0]['note']}), в базе агента подходящих нет.")
        m = s.market
        return {"found": len(listings), "in_budget": 0,
                "market": {"min": m["min"], "typical_from": m["p25"], "typical_to": m["p75"]} if m else None,
                "alternatives": alternatives,
                "hint": "Покажи варианты изменения кнопками ask_client. Целых квартир в бюджете нет (доли и комнаты не показываю) — предложи поднять бюджет, "
                        "меньше комнат или весь город"}
    out: dict = {}
    if p.get("prefer_center"):
        await _more_pages(s, lambda: False)  # ищем все допустимые комнатности, а не только свежую первую страницу
    if st["named"] and not p.get("prefer_center"):  # район, который назвал клиент: догружаем страницы и оставляем только его, если хватает
        await _more_pages(s, lambda: sum(in_districts(x, st["named"]) for x in st["pool"]) >= BATCH)
        inside = [x for x in st["pool"] if in_districts(x, st["named"])]
        s.prefs.districts_plus |= {x["district"] for x in inside}
        if len(inside) >= 3:
            st["pool"], st["strict"] = inside, True
        out["in_named_districts"] = len(inside)
    st["map_ok"] = await check_map(s, st["pool"], announce=True)
    await _more_pages(s, lambda: sum(selectable(x, s.prefs) for x in st["pool"]) >= BATCH)
    good = [x for x in st["pool"] if selectable(x, s.prefs)]
    confirmed = sum(fits(x, s.prefs) for x in good)
    out.update(wishes=wishes_text(s.prefs), fit_wishes=confirmed, map_checked=st["map_ok"],
               confirmed=confirmed, unverified=len(good) - confirmed, rejected=len(st["pool"]) - len(good))
    score_into(st["pool"], st["comps"], p, st["meta"])
    order = ranked(good, st["meta"], s.prefs)
    rows = [{"id": x["id"], "address": x["address"], "district": x.get("district"), "area": x["area"],
             "price": x["price"], **st["meta"][x["id"]]["fair"]} for x in order[:8]]
    for r in report:  # с учётом догруженных страниц; из базы — отдельной строкой
        if r["status"] == "db":
            r["count"] = sum(1 for x in st["listings"] if x.get("stored_at"))
        else:
            r["count"] = sum(1 for x in st["listings"] if not x.get("stored_at") and bool(x.get("is_new")) == r.get("new", False) and x.get("city") == r.get("city", p["city"]) and x.get("rooms") == r.get("rooms", p["rooms"]))
    s.push("result", step=3, kind="search", data={"report": report, "total": len(st["listings"]), "matched": len(good),
                                                   "rows": rows, "store": sources.store.stats(p), **out})
    over = sum(1 for x in good if st["meta"][x["id"]]["fair"]["overprice_pct"] > 1)
    s.push("search_progress", on=False)
    await s.finish_step(3)
    return {"found": len(st["listings"]), "in_budget": len(st["pool"]), "selectable": len(good),
            "overpriced": over, "from_agent_db": len(known), "price_from": min((x["price"] for x in good), default=None),
            "price_to": max((x["price"] for x in good), default=None),
            "sources": [{"source": r["source"], "count": r["count"], "status": r["status"]} for r in report], **out}


@action("probe_budget", 3,
        "Проверить, есть ли квартиры с пожеланиями клиента дороже бюджета (до указанной суммы). Запрос не меняет — "
        "нужно, чтобы честно сказать: «в бюджете нет, есть на X дороже» или «таких нет совсем».",
        budget_max={"type": "integer", "description": "до какой цены смотреть, руб. (обычно бюджет × 1,3)"})
async def probe_budget(s, budget_max: int) -> dict:
    if not s.search:
        raise ActionError("Сначала search_listings.")
    p = s.profile
    richer = {**p, "budget_max": int(budget_max)}
    listings, _ = await sources.collect(richer)
    st = s.search
    above = [x for x in listings if passes_hard_filters(x, richer) and x["price"] > p["budget_max"]
             and (not st["strict"] or in_districts(x, st["named"]))]
    await check_map(s, above)
    unverified = sum(match(x, s.prefs)["status"] == "unverified" for x in above)
    above = sorted((x for x in above if fits(x, s.prefs)), key=lambda x: x["price"])
    return {"checked_up_to": int(budget_max), "found_above_budget": len(above), "unverified_above_budget": unverified,
            "cheapest": above[0]["price"] if above else None,
            "over_budget_by": above[0]["price"] - p["budget_max"] if above else None, "wishes": wishes_text(s.prefs)}


@action("show_selection", 4,
        "Выполнить универсальный запрос к текущему пулу и показать квартиры: фильтры полей, порядок, "
        "scope all/shown/liked, limit1–30, mode rank/more, агрегаты. По умолчанию применяет проверенную "
        "команду пользователя. Срок сдачи delivery_year, год постройки year — разные поля. "
        "Затем ждёт ответ клиента. Новые рекомендации не вытесняются избранным.", query=Q.QUERY_SCHEMA)
async def show_selection(s, query: dict | None = None) -> dict:
    st = s.search
    if not st:
        raise ActionError("Сначала search_listings.")
    effective = (s.selection_query if s.selection_dirty and s.selection_query else query or s.selection_query or Q.default_query())
    try:
        Q.validate_query(effective)
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionError(f'Неверный запрос к подборке: {exc}') from exc
    s.selection_query = dict(effective)
    s.profile['_selection_query'] = dict(effective)
    p, prefs, meta = s.profile, s.prefs, st["meta"]
    poi = set(prefs.poi) | {f['field'].split(':',1)[1] for f in effective['filters'] if f['field'].startswith('nearby:')}
    if any(k not in (x.get('nearby') or {}) for x in st['pool'] for k in poi):
        st['map_ok'] = await check_map(s,st['pool'], kinds=list(poi))
    pool = [x for x in st['pool'] if passes_hard_filters(x,p)
            and x.get('district') not in prefs.districts_minus and selectable(x,prefs)
            and x['id'] not in prefs.disliked]
    # Подгрузка нужна для следующей страницы, но не для ограничения текущих карточек.
    if effective['scope']=='all' and (effective['mode']=='more' or not s.shown):
        unseen=[x for x in pool if x['id'] not in st['shown_ids']]
        if len(unseen)<effective['limit']:
            before=len(st['pool'])
            await _more_pages(s,lambda:len(st['pool'])>before)
            pool += [x for x in st['pool'][before:] if passes_hard_filters(x,p)
                     and selectable(x,prefs) and x['id'] not in prefs.disliked
                     and x.get('district') not in prefs.districts_minus]
    score_into(pool,st['comps'],p,meta)
    order=ranked(pool,meta,prefs)
    data=[{**x,'ranking':meta[x['id']]['ranking']} for x in order]
    result=Q.execute(data,effective,liked=prefs.liked,
                     shown=s.selection_scope_ids or [x['id'] for x in s.shown],seen=st['shown_ids'])
    batch=result.pop('items')
    first=not s.shown
    s.shown=[card(x,meta,prefs) for x in batch]
    s.selection_scope_ids=[x['id'] for x in batch]
    s.selection_dirty=False
    st['shown_ids'].update(x['id'] for x in batch)
    s.call_targets=[]
    s.push('result',step=4,kind='selection',data={
        'cards':s.shown,'liked':prefs.liked,'disliked':prefs.disliked,'learned':prefs.learned(),
        'query':effective,'remaining':result['remaining'],'total':result['total'],'aggregates':result['aggregates'],'groups':result['groups'],
    })
    if not batch:
        await s.say('Новых подходящих вариантов по этой команде нет. Отметки сохранены; можно изменить условия или показать понравившиеся.')
    elif first:
        await s.say('Вот лучшие варианты. Отметьте «Нравится» или «Не то» — учту и подберу ещё. Можно менять условия, порядок и количество; звоню только по вашей просьбе.')
    r=await s.ask('selection','Подборка','Выберите квартиры, попросите ещё или уточните условия.')
    d=r['data']
    by_id={x['id']:x for x in st['pool']}
    prefs.liked=[i for i in d.get('liked',prefs.liked) if i in by_id and selectable(by_id[i],prefs)]
    prefs.disliked=[i for i in d.get('disliked',prefs.disliked) if i in by_id]
    P.learn(prefs,by_id)
    if d.get('action')=='more':
        s.selection_query={**effective,'mode':'more','scope':'all'}
        s.selection_dirty=True
        s.request_intent='selection'
    elif d.get('action')=='proceed':
        s.call_targets=list(prefs.liked)
        if not prefs.liked and batch:
            # Кнопка без отметок отдельно спрашивает согласие на три текущих лучших.
            s.call_targets=[x['id'] for x in batch[:3]]
        s.request_intent='call'
    number={x['id']:i+1 for i,x in enumerate(batch)}
    return {'action':d.get('action') or 'text','client_said':d.get('text'),
            'shown':[{'n':number[x['id']],**brief(x,meta),'match':match(x,prefs)} for x in batch],
            'liked':[{'n':number.get(i),**brief(by_id[i],meta)} for i in prefs.liked],
            'call_targets':s.call_targets,'remaining':result['remaining'],'aggregates':result['aggregates'],'groups':result['groups'],'learned':prefs.learned()}


@action("offer_search_alternatives", 3,
        "Если подходящих квартир нет — предложить кнопками сменить рынок, район или комнатность. Условия без ответа не меняет.")
async def offer_search_alternatives(s):
    if not s.search:
        raise ActionError("Сначала search_listings.")
    options = s.search.get("alternatives") or []
    if not options:
        if s.profile.get("housing") == "secondary":
            options.append("Посмотреть новостройки")
        elif s.profile.get("housing") == "new":
            options.append("Посмотреть вторичку")
        options += ["Смягчить пожелания", "Изменить район", "Рассмотреть меньше комнат"]
    reports = s.search.get("report") or []
    unavailable = reports and all(r.get("status") == "unavailable" for r in reports)
    if unavailable:
        options = ["Повторить поиск", *options]
    text = ("Не удалось получить выдачу от источника — это не означает, что квартир нет. Можно повторить запрос или изменить условия."
            if unavailable else "Подходящих подтверждённых вариантов пока нет. Что готовы рассмотреть? Бюджет без вашего решения не увеличу.")
    r = await s.ask("clarify", "Источник недоступен" if unavailable else "Расширить поиск?", text, chips=options)
    return {"client_said": r["data"].get("text", ""), "hint": "запиши только выбранное изменение через update_request и повтори поиск"}
