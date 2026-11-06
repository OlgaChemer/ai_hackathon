"""Параметры профиля: контракт извлечения ввода и проверки машинных изменений."""

from .. import geo
from .features import FEATURES
from .criteria import CRITERIA_KEYS
from .listing_query import PROFILE_FILTER_SCHEMA

CHANGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "field_states": {"type":"object","additionalProperties":False,
            "properties":{key:{"type":"string","enum":["unknown","unrestricted"]} for key in ('budget_max','rooms','districts','okrug','housing')},
            "description":"Только явно названные статусы: не знаю/помоги определить — unknown; любой/неважно/сними ограничение — unrestricted. Это снимает прежнее ограничение поля. Неназванные поля не добавляй. Для конкретного значения используй обычное поле."},
        "filters": {"type":"array", "maxItems":20, "items":PROFILE_FILTER_SCHEMA,
                    "description":"Общие сравнения полей: срок сдачи — delivery_year (не year), точный год op=eq, не позднее lte, диапазон between. Значения строками. Только явно названные изменения; remove снимает фильтр поля. Неназванные фильтры сохраняет код."},
        "center_required": {"type":"boolean", "description":"true — именно В ЦЕНТРЕ обязательно; false — центр необязателен. Не добавляй запрет для «желательно ближе к центру». Центр по умолчанию — 5 км по прямой от опорной точки; код раскрывает определение."},
        "seller_questions": {"type":"array","maxItems":12,"items":{"type":"string","maxLength":300},
                             "description":"Дополнительные явно названные клиентом вопросы продавцу; новый список целиком с сохранением прежних. Не означает разрешение звонить."},
        "city": {"type": "string", "description": "город, именительный падеж"},
        "rooms": {
            "minimum": 0,
            "maximum": 20,
            "type": "integer",
            "description": "комнат; студия = 0",
        },
        "rooms_options": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 5},
            "maxItems": 6,
            "description": "ВСЕ допустимые числа комнат: 1–2 комнаты -> [1,2], не выбирай одно за клиента",
        },
        "cities": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
            "description": "Дополнительные явно названные территории: Москва или Подмосковье -> city=Москва, cities=[Московская область]",
        },
        "prefer_center": {
            "type": "boolean",
            "description": "Желательно в центре или ближе к центру — учитывается при сравнении всех допустимых комнатностей",
        },
        "budget_max": {
            "minimum": 1,
            "maximum": 1000000000,
            "type": "integer",
            "description": "максимальная цена, рубли",
        },
        "okrug": {
            "type": "string",
            "description": "для Москвы: ЦАО, САО, СВАО, ВАО, ЮВАО, ЮАО, ЮЗАО, ЗАО, СЗАО; по названному району — его округ",
        },
        "districts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "районы, где искать (именительный падеж)",
        },
        "avoid_districts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "районы, которые не показывать",
        },
        "housing": {
            "type": "string",
            "enum": ["secondary", "new", "both"],
            "description": "вторичка, новостройки или и то и другое",
        },
        "payment": {
            "type": "string",
            "enum": ["cash", "mortgage_ready", "mortgage_saving"],
            "description": "cash — вся сумма своими деньгами (наличными, наличка, за свои, без ипотеки); mortgage_ready — ипотека и взнос есть; mortgage_saving — ипотека, взнос ещё копит. Записывай уже названную оплату сразу, не переспрашивай.",
        },
        "children": {
            "minimum": 0,
            "maximum": 30,
            "type": "integer",
            "description": "детей в семье (0 — нет)",
        },
        "youngest_age": {
            "minimum": 0,
            "maximum": 100,
            "type": "integer",
            "description": "возраст младшего ребёнка, лет",
        },
        "matcap": {
            "type": "boolean",
            "description": "есть неизрасходованный маткапитал",
        },
        "features": {
            "type": "array",
            "items": {"type": "string", "enum": list(FEATURES)},
            "description": "обязательные удобства: "
            + "; ".join(f"{k} — {v[0]}" for k, v in FEATURES.items()),
        },
        "poi": {
            "type": "array",
            "items": {"type": "string", "enum": list(geo.POI)},
            "description": "что должно быть в 1 км (проверю по карте): "
            + "; ".join(f"{k} — {v[0]}" for k, v in geo.POI.items()),
        },
        "kitchen_min": {
            "minimum": 0,
            "maximum": 500,
            "type": "number",
            "description": "кухня от, м²",
        },
        "area_min": {
            "minimum": 0,
            "maximum": 2000,
            "type": "number",
            "description": "площадь от, м²",
        },
        "not_first_floor": {
            "type": "boolean",
            "description": "true — первый этаж запрещён; false — разрешён. Если клиент исключает первый И последний этаж, запиши оба ограничения, не пропускай отрицательные условия.",
        },
        "not_last_floor": {
            "type": "boolean",
            "description": "true — последний этаж запрещён; false — разрешён. При изменении первого этажа не снимай запрет последнего без слов клиента.",
        },
        "condition": {
            "type": "string",
            "enum": ["ready", "cosmetic", "repair", "any"],
            "description": "готова к заселению; косметический; под ремонт; не важно",
        },
        "material": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["кирпич", "монолит", "панель", "блочный", "дерево"],
            },
        },
        "year_min": {"type": "integer", "minimum": 1800, "maximum": 2100},
        "timeline": {"type": "string", "enum": ["month", "quarter", "exploring"]},
        "soft_criteria": {
            "type": "array",
            "items": {"type": "string", "enum": CRITERIA_KEYS},
            "description": "Ключи условий, которые клиент назвал желательными, а не обязательными. Список целиком. "
            "Например poi:park — желательно парк, feature:parking — желательно парковка. Остальные условия обязательны.",
        },
        "clear_criteria": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    *CRITERIA_KEYS,
                    "prefer_center",
                    "districts",
                    "avoid_districts",
                ],
            },
            "description": "Полностью убрать только отменённые клиентом условия, сохранив остальные. Например «парк больше не нужен» -> [poi:park], «ремонт любой» -> [condition], «любой этаж» -> [not_first_floor,not_last_floor]. Удобства: feature:ключ. Снятие условия одновременно убирает его из желательных; удалять один soft_criteria недостаточно.",
        },
        "metro_max_minutes": {
            "minimum": 0,
            "maximum": 300,
            "type": "integer",
            "description": "до метро не дольше, минут",
        },
        "wishes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "прочие пожелания коротко, которые не ложатся в поля",
        },
    },
}
