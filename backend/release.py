"""Версии контрактов в отчётах приёмки; не описание инфраструктуры для клиента."""
import hashlib
import json
from pathlib import Path
from .config import ROOT


def fingerprint(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def manifest(llm=None):
    from .agent import prompts,understanding,capabilities,listing_query,request_schema
    from .calculation_policy import POLICY
    from .db.migrations import HEAD
    return {'prompt_version':fingerprint([prompts.AGENT_SYSTEM,prompts.QA_SYSTEM,understanding.ROUTER_SYSTEM,understanding.PROFILE_SYSTEM]),
            'contract_version':fingerprint([capabilities.catalog(),listing_query.QUERY_SCHEMA,request_schema.CHANGE_SCHEMA]),
            'policy_version':fingerprint(POLICY),'schema_version':HEAD,
            'runtime_version':fingerprint({p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((ROOT/'backend').rglob('*.py'))}),
            'provider':getattr(llm,'name',None),'requested_model':getattr(llm,'model',None)}
