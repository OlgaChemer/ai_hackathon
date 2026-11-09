"""Один структурированный разбор ввода. Отдельных критиков и самооценок уверенности нет."""
from __future__ import annotations

from copy import deepcopy
import asyncio
import json

from ..llm import LLMError,Tool,ToolCall,LLMResponse,parse_json_loose
from ..llm.base import StructuredOutputError
from ..runtime import Turn, record
from ..config import settings
from . import guard, validation, input_frame
from .request_schema import CHANGE_SCHEMA
from .listing_query import FIELDS


PROFILE_SYSTEM='''Извлеки ВСЕ изменения условий квартиры из новых messages. Верни JSON по схеме, никаких действий не выполняй.
profile — прежние условия для сохранения неназванного; не копируй их в результат.
messages идут по времени: последнее уточнение заменяет прежнюю неоднозначность. «Новгород» затем «Великий Новгород, не Нижний» -> только Великий Новгород, без отдельного прежнего Новгорода.
reading_command — уже принятая команда отбора, если есть. Её количество и показатель НЕ условия квартиры.
«Три самых выгодных», «две большие», «4 а не3», «по моим параметрам» не задают новые условия: fields=[], criteria=[], places=[], unsupported=[].
Сортировка не создаёт filters: «3 самых выгодных за квадратный метр» -> filters=[]; это порядок price_per_m2 asc, а не порог цены метра. Не добавляй технические проверки положительности, наличия или валидности данных (>0, exists): их делает исполнитель, они не пожелания клиента. Порог извлекай только из явно названного ограничения.
Если рядом есть ЯВНОЕ новое условие («сдача2027, покажи три просторных»), извлеки delivery_year; число3 — только команда отбора.
fields — СПИСОК только изменённых фактов [{key,value,source: целая НОВАЯ реплика из enum source}]; неназванных полей в списке нет. Для fields/criteria/filters/states не выбирай отдельное случайное слово, цитируй целую новую реплику; places.words по-прежнему использует исходные слова названия.
value строкового поля — строка без дополнительных кавычек: cash/new. Для массива/числа/boolean — JSON в строке: [1,2], 30000000, true.
allowed_fields и allowed_criteria задают типы и коды значений. Если есть enum, верни код из enum, а не его перевод или описание.
criteria — СПИСОК [{key,priority:required/preferred/remove,value,source}]; пустой при отсутствии новых условий. Для poi/feature value=true; для remove пусто.
places — только явно названные допустимые города, source — исходное слово, lemma — его начальная форма. Не достраивай имя: Нижнего -> Нижний. Центр/школа/новостройка не города.
Москва или Подмосковье — обе территории. При отмене Москвы и области не сохраняй их как альтернативы.
rooms_options — все допустимые числа комнат 1–5: однушка или двушка -> [1,2]; 1 или3 -> [1,3].
Студия только при явной просьбе: studio_requested=true. Явный запрет студии false, иначе не добавляй этот ключ. 1–2 НЕ включает студию.
Суммы в рублях; «пять с половиной лямов» 5500000. Наличка/за свои/без ипотеки -> payment cash.
Вторичка -> housing secondary; новостройка -> new; оба -> both. Не пропускай названный рынок и оплату.
«В центре» -> prefer_center true и center_required true; «желательно ближе» -> true/false.
Спальный район -> fields district_type=sleeping; тихий район/тихое место -> quiet; престижный/элитный район -> prestige; деловой район/бизнес-район/рядом с офисами -> business. Это поддержанное условие, не unsupported. Отмена — clear_criteria district_type.
Не выдумывай обязательность мягких условий. Парк желателен -> poi:park preferred; не первый/последний этаж -> required.
Площадь/кухня/год постройки — criteria. Произвольные проверяемые ограничения — верхний список filters.
Для одного свойства используй ОДНО представление: именованный criterion ИЛИ filters, не оба.
При отмене свойства снимай его существующее представление из profile: filters с priority remove, values []; именованный criterion с priority remove.
Отмени ограничение площади -> criteria [{key:area_min,priority:remove,value:"",source:"Отмени ограничение площади"}]. Не оставляй прежнюю area_min. Для отмены именованного поля можно также fields с action:remove; null означает удаление, не отсутствие изменения.
Год СДАЧИ: filters delivery_year eq ['2027']; не позднее: lte; интервал: between. НЕ year_min. Неназванные фильтры не повторяй.
Готовые критерии инфраструктуры: poi:school/clinic/kindergarten/...; не дублируй их в filters.
Школа до1км пешком — poi:school required, учитываем близость; не обещай маршруты. Радиус по прямой проверяет код.
Вопросы продавцу — fields.seller_questions, не разрешение звонка. Сохрани прежние вопросы, добавь новые.
selection — [] если команды нет, либо массив с одним запросом: rank, all по умолчанию, limit N (0 сохранить), sort [{field,direction}], aggregates [], group_by пустая строка.
Цена метра: price_per_m2 asc; самые большие: area desc; выгодные: value_score desc. selection не содержит порогов.
При одном изменении условия selection=[]. При отмене площади и «покажи две большие» передай И отмену, И selection rank2 area desc.
Несколько разрешённых комнатностей не требуют выбора ОДНОЙ: сохрани все варианты.
Не знаю/помоги определить поле — states [{key:имя,state:unknown,source:цитата}]. Любой/неважно/сними ограничение — state=unrestricted. Поддержаны budget_max, rooms, districts, okrug, housing. Это не новое конкретное значение; неназванное сохраняется. Если вместе с вопросом дан конкретный бюджет, сохрани сумму, не помечай его unknown.
Явное «бюджет неважен» обязательно запиши: states [{key:budget_max,state:unrestricted,source:'бюджет неважен'}]. «Комнатность не знаю» -> key=rooms,state=unknown. Несколько статусов — несколько элементов states. Это поддерживаемые данные, не unsupported и не отсутствие изменений; не записывай вложенный JSON статусов строкой в fields.
Не определил / ещё не решил / пока не знаю — unknown, а не unrestricted. Временное отсутствие решения не означает, что свойство неважно. Оба статуса допускают исследование без фильтра, но имеют разный смысл для следующего ответа клиенту.
Строгость числовых сравнений сохраняй: выше/больше/более/позже -> gt; ниже/меньше/раньше -> lt. «Не ниже/не менее/от» -> gte; «не выше/не более» -> lte. «Выше 10» исключает 10, это не gte.
Числовые и прочие универсальные сравнения передавай в верхнем filters: [{field:floor,op:gt,values:["10"],priority:required,source:цитата}]. Не записывай JSON фильтров строкой в fields. states и filters — типизированные данные, остальные поля по-прежнему в fields/criteria.
pending_decisions и recent_dialogue поясняют, к какому свойству относится краткий ответ («Любой», «Заехать сразу»). Значение толкуй с этим контекстом, но source всегда буквально из НОВОГО messages. Снятие запрета этажа — criteria not_first_floor/remove и not_last_floor/remove, общий filters floor/remove. Не снимай другие свойства из-за слова «любой».
Название улицы без указания, какой именно, не считается центром города: сохрани буквальное неподдержанное условие в unsupported. Частичное имя города не достраивай, код уточнит его по каталогу.
unsupported — только НОВОЕ условие вне доступных свойств, с буквальной цитатой; бассейн внутри квартиры не равен фитнесу рядом.
Не подбирай ближайшее по смыслу свойство вместо отсутствующего: удобство ВНУТРИ квартиры и расстояние до внешнего объекта — разные условия. Не кодируй внутренний бассейн как nearby:fitness=0. Если точного свойства нет, запиши неизвестный ключ либо unsupported, не придумывай расстояние.
При явной отмене неподдержанного условия: withdraw_unsupported [{text: точный вариант из enum сохранённых пожеланий, source: целая новая реплика}]. Не перефразируй прежнее условие. unsupported оставляй только для новых добавлений. Совмести отмену с selection, если просят отбор.
unsupported_properties связывает ключ неизвестного свойства с его прежним названием. Его можно удалить по этому ключу, как обычное свойство.
Пример: 1–2 комнаты, вторичка, за свои -> fields [{key:rooms_options,value:"[1,2]",source:"1–2"},{key:housing,value:"secondary",source:"вторичка"},{key:payment,value:"cash",source:"за свои"}].
requested_card — только ПРЯМАЯ просьба показать анкетную карточку: «сначала предложите выбор типа жилья через карточку Где искать» -> [ask_housing]. Это НЕ выбор рынка: housing НЕ добавляй, если клиент не назвал значение. Если клиент просто не назвал рынок, []. Это не карточки квартир.
Верни JSON со списками фактов. Неназванное не добавляй. Не заполняй семью, маткапитал, срок и другие параметры предположениями.
'''

