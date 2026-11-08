"""Действия шага «Запрос»: записать запрос клиента, показать цены рынка, спросить, подтвердить рамки."""
from __future__ import annotations

from ... import geo, sources
from .. import finance
from ..features import FEATURES
from ..numbers import fmt
from ..listing_query import FIELDS
from ..profile import default_budget_min, in_districts, merge_profile, missing_required, passes_hard_filters, profile_summary
from . import ActionError, action

LIST_KEYS = ("districts", "avoid_districts", "features", "poi", "wishes", "material", "soft_criteria", "rooms_options", "cities")
from ..capabilities import CORE_FIELDS, FILTER_FIELDS
CORE_KEYS = CORE_FIELDS
PROPERTY_FIELDS={'area_min':'area','kitchen_min':'kitchen','year_min':'year',
                 'metro_max_minutes':'metro_minutes','prefer_center':'center_km',
                 'condition':'condition','material':'material'}


def profile_brief(p: dict) -> dict:
    """Профиль для модели: только то, что известно."""
    keys = ("city", "cities", "rooms", "rooms_options", "prefer_center", "budget_max", "budget_min", "okrug", "districts", "avoid_districts", "housing", "payment", "children",
            "youngest_age", "matcap", "kitchen_min", "area_min", "not_first_floor", "not_last_floor", "metro_max_minutes", "wishes",
            "condition", "material", "year_min", "timeline", "soft_criteria", "filters", "center_required", "seller_questions")
    out = {k: p[k] for k in keys if p.get(k) not in (None, [], "", False)}
    if p.get("matcap") is False:
        out["matcap"] = False
    if p.get("features"):
        out["features"] = {k: FEATURES[k][0] for k in p["features"]}
    if p.get("poi"):
        out["near"] = {k: geo.POI[k][0] for k in p["poi"]}
    return out


def emit_profile(s) -> None:
    data = {**{k:v for k,v in s.profile.items() if not k.startswith("_")}, "market": s.market}
    if s.profile.get('city'):
        data["summary"] = profile_summary(s.profile)
    s.push("result", step=1, kind="profile", data=data)


@action('update_request', 1,
        'Разобрать новый ввод покупателя и обновить профиль. Параметры извлекаются отдельно из сообщения '
        'пользователя; передавать значения в этот инструмент не нужно. Возвращает профиль и недостающие условия.')
async def update_request(s) -> dict:
    from .. import understanding
    checked = await understanding.ensure(s)
    if checked and checked.get('needs_clarification'):
        return checked
    if checked and checked.get('profile_result'):
        return checked['profile_result']
    return {'profile': profile_brief(s.profile), 'missing': missing_required(s.profile),
            'red_lines_confirmed': s.red_lines_ok, 'already_reviewed': True,
            'notes': ['Новых условий для применения нет.']}


def updated_profile(profile, changes):
    """Чистое преобразование: одинаково для проверки кандидата и его применения."""
    p = merge_profile(changes, profile or None)
    replaced={field for key,field in PROPERTY_FIELDS.items() if key in changes}
    p['filters']=[f for f in p.get('filters') or [] if f['field'] not in replaced]
    if 'filters' in changes:
        from ..listing_query import validate_filters
        validate_filters(changes['filters'])
        filters = {f['field']:f for f in profile.get('filters') or [] if f['field'] not in replaced}
        for f in changes['filters']:
            for key,field in PROPERTY_FIELDS.items():
                if field==f['field'] and key not in changes:
                    p[key]=[] if key=='material' else False if key=='prefer_center' else None
                    p['soft_criteria']=[k for k in p.get('soft_criteria') or [] if k!=key]
            if f['priority']=='remove': filters.pop(f['field'],None)
            else: filters[f['field']]=f
        p['filters']=list(filters.values())
    for key in changes.get("clear_criteria", []):
        # Старые именованные условия и универсальный фильтр представляют
        # одно свойство. Его явная отмена снимает оба представления.
        field=PROPERTY_FIELDS.get(key,key)
        if field.startswith('poi:'):field='nearby:'+field[4:]
        replacement=any(f['field']==field and f['priority']!='remove' for f in changes.get('filters') or [])
        if not replacement:p['filters']=[f for f in p.get('filters') or [] if f['field']!=field]
        if key.startswith("poi:"):
            p["poi"] = [v for v in p["poi"] if v != key[4:]]
        elif key.startswith("feature:"):
            p["features"] = [v for v in p["features"] if v != key[8:]]
        elif key in ("not_first_floor", "not_last_floor", "prefer_center"):
            p[key] = False
        elif key in ("material", "districts", "avoid_districts"):
            p[key] = []
        else:
            p[key] = None
        p["soft_criteria"] = [v for v in p["soft_criteria"] if v != key]
    for key in LIST_KEYS:  # пустой список от модели — снять условие
        if key in changes and changes[key] == []:
            p[key] = []
    from ..conditions import reconcile
    return reconcile(p,changes)


