"""Один структурированный разбор ввода. Отдельных критиков и самооценок уверенности нет."""
from __future__ import annotations

from copy import deepcopy

from ..llm import LLMError
from ..llm.base import StructuredOutputError
from ..runtime import Turn, record
from ..config import settings
from . import guard, validation, input_frame
from .request_schema import CHANGE_SCHEMA
from .listing_query import FIELDS

SYSTEM = """Разбери новый ввод покупателя квартиры строго по input_schema. Ничего не выполняй.
messages — только ещё не обработанные реплики; recent_dialogue — контекст, не источник новых условий.
Более позднее уточнение дополняет/отменяет прежнее. Неназванное сохраняется. Не возвращай snapshot profile.

УСЛОВИЯ
fields: null — не названо; иначе {value: значение, source: буквальное слово/полное новое сообщение из enum}.
Суммы в рублях. rooms_options — ВСЕ допустимые комнатности 1–5, диапазон 1–2 = [1,2].
Студия не входит в 1–2 комнаты. Только явную просьбу студии передай studio_requested=true;
явный запрет — false; иначе null. Не заполняй семью, маткапитал и timeline по предположению.
criteria связывает каждое условие с required/preferred/remove; null — не названо.
Неназванные базовые поля и criteria не повторяй из profile.
Готовые критерии (не первый/последний этаж, инфраструктура) не дублируй filters.
fields.filters — только ЯВНЫЙ числовой порог/диапазон/значение свойства из listing_fields.
Сравнение и превосходная степень — не порог! «Из двух какая дешевле за м²» не означает цену <=2.
Не добавляй неназванные area>=0, value_score>0. В filters числа строками, не имена других полей.
Сдача в 2027: delivery_year eq ['2027']; не позднее 2027: lte; 2027–2028: between.
Год постройки year/year_min — другой параметр. Не заменяй им срок сдачи.
«В центре» обязательно: prefer_center=true, center_required=true; «желательно ближе» — true/false.
Название улицы и центр города — разные условия. «Ближе к центральной улице» без названия:
задай конкретный question, какую улицу клиент имеет в виду или можно ориентироваться на центр города.
Не обещай расстояние до улицы, которого система не вычисляет.

ТЕРРИТОРИИ
places — только явно названные допустимые города/области, все альтернативы через «или».
Москва или Подмосковье = два places. Отрицания не включай. Известный город не меняется без новой территории.
В words.source выбери исходное слово из enum с его регистром/формой; lemma — его начальная форма.
Одному исходному слову соответствует одно нормализованное. Не достраивай полное имя за клиента:
«Нижнего» -> source:Нижнего, lemma:Нижний; код уточнит полный город. «Новгород» неоднозначен.
Школа, центр, новостройка и год не города. Неподдержанное название — конкретный question, places=[].

ОПЕРАЦИИ НАД ДАННЫМИ
selection описывает цель чтения; фильтров и mode в этом объекте НЕТ — исполняемый запрос собирает код.
operation=rank: отобрать N/сортировать; scope=all по умолчанию, shown лишь при явном «из показанных».
operation=compare: «среди этих двух какая выгоднее/больше»; scope=shown, sort по нужному показателю.
Карточки при compare остаются на месте, ответ вычисляется кодом. Число «двух» задаёт группу, не цену.
operation=more: просит ЕЩЁ/НОВЫЕ варианты; только это действие исключает уже показанные.
operation=analyze: посчитать среднее/медиану/статистику; карточки не меняются.
Выгоднее за квадратный метр: price_per_m2 asc. Просторнее/больше: area desc.
Выгоднее без уточнения метрики: value_score desc. Дешевле: price asc.
limit — число карточек, не комнатность и не порог цены. «4 а не 3» меняет limit, sort=null (сохранить).
sort=null сохраняет прежний порядок, [] — обычный рейтинг. При новом полном отборе задай нужный sort.
«Эти не понравились, дай 4 больших» -> rank, all, limit4, area desc, никаких условий профиля.
В compare/analyze/more fields, criteria и places пусты: это чтение, не изменение параметров квартиры.
Новый фильтр + отбор: intent=refine, fields/criteria с новым условием и selection=rank.

НАМЕРЕНИЕ И ВОПРОСЫ
search — новый подбор; refine — новые/снятые условия; selection — операции с выдачей;
call — ЯВНО позвонить; seller_questions — добавить вопросы; question — информационный вопрос;
continue — продолжать; hold — остановиться/пока не искать; other — остальное.
«Что непонятно/что сломалось» — question: не пытайся заново менять город из прежней неудачной реплики.
question/continue/other не меняют профиль. Обычный вопрос не подтверждает ожидающую карточку.
Вопрос «а можно вместо Москвы Казань?» меняет город: refine, не FAQ.
call_ids — ID из workflow.selection только при явном call; [] — по понравившимся; иначе null.
Вопросы продавцу и новые условия не разрешают звонок. seller_questions — явно запрошенный список вопросов.
hold может сохранить явно названные поля/вопросы, но selection=null, call_ids=null.
question/options — один КОНКРЕТНЫЙ вопрос при настоящей неясности, иначе пустая строка/массив.
Вопрос должен называть, что уточнить: полный город, название улицы, сумма или объект; не «уточните запрос».
Если новая реплика исправляет неоднозначность прежней, применяй их вместе. Не добавляй условие без слов клиента.

НЕПОДДЕРЖАННОЕ
unsupported — явно запрошенное условие вне каталога свойств/criteria.
Бассейн ВНУТРИ квартиры не равен фитнесу/воде рядом. Не подменяй одно другим и не пиши это только в wishes.
При add text — буквальная цитата НОВОЙ реплики (например «внутри бассейн»), source — точная ссылка из enum.
Нельзя считать такие квартиры несуществующими: система пока не умеет проверять это условие.
При выборе предложенной альтернативы unsupported=[]: код сохраняет отметку о непроверенном условии.
При явной отмене action=remove, text — прежнее условие из workflow.unsupported_conditions.
При новом неподдержанном условии intent=refine/search/hold. Если неподдержанного нет, unsupported=[].
Верни JSON строго по схеме. Не выдавай разрешений на деньги/звонки/сделки и проценты уверенности.
"""