ROUTER_SYSTEM='''Определи смысл ТОЛЬКО новых messages одним вызовом смысловой функции. Ничего не выполняй.
profile и recent_dialogue — контекст; не источник новых условий.
unsupported_request — новое пожелание, для которого нет точного свойства в listing_fields/критериях: например удобство внутри квартиры, а не объект рядом. Передай исходную реплику quote дословно. Не подменяй такое пожелание nearby и не выдумывай радиус. Для составного ввода с новыми городом/комнатами/бюджетом используй profile_change и unsupported в разборе.
profile_change — любые НОВЫЕ/отменённые условия квартиры, город, комнаты, рынок, оплата, вопросы продавцу.
Включая составную просьбу: «отмени площадь и покажи две большие», «сдача в2027 и три просторных».
Отмена неподдержанного условия вместе с отбором тоже profile_change.
Параметры profile_change извлечёт следующий шаг. Не пытайся передавать их этой функции.
read_selection — команда отбора/сортировки текущего или предстоящего подбора. Количество и порядок сохраняются и до первого поиска. Сравнение показанных относится только к реально показанным объектам. update_conditions=false для обычного отбора/сортировки/сравнения.
Если вместе с отбором ДОБАВЛЯЮТ/СНИМАЮТ условие, update_conditions=true: «сдача2027 и три просторных», «отмени площадь, покажи две большие».
«По моим параметрам», «учитывая прежние условия» означают СОХРАНИТЬ профиль: update_conditions=false.
«Ищу1–2 комнаты» — параметры квартиры, не количество карточек: profile_change.
«В центре Москвы» — параметры; «школы рядом» — параметры; школа/центр/новостройка НЕ города.
Если есть только новые условия без команды выдачи, выбери profile_change.
informational_question — ОБЪЯСНИТЬ смысл показателя, устройство, предложение или предыдущую ошибку: «что означает цена по аналогам», «что означает непроверенное пожелание», «что сломалось/что надо уточнить». У этой функции нет аргументов, она не запускает расчёты.
request_information — ПОЛУЧИТЬ новые данные или выполнить расчёт. Выбери action: средние/типичные цены, «не знаю бюджет, какой средний?» -> market_prices; расчёт бюджета по доходам -> affordable_budget; расчёт ипотеки -> mortgage_programs; накопления -> savings_plan.
Исследование объявлений не требует заранее выбранных комнат или бюджета. «Бюджет 20 млн, на сколько комнат хватит?» -> market_prices, update_conditions=true, research respect_budget=true, group_by=rooms, aggregates по price min/median. «Какая площадь доступна за эту сумму?» -> respect_budget=true, group_by=null, aggregates area min/max/median. Средняя цена без бюджетного ограничения -> respect_budget=false. Это запрос данных, не повод просить выбрать исследуемое свойство. Если нового бюджета/параметров нет, update_conditions=false.
«Не знаю, какой средний?» спрашивает ЦЕНУ КВАРТИР: market_prices, даже если сейчас открыта форма финансов. affordable_budget считает, сколько МОЖЕТ ПОЗВОЛИТЬ СЕБЕ конкретный человек, не среднюю рыночную цену. Для него нужна просьба оценить личные доходы/расходы/накопления или явное согласие на такой расчёт. Не выбирай его из-за одного отсутствующего бюджета.
Если в таком вопросе есть новые условия (город, комнаты, рынок и т.п.), выбери action и update_conditions=true: эти условия извлечёт отдельный шаг. Не теряй параметры составного вопроса.
Короткое «да/покажи/давай» относится к последнему предложению в recent_dialogue. Если агент предложил показать цены/сделать расчёт — это request_information с соответствующим action. Не придумывай бюджет или согласие на звонок/сделку из такого ответа.
Для чтения цен отдельное разрешение не нужно. Если спрашивают о смысле показателя и расчёт не требуется — informational_question без аргументов. Прежняя карточка бюджета не запрещает получить ориентиры.
Статистика «по этим/найденным квартирам» выполняется read_selection analyze/compare, а не market_prices: область текущей подборки сохраняется.
call_intent — только ЯВНО позвонить; listing_ids из показанных ID; [] — по понравившимся.
Вопросы продавцу/условия не разрешают звонок. pause_request — явная пауза/пока не звони, даже с новыми вопросами.
Новое неизвестное свойство без других изменений («внутри бассейн/сауна») — unsupported_request. Составной ввод с базовыми параметрами — profile_change с явным unsupported. Не подменяй удобство внутри квартиры фитнесом рядом.
Неоднозначный город/улица — profile_change: условия извлечёт отдельный шаг, уточнение сформирует код. Диапазон комнат 1–2 — обе комнатности, не вопрос выбора одной.
continue_input — продолжить без изменений; other_input — остальное.
Чтение: operation rank — команда изменить карточки (выбрать N/сортировать), compare — вопрос, какой объект лучше по показателю, analyze — статистика, more — ЕЩЁ/НОВЫЕ.
Вопрос о победителе («какая самая просторная», «у какой ниже цена») — compare даже без слова «сравни». Ответь по данным, сохрани карточки. Не наследуй operation=rank и limit из предыдущей команды показа.
scope all по умолчанию для нового отбора; shown — при отсылке к текущим карточкам («из этих/показанных/из них»); liked — по понравившимся.
«Понравившиеся/избранные отсортируй» -> scope liked, даже если liked сейчас пустой. Не расширяй область до all молча: исполнитель объяснит отсутствие отмеченных. Указанная область важнее значения по умолчанию.
«Среди этих двух какая выгоднее за кв м» -> compare/shown/price_per_m2/asc, limit=null. Двух — группа, не порог цены и не новый размер карточек.
«Где из этих двух больше площадь?» -> compare/shown/area/desc.
order_by: больше/просторнее -> area; дешевле -> price; выгоднее метр -> price_per_m2; просто выгоднее -> value_score.
direction: asc дешевле/меньше, desc больше/выше рейтинг.
«Эти не понравились, дай четыре самых больших» -> rank/all/limit4/area/desc.
keep_order=true ТОЛЬКО если меняют одно количество без метрики/явно сохраняют порядок. Иначе false.
«4 а не3» -> rank/all/limit4, count_source:4, keep_order:true. Не брать прежний размер выдачи из контекста.
count_source — буквальная цитата НОВОГО ввода с запрошенным limit. Неназванное количество -> limit=null, count_source=null.
«Подбери новые с учётом отметок» -> more/all, limit=null. Только more исключает уже показанные.
analyze: средняя цена метра -> aggregates [{field:price_per_m2,function:mean}], scope shown при «этих».
Вызови одну смысловую функцию по её схеме. Это данные намерения, не согласия на звонки/деньги/сделку.
'''

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
    if isinstance(exc,UnderstandingError) and 'город' in str(exc).casefold():
        text=('Не смог понять, какой город вы имеете в виду. Напишите его полное название, например «Уфа» или «Казань». '
              'Остальные условия сохранены.')
    if not isinstance(exc,UnderstandingError):
        text=('Сервис обработки сообщения сейчас не ответил. Условия и последняя подборка сохранены; '
              'можно повторить сообщение позже. Это не просьба изменить ваши условия.')
    s.last_issue={'kind':'model_format' if isinstance(exc,UnderstandingError) else 'upstream_unavailable','message':text}
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
        if s.durable:
            from .. import db
            from ..db import repo
            saved=db.required(repo.recent_messages,s.id,12)
            if saved:return saved
        # После перезапуска история событий ещё не загружена в Session,
        # но сохранённый разговор уже есть. Он нужен для коротких исправлений.
        memory=[{'role':'agent' if m['role']=='assistant' else 'user','text':m['content']}
                for m in s.messages if m.get('role') in ('user','assistant') and m.get('content')]
        rows=memory+[row for row in rows if not memory or row!=memory[-1]]
    return rows[-8:]


