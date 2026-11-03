"""Один структурированный разбор ввода. Отдельных критиков и самооценок уверенности нет."""
from __future__ import annotations

from copy import deepcopy

from ..llm import LLMError
from ..llm.base import StructuredOutputError
from ..runtime import Turn, record
from ..config import settings
from . import guard, validation, input_frame
from .request_schema import CHANGE_SCHEMA

SYSTEM = """Разбери новый ввод покупателя квартиры в структурированные поля. Ты ничего не выполняешь.
Вход — messages с seq, прежний profile, каталог городов и единственная схема ответа input_schema.
Прочитай все сообщения вместе: более позднее уточнение отменяет/дополняет прежнее. Неназванное сохраняется.
Запиши ВСЕ явно названные условия, включая оплату, рынок, отрицания и альтернативы. Суммы — целые рубли.
Не оценивай доступность квартир: даже если бюджет кажется мал, сохрани названные комнаты и условия,
не подменяй их более дешёвым вариантом. Условные будущие изменения сохраняй в wishes, а не применяй заранее.
Сначала выдели территории, затем остальные поля и каждое условие вместе с его обязательностью.
places — итоговые территории, где клиент ХОЧЕТ искать. Не включай территории из отрицаний.
В places.words выбери каждое исходное слово названия из enum source (сохрани его форму),
а в lemma приведи только его в начальную форму.
Одному исходному слову соответствует одно нормализованное слово. НЕ добавляй отсутствующие части имени.
ambiguous_catalog_names перечисляет общие части разных названий. Передай такую часть без достраивания:
код сам предложит уточнить, какой из соответствующих городов нужен. Полное позднейшее имя разрешает вопрос.
fields — объект обычных параметров. Не названное клиентом поле = null, а не прежнее значение.
Альтернативы комнат сохраняй полностью в rooms_options; перечисление не превращай в диапазон.
Неизвестное значение — null. Не добавляй параметров без слов клиента.
criteria — объект условий из input_schema. Не названный критерий = null.
Для удобств, инфраструктуры и запретов этажей: required — обязательно, preferred — желательно,
remove — снять условие, null — не менялся. Условие и его желательность — одно значение.
Для числовых условий, ремонта и материала: {value: значение, priority: required/preferred/remove}.
При снятии такого условия value=null. Если число не названо, не придумывай его: value=null,
качественное пожелание сохрани в wishes. Не теряй второе отрицание в одной фразе.
Остальные пожелания и их обязательность сохраняет код: не повторяй их в ответе.
question — один вопрос только при противоречии или неясном смысле. Известные параметры не переспрашивай.
options — варианты только к такому вопросу; иначе пустой массив. Не выбирай трактовку за клиента.
При коротком ответе учитывай его question и все ещё не разобранные сообщения вместе.
После разбора определи intent:
question — клиент спрашивает о термине/расчёте/уже показанной квартире, профиль не меняется, все списки пусты;
refine — просит поменять подбор, в том числе в вопросительной форме; search — хочет начать поиск;
continue — просит продолжать прежнее; hold — явно просит пока не искать или остановиться; other — остальное.
При continue/hold/other профиль не меняется: fields и criteria = null, places пуст.
Ответ с новым значением на уточнение (даже из одного слова) — refine, а не continue.
Обычный информационный вопрос не является ни подтверждением карточки, ни просьбой начать поиск.
Верни JSON строго по схеме. Не пиши процент уверенности и не выдавай разрешений на деньги или сделки.
"""

class UnderstandingError(LLMError):
    """Не удалось надёжно проверить сообщение; неподтверждённые изменения не применяются."""


def catalog_ambiguities(cities):
    """Индекс справочника, не разбор клиентского текста; новые города учитываются автоматически."""
    parts = {}
    for city in cities:
        for word in city.split():
            parts.setdefault(word, []).append(city)
    return {word: names for word, names in parts.items() if len(names) > 1}


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
             'workflow':{'has_search':s.search is not None,'has_selection':bool(s.shown),'has_calls':bool(s.calls),'chosen':bool(s.chosen)}}
    revision=s.client_revision
    error=None
    for attempt in range(2):
        instruction=SYSTEM+'\n'+guard.POLICY
        if error:instruction+='\nПредыдущий ответ не прошёл проверку формата: '+error+'. Исправь поля, смысл не угадывай.'
        messages=[{'role':'system','content':instruction},guard.data_message('request_review',payload)]
        try:
            result=await s.llm.chat_json(messages,tag='request_review',temperature=.01,max_tokens=2200,json_schema=contract)
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
    if review['decision'] == 'apply':
        result = apply_request(s, deepcopy(review['changes']))
    else:
        result = None
    s.request_review_seq = review['seq']
    s.request_intent = review['intent']
    s.request_inputs = [x for x in s.request_inputs if x['seq'] >= review['seq']]
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