class UnderstandingError(LLMError):
    """Не удалось надёжно проверить сообщение; неподтверждённые изменения не применяются."""


def failed_input(s, exc):
    """Не заставляем следующий вопрос заново разбирать уже неудачную реплику."""
    version=max((x['seq'] for x in s.request_inputs),default=s.request_review_seq)
    s.request_review_seq=max(version,s.request_review_seq)
    s.request_inputs=[x for x in s.request_inputs if x['seq']>version]
    s.request_review=None
    s.request_intent='hold'
    s.call_targets=[]
    text=('Не смог обработать сообщение из-за ошибки разбора на моей стороне. '
          'Условия и последняя подборка сохранены. Уточнять их не требуется: можно отправить сообщение ещё раз или продолжить с прежними вариантами.')
    if not isinstance(exc,UnderstandingError):
        text=('Сервис обработки сообщения сейчас не ответил. Условия и последняя подборка сохранены; '
              'можно повторить сообщение позже. Это не просьба изменить ваши условия.')
    s.last_issue={'kind':'input_error','message':text}
    s.profile['_last_issue']=dict(s.last_issue)
    s.remember()
    return text


def catalog_ambiguities(cities):
    """Индекс справочника, не разбор клиентского текста; новые города учитываются автоматически."""
    parts = {}
    for city in cities:
        for word in city.split():
            parts.setdefault(word, []).append(city)
    return {word: names for word, names in parts.items() if len(names) > 1}