def semantic_tools(contract,inputs,unsupported,has_results=True):
    from .capabilities import POLICIES
    information_actions=[name for name,policy in POLICIES.items() if policy.information]
    from .selection_request import SCHEMA
    from .research import SCHEMA as RESEARCH_SCHEMA
    def obj(props,required=()):return {'type':'object','additionalProperties':False,'properties':props,'required':list(required)}
    quotes=list(dict.fromkeys([*input_frame.source_words(inputs),*(row['text'] for row in inputs),*unsupported]))
    read_schema=deepcopy(SCHEMA)
    read_schema['properties'].pop('sort')
    read_schema['required'].remove('sort')
    read_schema['properties']['order_by']={'type':'string','enum':list(FIELDS),
        'description':'Показатель: площадь/больше -> area; дешевле -> price; дешевле метр -> price_per_m2; выгодность без уточнения -> value_score. При keep_order:true игнорируется.'}
    read_schema['properties']['direction']={'type':'string','enum':['asc','desc'],
        'description':'asc — от меньшего к большему (дешевле); desc — от большего к меньшему (больше площадь, выше выгодность).'}
    read_schema['required']+=['order_by','direction']
    read_schema['properties']['keep_order']={'type':'boolean','description':'true только если клиент меняет ОДНО количество без новой метрики («4 а не 3») или явно сохраняет порядок. При словах выгоднее/дешевле/больше/новом показателе false.'}
    read_schema['required'].append('keep_order')
    read_schema['properties']['update_conditions']={'type':'boolean',
        'description':'true если реплика ДОБАВЛЯЕТ/СНИМАЕТ условие квартиры вместе с отбором (сдача2027, площадь>=80, отмена площади). false для одного количества/сортировки, «по моим параметрам» и «4 а не3».'}
    read_schema['required'].append('update_conditions')
    read_schema['properties']['count_source']={'type':['string','null'],
        'maxLength':1000,
        'description':'Буквальная цитата НОВОЙ реплики, откуда взято количество limit. При limit=null — null. «4 а не 3» -> limit4, count_source:4. Не цитируй прежнюю выдачу.'}
    read_schema['required'].append('count_source')
    global_words={'все','всех','весь','вся','всю','всей','всего','всем','целиком','полностью','пула','пулу','пуле','базы'}
    global_quotes=[row['text'] for row in inputs if any(word.casefold().replace('ё','е') in global_words
                    for word in input_frame.source_words([row]))]
    read_schema['properties']['scope_source']={'type':['string','null'],'enum':[None,*global_quotes],
        'description':'Только для compare/all: исходная просьба сравнивать весь найденный пул. Без прямой просьбы шире показанных используй compare/shown. Для rank/analyze не требуется.'}
    tools=[
        Tool('read_selection','Задать отбор N и порядок текущего или предстоящего подбора, сравнить показанные, посчитать статистику или получить ещё варианты. Команда количества/сортировки допустима и до первого поиска. Условия профиля не меняет. Новое количество только из новой реплики, при отсутствии limit=null.',read_schema),
        Tool('profile_change','Клиент меняет условия квартиры/города/бюджета, снимает ограничение либо добавляет вопросы продавцу. Включая составную просьбу изменить условия и отобрать N. Параметры извлечёт отдельный структурированный разбор. Для одного лишь отбора без изменения условий — read_selection.',obj({})),
        Tool('unsupported_request','Новое пожелание вне проверяемых свойств: удобство внутри квартиры не заменяется внешней инфраструктурой. Это явный запрос клиента, а не ошибка формата и не неизвестные данные объявления. Только распознать ограничение возможности, ничего не исполнять.',obj({
            'quote':{'type':'string','minLength':1,'maxLength':1000,
                     'description':'Дословная новая реплика, содержащая неподдерживаемое пожелание.'}},['quote'])),
        Tool('informational_question','Объяснить смысл, устройство, показатель, прежнюю ошибку. «Что означает цена по аналогам/непроверенное пожелание?» — объяснение, без новой оценки рынка. Не изменяет условия и не выбирает инструмент.',obj({})),
        Tool('request_information','Получить данные или выполнить расчёт. Включая вопрос «на сколько комнат хватит бюджета»: исследовать объявления, respect_budget=true, group_by=rooms. Не требует заранее выбрать неизвестное свойство. Использует существующий инструмент.',obj({
            'action':{'type':'string','enum':information_actions,
                      'description':'market_prices — исследовать объявления: цены, доступную комнатность/площадь/этаж по известным условиям; affordable_budget — личная платёжеспособность по доходам/накоплениям; mortgage_programs — ипотека; savings_plan — накопления.'},
            'research':RESEARCH_SCHEMA,
            'update_conditions':{'type':'boolean','description':'true — вместе с вопросом названы/изменены условия. Отдельный шаг проверит их перед исполнением.'}},['action','research','update_conditions'])),
        Tool('call_intent','Клиент явно просит звонить. Сам ничего не исполняет. Без явной просьбы запрещён; для вопросов продавцу используй profile_change.',obj({
            'listing_ids':{'type':'array','maxItems':10,'items':{'type':'string'},'description':'ID показанных объектов; пусто — по понравившимся'}},['listing_ids'])),
        Tool('pause_request','Клиент просит остановиться/пока не искать/пока не звонить. Можно сохранить явно добавленные вопросы и условия без действий.',obj({})),
        Tool('continue_input','Клиент просит продолжить без изменения условий.',obj({})),
        Tool('other_input','Разговорная или неясная реплика без изменения параметров и команды. Ответить с контекстом текущего вопроса/ошибки, не возобновлять бизнес-действия.',obj({})),
    ]
    return tools