def apply_request(s, changes, *, condition_sources=None):
    """Только структурированные, проверенные изменения. Естественный язык здесь не читается."""
    old_profile = dict(s.profile)
    before = {k: s.profile.get(k) for k in CORE_KEYS}
    p = updated_profile(s.profile, changes)
    if 'deposit' in changes:p.pop('_deposit_skipped',None)
    if condition_sources:
        from ..conditions import normalize
        p['_condition_sources']={**(p.get('_condition_sources') or {}),**condition_sources}
        normalize(p)
    notes = []
    if p.get("city") and p["city"] not in sources.SUPPORTED_CITIES:
        notes.append(f"Города «{p['city']}» нет среди {len(sources.SUPPORTED_CITIES)} городов Циана, которые я знаю. "
                     f"Например: {', '.join(sources.FEATURED_CITIES)}.")
        p["city"] = None
    p["cities"] = [c for c in p.get("cities") or [] if c in sources.SUPPORTED_CITIES]
    missing = missing_required(p)
    if not missing:
        p["summary"] = profile_summary(p)
    s.profile = p
    from ...sources.market_estimate import current
    from ..research import profile as research_profile
    if s.market and not current(s.market,research_profile(s)):s.market=None
    changed_keys={k for k in old_profile.keys() | p.keys()
                  if not k.startswith('_') and k not in ('summary','market') and p.get(k)!=old_profile.get(k)}
    if p!=old_profile:s.profile_revision+=1
    s.prefs.sync(p)
    if any(p.get(k) != before[k] for k in CORE_KEYS):
        budget_changed = p.get("budget_max") != old_profile.get("budget_max")
        if budget_changed:
            p['_approved_budget_max'] = None
            p['budget_min'] = changes['budget_min'] if 'budget_min' in changes else default_budget_min(p.get('budget_max'))
            notes.append("Лимит изменился — подтвердите новую сумму; поиск обновится после подтверждения.")
        else:
            notes.append("Условия поиска изменились; подтверждённый лимит сохранён.")
        s.invalidate_deal(keep_limit=not budget_changed)
        if p.get("city") != before["city"]:
            s.market = None
    elif s.search and (changed_keys & FILTER_FIELDS):
        # Возвращаем ранее отфильтрованные варианты при снятии жёсткого ограничения.
        # Рынок/город/комнаты не менялись: исходная выдача остаётся актуальным пулом.
        if "listings" in s.search:
            s.search["pool"] = [x for x in s.search["listings"] if passes_hard_filters(x, p)
                                and (not s.search.get("strict") or in_districts(x, s.search.get("named")))]
        s.search["shown_ids"].clear()
        # Видимые карточки — последний успешный снимок, не рабочий пул.
        # Меняем их только после успешного вычисления следующей выдачи.
        s.invalidate_decisions(financial=finance.programs_key(p)!=finance.programs_key(old_profile))
        notes.append("Пожелания учтены: при следующем показе подборки (show_selection) отфильтрую и пересортирую.")
    elif changed_keys & {'payment','children','youngest_age','matcap','deposit'}:
        s.invalidate_decisions(purchase=False,financial=True)
        if s.search:s.selection_dirty=True
    if changed_keys-{'seller_questions'}:
        s.selection_profile_stale=True
        s.profile['_selection_stale']=True
        if s.shown:s.push('selection_notice',text='Условия изменились. До успешного нового отбора сохранены предыдущие карточки; они относятся к прежним условиям.')
        s.call_targets = []
    emit_profile(s)
    return {"profile": profile_brief(p), "missing": missing, "red_lines_confirmed": s.red_lines_ok, "notes": notes}


