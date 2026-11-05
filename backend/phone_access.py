"""Операторское разрешение на WebRTC/TTS: владелец, карточка, объект, квота и срок."""
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy import select,func
from . import db
from .db.models import PhoneGrant,Chat,utcnow
from .config import settings


def actor(visitor,user):return hashlib.sha256(f'user:{user}'.encode() if user is not None else f'visitor:{visitor}'.encode()).hexdigest()


def reserve(connection,s,who,decision):
    ticket=s.pending_decisions.get('call') or {};operation=s.active_operation or {}
    listing=(ticket.get('payload') or {}).get('listing') or {}
    stored=connection.get(Chat,s.id)
    if stored is None or (stored.state_version or 0)!=s.state_version:
        return {'error':'Состояние диалога обновилось. Откройте актуальную карточку звонка.','status':409}
    if (ticket.get('status')!='waiting' or ticket.get('decision_id')!=decision
            or operation.get('name')!='call_seller' or listing.get('id') not in s.call_targets):
        return {'error':'Нет актуального разрешения звонить по выбранной квартире.','status':409}
    existing=connection.scalar(select(PhoneGrant).where(PhoneGrant.chat_id==s.id,PhoneGrant.decision_id==decision))
    if existing:return {'error':'Это разрешение уже использовано. Проверьте исход звонка перед новым разрешением.','status':409}
    used=connection.scalar(select(func.count()).select_from(PhoneGrant).where(PhoneGrant.actor==who,PhoneGrant.created_at>=utcnow()-timedelta(hours=1)))
    if used>=settings.phone_calls_per_hour:return {'error':'Лимит телефонных операций за час исчерпан.','status':429}
    identifier=secrets.token_hex(16);secret=secrets.token_urlsafe(32)
    row=PhoneGrant(id=identifier,chat_id=s.id,actor=who,operation_id=operation['id'],listing_id=listing['id'],
        decision_id=decision,token_hash=hashlib.sha256(secret.encode()).hexdigest(),status='issued',tts_chars=0,
        expires_at=utcnow()+timedelta(seconds=settings.phone_call_max_seconds+60))
    connection.add(row);connection.flush()
    return {'id':identifier,'token':identifier+'.'+secret,'listing_id':listing['id']}


def lookup(connection,token,who):
    if not isinstance(token,str) or '.' not in token:return None
    identifier,secret=token.split('.',1)
    row=connection.get(PhoneGrant,identifier)
    if row is None or row.actor!=who:return None
    if not secrets.compare_digest(row.token_hash,hashlib.sha256(secret.encode()).hexdigest()):return None
    return row


def identify(connection,token,who,chat_id,decision):
    row=lookup(connection,token,who)
    if row is None or row.chat_id!=chat_id or row.decision_id!=decision:return None
    return row.id


def confirmation(connection,identifier,chat_id,operation_id,decision):
    row=connection.get(PhoneGrant,identifier)
    return bool(row and row.chat_id==chat_id and row.operation_id==operation_id and row.decision_id==decision
                and row.status=='issued' and row.expires_at>utcnow())


def charge_tts(connection,token,who,characters):
    row=lookup(connection,token,who)
    if row is None or row.expires_at<=utcnow() or row.status not in ('issued','connected'):return False
    chat=connection.get(Chat,row.chat_id);state=(chat.state or {}).get('data',{}) if chat else {}
    op=state.get('active_operation') or (state.get('operations') or {}).get(row.operation_id) or {}
    if op.get('id')!=row.operation_id or (op.get('call_mode') not in (None,'phone')):return False
    if row.tts_chars+characters>settings.phone_tts_char_limit:return False
    row.tts_chars+=characters
    return True


def close(connection,token,who):
    row=lookup(connection,token,who)
    if row:row.status='closed'
    return bool(row)
