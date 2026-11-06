"""Единственная точка фиксации снимка и доставки критических событий."""
from contextlib import contextmanager
from copy import deepcopy

from .. import db
from ..db import repo
from . import state


def commit(s, events=(), command=None):
    saved=state.snapshot(s)
    profile=deepcopy(s.profile)
    memory=deepcopy(s.messages)
    version=s.state_version
    try:
        if s.durable:
            version=db.required(repo.commit_dialogue,s.id,version,saved,profile,memory,list(events),command)
    except db.PersistenceError:
        if s._durable_snapshot:state.restore(s,s._durable_snapshot)
        raise
    s.state_version=version
    from ..telemetry import dependency
    dependency('database','available')
    s._durable_snapshot=saved
    for ev in events:
        s.seq=ev['seq']
        s.history.append(ev)
        from ..events import deliver
        deliver(s,ev)
    from ..config import settings
    del s.history[:-settings.history_memory_events]


def push(s, type_, payload):
    if s._owner is not None:
        if s._replaying_operation and type_!='confirm':return {'type':type_,**payload,'seq':0,'replay':True}
        s._event_buffer.append({'type':type_,**payload})
        if type_=='search_progress' or type_.startswith('call_'):
            s.flush()
        return {'type':type_,**payload,'seq':s._owner.seq+len(s._event_buffer)}
    ev={'type':type_,**payload,'seq':s.seq+len(s._event_buffer)+1,
        'profile_revision':s.profile_revision,'selection_revision':s.selection_revision}
    if s._batch_depth:
        s._event_buffer.append(ev)
    else:commit(s,[ev],s._command)
    return ev


@contextmanager
def atomic(s):
    """Только синхронный участок: никаких await внутри транзакции состояния."""
    if s._owner is not None:raise RuntimeError('Транзакция принадлежит владельцу диалога')
    outer=s._batch_depth==0
    before=state.snapshot(s) if outer else None
    s._batch_depth+=1
    try:
        yield
        if outer:
            events=list(s._event_buffer)
            s._event_buffer.clear()
            commit(s,events,s._command)
    except BaseException:
        if outer:
            s._event_buffer.clear()
            state.restore(s,before)
        raise
    finally:s._batch_depth-=1