@action("ask_extras", 1,
        "Карточка «Что ещё важно»: тип жилья, апартаменты, ремонт, этаж — быстрые варианты на выбор. "
        "Показывается один раз после подтверждения рамок и до поиска; после неё сразу поиск.")
async def ask_extras(s) -> dict:
    p = s.profile
    if p.get("_extras_asked") and s.requested_action != 'ask_extras':
        return {"already_asked": True, "housing": p.get("housing"), "apartments": p.get("apartments"),
                "condition": p.get("condition")}
    r = await s.ask(
        "extras", "Что ещё важно",
        "Четыре быстрых вопроса — можно выбрать вариант или пропустить. После этого сразу ищу квартиры.",
        payload={"housing": p.get("housing"), "apartments": p.get("apartments"),
                 "condition": p.get("condition"),
                 "not_first_floor": bool(p.get("not_first_floor")),
                 "not_last_floor": bool(p.get("not_last_floor"))},
    )
    if r["approved"] is None:
        return {"client_said": r["data"].get("text", "")}
    data = r["data"] or {}
    if not r["approved"]:
        p["_extras_asked"] = True
        return {"skipped": True, "note": "клиент пропустил дополнительные вопросы — ищи по известному"}
    applied = {}
    if data.get("housing") in ("secondary", "new", "both"):
        p["housing"] = data["housing"]
        applied["тип жилья"] = {"secondary": "вторичка", "new": "новостройка", "both": "без разницы"}[p["housing"]]
    if data.get("apartments") in ("hide", "only", "any"):
        p["apartments"] = data["apartments"]
        applied["апартаменты"] = {"hide": "не показывать", "only": "только апартаменты", "any": "без разницы"}[p["apartments"]]
    if data.get("condition") in ("repair", "cosmetic", "ready", "any"):
        p["condition"] = data["condition"]
        applied["ремонт"] = {"repair": "без ремонта", "cosmetic": "косметический",
                             "ready": "с ремонтом", "any": "не важно"}[p["condition"]]
    if "not_first_floor" in data:
        p["not_first_floor"] = bool(data["not_first_floor"])
        if p["not_first_floor"]:
            applied["этаж"] = "не первый"
    if "not_last_floor" in data:
        p["not_last_floor"] = bool(data["not_last_floor"])
        if p["not_last_floor"]:
            applied["этаж"] = (applied.get("этаж", "") + " и не последний").strip()
    p["_extras_asked"] = True
    p["summary"] = profile_summary(p)
    s.invalidate_deal(keep_limit=True)
    emit_profile(s)
    return {"applied": applied or {"note": "выбор не изменился"}}


@action("market_prices", 1,
        "Исследовать реальные объявления по проверенному запросу: цены, доступные комнаты/площадь и другие агрегаты. "
        "Достаточно города. Проверенный исследовательский запрос определяет группировку и применение бюджета; неназванные условия не требуются.")