def dialogue_context(s):
    rows=[{'role':e['role'],'text':e['text']} for e in s.history
          if e.get('type')=='message' and e.get('role') in ('user','agent')]
    if sum(row['role']=='user' for row in rows)<2:
        # После перезапуска история событий ещё не загружена в Session,
        # но сохранённый разговор уже есть. Он нужен для коротких исправлений.
        memory=[{'role':'agent' if m['role']=='assistant' else 'user','text':m['content']}
                for m in s.messages if m.get('role') in ('user','assistant') and m.get('content')]
        rows=memory+[row for row in rows if not memory or row!=memory[-1]]
    return rows[-8:]


async def prepare(s):
    """Один принятый разбор на сообщение; предложения инструментов не служат источником пожеланий."""
    inputs=[x for x in s.request_inputs if x['seq'] > s.request_review_seq]
    if not inputs:return None
    version=inputs[-1]['seq']
    if s.request_review and s.request_review['seq']==version and s.request_review['base_profile']==s.profile:
        return s.request_review
    from .. import sources
    base_profile=deepcopy(s.profile)
    contract=input_frame.schema(CHANGE_SCHEMA, inputs)
    payload={'supported_cities':sources.SUPPORTED_CITIES,'ambiguous_catalog_names':catalog_ambiguities(sources.SUPPORTED_CITIES),
             'messages':inputs,'profile':{k:v for k,v in base_profile.items() if not k.startswith('_')},'input_schema':contract,
             'listing_fields':{key: {'type':v[0],'label':v[1]} for key,v in FIELDS.items()},
             'recent_dialogue':dialogue_context(s),
             'workflow':{'has_search':s.search is not None,'has_selection':bool(s.shown),'has_calls':bool(s.calls),'chosen':bool(s.chosen),
                         'selection_query':s.selection_query or s.profile.get('_selection_query'),
                         'unsupported_conditions':s.profile.get('_unsupported_conditions') or [],
                         'selection':[{'id':x['id'],'number':i+1,'address':x.get('address')} for i,x in enumerate(s.shown)],
                         'liked':s.prefs.liked,'last_issue':s.last_issue}}
    revision=s.client_revision
    error=None
    for attempt in range(2):
        instruction=SYSTEM+'\n'+guard.POLICY
        if error:instruction+='\nПредыдущий ответ не прошёл проверку формата: '+error+'. Исправь поля, смысл не угадывай.'
        messages=[{'role':'system','content':instruction},guard.data_message('request_review',payload)]
        try:
            result=await s.llm.chat_json(messages,tag='request_review',temperature=.01,max_tokens=3500,json_schema=contract)
            result = input_frame.decode(result, payload['profile'], payload, CHANGE_SCHEMA)
            validation.validate(result['changes'], CHANGE_SCHEMA)
            if not guard.input_verdict(result['changes']).ok or guard.output(result['changes'], limit=False) != result['changes']:
                raise ValueError('Небезопасные изменения')
        except StructuredOutputError:
            error='Некорректный JSON';continue
        except (ValueError,TypeError,KeyError) as exc:
            error=str(exc)[:160]
            record('request_review_invalid',session=s.id,problem=guard.output(error),attempt=attempt+1)
            continue
        except LLMError:raise
        if s.request_inputs[-1]['seq']!=version or s.client_revision!=revision or s.profile!=base_profile:
            raise UnderstandingError('Поступило новое сообщение во время разбора')
        s.request_review={'seq':version,'base_profile':base_profile,**result}
        record('request_review',session=s.id,decision=result['decision'],intent=result['intent'],fields=len(result['changes']),repaired=bool(attempt))
        return s.request_review
    raise UnderstandingError('Не удалось получить корректный структурированный ввод')


