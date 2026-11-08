"""Канонические условия. Старые поля профиля — входной адаптер для одной модели."""
from copy import deepcopy

THRESHOLD={'quiet_road_m':200,'quiet_rail_m':300}  # совпадает с agent/districts.THRESHOLDS

ALIASES={'area_min':'area','kitchen_min':'kitchen','year_min':'year',
         'metro_max_minutes':'metro_minutes','center_required':'center_km',
         'condition':'condition','material':'material','not_first_floor':'floor',
         'not_last_floor':'floor_last'}
UNITS={'area':'m2','kitchen':'m2','price':'RUB','price_per_m2':'RUB/m2',
       'center_km':'km','metro_minutes':'minute','year':'year','delivery_year':'year'}


def property_id(key):
    if key.startswith('poi:'):return 'nearby:'+key[4:]
    return ALIASES.get(key,key)


def compile_profile(profile):
    from . import criteria as C,listing_query as Q
    from .. import geo
    soft=set(profile.get('soft_criteria') or [])
    source=profile.get('_condition_sources') or {}
    conditions={}
    def add(field,op,values,key,label,priority=None):
        conditions[field]={'field':field,'op':op,'values':values,
                           'unit':'m' if field.startswith('nearby:') else UNITS.get(field),
                           'priority':priority or ('preferred' if key in soft else 'required'),
                           'source':deepcopy(source.get(field) or {'kind':'legacy'}),
                           'key':key,'label':label,'support':'supported'}
    for key in ('area_min','kitchen_min','year_min','metro_max_minutes'):
        value=profile.get(key)
        if value is not None and value is not False:
            add(ALIASES[key],'lte' if key=='metro_max_minutes' else 'gte',[value],key,C.SCALARS[key])
    if profile.get('not_first_floor'):add('floor','ne',[1],'not_first_floor',C.SCALARS['not_first_floor'])
    if profile.get('not_last_floor'):add('floor_last','ne_field',['floors'],'not_last_floor',C.SCALARS['not_last_floor'])
    if profile.get('condition') not in (None,'','any'):
        add('condition','condition_at_least',[profile['condition']],'condition',C.SCALARS['condition'])
    if profile.get('material'):
        add('material','contains_any',list(profile['material']),'material',C.SCALARS['material'])
    for key in profile.get('features') or []:add('feature:'+key,'eq',[True],'feature:'+key,C.FEATURES[key][0])
    for key in profile.get('poi') or []:add('nearby:'+key,'lte',[geo.RADIUS],'poi:'+key,geo.POI[key][0])
    if profile.get('center_required'):
        add('center_km','lte',[5],'center_required','центр: не дальше 5 км по прямой','required')
    kind=profile.get('district_type')
    if kind=='quiet':
        # «Тихо» — свойство дома, а не района: расстояние до крупной дороги и железной дороги по OpenStreetMap
        add('road_m','gte',[THRESHOLD['quiet_road_m']],'district_type','тихо: крупная дорога не ближе 200 м','preferred')
        add('rail_m','gte',[THRESHOLD['quiet_rail_m']],'district_type','тихо: железная дорога не ближе 300 м','preferred')
    elif kind in ('sleeping','prestige','business'):
        label={'sleeping':'спальный район','prestige':'престижный район','business':'деловой район'}[kind]+' (оценка по ценам и застройке)'
        add('district_type','has_label',[kind],'district_type',label,'preferred')
    for f in profile.get('filters') or []:
        if f['priority']=='remove':continue
        field=f['field']
        values=[Q._typed(v,field) for v in f['values']]
        op=f['op']
        if field=='material' and op in ('eq','in'):op='contains_any'
        add(field,op,values,'filter:'+field,Q.describe(f),f['priority'])
    return list(conditions.values())


def normalize(profile):
    profile['_conditions']=compile_profile(profile)
    profile['_conditions_version']=1
    return profile