def frame_from_call(call,tools,inputs):
    tool=next((tool for tool in tools if tool.name==call.name),None)
    if tool is None:raise ValueError('Неизвестная смысловая функция')
    validation.validate(call.arguments,tool.parameters)
    a=deepcopy(call.arguments)
    if call.name=='read_selection':
        scope_source=a.pop('scope_source',None)
        if a['operation']=='compare' and a['scope']=='all' and not scope_source:
            raise ValueError('compare/all требует прямой просьбы сравнивать весь найденный пул. Отсылка к текущим карточкам — compare/shown, без расширения области.')
        if a['operation']=='compare':
            # Сравниваются объекты; группировка имеет смысл только для analyze.
            a['group_by']=None
        quote=a.pop('count_source')
        keep=a.pop('keep_order')
        update=a.pop('update_conditions')
        field,direction=a.pop('order_by'),a.pop('direction')
        a['sort']=None if keep else [{'field':field,'direction':direction}]
        if a['limit'] is not None:
            if not quote or not any(quote.casefold() in row['text'].casefold() for row in inputs):
                # Отсутствие вспомогательной цитаты не ломает корректный
                # отбор: проверяем число по самой новой реплике целиком.
                quote=inputs[-1]['text']
            numbers=[int(word) for word in input_frame.source_words([{'text':quote}]) if word.isdecimal()]
            if numbers and a['limit'] not in numbers:
                raise ValueError('Количество limit не соответствует числу в count_source. Возьми запрошенное новое количество, не прежний размер выдачи.')
        return {'intent':'refine' if update else 'selection','selection':a,**({'needs_extraction':True} if update else {})}
    if call.name=='profile_change':return {'intent':'refine','needs_extraction':True}
    if call.name=='unsupported_request':
        if not any(a['quote'].casefold() in row['text'].casefold() for row in inputs):
            raise ValueError('Неподдержанное пожелание требует дословной цитаты нового ввода')
        return {'intent':'refine','unsupported':[{'text':a['quote'][:200],'source':a['quote'],'action':'add'}]}
    if call.name=='pause_request':return {'intent':'hold','needs_extraction':True}
    if call.name=='informational_question':return {'intent':'question'}
    if call.name=='request_information':
        from .research import query
        research=query(a.get('research'))[0]
        return {'intent':'refine' if a.get('update_conditions') else 'question',**({'requested_action':a['action']} if a.get('action') else {}),
                'research':research,
                **({'needs_extraction':True} if a.get('update_conditions') else {})}
    if call.name=='call_intent':return {'intent':'call','call_ids':a['listing_ids']}
    return {'intent':'continue' if call.name=='continue_input' else 'question'}


