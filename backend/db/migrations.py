"""Явные миграции. Уже опубликованный manifest неизменяем; следующая схема — новая версия."""
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path
from sqlalchemy import inspect,text

ROOT=Path(__file__).parent
MANIFESTS=('schema_v1.json','schema_v2.json','schema_v3.json')
HEAD=len(MANIFESTS)


class SchemaError(RuntimeError):pass


def _manifests(dialect):
    rows=[]
    for version,name in enumerate(MANIFESTS,1):
        raw=(ROOT/name).read_bytes();data=json.loads(raw)
        if dialect not in data:raise SchemaError('Для этой БД не определены миграции.')
        rows.append((version,hashlib.sha256(raw).hexdigest(),data[dialect]))
    return rows


def upgrade(engine,metadata):
    migrations=_manifests(engine.dialect.name)
    declared={}
    for _,_,tables in migrations:
        for table in tables:declared.setdefault(table['name'],{}).update(table['columns'])
    model={t.name:set(t.columns.keys()) for t in metadata.sorted_tables}
    if model!={name:set(cols) for name,cols in declared.items()}:
        raise SchemaError('Модель БД изменилась без явной миграции.')
    with engine.begin() as connection:
        if engine.dialect.name=='sqlite':connection.exec_driver_sql('BEGIN IMMEDIATE')
        connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum VARCHAR(64) NOT NULL, applied_at VARCHAR(40) NOT NULL)')
        applied=dict(connection.execute(text('SELECT version,checksum FROM schema_migrations')).all())
        if set(applied)!=set(range(1,len(applied)+1)) or any(v>HEAD for v in applied):
            raise SchemaError('Версия БД не поддерживается этим приложением; автоматического отката нет.')
        for version,checksum,tables in migrations:
            if version in applied:
                if applied[version]!=checksum:raise SchemaError('Контрольная сумма опубликованной миграции изменилась.')
                continue
            inspector=inspect(connection);known=set(inspector.get_table_names())
            for table in tables:
                name=table['name']
                if name not in known:connection.exec_driver_sql(table['create']);known.add(name)
                else:
                    have={c['name'] for c in inspect(connection).get_columns(name)}
                    # Только заранее перечисленные поля adoption-миграции прежних SQLite.
                    for column,kind in table['columns'].items():
                        if column not in have:
                            connection.exec_driver_sql(f'ALTER TABLE "{name}" ADD COLUMN "{column}" {kind}')
                existing={i['name'] for i in inspect(connection).get_indexes(name)}
                for index in table['indexes']:
                    if index['name'] not in existing:connection.exec_driver_sql(index['create'])
            connection.execute(text('INSERT INTO schema_migrations(version,checksum,applied_at) VALUES (:v,:c,:a)'),
                {'v':version,'c':checksum,'a':datetime.now(timezone.utc).isoformat()})
        for name,columns in declared.items():
            actual={c['name'] for c in inspect(connection).get_columns(name)}
            if not set(columns)<=actual:raise SchemaError('Сохранённая версия схемы не соответствует структуре БД.')


def revision(connection):
    return connection.execute(text('SELECT MAX(version) FROM schema_migrations')).scalar()
