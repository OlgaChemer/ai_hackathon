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

SYSTEM = """Разбери новый ввод покупателя квартиры в структурированные поля. Ты ничего не выполняешь.
Вход — messages с seq, прежний profile, каталог городов и единственная схема ответа input_schema.
Прочитай все сообщения вместе: более позднее уточнение отменяет/дополняет прежнее. Неназванное сохраняется.
Запиши ВСЕ явно названные условия, включая оплату, рынок, отрицания и альтернативы. Суммы — целые рубли.
Не оценивай доступность квартир: даже если бюджет кажется мал, сохрани названные комнаты и условия,
не подменяй их более дешёвым вариантом. Условные будущие изменения сохраняй в wishes, а не применяй заранее.
Сначала определи, какие части запроса действительно меняются. Город из profile сохраняется, если новый не назван.
Сначала выдели территории, затем остальные поля и каждое условие вместе с его обязательностью.
Центр Москвы — Москва и prefer_center=true; «в центре» обязательно: center_required=true,
«желательно ближе к центру» — center_required=false. Не выделяй центр как город.
Школа, садик, новостройка, срок сдачи не города: для них places=[].
places — итоговые территории, где клиент ХОЧЕТ искать. Не включай территории из отрицаний.
Перечисление с «или» сохраняет ВСЕ допустимые территории, не выбирай одну за клиента.
Пример «Москва или Подмосковье» -> два places: [{words:[{source:Москва,lemma:Москва}]},
{words:[{source:Подмосковье,lemma:Подмосковье}]}]. Область не заменяет город Москву.
В places.words выбери каждое исходное слово названия из enum source (сохрани его форму),
а в lemma приведи только его в начальную форму.
Одному исходному слову соответствует одно нормализованное слово. НЕ добавляй отсутствующие части имени.
ambiguous_catalog_names перечисляет общие части разных названий. Передай такую часть без достраивания:
код сам предложит уточнить, какой из соответствующих городов нужен. Полное позднейшее имя разрешает вопрос.
fields — объект изменений: каждое поле null (не названо) либо {value: типизированное значение, source: исходное слово/полное сообщение из enum}.
Не возвращай snapshot profile. Source выбирай из нового ввода, не из вопроса агента/профиля.
Пример: «Оставь три лучших» -> fields все null, places=[], criteria все null,
intent=selection, selection={scope:all,mode:rank,limit:3,filters:[],sort:[],aggregates:[],group_by:null}.
Число предложений — selection.limit, НЕ rooms/rooms_options. «4 самые большие» — четыре квартиры,
комнатность прежняя. «4, а не 3» после отбора трёх меняет limit=4, сохраняя предыдущую сортировку.
workflow.selection_query и recent_dialogue содержат контекст таких коротких исправлений.
«Эти не понравились, дай четыре самых больших по убыванию площади» — scope=all, limit=4, area desc.
Новый отбор/порядок по умолчанию работает по ВСЕМ найденным, не только по прежним трём.
scope=shown используй лишь при явной просьбе ограничиться именно показанными объектами.
Пример: «Школа рядом» -> fields все null, places=[], criteria.poi:school=required.
Пример: «Новостройка со сдачей в2027» -> fields.housing={value:new,source:Новостройка},
fields.filters={value:[delivery_year eq ['2027'] required],source:в2027}; остальные поля null.
Не дублируй обычные poi/бюджет/комнаты универсальными filters, когда для них есть точное базовое поле.
Запрет первого и последнего этажей запиши в criteria.not_first_floor и criteria.not_last_floor.
В filters сравниваются поле и КОНСТАНТЫ: нельзя передавать имена других полей, last, floors вместо числа.
Если готовый criteria точно выражает условие, не добавляй второй эквивалентный фильтр.
housing записывай только при явном выборе вторички, новостройки или обоих рынков.
Просьба предложить выбор типа жилья/карточку «Где искать» НЕ выбирает оба рынка: fields.housing=null.
Не заполняй timeline, маткапитал и прочие финансовые поля исходя из общего контекста: только со слов клиента.
Альтернативы комнат сохраняй полностью в rooms_options; перечисление не превращай в диапазон.
Неизвестное значение — null. Не добавляй параметров без слов клиента.
fields.filters — универсальные сравнения полей из каталога listing_fields; можно комбинировать несколько.
Сдача в 2027 -> delivery_year eq ['2027']; не позднее 2027 -> lte; 2027–2028 -> between.
Каждый фильтр содержит values: для eq/ne/lt/lte/gt/gte ровно одну строку, для between две,
для in непустой список, для exists пустой. Например: {field:delivery_year,op:eq,values:['2027'],priority:required}.
Если условие не менялось, не добавляй для него пустой фильтр. Все значения — строки, не числа.
НЕ используй year/year_min для срока сдачи: это год постройки. Убрать условие -> priority=remove.
Рядом поликлиника/сад/школа — criteria.poi. Не задавай время пешком по прямому расстоянию.
Если клиент задаёт только качественное «рядом», критерий poi с проверкой в 1км по прямой.
fields.seller_questions — только новые/сохранённые явно запрошенные вопросы продавцу, не разрешение звонка.
selection — запрос к найденным объявлениям: scope all/shown/liked, mode rank/more, limit, filters, sort, aggregates, group_by(null если нет).
Производные поля value_score/discount_pct используй только в selection.filters, не в fields.filters.
«Оставь три лучших» -> intent=selection, selection.limit=3, mode=rank, scope=all.
«Ещё» -> mode=more, scope=all. «Самые просторные» -> sort area desc; «дешевле» -> price asc.
Неограниченное количество/порядок не выдумывай: обычная порция limit=10, sort=[], filters=[], aggregates=[].
При коротком исправлении количества сохраняй остальную workflow.selection_query; новый полный отбор её заменяет.
«Выгодные» — сортировка value_score desc. Не добавляй неназванный фильтр value_score>0 или area>=0.
Можно одновременно изменить поля и задать selection. Новые постоянные условия — fields/criteria,
временные операции над выдачей — selection. Запрос подсчёта/сравнения квартир — selection, не FAQ.
call_ids — только явная просьба звонить (intent=call). Возьми ID из workflow.selection; [] означает по понравившимся.
«Что спросить продавца» и новые пожелания не разрешают звонить. Иначе call_ids=null.
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
refine — меняет фильтры, в том числе вопросом; selection — операции над выдачей; call — просьба звонить;
seller_questions — дополнить вопросы продавцу; search — начать поиск;
continue — просит продолжать прежнее; hold — явно просит пока не искать или остановиться; other — остальное.
При continue/other профиль не меняется: fields и criteria = null, places пуст.
При hold можно сохранить явно названные условия/вопросы, но selection=null и call_ids=null:
«Спроси про шумоизоляцию, пока не звони» -> intent=hold и fields.seller_questions={value:[вопросы],source:слово из ввода}.
Ответ с новым значением на уточнение (даже из одного слова) — refine, а не continue.
Обычный информационный вопрос не является ни подтверждением карточки, ни просьбой начать поиск.
unsupported — явно запрошенные условия ВНЕ каталога listing_fields и поддержанных criteria.
Например бассейн ВНУТРИ квартиры нельзя заменить nearby:fitness, парком или пожеланием в wishes:
unsupported=[{text:'внутри бассейн',source:точная цитата,action:add}], intent=refine.
При add text — буквальная цитата НОВОГО сообщения (например «внутри бассейн»), не пересказ и не память.
Если клиент выбрал предложенную альтернативу, unsupported=[]: прежнее непроверенное условие хранится кодом.
Не утверждай, что таких квартир нет: система пока не умеет это проверять.
Неподдержанный срок/географию/операцию также обозначай явно. Поддержанные поля извлеки как обычно,
но не притворяйся, что неподдержанное условие было выполнено. Без таких условий unsupported=[].
Явную отмену прежнего неподдержанного условия передай с action=remove и прежним text из workflow.
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
                         'liked':s.prefs.liked}}
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
    if review['decision'] == 'apply':
        result = apply_request(s, deepcopy(review['changes']))
    else:
        result = None
    s.request_review_seq = review['seq']
    s.request_intent = review['intent']
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
                       'это условие останется непроверенным.')
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
        s.profile['_selection_query'] = deepcopy(s.selection_query)
    elif review['intent'] in ('search','refine'):
        from .listing_query import default_query
        s.selection_query = {**(s.selection_query or default_query()),'mode':'rank','scope':'all'}
        s.selection_dirty = True
        s.profile['_selection_query'] = deepcopy(s.selection_query)
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