async def prepare(s):
    import asyncio
    seq=s.request_inputs[-1]['seq'] if s.request_inputs else s.request_review_seq
    if s.review_task is not None and not s.review_task.done() and s.review_task_seq==seq:
        return await asyncio.shield(s.review_task)
    s.review_task_seq=seq
    s.review_task=asyncio.create_task(_prepare(s))
    return await asyncio.shield(s.review_task)


async def _prepare(s):
    """Один принятый разбор на сообщение; предложения инструментов не служат источником пожеланий."""
    inputs=[x for x in s.request_inputs if x['seq'] > s.request_review_seq]
    if not inputs:return None
    version=inputs[-1]['seq']
    if s.request_review and s.request_review['seq']==version and s.request_review['base_profile']==s.profile:
        return s.request_review
    from .. import sources
    base_profile=deepcopy(s.profile)
    from .clarification import choice
    answered=choice(s,inputs)
    if answered is not None:
        validation.validate(answered,CHANGE_SCHEMA)
        s.request_review={'seq':version,'base_profile':base_profile,'intent':'refine',
            'decision':'apply' if answered else 'unchanged','changes':answered,'question':'','options':[]}
        record('request_review',session=s.id,decision=s.request_review['decision'],intent='refine',
               changed_keys=list(answered),source='bound_card_choice')
        return s.request_review
    from .clarification import confirm_city
    confirmed=confirm_city(s,inputs,base_profile)
    if confirmed:
        validation.validate(confirmed,CHANGE_SCHEMA)
        s.request_review={'seq':version,'base_profile':base_profile,'intent':'refine' if (s.search or s.shown) else 'search',
                          'decision':'apply','changes':confirmed,'question':'','options':[],'selection':None,'call_ids':None,
                          'unsupported':[]}
        record('request_review',session=s.id,decision='apply',intent=s.request_review['intent'],
               changed_keys=list(confirmed),source='city_confirmation')
        return s.request_review
    fast_revision=s.client_revision
    fast=await asyncio.to_thread(_rule_parse,s,inputs,base_profile)
    if s.request_inputs[-1]['seq']!=version or s.client_revision!=fast_revision or s.profile!=base_profile:
        from .session import Superseded
        raise Superseded()
    if fast is not None and settings.rule_parser=='on':
        # Простое сообщение («двушка в Казани до 9 млн») разобрано кодом — без двух запросов к модели.
        s.request_review={'seq':version,'base_profile':base_profile,'intent':'refine' if (s.search or s.shown) else 'search',
                          'decision':'apply','changes':fast,'question':'','options':[],'selection':None,'call_ids':None,
                          'unsupported':[]}
        record('rule_parser',session=s.id,outcome='accepted',changed_keys=list(fast))
        record('request_review',session=s.id,decision='apply',intent=s.request_review['intent'],fields=len(fast),
               changed_keys=list(fast),source='rules')
        return s.request_review
    contract=input_frame.sparse_schema(CHANGE_SCHEMA, inputs)
    tools=semantic_tools(contract,inputs,s.profile.get('_unsupported_conditions') or [],bool(s.shown or s.search))
    extraction=input_frame.schema(CHANGE_SCHEMA,inputs)
    omitted=('intent','call_ids','question','options')
    for key in omitted:extraction['properties'].pop(key)
    extraction['required']=[k for k in extraction['required'] if k not in omitted]
    if extraction['properties']['places']['maxItems']==0:
        extraction['properties']['places']['items']['properties']['words']['items']['properties']['source'].pop('enum',None)
    from . import profile_patch
    extraction=profile_patch.schema(extraction)
    payload={'supported_cities':sources.SUPPORTED_CITIES,'ambiguous_catalog_names':catalog_ambiguities(sources.SUPPORTED_CITIES),
             'messages':inputs,'profile':{k:v for k,v in base_profile.items() if not k.startswith('_')},
             'listing_fields':{key: {'type':v[0],'label':v[1]} for key,v in FIELDS.items()},
             'recent_dialogue':dialogue_context(s),
             'pending_decisions':[{'kind':t['kind'],'text':t['text'],'options':t.get('payload',{}).get('chips',[])}
                                  for t in s.pending_decisions.values() if t.get('status')=='waiting'],
             'workflow':{'has_search':s.search is not None,'has_selection':bool(s.shown),'has_calls':bool(s.calls),'chosen':bool(s.chosen),
                         'selection_query':s.selection_query or s.profile.get('_selection_query'),
                         'unsupported_conditions':s.profile.get('_unsupported_conditions') or [],
                         'selection':[{'id':x['id'],'number':i+1,'address':x.get('address')} for i,x in enumerate(s.shown)
                                      if not s.selection_scope_ids or x['id'] in s.selection_scope_ids],
                         'liked':s.prefs.liked,'last_issue':s.last_issue}}
    revision=s.client_revision
    # Буквальный выбор из открытой уточняющей карточки уже имеет адресата.
    # Не просим роутер заново угадывать, является ли «Любой» продолжением.
    waiting=s.pending_decisions.get('clarify') or {}
    card_answer=((s.active_operation or {}).get('name')=='ask_client' and waiting.get('status')=='waiting'
                 and inputs[-1]['text'].strip().casefold() in
                 {str(option).strip().casefold() for option in waiting.get('payload',{}).get('chips') or []})
    error=None
    for attempt in range(2):
        instruction=ROUTER_SYSTEM+'\n'+guard.POLICY
        if error:instruction+='\nПредыдущий ответ не прошёл проверку формата: '+error+'. Исправь поля, смысл не угадывай.'
        messages=[{'role':'system','content':instruction},guard.data_message('request_review',payload)]
        try:
            requested_action=None
            condition_sources={}
            original_intent=None
            response=(LLMResponse('',[ToolCall('profile_change',{})]) if card_answer else
                      await s.llm.chat(messages,tools=tools,tag='request_review',temperature=.01,max_tokens=2500))
            if response.tool_calls:
                if len(response.tool_calls)!=1:raise ValueError('Ожидался один смысловой результат')
                result=frame_from_call(response.tool_calls[0],tools,inputs)
                original_intent=result['intent']
                result.pop('needs_extraction',None)
                rank=(result.get('selection') or {}).get('operation')=='rank'
                # Маршрутизатор может выбрать отбор и пропустить новые условия
                # в той же фразе. Каждый новый ввод проверяется независимо от
                # предложенного update_conditions; это один раз на новый ввод.
                data={k:payload[k] for k in ('messages','profile','supported_cities','ambiguous_catalog_names','listing_fields','pending_decisions')}
                data['unsupported_conditions']=s.profile.get('_unsupported_conditions') or []
                data['unsupported_properties']=s.profile.get('_unsupported_keys') or {}
                data['allowed_fields']=input_frame.basic_fields(CHANGE_SCHEMA)
                from .criteria import CRITERIA_KEYS
                data['allowed_criteria']={key:input_frame.criterion_value(key,CHANGE_SCHEMA) for key in CRITERIA_KEYS}
                contract_for_changes=deepcopy(extraction)
                unsupported_route=response.tool_calls[0].name=='unsupported_request'
                if unsupported_route:
                    # Неподдержанное удобство нельзя переинтерпретировать как
                    # разрешённую инфраструктуру. До решения клиента можно
                    # сохранить лишь ещё неизвестные базовые параметры.
                    basics={'city','cities','rooms_options','studio_requested','budget_max','payment','housing'}
                    if base_profile.get('city'):basics-={'city','cities'}
                    if base_profile.get('rooms') is not None:basics-={'rooms_options','studio_requested'}
                    basics-={key for key in ('budget_max','payment','housing') if base_profile.get(key) is not None}
                    for group in ('criteria','filters','states','selection','requested_card'):
                        contract_for_changes['properties'][group]['maxItems']=0
                    if not basics:contract_for_changes['properties']['fields']['maxItems']=0
                    else:contract_for_changes['properties']['fields']['items']['properties']['key']['enum']=sorted(basics)
                    if 'city' not in basics:contract_for_changes['properties']['places']['maxItems']=0
                if response.tool_calls[0].name!='profile_change':
                    if result.get('selection') is not None:data['reading_command']=result['selection']
                    elif response.tool_calls[0].name=='call_intent':data['message_purpose']='Просьба звонить. Номер квартиры — ссылка на объект, не количество карточек или комнат. Извлеки только явно новые условия; selection запрещён.'
                    contract_for_changes['properties'].pop('selection')
                    contract_for_changes['required'].remove('selection')
                prompt=PROFILE_SYSTEM+'\n'+guard.POLICY
                if error:prompt+='\nОшибка прежнего разбора: '+error
                strict_schema=profile_patch.generation_schema(contract_for_changes,inputs,s.profile.get('_unsupported_conditions'))
                data['output_schema']=strict_schema
                from .clarification import INFORMATION_ACKS
                information_ack=(response.tool_calls[0].name=='request_information' and len(inputs)==1
                                 and inputs[0]['text'].strip().casefold() in INFORMATION_ACKS)
                read_only=(response.tool_calls[0].name in ('informational_question','other_input') or
                           (response.tool_calls[0].name=='read_selection' and result['intent']=='selection'
                            and (result.get('selection') or {}).get('operation') in ('compare','analyze','more')))
                no_facts=read_only or (unsupported_route and not basics)
                parsed=(LLMResponse(json.dumps({key:[] for key in strict_schema['properties']})) if information_ack or no_facts else
                        await s.llm.chat([{'role':'system','content':prompt},guard.data_message('profile_changes',data)],
                            json_mode=True,json_schema=strict_schema,tag='profile_extract',temperature=.01,max_tokens=3000))
                changes=parse_json_loose(parsed.text)
                changes=profile_patch.decode(changes,contract_for_changes,inputs,s.profile.get('_unsupported_keys'),provenance=condition_sources,profile=base_profile,
                                             reading=result.get('selection') if rank else None)
                requested_action=changes.pop('_requested_action',None)
                pure_read=(result.get('selection') or {}).get('operation') in ('compare','analyze','more')
                if not pure_read and result['intent'] not in ('call','hold') and (any(v is not None for v in (changes.get('fields') or {}).values()) or
                             any(v is not None for v in (changes.get('criteria') or {}).values()) or
                             changes.get('places') or changes.get('unsupported')):
                    result['intent']='refine'
                result={**changes,**result}
            else:result=parse_json_loose(response.text)
            research_request=result.pop('research',None)
            information_action=result.pop('requested_action',None)
            if information_action:
                from .capabilities import POLICIES
                if information_action not in POLICIES or not POLICIES[information_action].information:
                    raise ValueError('Информационный запрос не разрешает это действие')
            requested_action=information_action or requested_action
            result=input_frame.complete_frame(result,CHANGE_SCHEMA)
            result = input_frame.decode(result, payload['profile'], payload, CHANGE_SCHEMA)
            if (original_intent is not None and not result['changes'] and not result.get('unsupported')
                    and result['decision']!='clarify'):
                # Повтор уже известного факта не превращает информационный
                # вопрос в изменение профиля и ответ на ожидающую карточку.
                result['intent']=original_intent
            if requested_action:result['requested_action']=requested_action
            if research_request is not None:result['research']=research_request
            if condition_sources:result['condition_sources']=condition_sources
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
            from .session import Superseded
            raise Superseded()
        s.request_review={'seq':version,'base_profile':base_profile,**result}
        if fast is not None:_shadow_compare(s,fast,result)
        query=result.get('selection') or {}
        record('request_review',session=s.id,decision=result['decision'],intent=result['intent'],
               fields=len(result['changes']),changed_keys=list(result['changes']),
               operation=result.get('selection_operation'),scope=query.get('scope'),
               ordering=query.get('sort'),count=query.get('limit'),requested_action=result.get('requested_action'),repaired=bool(attempt))
        return s.request_review
    raise UnderstandingError('Не удалось получить корректный структурированный ввод: '+str(error))


