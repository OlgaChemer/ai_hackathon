"""Снимок только своего диалога. Расчётные допущения отделены от фактов объявления."""
from copy import deepcopy
import math
import statistics

from fastapi import HTTPException
from sqlalchemy import select
from ..db.models import Chat,Listing

KINDS={'repair':'Оценка ремонта','compare':'Сравнение полной стоимости','market':'Исследование рынка'}


def clean_listing(x):
    keys=('id','url','source','source_name','city','district','address','rooms','area','price','kitchen','floor',
          'floors','condition','year','is_new','deadline','delivery_year','nearby','description','provenance','photo','photos')
    row={k:deepcopy(x[k]) for k in keys if k in x}
    row['description']=str(row.get('description') or '')[:1200]
    return row


def context(connection,uid,chat_id=None,liked=None):
    chats=list(connection.scalars(select(Chat).where(Chat.user_id==uid).order_by(Chat.updated_at.desc()).limit(50)))
    choices=[{'id':c.id,'title':c.title} for c in chats]
    c=next((c for c in chats if c.id==chat_id),None) if chat_id else next(iter(chats),None)
    if chat_id and c is None:raise HTTPException(404,'Диалог недоступен.')
    if c is None:return {'chats':choices,'chat_id':None,'profile':{},'liked':[],'revision':0}
    state=(c.state or {}).get('data',{})
    profile=state.get('profile') or c.profile or {}
    search=state.get('search') or {}
    rows={x['id']:x for x in [*(search.get('listings') or []),*(state.get('shown') or [])] if x.get('id')}
    # Для старых диалогов восстанавливаем только действительно показанные данные.
    if not rows:
        from ..db import repo
        last=repo.last_nonempty_selection(connection,c.id) or {}
        rows={x['id']:x for x in last.get('cards') or [] if x.get('id')}
    saved=(state.get('prefs') or {}).get('liked') or []
    ids=list(dict.fromkeys(liked if liked is not None else saved))
    known=set(rows)|set(saved)
    if set(ids)-known:raise HTTPException(422,'В отметках есть квартира, которой нет в этом подборе.')
    missing=[i for i in ids if i not in rows]
    if missing:
        rows.update({x.id:x.data for x in connection.scalars(select(Listing).where(Listing.id.in_(missing)))})
    if set(ids)-set(rows):raise HTTPException(422,'Часть понравившихся квартир не удалось восстановить. Откройте подбор и обновите отметки.')
    selected=[clean_listing(rows[i]) for i in ids if i in rows]
    return {'chats':choices,'chat_id':c.id,'profile':deepcopy(profile),'liked':selected,
            'revision':state.get('profile_revision',0),'selection_revision':state.get('selection_revision',0)}


def assumptions(rate,reserve):
    if not math.isfinite(rate) or not 0<=rate<=300_000:raise HTTPException(422,'Цена ремонта за м²: от 0 до 300 000 ₽.')
    if not math.isfinite(reserve) or not 0<=reserve<=100:raise HTTPException(422,'Запас: от 0 до 100%.')
    return {'repair_per_m2':rate,'reserve_pct':reserve,'nature':'Сценарные допущения пользователя, не смета подрядчика и не рыночная оценка ремонта.'}


def facts(snapshot):
    rows=snapshot['listings'];kind=snapshot['kind'];a=snapshot['assumptions']
    if kind in ('repair','compare'):
        result=[]
        for x in rows:
            area=x.get('area');price=x.get('price')
            valid_area=isinstance(area,(float,int)) and not isinstance(area,bool) and math.isfinite(area) and area>0
            valid_price=isinstance(price,(float,int)) and not isinstance(price,bool) and math.isfinite(price) and price>0
            repair=round(area*a['repair_per_m2']) if valid_area else None
            total_repair=round(repair*(1+a['reserve_pct']/100)) if repair is not None else None
            result.append({'id':x['id'],'address':x.get('address') or x['id'],'price':price if valid_price else None,
                'area':area if valid_area else None,'price_per_m2':round(price/area) if valid_area and valid_price else None,
                'repair':repair,'repair_with_reserve':total_repair,
                'total':round(price+total_repair) if valid_price and total_repair is not None else None})
        return {'kind':kind,'assumptions':a,'rows':result,'count':len(result)}
    valid=[x for x in rows if isinstance(x.get('price'),(int,float)) and x['price']>0]
    prices=[x['price'] for x in valid]
    meters=[x['price']/x['area'] for x in valid if x.get('area') and x['area']>0]
    groups=[]
    for room in sorted({x.get('rooms') for x in valid},key=lambda r: -1 if r is None else r):
        group=[x for x in valid if x.get('rooms')==room]
        groups.append({'rooms':room,'count':len(group),'median_price':round(statistics.median(x['price'] for x in group))})
    return {'kind':kind,'count':len(valid),'min_price':min(prices) if prices else None,
        'median_price':round(statistics.median(prices)) if prices else None,
        'median_price_per_m2':round(statistics.median(meters)) if meters else None,'groups':groups,
        'coverage':snapshot.get('coverage'),'whole_market':False}
