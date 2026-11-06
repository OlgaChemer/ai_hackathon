"""Буквальный выбор из адресованной карточки; свободный текст понимает модель."""
import re
from .conditions import ALIASES
from .listing_query import FIELDS

LABELS={**{key:value[1].split(',')[0] for key,value in FIELDS.items()},
        'budget_max':'бюджет','rooms':'комнат','city':'город'}
ANY={'любой','любая','любое','любые','неважно','не важно','без ограничений'}
UNKNOWN={'не знаю','пока не знаю','не определился','не определилась'}


def _words(text):
    return {w[:5] for w in re.findall(r'[а-яё]+',text.casefold().replace('ё','е')) if len(w)>2}


def choice(s,inputs):
    operation=s.active_operation or {};ticket=s.pending_decisions.get('clarify') or {}
    text=inputs[-1]['text'].strip().casefold()
    if operation.get('name')!='ask_client' or ticket.get('status')!='waiting':return None
    if text not in {str(c).strip().casefold() for c in ticket.get('payload',{}).get('chips') or []}:return None
    if text not in ANY|UNKNOWN:return None
    target=(operation.get('arguments') or {}).get('field')
    if not target:
        words=_words(ticket.get('text',''))
        matches=[(len(_words(label)),key) for key,label in LABELS.items() if _words(label) and _words(label)<=words]
        best=max((score for score,key in matches),default=0)
        targets={key for score,key in matches if score==best}
        if len(targets)!=1:return None  # неоднозначная старая карточка остаётся задачей модели
        target=targets.pop()
    if target in ('budget_max','rooms','districts','okrug','housing'):
        return {'field_states':{target:'unknown' if text in UNKNOWN else 'unrestricted'}}
    if text in UNKNOWN:return {}  # отсутствие знания не даёт согласия снять известное условие
    if target not in FIELDS or target in ('city','value_score','discount_pct'):return None
    aliases=[key for key,value in ALIASES.items() if value==target or (target=='floor' and value=='floor_last')]
    changes={'filters':[{'field':target,'op':'exists','values':[],'priority':'remove'}]}
    if aliases:changes['clear_criteria']=aliases
    return changes
