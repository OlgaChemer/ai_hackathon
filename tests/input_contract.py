"""Фабрика ответов тестовых моделей по тому же контракту, что использует GigaChat."""

from copy import deepcopy

from backend.agent import input_frame
from backend.agent.request_schema import CHANGE_SCHEMA


def slots(*, fields=None, criteria=None, places=None, intent="refine"):
    schema = input_frame.schema(CHANGE_SCHEMA)
    return {
        "places": [
            {"words": [{"source": word, "lemma": word} for word in place.split()]}
            for place in places or []
        ],
        "fields": {
            **{key: None for key in schema["properties"]["fields"]["properties"]},
            **(fields or {}),
        },
        "criteria": {
            **{key: None for key in schema["properties"]["criteria"]["properties"]},
            **(criteria or {}),
        },
        "question": "",
        "options": [],
        "intent": intent,
    }


def input_reply(text, changes=None, *, intent="refine", profile=None):
    """Компактные фикстуры параметров превращаются в настоящий внешний контракт только в тестах."""
    changes, profile = deepcopy(changes or {}), profile or {}
    result = slots(intent=intent)
    for key in result["fields"]:
        if key in changes:
            result["fields"][key] = changes[key]
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
                        if token.casefold().startswith(word[:3].casefold())
                    ),
                    word,
                ),
                "lemma": word,
            }
            for word in place.split()
        ]
        result["places"].append({"words": words})
    return result
