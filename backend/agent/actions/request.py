"""Действия шага «Запрос»: записать запрос клиента, показать цены рынка, спросить, подтвердить рамки."""
from __future__ import annotations

from ... import geo, sources
from ..features import FEATURES
from ..numbers import fmt
from ..profile import in_districts, merge_profile, missing_required, passes_hard_filters, profile_summary
from . import ActionError, action

LIST_KEYS = ("districts", "avoid_districts", "features", "poi", "wishes", "material", "soft_criteria", "rooms_options", "cities")
CORE_KEYS = ("city", "rooms", "budget_max", "okrug", "housing", "rooms_options", "cities", "districts", "prefer_center", "center_required")
PROPERTY_FIELDS={'area_min':'area','kitchen_min':'kitchen','year_min':'year',
                 'metro_max_minutes':'metro_minutes','prefer_center':'center_km',
                 'condition':'condition','material':'material'}


def profile_brief(p: dict) -> dict:
    """Профиль для модели: только то, что известно."""
    keys = ("city", "cities", "rooms", "rooms_options", "prefer_center", "budget_max", "okrug", "districts", "avoid_districts", "housing", "payment", "children",
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
    if not missing_required(s.profile):
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
        p['filters']=[f for f in p.get('filters') or [] if f['field']!=field]
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
    return p


def apply_request(s, changes):
    """Только структурированные, проверенные изменения. Естественный язык здесь не читается."""
    old_profile = dict(s.profile)
    before = {k: s.profile.get(k) for k in CORE_KEYS}
    p = updated_profile(s.profile, changes)
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
    s.prefs.sync(p)
    if any(p.get(k) != before[k] for k in CORE_KEYS):
        budget_changed = p.get("budget_max") != old_profile.get("budget_max")
        if budget_changed:
            p['_approved_budget_max'] = None
            notes.append("Лимит изменился — подтвердите новую сумму; поиск обновится после подтверждения.")
        else:
            notes.append("Условия поиска изменились; подтверждённый лимит сохранён.")
        s.invalidate_deal(keep_limit=not budget_changed)
        if p.get("city") != before["city"]:
            s.market = None
    elif s.search and any(p.get(k) != old_profile.get(k) for k in changes) and (changes.keys() & {"features", "poi", "kitchen_min", "area_min", "not_first_floor", "not_last_floor",
                                         "condition", "material", "year_min", "metro_max_minutes", "soft_criteria", "clear_criteria", "avoid_districts", "filters", "wishes"}):
        # Возвращаем ранее отфильтрованные варианты при снятии жёсткого ограничения.
        # Рынок/город/комнаты не менялись: исходная выдача остаётся актуальным пулом.
        if "listings" in s.search:
            s.search["pool"] = [x for x in s.search["listings"] if passes_hard_filters(x, p)
                                and (not s.search.get("strict") or in_districts(x, s.search.get("named")))]
        s.search["shown_ids"].clear()
        # Видимые карточки — последний успешный снимок, не рабочий пул.
        # Меняем их только после успешного вычисления следующей выдачи.
        s.calls.clear()
        s.agreed.clear()
        s.chosen = s.current = None
        s.journal.clear()
        notes.append("Пожелания учтены: при следующем показе подборки (show_selection) отфильтрую и пересортирую.")
    if any(p.get(k) != old_profile.get(k) for k in changes if k != 'seller_questions'):
        s.selection_profile_stale=True
        s.profile['_selection_stale']=True
        if s.shown:s.push('selection_notice',text='Условия изменились. До успешного нового отбора сохранены предыдущие карточки; они относятся к прежним условиям.')
        s.call_targets = []
    emit_profile(s)
    return {"profile": profile_brief(p), "missing": missing, "red_lines_confirmed": s.red_lines_ok, "notes": notes}


@action("market_prices", 1,
        "Цены рынка по запросу (город, комнаты, округ, рынок) без учёта бюджета: квартили и минимум, руб. "
        "Помогает клиенту выбрать бюджет; клиент видит их на экране.")
async def market_prices(s) -> dict:
    if not (s.profile.get("city") and s.profile.get("rooms") is not None):
        raise ActionError("Сначала нужны город и число комнат (update_request).")
    await s.say(f"Смотрю цены на Циане ({s.profile['city']}), чтобы подсказать бюджет…")
    s.market = await sources.market(s.profile)
    if not s.market:
        raise ActionError("Циан сейчас не отдал цены — спроси бюджет без подсказки.")
    emit_profile(s)
    m = s.market
    return {"typical_from": m["p25"], "median": m["median"], "typical_to": m["p75"], "min": m["min"],
            "per_m2": round(m["ppm2"], -3), "listings": m["count"]}


@action("ask_client", 1,
        "Задать клиенту вопрос карточкой с вариантами ответа (кнопки). Клиент может ответить и своими словами. "
        "Возвращает ответ клиента. Для простого разговора без вариантов просто напиши клиенту текстом.",
        question={"type": "string", "description": "вопрос, 1–2 предложения"},
        options={"type": "array", "items": {"type": "string"}, "description": "2–5 коротких вариантов ответа"})
async def ask_client(s, question: str, options: list[str] | None = None) -> dict:
    r = await s.ask("clarify", "Уточнение", question, chips=[str(o)[:60] for o in (options or [])][:5], market=s.market)
    return {"answer": r["data"].get("text", "")}


@action("confirm_red_lines", 1,
        "Показать клиенту рамки (город, комнаты, бюджет — выше которого агент не пойдёт) и получить подтверждение. "
        "Подтверждение один раз в начале подбора и заново только при изменении лимита. Клиент может поправить бюджет на карточке.")
async def confirm_red_lines(s) -> dict:
    p = s.profile
    if s.red_lines_ok:
        return {"approved": True, "budget_max": p["budget_max"], "already_confirmed": True}
    if missing_required(p):
        raise ActionError(f"Не хватает: {', '.join(missing_required(p))} — сначала узнай у клиента и запиши (update_request).")
    emit_profile(s)
    r = await s.ask(
        "red_lines", "Красные линии",
        "Подтвердите верхний лимит покупки. Любую итоговую цену подтверждаете вы. Повторное подтверждение понадобится только при изменении лимита.",
        payload={"budget_max": p["budget_max"], "rooms": p["rooms"], "city": p["city"],
                 "rooms_options": p.get("rooms_options"), "cities": p.get("cities"),
                 "authority_rule": "не выше верхней границы справедливой цены и бюджета"},
    )
    if r["approved"] is None:
        return {"approved": False, "client_said": r["data"].get("text", "")}
    if not r["approved"]:
        s.request_intent = "hold"
        return {"approved": False, "note": "клиент хочет поменять запрос — спроси, что именно"}
    if r["data"].get("budget_max") and int(r["data"]["budget_max"]) != p["budget_max"]:
        s.invalidate_deal()
        p["budget_max"] = int(r["data"]["budget_max"])
        p["summary"] = profile_summary(p)
        emit_profile(s)
    s.red_lines_ok = True
    p['_approved_budget_max'] = p['budget_max']
    s.remember()
    await s.finish_step(1)
    return {"approved": True, "budget_max": p["budget_max"], "budget_text": fmt(p["budget_max"])}
