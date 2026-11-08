"""Буквальный выбор из адресованной карточки; свободный текст понимает модель."""
import re
from .conditions import ALIASES
from .listing_query import FIELDS

LABELS={**{key:value[1].split(',')[0] for key,value in FIELDS.items()},
        'budget_max':'бюджет','rooms':'комнат','city':'город','districts':'район','housing':'рынок'}
ANY={'любой','любая','любое','любые','неважно','не важно','без ограничений'}
UNKNOWN={'не знаю','пока не знаю','не определился','не определилась'}
INFORMATION_ACKS={'да','давай','давайте','покажи','покажите','хорошо','ок','ага'}


def _words(text):
    return {w[:5] for w in re.findall(r'[а-яё]+',text.casefold().replace('ё','е')) if len(w)>2}


def names_property(key,quote,*,bound_field=None,question=''):
    subjects={'budget_max':r'бюджет|цен[ауы]|стоимост|лимит|сумм','rooms':r'комнат|студи|однуш|двуш|треш',
              'districts':r'район|локаци|расположен','okrug':r'округ','housing':r'рынок|новостро|вторич|тип\w*\s+жиль'}
    subject=subjects.get(key,re.escape(LABELS.get(key,key)))
    return bool(bound_field==key or re.search(subject,quote.casefold().replace('ё','е'))
                or re.search(subject,question.casefold().replace('ё','е')))


def ground_state(key,state,quote,*,bound_field=None,question=''):
    """Закрытые статусы: отсутствие решения не означает разрешение всех значений."""
    text=quote.casefold().replace('ё','е')
    if not names_property(key,quote,bound_field=bound_field,question=question):
        if not re.search(r'(?:все|всё|всех)\s+ограничен',text):return None
    label=LABELS.get(key,'')
    clauses=[part for part in re.split(r'[,;.!?]',text) if label and label in part]
    if len(clauses)==1:text=clauses[0]
    unknown=bool(re.search(r'не\s+(?:зна\w*|определ\w*|решил\w*)',text))
    unrestricted=bool(re.search(r'люб(?:ой|ая|ое|ые|ую)|не\s*важ\w*|(?:сними|убери|отмени)|без\s+(?:ограничен\w*|разницы|лимита)|неогранич\w*|все\s+равно|не\s+имеет\s+значения|не\s+принципиаль\w*',text))
    if unknown and not unrestricted:return 'unknown'
    if unrestricted and not unknown:return 'unrestricted'
    return state if unknown or unrestricted else None


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


YES={'да','ага','угу','верно','точно','конечно','именно','правильно','так','yes','ок','окей'}
NOISE={'все','всё','да','это','он','она','еще','ещё','в','во','именно','так','город','верно','конечно'}


def _near(a,b):
    """Не больше одной опечатки (замена, вставка или удаление буквы): «офа» ~ «уфа»."""
    if abs(len(a)-len(b))>1:return False
    if len(a)==len(b):return sum(x!=y for x,y in zip(a,b))<=1
    if len(a)>len(b):a,b=b,a
    return any(b[:i]+b[i+1:]==a for i in range(len(b)))


def confirm_city(s,inputs,base_profile):
    """«Это Уфа?» -> «Да» / «Да, в Уфе» / «Да, офа»: город из вопроса агента, а не из слов клиента.
    Только если карточка ждёт ответа, в вопросе ровно один город и ответ — согласие без других условий."""
    from ..sources import SUPPORTED_CITIES,normalize_city
    ticket=s.pending_decisions.get('clarify') or {}
    if ticket.get('status')!='waiting' or len(inputs)!=1:return None
    question=(ticket.get('text') or '').replace('ё','е')
    proposed=[c for c in SUPPORTED_CITIES if re.search(r'(?<![а-яё])'+re.escape(c.replace('ё','е'))+r'(?![а-яё-])',question,re.I)]
    if len(proposed)!=1 or '?' not in question or base_profile.get('city')==proposed[0]:return None
    city=proposed[0]
    words=re.findall(r'[а-яёa-z-]+',inputs[-1]['text'].casefold())
    if not words or words[0] not in YES or len(words)>4:return None
    rest=[w for w in words[1:] if w not in NOISE]
    target=city.casefold().replace('ё','е')
    if any(not (normalize_city(w)==city or _near(w.replace('ё','е'),target)) for w in rest):return None
    return {'city':city}
