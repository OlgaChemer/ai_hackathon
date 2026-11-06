"""Проверка приёма задач отдельно от жизни процесса и внешних возможностей."""
import time
from sqlalchemy import text
from . import db,telemetry
from .db import migrations
from .config import settings


def report(llm=None):
    try:
        with db.session() as connection:
            connection.execute(text('SELECT 1'))
            current=migrations.revision(connection)
        database={'ready':current==migrations.HEAD,'revision':current,'expected':migrations.HEAD}
    except Exception:
        database={'ready':False,'reason':'storage_unavailable'}
    previous=telemetry.dependencies().get('database')
    if database['ready'] and previous and previous['status']=='degraded' and previous['age_seconds']<20:
        database.update(ready=False,reason='recent_write_failure')
    observed=telemetry.dependencies().get('llm')
    recent_failure=bool(observed and observed['status']=='degraded' and observed['age_seconds']<20)
    from .agent.session import SESSIONS,running
    active=sum(running(s) for s in SESSIONS.values())
    llm_status={'configured':llm is not None,'connection':observed['status'] if observed else 'unverified',
                'observation_age_seconds':observed['age_seconds'] if observed else None}
    capacity={'active':active,'limit':settings.max_active_dialogues,'ready':active<settings.max_active_dialogues}
    ready=database['ready'] and llm is not None and not recent_failure and capacity['ready']
    return {'ready':ready,'database':database,'llm':llm_status,'capacity':capacity,
            'external_check':'passive_observation_no_billable_probe','processes_supported':1}
