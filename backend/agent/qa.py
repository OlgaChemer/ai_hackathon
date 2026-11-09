"""Вопросы клиента в любой момент: «что такое СБР?», «почему справедливая цена такая?», «сколько я переплачу?».

Отвечает модель с числами из текущей сессии (их считает код). Текущий шаг конвейера при этом ждёт дальше.
Без модели заготовленных ответов нет: агент честно говорит, что ответить сейчас не может.
"""
from __future__ import annotations


from ..llm import LLMError
from ..llm.base import LLMBusy
from ..config import settings
from ..runtime import Turn, TurnStopped
from . import guard, prompts
from .numbers import fmt
from ..calculation_policy import POLICY, metadata


def build_context(s) -> dict:
    """Выжимка сессии для ответа: запрос, финансы, ипотека, подборка, текущая квартира, итог торга."""
    p = s.profile or {}
    from .listing_query import FIELDS
    from .features import FEATURES
    from .. import geo
    ctx: dict = {"шаг": s.stage, "допущения расчёта": {
        **metadata(), "ипотека": POLICY['mortgage'], "продукты": POLICY['savings']['products'],
        "доля платы от экономии": POLICY['economics']['fee_pct']}}
    ctx['проверяемые параметры'] = [*(value[1] for value in FIELDS.values()),
                                   *(value[0] for value in FEATURES.values()),
                                   *(value[0] for value in geo.POI.values())]
    from .capabilities import catalog
    ctx['доступные действия']=[{k:r[k] for k in ('name','description','allowed','reason')} for r in catalog(s)]
    ctx['непроверяемые пожелания'] = p.get('_unsupported_conditions') or []
    ctx['последняя ошибка обработки'] = s.last_issue
    if p.get("city"):
        ctx["запрос"] = {k:v for k,v in p.items() if not k.startswith('_') and k not in ('summary','market') and v not in (None,[],"")}
        ctx["способ оплаты"] = {"cash": "наличные", "mortgage_ready": "ипотека, взнос есть",
                                "mortgage_saving": "ипотека, взнос копит"}.get(p.get("payment"), "не выяснен")
    from .understanding import dialogue_context
    ctx['последние реплики']=dialogue_context(s)
    ctx['ожидаемые решения']=[{'kind':t['kind'],'text':t['text']} for t in s.pending_decisions.values() if t['status']=='waiting']
    if s.market:
        from ..sources.market_estimate import current
        from .research import profile as research_profile
        ctx['рыночные ориентиры']=s.market if current(s.market,research_profile(s)) else {
            'status':'outdated','note':'Прежние цифры относятся к другому или устаревшему срезу. Для актуальных цен нужен market_prices; не выдавай старый срез за текущую подборку.'}
    if s.mortgage:
        chosen = next(c for c in s.mortgage["programs"] if c["key"] == s.mortgage["chosen"])
        ctx["ипотека"] = {"считаем по": chosen["title"], "ставка": f"{chosen['rate'] * 100:.1f}%".replace(".", ","),
                          "кредит": fmt(chosen["loan"]), "взнос": fmt(chosen["down_payment"]), "платёж": fmt(chosen["monthly"]),
                          "переплата за весь срок": fmt(chosen["overpay"]), "срок": f"{chosen['years']} лет",
                          "почему эта программа": s.mortgage["explanation"]}
    if s.finance:
        f = s.finance
        ctx["накопления"] = {"нужно на взнос и сделку": fmt(f["need"]), "доступно сейчас": fmt(f["available"]),
                             "не хватает": fmt(f["gap"]), "откладывать в месяц": fmt(f["monthly"]), "вывод": f["verdict"]}
    if s.shown:
        from .coverage import report
        ctx['охват и полнота подбора']=report(s)
        ctx["подборка"] = [{"адрес": x.get("address"), "цена": fmt(x["price"]), "справедливая": fmt(x["fair"]["fair"]),
                            "коридор": f"{fmt(x['fair']['low'])}–{fmt(x['fair']['high'])}", "площадь": x.get("area"),
                            "основа оценки": x["fair"]["basis"], "что_рядом": x.get("nearby", []),
                            "проверка_условий": x.get("match")} for x in
                           [x for x in s.shown if not s.selection_scope_ids or x['id'] in s.selection_scope_ids][:10]]
    if s.current:
        x, fp = s.current["listing"], s.current["fair"]
        ctx["текущая квартира"] = {"адрес": x.get("address"), "цена в объявлении": fmt(x["price"]),
                                   "справедливая цена": fmt(fp["fair"]), "коридор": f"{fmt(fp['low'])}–{fmt(fp['high'])}",
                                   "основа оценки": fp["basis"], "цель торга": fmt(fp["target"])}
    if s.current:
        from .features import missing_info
        ctx['программа разговора'] = [q['question'] for q in missing_info(s.current['listing'],3,s.prefs.features)] + list(p.get('seller_questions') or [])
    if s.journal.get("final_price"):
        ctx["итог торга"] = {"цена объявления": fmt(s.journal["base_price"]), "договорились": fmt(s.journal["final_price"]),
                             "экономия": fmt(s.journal.get("savings", 0))}
    return ctx


async def reply(s, text: str) -> str:
    system = prompts.QA_SYSTEM + ("\n" + prompts.VOICE_STYLE if s.voice_mode else "") + "\n" + guard.POLICY
    turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
    try:
        resp = await turn.run(lambda: s.llm.chat([{"role": "system", "content": system}, guard.data_message("session_context", build_context(s)),
                                  {"role": "user", "content": text}],
                                temperature=0.3, max_tokens=500, tag="answer"), kind="llm", name="answer")
        reply = resp.text.strip()
        if not reply:
            raise LLMError("пустой ответ")
        turn.finish("answered")
    except (LLMError, TurnStopped) as exc:
        turn.finish(type(exc).__name__)
        reply = "Сейчас не могу ответить на вопрос: модель недоступна. Продолжаю работу — спросите чуть позже."
        if isinstance(exc, LLMBusy):
            reply = "Модель сейчас занята. Спросите чуть позже — текущая карточка и условия поиска сохранены."
    return reply


async def answer(s,text: str) -> None:
    await s.say(await reply(s,text))