async def market_prices(s) -> dict:
    from .. import guidance
    if not s.requested_action and guidance.help_field(s):s.research_query=guidance.research_query(s)
    if not s.profile.get("city"):
        raise ActionError("Для исследования объявлений нужен город; комнаты и бюджет необязательны.")
    if (s.research_query or {}).get('respect_budget') and s.profile.get('budget_max') is None:
        raise ActionError('Чтобы посчитать доступные варианты в пределах бюджета, нужна сумма. Рыночные цены можно исследовать без неё.')
    helping=guidance.help_field(s) is not None
    from ...sources.market_estimate import current
    from ..research import profile as research_profile
    if not current(s.market,research_profile(s)):
        await s.say(f"Исследую объявления на Циане ({s.profile['city']}) по вашим условиям…")
        try:
            s.market = await sources.market(research_profile(s),on_progress=lambda count:s.signal('search_progress',on=True,
                text=f'Проверяю ценовые ориентиры: получено {count} объявлений'))
        finally:s.signal('search_progress',on=False)
    if not s.market or s.market.get('error_kind'):
        guidance.record_market(s,success=False)
        details=s.market or {};count=details.get('count',0)
        message=(f"Для надёжного ценового ориентира нашлось только {count} объявлений с подтверждёнными условиями; нужно хотя бы 6. "
                 if details.get('received') else "Циан сейчас не вернул объявления для ценового ориентира. ")
        message+='Ваши условия сохранены. Можно попробовать позже, обсудить изменение условий или рассчитать бюджет по доходам.'
        s.market=None
        s.last_issue={'kind':details.get('error_kind','market_source_unavailable'),'message':message}
        await s.say(message,await_reply=not helping)
        return {'error_kind':s.last_issue['kind'],'error':message,'coverage':details}
    guidance.record_market(s)
    emit_profile(s)
    m = s.market
    centre=' До 5 км по прямой от опорной точки центра.' if (m.get('scope') or {}).get('center_radius_km') else ''
    if any(r.get('city')=='Москва' and r.get('query_okrug')=='ЦАО' for r in m.get('reports') or []):
        centre+=' Для этой выборки просмотрены объявления ЦАО.'
    markets='; '.join(('новостройки' if row['new'] else 'вторичка')+f": {row['count']}" for row in m.get('markets') or [])
    if not markets:markets='рынок по запросу'
    analysis=m.get('analysis') or {}
    from ..listing_query import FIELDS
    def facts(rows):
        labels={'min':'минимум','max':'максимум','mean':'среднее','median':'медиана','count':'известных значений'}
        return '; '.join(f"{labels[a['function']]} ({FIELDS[a['field']][1]}): "+
                        (f"{a['value']:,.1f}".replace(',',' ').replace('.0','') if a.get('value') is not None else 'нет данных')
                        +f" [данных: {a['known']}]" for a in rows)
    grouped=analysis.get('groups') or []
    research=m.get('scope',{}).get('research') or {}
    findings=''
    if grouped:
        label=FIELDS[research['group_by']][1]
        findings='\n'+'\n'.join(f"{label}: {g['value'] if g['value'] is not None else 'неизвестно'} — {g['count']} объявлений; {facts(g['aggregates'])}." for g in grouped[:12])
        if len(grouped)>12:findings+=f'\nПоказаны 12 из {len(grouped)} групп.'
    elif analysis.get('aggregates'):findings='\n'+facts(analysis['aggregates'])+'.'
    budget_note=f" В пределах {fmt(s.profile['budget_max'])}." if m.get('scope',{}).get('budget_applied') else ' Бюджетный фильтр не применялся.'
    await s.say(f"По полученной выборке: медиана {fmt(m['median'])}, средняя цена {fmt(m.get('mean',m['median']))}; "
                f"средняя половина предложений — {fmt(m['p25'])}–{fmt(m['p75'])}. "
                f"Проверено {m['count']} объявлений, {markets}.{centre} "
                +('Часть данных из кэша или охват неполный. ' if m.get('reasons') else '')
                +budget_note+findings+"\nЭто результат по полученной выборке, а не полный рынок. Условия и согласия не изменены.")
    return {"typical_from": m["p25"], "median": m["median"], "typical_to": m["p75"], "min": m["min"],
            "mean":m.get('mean'),"per_m2": round(m["ppm2"], -3) if m.get('ppm2') is not None else None, "listings": m["count"],
            'scope':m.get('scope'),'analysis':analysis,'coverage':{'reports':m.get('reports'),'reasons':m.get('reasons')}}


