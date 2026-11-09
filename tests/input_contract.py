"""Фабрика ответов тестовых моделей по тому же контракту, что использует GigaChat."""

from copy import deepcopy

from backend.agent import input_frame
from backend.agent.request_schema import CHANGE_SCHEMA


def slots(*, fields=None, criteria=None, places=None, intent="refine", selection=None, call_ids=None, unsupported=None):
    fields=deepcopy(fields or {})
    if 'rooms' in fields:
        rooms=fields.pop('rooms')
        if rooms==0:fields['studio_requested']=True
        elif 'rooms_options' not in fields:fields['rooms_options']=[rooms]
    if 0 in (fields.get('rooms_options') or []):
        fields['studio_requested']=True
        fields['rooms_options']=[n for n in fields['rooms_options'] if n!=0]
    if selection is not None and 'operation' not in selection:
        selection={'operation':'more' if selection['mode']=='more' else 'rank',
                   **{k:selection[k] for k in ('scope','limit','sort','aggregates','group_by')}}
    schema = input_frame.schema(CHANGE_SCHEMA)
    return {
        "places": [
            {"words": [{"source": word, "lemma": word} for word in place.split()]}
            for place in places or []
        ],
        "fields": {
            **{key: None for key in schema["properties"]["fields"]["properties"]},
            **{k: {'value':v,'source':''} if v is not None else None for k,v in (fields or {}).items()},
        },
        "criteria": {
            **{key: None for key in schema["properties"]["criteria"]["properties"]},
            **(criteria or {}),
        },
        "question": "",
        "options": [],
        "intent": intent,
        "selection": selection,
        "unsupported": unsupported or [],
        "call_ids": call_ids,
    }


def input_reply(text, changes=None, *, intent="refine", profile=None):
    """Компактные фикстуры параметров превращаются в настоящий внешний контракт только в тестах."""
    changes, profile = deepcopy(changes or {}), profile or {}
    if 'rooms' in changes and 'rooms_options' not in changes:
        changes['rooms_options']=[changes['rooms']]
    if 0 in (changes.get('rooms_options') or []):
        changes['studio_requested']=True
        changes['rooms_options']=[n for n in changes['rooms_options'] if n!=0]
    result = slots(intent=intent)
    for key in result["fields"]:
        if key in changes:
            result["fields"][key] = {"value":changes[key],"source":text} if changes[key] is not None else None
    preferred = set(changes.get("soft_criteria", profile.get("soft_criteria") or []))
    for key in result["criteria"]:
        if key in changes:
            value = changes[key]
            if isinstance(value, bool):
                result["criteria"][key] = (
                    ("preferred" if key in preferred else "required")
                    if value
                    else "remove"
                )
            else:
                result["criteria"][key] = {
                    "value": value,
                    "priority": "preferred" if key in preferred else "required",
                }
    for group, prefix in (("poi", "poi:"), ("features", "feature:")):
        if group not in changes:
            continue
        for key in set(profile.get(group) or []) | set(changes[group]):
            name = prefix + key
            result["criteria"][name] = (
                ("preferred" if name in preferred else "required")
                if key in changes[group]
                else "remove"
            )
    for key in changes.get("clear_criteria", []):
        result["criteria"][key] = (
            "remove"
            if key.startswith(("poi:", "feature:"))
            or key in ("not_first_floor", "not_last_floor")
            else {"value": None, "priority": "remove"}
        )
    tokens = input_frame.source_words([{"text": text}])
    for place in ([changes["city"]] if changes.get("city") else []) + changes.get(
        "cities", []
    ):
        words = [
            {
                "source": next(
                    (
                        token
                        for token in tokens
                        if token.casefold().startswith(word[:3 if len(word) > 3 else 2].casefold())
                    ),
                    word,
                ),
                "lemma": word,
            }
            for word in place.split()
        ]
        result["places"].append({"words": words})
    return result