async def ensure(s):
    """Применяет проверенные изменения перед действием; уточнение ничего не разрешает."""
    review = await prepare(s)
    if review is None:
        return None
    if review['decision'] == 'clarify':
        answer = await s.ask('clarify', 'Уточню запрос', review['question'], chips=review['options'])
        # Ответ — новый ввод для следующего хода. Текущий инструмент не исполнялся.
        return {'needs_clarification': True, 'question': review['question'], 'client_said': answer['data'].get('text', '')}
    from .actions.request import apply_request
    if review.get('selection') is not None:
        s.selection_scope_ids = [x['id'] for x in s.shown]
        s.selection_operation = review.get('selection_operation','rank')
    changes = deepcopy(review['changes'])
    has_unsupported = any(item['action']=='add' for item in review.get('unsupported') or [])
    if has_unsupported:
        # Неподдержанное условие нельзя молча подменить похожим проверяемым
        # (бассейн внутри квартиры -> фитнес рядом). Существующий профиль
        # сохраняем целиком; при первом входе принимаем только недостающие
        # базовые факты, чтобы после выбора альтернативы не спрашивать их заново.
        basic={'city','cities','rooms','rooms_options','budget_max','payment','housing'}
        changes={key:value for key,value in changes.items() if key in basic
                 and s.profile.get(key) in (None, [], '')}
    if review['decision'] == 'apply' and changes:
        result = apply_request(s, changes)
    else:
        result = None
    s.request_review_seq = review['seq']
    s.request_intent = review['intent']
    s.last_issue=None
    s.profile.pop('_last_issue',None)
    unsupported = list(s.profile.get('_unsupported_conditions') or [])
    additions = []
    for item in review.get('unsupported') or []:
        if item['action'] == 'remove':
            unsupported = [text for text in unsupported if text != item['text']]
        else:
            if item['text'] not in unsupported:
                unsupported.append(item['text'])
            if item['text'] not in additions:
                additions.append(item['text'])
    if review.get('unsupported'):
        s.profile['_unsupported_conditions'] = unsupported
    if additions:
        # Сохраняем явно названные поддержанные условия, но не выполняем
        # частичный поиск без решения клиента о неподдержанном пожелании.
        s.request_intent = 'hold'
        s.call_targets = []
        s.remember()
        explanation = ('Пока не умею надёжно проверять условие «' + '», «'.join(additions)
                       + '» по данным объявлений. Это не означает, что таких квартир нет. '
                       'Могу отобрать самые выгодные предложения по оценке и цене или самые просторные квартиры; '
                       'это условие останется непроверенным. '
                       'Прежние параметры подбора сохранены; новые дополнительные фильтры из этой реплики не применяю.')
        await s.say(explanation)
        answer = await s.ask('clarify','Как продолжить?',
                             'Какой вариант вам показать? Остальные условия сохранены.',
                             chips=['Самые выгодные','Самые просторные','Оставить подборку'])
        return {'needs_clarification':True,'question':explanation,
                'client_said':answer['data'].get('text','')}
    if review['intent'] not in ('seller_questions','question'):
        s.call_targets = []
    if review.get('selection') is not None:
        s.selection_query = deepcopy(review['selection'])
        s.selection_dirty = True
    elif review['intent'] in ('search','refine'):
        from .listing_query import default_query
        s.selection_query = {**(s.selection_query or default_query()),'mode':'rank','scope':'all'}
        s.selection_dirty = True
        s.selection_operation='rank'
    if review['intent'] == 'call' and review.get('call_ids') is not None:
        ids = review['call_ids'] or s.prefs.liked
        shown = {x['id'] for x in s.shown} | set(s.prefs.liked)
        s.call_targets = list(dict.fromkeys(i for i in ids if i in shown))
        if not s.call_targets:
            await s.say('Выберите квартиру для звонка: отметьте «Нравится» или укажите номер из текущей подборки.')
    s.request_inputs = [x for x in s.request_inputs if x['seq'] >= review['seq']]
    s.remember()
    return {'reviewed': True, 'profile_result': result}


async def prepare_from_http(s):
    """Проверка реплики при открытой карточке: ожидание человека не держит очередь модели."""
    turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
    s.signal('typing', on=True)
    try:
        return await turn.run(lambda: prepare(s), kind='llm', name='request_review')
    finally:
        s.signal('typing', on=False)
        turn.finish('input_review')