@action("ask_client", 1,
        "Задать клиенту вопрос карточкой с вариантами ответа (кнопки). Клиент может ответить и своими словами. "
        "Возвращает ответ клиента. Для простого разговора без вариантов просто напиши клиенту текстом.",
        question={"type": "string", "description": "вопрос, 1–2 предложения"},
        options={"type": "array", "items": {"type": "string"}, "description": "2–5 коротких вариантов ответа"},
        field={"type":"string","enum":["budget_max","housing","okrug","districts",*FIELDS],
               "description":"Одно свойство, о котором вопрос, если применимо. Позволяет связать краткое «Любой» с нужным полем. Для общего вопроса можно не передавать."})
async def ask_client(s, question: str, options: list[str] | None = None, field: str | None = None) -> dict:
    r = await s.ask("clarify", "Уточнение", question, chips=[str(o)[:60] for o in (options or [])][:5], market=s.market)
    return {"answer": r["data"].get("text", "")}


@action("confirm_red_lines", 1,
        "Показать клиенту рамки (город, комнаты, бюджет — выше которого агент не пойдёт) и получить подтверждение. "
        "Подтверждение один раз в начале подбора и заново только при изменении лимита. Клиент может поправить бюджет на карточке.")
async def confirm_red_lines(s) -> dict:
    p = s.profile
    if s.red_lines_ok and s.requested_action!='confirm_red_lines':
        return {"approved": True, "budget_max": p["budget_max"], "budget_min": p.get("budget_min"),
                "already_confirmed": True}
    if p.get('budget_max') is None:
        raise ActionError('Для подтверждения лимита нужна конкретная сумма. Поиск без лимита разрешён.')
    if p.get('budget_min') is None:  # клиент не называл нижнюю границу — берём −30% от максимума
        p['budget_min'] = default_budget_min(p['budget_max'])
        p['summary'] = profile_summary(p)
    emit_profile(s)
    r = await s.ask(
        "red_lines", "Границы подбора",
        "Проверьте город, комнаты и диапазон цены. В этих границах я ищу, звоню и торгуюсь сам: "
        "выше верхней границы не пойду, ниже нижней квартиры не предложу. Ползунком можно сдвинуть обе границы. "
        "Итоговую цену всё равно подтверждаете вы.",
        payload={"budget_max": p["budget_max"], "budget_min": p.get("budget_min"),
                 "default_low_share": 0.3, "step": 500_000, "floor": 1_000_000,
                 "rooms": p["rooms"], "city": p["city"],
                 "rooms_options": p.get("rooms_options"), "cities": p.get("cities"),
                 "authority_rule": "не выше верхней границы цены в объявлении и вашего бюджета"},
    )
    if r["approved"] is None:
        return {"approved": False, "client_said": r["data"].get("text", "")}
    if not r["approved"]:
        s.request_intent = "hold"
        return {"approved": False, "note": "клиент хочет поменять запрос — спроси, что именно"}
    data = r["data"] or {}
    new_max, new_min = data.get("budget_max"), data.get("budget_min")
    try:
        new_max = int(new_max) if new_max not in (None, "") else None
        new_min = int(new_min) if new_min not in (None, "") else None
    except (TypeError, ValueError):
        new_max = new_min = None
    if new_min is not None and new_max is not None and new_min > new_max:
        new_min = default_budget_min(new_max)
    if (new_max and new_max != p["budget_max"]) or (new_min is not None and new_min != p.get("budget_min")):
        s.invalidate_deal()
        if new_max:
            p["budget_max"] = new_max
        p["budget_min"] = new_min if new_min is not None else default_budget_min(p["budget_max"])
        p["summary"] = profile_summary(p)
        emit_profile(s)
    s.red_lines_ok = True
    p['_approved_budget_max'] = p['budget_max']
    p['_approved_budget_min'] = p.get('budget_min')
    s.remember()
    await s.finish_step(1)
    return {"approved": True, "budget_max": p["budget_max"], "budget_min": p.get("budget_min"),
            "budget_text": f"{fmt(p['budget_min'])}–{fmt(p['budget_max'])}" if p.get("budget_min") else fmt(p["budget_max"])}