def evaluate(condition,x):
    from . import criteria as C,listing_query as Q
    from .features import feature_evidence
    field,op,values=condition['field'],condition['op'],condition['values']
    source='cian';note=None;checked=None
    if field.startswith('feature:'):
        evidence=feature_evidence(x,field.split(':',1)[1])
        status=evidence['status'];source=evidence['source'];note=evidence['evidence']
    elif field.startswith('nearby:'):
        key=field.split(':',1)[1]
        nearby=x.get('nearby') or {}
        if isinstance(nearby,list):nearby={p['kind']:p for p in nearby}
        evidence=(x.get('nearby_evidence') or {}).get(key) or {}
        source=evidence.get('source');checked=evidence.get('checked_at')
        item=nearby.get(key)
        if key not in nearby:status=None
        elif item is None:status=False
        elif op=='exists':status=True
        else:
            actual=item.get('dist')
            status=_compare(actual,op,values) if actual is not None else None
        if item:note=f"{item.get('name')}, {item.get('dist')} м"
        elif key not in nearby and key in C.POI_TEXT:
            claim,quote=C.text_evidence(x.get('description') or '',C.POI_TEXT[key])
            if claim is not None:source,note='cian_description',quote
    elif op=='has_label':
        from .districts import LABELS,of_listing
        got=of_listing(x)
        status=None if got is None else values[0] in got
        source='osm+prices';note=', '.join(LABELS.get(v,v) for v in got) if got else None
    elif op=='condition_at_least':
        actual=x.get('condition');rank=C.CONDITION.get((actual or '').lower())
        status=rank>=C.NEED[values[0]] if rank is not None else None
        if values[0]=='ready' and x.get('is_new') and x.get('finished') is False:status=False
        note=actual
    elif op=='contains_any':
        actual=x.get(field)
        status=any(str(v).casefold() in str(actual).casefold() for v in values) if actual is not None else None
        note=actual
    elif op=='ne_field':
        actual=x.get('floor');other=x.get(values[0])
        status=actual!=other if actual is not None and other is not None else None
        note=actual
    else:
        actual=Q.field_value(x,field)
        status=_compare(actual,op,values)
        note=actual
        if field in ('center_km','road_m','rail_m'):source='map'
    if field in (x.get('listed') or {}):source='seller_call'
    provenance=(x.get('fact_sources') or {}).get(field) or x.get('provenance') or {}
    if checked is None:checked=provenance.get('observed_at') or x.get('stored_at')
    if source in ('cian','seller_call'):
        source=provenance.get('source') or source
        checked=provenance.get('observed_at') or x.get('stored_at') or checked
    return {'key':condition['key'],'condition_id':field,'label':condition['label'],'status':status,
            'required':condition['priority']=='required','source':source,
            'evidence':str(note) if note is not None else None,'checked_at':checked}


def _compare(actual,op,values):
    # Наличие поля неизвестно до проверки источника; None не равен отрицанию.
    if actual is None:return None
    if op=='exists':return True
    if isinstance(actual,str):actual=actual.casefold()
    values=[v.casefold() if isinstance(v,str) else v for v in values]
    if op=='in':return actual in values
    if op=='between':return values[0]<=actual<=values[1]
    value=values[0]
    return {'eq':lambda:actual==value,'ne':lambda:actual!=value,'lt':lambda:actual<value,
            'lte':lambda:actual<=value,'gt':lambda:actual>value,'gte':lambda:actual>=value}[op]()


def reconcile(profile,changes):
    """Удалить алиасы заменённого условия; UI-поля остаются совместимым представлением."""
    fields={property_id(k) for k in changes if k in ALIASES}
    for group,prefix in (('poi','nearby:'),('features','feature:')):
        if group in changes:
            fields|={prefix+k for k in changes[group]}
            if not changes[group]:
                fields|={f['field'] for f in profile.get('filters') or [] if f['field'].startswith(prefix)}
    generic={f['field'] for f in changes.get('filters') or []}
    profile['filters']=[f for f in profile.get('filters') or [] if f['field'] not in fields or f['field'] in generic]
    for field in generic:
        for key,alias in ALIASES.items():
            if alias==field and key not in changes:
                profile[key]=False if key in ('not_first_floor','not_last_floor','center_required') else [] if key=='material' else None
                profile['soft_criteria']=[k for k in profile.get('soft_criteria') or [] if k!=key]
        if field.startswith('nearby:'):
            profile['poi']=[k for k in profile.get('poi') or [] if 'nearby:'+k!=field]
        if field=='center_km':profile['center_required']=False
    for key in changes.get('clear_criteria') or []:
        field=property_id(key)
        replacement=any(f['field']==field and f['priority']!='remove' for f in changes.get('filters') or [])
        if not replacement:profile['filters']=[f for f in profile.get('filters') or [] if f['field']!=field]
    return normalize(profile)
