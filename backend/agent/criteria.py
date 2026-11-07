"""Каждое условие отдельно: совпало / не совпало / неизвестно, с источником.

Обязательный промах исключает квартиру. Неизвестность не считается совпадением.
Желательные условия влияют только на ранжирование. Общего порога «70%» нет.
"""
from .. import geo
from .features import FEATURES, feature_evidence, text_evidence

SCALARS = {'condition': 'состояние', 'material': 'материал дома', 'year_min': 'год постройки',
           'not_first_floor': 'не первый этаж', 'not_last_floor': 'не последний этаж',
           'area_min': 'общая площадь', 'kitchen_min': 'площадь кухни', 'metro_max_minutes': 'метро'}
CRITERIA_KEYS = [*SCALARS, *(f'feature:{k}' for k in FEATURES), *(f'poi:{k}' for k in geo.POI)]
CONDITION = {'без отделки': 0, 'требует ремонта': 0, 'предчистовая': 1, 'косметика': 2,
             'чистовая отделка': 3, 'евроремонт': 3, 'дизайнерский': 3}
NEED = {'ready': 3, 'cosmetic': 2, 'repair': 0}
POI_TEXT = {'park': r'\bпарк\b|\bпарка\b|\bпарком\b|\bсквер', 'school': r'\bшкол',
            'kindergarten': r'\bсадик|детск\w* сад', 'clinic': r'поликлиник', 'water': r'\bрек|озер|набережн'}


def evaluate(profile: dict, x: dict) -> dict:
    from .conditions import compile_profile, evaluate as check_condition
    rows=[check_condition(c,x) for c in compile_profile(profile)]
    failed=[r['label'] for r in rows if r['required'] and r['status'] is False]
    unknown=[r['label'] for r in rows if r['required'] and r['status'] is None]
    score=sum(6 if r['status'] is True else -4 if r['status'] is False else 0 for r in rows if not r['required'])
    return {'status':'rejected' if failed else 'unverified' if unknown else 'confirmed',
            'checks':rows,'failed':failed,'unknown':unknown,'preference_score':score}


def match(x, prefs):
    return evaluate({**prefs.profile, "features": prefs.features, "poi": prefs.poi}, x)


def selectable(x, prefs):
    """Можно показать с явным статусом; unverified никогда не означает совпадение."""
    return match(x,prefs)['status'] != 'rejected'
