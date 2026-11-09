"""Ориентиры по ограниченной выборке без бюджета; условия и рынки не подменяются."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import time

from ..config import settings
from . import cian,store


def scope(profile):
    from ..agent.conditions import compile_profile
    p=deepcopy(profile)
    conditions=[c for c in compile_profile(p) if c['priority']=='required' and c['field'] not in ('price','price_per_m2')]
    center=next((c['values'][0] for c in conditions if c['field']=='center_km' and c['op'] in ('lt','lte')),None)
    from ..agent.research import query
    research=query(p.get('_research_query'))[0]
    return {'sample_policy':2,'cities':list(dict.fromkeys([p.get('city'),*(p.get('cities') or [])])),
            'rooms':p.get('rooms_options') or [p.get('rooms')],
            'markets':[False,True] if p.get('housing') in (None,'both') else [p['housing']=='new'],
            'okrug':p.get('okrug'),'districts':p.get('districts') or [],'avoid_districts':p.get('avoid_districts') or [],
            'conditions':[{k:c[k] for k in ('field','op','values','label')} for c in conditions],
            'research':research,'budget_max':p.get('budget_max') if research['respect_budget'] else None,
            'budget_min':p.get('budget_min') if research['respect_budget'] else None,
            'apartments':p.get('apartments'),
            'budget_applied':research['respect_budget'] and p.get('budget_max') is not None,
            'whole_market':False,'center_radius_km':center}


def key(profile):
    definition=scope(profile)
    definition['conditions']=sorted([{k:v for k,v in c.items() if k!='label'} for c in definition['conditions']],key=lambda c:c['field'])
    def canonical(value):
        if isinstance(value,dict):return {k:canonical(v) for k,v in value.items()}
        if isinstance(value,list):return [canonical(v) for v in value]
        if type(value) is float and value.is_integer():return int(value)
        return value
    return hashlib.sha256(json.dumps(canonical(definition),sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]


def current(value,profile):
    if not value or value.get('profile_key')!=key(profile):return False
    try:return (datetime.now(timezone.utc)-datetime.fromisoformat(value['generated_at'])).total_seconds()<900
    except (KeyError,TypeError,ValueError):return False


def matches(item,definition):
    from ..agent.conditions import evaluate
    from ..agent.profile import in_districts
    if item.get('city') not in definition['cities']:return False
    if None not in definition['rooms'] and item.get('rooms') not in definition['rooms']:return False
    if definition.get('budget_max') is not None and item['price']>definition['budget_max']:return False
    if definition.get('budget_min') is not None and item['price']<definition['budget_min']:return False
    if definition.get('apartments')=='hide' and item.get('is_apartments'):return False
    if definition.get('apartments')=='only' and not item.get('is_apartments'):return False
    if bool(item.get('is_new')) not in definition['markets']:return False
    if definition['okrug'] and item.get('okrug')!=definition['okrug']:return None if not item.get('okrug') else False
    if definition['districts'] and not in_districts(item,definition['districts']):return None if not item.get('district') else False
    if definition['avoid_districts']:
        if not item.get('district'):return None
        if in_districts(item,definition['avoid_districts']):return False
    checks=[evaluate({**c,'key':c['field'],'priority':'required'},item)['status'] for c in definition['conditions']]
    return False if False in checks else None if None in checks else True


async def estimate(profile,on_progress=None):
    definition=scope(profile);deadline=time.monotonic()+settings.search_budget_s
    rows={};reports=[];reasons=set();accepted=[];unknown=0
    # Подбор использует недавние реальные объявления из общей базы. Они нужны
    # и здесь, чтобы несколько первых страниц источника не подменяли весь срез.
    from . import stored
    saved_profile=deepcopy(profile)
    if not definition['budget_applied']:
        saved_profile['budget_max']=saved_profile['budget_min']=None
    known=stored(saved_profile)
    rows.update({x['id']:x for x in known})
    if known:
        reasons.add('recent_saved_listings')
        for new in definition['markets']:
            count=sum(bool(x.get('is_new'))==new for x in known)
            if count:reports.append({'source':'saved_listings','status':'db','new':new,'page':0,'count':count,'max_age_days':store.SEEN_DAYS})
    for page in range(1,4):
        for city in definition['cities']:
            for rooms in definition['rooms']:
                for new in definition['markets']:
                    query_okrug=definition['okrug']  # округ ограничивает источник только по явному выбору клиента
                    report={'city':city,'rooms':rooms,'new':new,'page':page,'query_okrug':query_okrug}
                    try:
                        remaining=deadline-time.monotonic()
                        if remaining<=0:raise asyncio.TimeoutError()
                        items,meta=await asyncio.wait_for(cian.search(city,rooms,definition['budget_max'],page,query_okrug,new),remaining)
                        items=[{**item,'provenance':{'source':'cian','status':meta.get('status'),
                                'observed_at':meta.get('fetched_at'),'url':item.get('url')}} for item in items]
                        if meta.get('status')=='live':store.remember(items)
                        report.update(meta,count=len(items))
                        for item in items:rows[item['id']]=item
                    except asyncio.TimeoutError:
                        report.update(status='unavailable',count=0,reason='search_budget_exhausted')
                        reasons.add('search_budget_exhausted')
                    except cian.SourceUnavailable:
                        report.update(status='unavailable',count=0,reason='source_unavailable')
                        reasons.add('source_unavailable')
                    reports.append(report)
                    if on_progress:on_progress(len(rows))
        cleaned=cian.drop_outliers(list(rows.values()))
        kinds=list({c['field'].split(':',1)[1] for c in definition['conditions'] if c['field'].startswith('nearby:')})
        if kinds:
            from .. import geo
            preliminary={**definition,'conditions':[c for c in definition['conditions'] if not c['field'].startswith('nearby:')]}
            candidates=[x for x in cleaned if matches(x,preliminary) is True]
            await geo.nearby(candidates[:30],kinds,budget_s=max(0,min(settings.places_budget_s,deadline-time.monotonic())))
        checks=[(x,matches(x,definition)) for x in cleaned]
        accepted=[x for x,match in checks if match is True];unknown=sum(match is None for _,match in checks)
        if len(accepted)>=12 or time.monotonic()>=deadline or not any(r['count'] for r in reports if r['page']==page):break
    if page==3:reasons.add('page_limit')
    if unknown:reasons.add('unverified_conditions')
    if any(r.get('status') in ('cached','snapshot') for r in reports):reasons.add('cached_data')
    covered={r['new'] for r in reports if r['count']}
    if set(definition['markets'])-covered:reasons.add('incomplete_markets')
    result={'scope':definition,'profile_key':key(profile),'generated_at':datetime.now(timezone.utc).isoformat(),
            'count':len(accepted),'received':len(rows),'unverified':unknown,'reports':reports,'reasons':sorted(reasons),
            'status':'live' if reports and all(r.get('status')=='live' for r in reports) else 'partial',
            'markets':[{'new':new,'count':sum(bool(x.get('is_new'))==new for x in accepted)} for new in definition['markets']]}
    from ..agent.research import execute
    result['analysis']=execute(accepted,definition['research'])
    if len(accepted)<6:return {**result,'error_kind':'insufficient_market_sample' if rows else 'market_source_unavailable'}
    from ..market_statistics import prices
    return {**result,**prices(accepted)}
