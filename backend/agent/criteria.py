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
    rows = []
    soft = set(profile.get('soft_criteria') or [])
    def add(key, label, status, source='cian', evidence=None, checked_at=None):
        rows.append({'key': key, 'label': label, 'status': status, 'required': key not in soft,
                     'source': source, 'evidence': evidence, 'checked_at': checked_at})
    for key, label in SCALARS.items():
        want = profile.get(key)
        if want in (None, False, [], '', 'any'):
            continue
        status, actual = None, None
        if key == 'condition':
            actual = x.get('condition')
            rank = CONDITION.get((actual or '').lower())
            status = rank >= NEED[want] if rank is not None else None
            if want == "ready" and x.get("is_new") and x.get("finished") is False:
                status = False
        elif key == 'material':
            actual = x.get('material')
            status = any(m in actual.lower() for m in want) if actual else None
        elif key == 'not_first_floor':
            actual = x.get('floor'); status = actual != 1 if actual is not None else None
        elif key == 'not_last_floor':
            actual = x.get('floor')
            status = actual != x['floors'] if actual is not None and x.get('floors') else None
        elif key == 'metro_max_minutes':
            actual = (x.get('metro') or {}).get('minutes')
            mode = (x.get('metro') or {}).get('mode')
            # Транспорт нельзя переводить в минуты ходьбы произвольным множителем.
            status = actual <= want if actual is not None and mode == 'пешком' else None
        else:
            actual = x.get({'year_min': 'year', 'area_min': 'area', 'kitchen_min': 'kitchen'}[key])
            status = actual >= want if actual is not None else None
        corrected = {'year_min': 'year', 'area_min': 'area', 'kitchen_min': 'kitchen'}.get(key, key)
        add(key, label, status, 'seller_call' if corrected in (x.get('listed') or {}) else 'cian', str(actual) if actual is not None else None)
    for k in profile.get('features') or []:
        info = feature_evidence(x, k)
        add(f'feature:{k}', FEATURES[k][0], **info)
    for k in profile.get('poi') or []:
        near = x.get('nearby') or {}
        evidence = (x.get('nearby_evidence') or {}).get(k, {})
        item = near.get(k)
        status = bool(item) if k in near else None
        if item and item.get('dist') is not None:
            status = item['dist'] <= geo.RADIUS
        note = f"{item.get('name')}, {item.get('dist')} м" if item else None
        source = evidence.get('source')
        if k not in near and k in POI_TEXT:
            claim, quote = text_evidence(x.get('description') or '', POI_TEXT[k])
            if claim is not None:
                source, note = 'cian_description', quote
                # Упоминание парка в рекламе не доказывает расстояние до него.
        add(f'poi:{k}', geo.POI[k][0], status, source, note, evidence.get('checked_at'))
    failed = [r['label'] for r in rows if r['required'] and r['status'] is False]
    unknown = [r['label'] for r in rows if r['required'] and r['status'] is None]
    score = sum(6 if r['status'] is True else -4 if r['status'] is False else 0 for r in rows if not r['required'])
    return {'status': 'rejected' if failed else 'unverified' if unknown else 'confirmed',
            'checks': rows, 'failed': failed, 'unknown': unknown, 'preference_score': score}
