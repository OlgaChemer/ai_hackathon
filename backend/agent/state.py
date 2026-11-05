"""Версионируемый снимок процесса: только данные, без Futures и задач Python."""
from copy import deepcopy
from dataclasses import asdict

FIELDS = ('profile','messages','status','voice_mode','red_lines_ok','market','finance','mortgage',
          'programs_key','programs_result','search','shown','selection_query','selection_scope_ids',
          'selection_dirty','selection_operation','selection_profile_stale','last_issue','call_targets',
          'calls','agreed','chosen','current','journal','opened','map_warned','stage',
          'request_inputs','request_review_seq','request_review','request_intent','requested_action',
          'client_revision','profile_revision','selection_revision','pending_decisions','operations',
          'active_operation','qa_pending','started','query')
SET_FIELDS = ('districts_plus','explicit_avoids','districts_minus','conditions_plus')


def encode(value):
    if isinstance(value,dict):return {str(k):encode(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [encode(v) for v in value]
    if isinstance(value,set):return [encode(v) for v in sorted(value)]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise TypeError('Несериализуемое значение состояния: '+type(value).__name__)


def snapshot(s):
    data={name:encode(getattr(s,name)) for name in FIELDS}
    data['prefs']=encode(asdict(s.prefs))
    if data['search']:
        data['search'].pop('map_deadline',None)
        data['search'].pop('map_revision',None)
        data['search'].pop('source_deadline',None)
    return {'format':1,'data':data}


def restore(s, saved):
    if not isinstance(saved,dict) or saved.get('format')!=1:raise ValueError('Неизвестный формат снимка диалога')
    data=deepcopy(saved['data'])
    for name in FIELDS:
        if name in data:setattr(s,name,data[name])
    s.status={int(k):v for k,v in s.status.items()}
    if s.programs_key is not None:s.programs_key=tuple(s.programs_key)
    if s.search is not None:s.search['shown_ids']=set(s.search.get('shown_ids') or [])
    from .preferences import Prefs
    prefs=data.get('prefs') or {}
    for name in SET_FIELDS:prefs[name]=set(prefs.get(name) or [])
    s.prefs=Prefs(**prefs)
    s.prefs.profile=deepcopy(s.profile)