def _rule_parse(s,inputs,base_profile):
    """Разбор кодом одного нового сообщения без открытых карточек; None — пусть разбирает модель."""
    if settings.rule_parser not in ('shadow','on') or len(inputs)!=1:
        return None
    if any(t.get('status')=='waiting' for t in s.pending_decisions.values()) or s.profile.get('_unsupported_conditions'):
        return None
    from . import rule_parser
    try:
        fast=rule_parser.parse(inputs[0]['text'],base_profile)
        if fast is None:
            record('rule_parser',session=s.id,outcome='declined')
            return None
        validation.validate(fast,CHANGE_SCHEMA)
        if not guard.input_verdict(fast).ok:
            return None
        return fast
    except Exception as exc:  # noqa: BLE001 — разбор кодом необязателен: при ошибке работает модель
        record('rule_parser',session=s.id,outcome='error',problem=type(exc).__name__)
        return None


def _shadow_compare(s,fast,result):
    """Теневой режим: совпал ли разбор кодом с моделью. Совпадение — те же значения и никаких лишних изменений."""
    empty=lambda v:v in (None,[],'',False)  # noqa: E731
    model={k:v for k,v in (result.get('changes') or {}).items() if not empty(v)}
    fast={k:v for k,v in fast.items() if not empty(v)}
    if 'rooms' in model and model.get('rooms_options',[model['rooms']])[:1]==[model['rooms']] and 'rooms' not in fast:
        model.pop('rooms')  # «двушка» модель может записать и как rooms, и как rooms_options — это одно и то же
    same=(result.get('decision')=='apply' and result.get('intent') in ('search','refine') and not result.get('selection')
          and not result.get('unsupported') and set(model)==set(fast) and all(model[k]==fast[k] for k in fast))
    record('rule_parser',session=s.id,outcome='shadow_agree' if same else 'shadow_disagree',
           rules=fast,model={k:model.get(k) for k in set(model)|set(fast)},model_intent=result.get('intent'))


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
        s.selection_scope_ids = [i for i in s.selection_scope_ids if any(x['id']==i for x in s.shown)] or [x['id'] for x in s.shown]
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
        result = apply_request(s, changes, condition_sources=review.get('condition_sources'))
    else:
        result = None
    s.request_review_seq = review['seq']
    s.request_intent = review['intent']
    s.last_issue=None
    s.profile.pop('_last_issue',None)
    unsupported = list(s.profile.get('_unsupported_conditions') or [])
    unsupported_keys=dict(s.profile.get('_unsupported_keys') or {})
    additions = []
    for item in review.get('unsupported') or []:
        if item['action'] == 'remove':
            unsupported = [text for text in unsupported if text != item['text']]
            unsupported_keys={k:v for k,v in unsupported_keys.items() if v!=item['text']}
        else:
            if item.get('key'):
                old=unsupported_keys.get(item['key'])
                if old and old!=item['text']:unsupported=[text for text in unsupported if text!=old]
                unsupported_keys[item['key']]=item['text']
            if item['text'] not in unsupported:
                unsupported.append(item['text'])
            if item['text'] not in additions:
                additions.append(item['text'])
    if review.get('unsupported'):
        s.profile['_unsupported_conditions'] = unsupported
        s.profile['_unsupported_keys']=unsupported_keys
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
                             'Не могу проверить условие «'+'», «'.join(additions)+'». Показать самые выгодные, самые просторные или оставить прежнюю подборку?',
                             chips=['Самые выгодные','Самые просторные','Оставить подборку'])
        return {'needs_clarification':True,'question':explanation,
                'client_said':answer['data'].get('text','')}
    from .capabilities import POLICIES
    previous=s.requested_action
    requested=review.get('requested_action')
    if requested=='market_prices':s.research_query=review.get('research')
    if not requested and previous in POLICIES and POLICIES[previous].information and review['intent'] not in ('hold','call','selection'):
        requested=previous
    s.requested_action=requested if review['intent']!='hold' else None
    if s.requested_action in POLICIES and POLICIES[s.requested_action].information:
        s.request_intent='question'  # изменения применены, цель хода — ответ исследования
    if s.requested_action:s.profile['_requested_action']=s.requested_action
    else:s.profile.pop('_requested_action',None)
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
