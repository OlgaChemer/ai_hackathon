"""Удобства, о которых просит клиент (балкон, санузел, гардеробная…), и что спросить у продавца.

Есть ли удобство — по полям площадки, описанию и словам продавца из звонка (x["confirmed"] — важнее всего).
"""
from __future__ import annotations

import re


# ключ: (как назвать, что искать в пожелании и описании, вопрос продавцу)
FEATURES = {
    "balcony": ("балкон или лоджия", r"балкон|лоджи", "есть ли балкон или лоджия"),
    "loggia": ("лоджия", r"лоджи", "есть ли лоджия"),
    "balcony_only": ("балкон", r"балкон", "есть ли именно балкон"),
    "balcony_loggia": ("и балкон, и лоджия", r"балкон|лоджи", "есть ли и балкон, и лоджия"),
    "wardrobe": ("гардеробная", r"гардеробн", "есть ли гардеробная"),
    "storage": ("кладовая", r"кладов", "есть ли кладовая"),
    "parking": ("парковка", r"паркинг|парковк|машино-?мест", "есть ли парковка или машиноместо"),
    "separate_wc": ("раздельный санузел", r"раздельн\w*\s+(?:сан)?узел|(?:сан)?узел\w*\s+раздельн", "санузел раздельный или совмещённый"),
    "furniture": ("мебель", r"мебел", "остаётся ли мебель"),
}


FEATURE_WITH = {"balcony": "балконом или лоджией", "balcony_loggia": "балконом и лоджией", "loggia": "лоджией", "balcony_only": "балконом", "wardrobe": "гардеробной", "storage": "кладовой",
                "parking": "парковкой", "separate_wc": "раздельным санузлом", "furniture": "мебелью"}


def merge_features(*lists: list[str]) -> list[str]:
    """Объединяет пожелания; «и балкон, и лоджия» уже включает «балкон или лоджия»."""
    out = list(dict.fromkeys(k for keys in lists for k in keys))
    return [k for k in out if not (k == "balcony" and "balcony_loggia" in out)]


def text_evidence(text: str, pattern: str) -> tuple[bool | None, str | None]:
    """Короткая цитата и три состояния. Противоречивое описание требует уточнения."""
    findings = []
    for clause in re.split(r"[.!?;,\n]", (text or "").lower().replace("ё", "е")):
        if not re.search(pattern, clause):
            continue
        neg = re.search(rf"\b(?:без|нет|отсутствует|отсутствуют)\s+(?:\w+\s+){{0,2}}(?:{pattern})", clause)
        neg = neg or re.search(rf"(?:{pattern})\w*\s+(?:нет|отсутствует|отсутствуют|не предусмотрен)", clause)
        findings.append((not bool(neg), clause.strip()[:180]))
    if not findings:
        return None, None
    if len({v for v, _ in findings}) != 1:
        return None, " / ".join(q for _, q in findings)[:180]
    return findings[0]


def feature_evidence(x: dict, key: str) -> dict:
    value = (x.get("confirmed") or {}).get(key)
    if value is not None:
        return {"status": bool(value), "source": "seller_call", "evidence": "уточнено продавцом"}
    total, loggias = x.get("balconies"), x.get("loggias")
    if key == "balcony_loggia":
        parts = [feature_evidence(x, k) for k in ("balcony_only", "loggia")]
        states = [p['status'] for p in parts]
        result = False if False in states else True if all(v is True for v in states) else None
        return {"status": result, "source": "cian", "evidence": "; ".join(str(p['evidence']) for p in parts)}
    if key == "balcony" and total is not None:
        value = total > 0
    elif key == "loggia" and loggias is not None:
        value = loggias > 0
    elif key == "balcony_only" and total is not None and loggias is not None:
        value = total - loggias > 0
    elif key == "balcony_only" and total == 0:
        value = False
    elif key == "separate_wc" and x.get("separate_wc") is not None:
        value = bool(x["separate_wc"])
    if value is not None:
        return {"status": value, "source": "cian", "evidence": "поле объявления"}
    value, quote = text_evidence(x.get("description") or "", FEATURES[key][1])
    return {"status": value, "source": "cian_description" if quote else None, "evidence": quote}


def feature_status(x: dict, key: str) -> bool | None:
    return feature_evidence(x, key)["status"]


MISSING_QUESTIONS = {
    "condition": ("состояние и ремонт", "в каком состоянии квартира, какой ремонт"),
    "year": ("год постройки", "какого года дом"),
    "kitchen": ("площадь кухни", "какая площадь кухни"),
    "floors": ("этажность дома", "сколько этажей в доме"),
}


ALWAYS_ASK = [("owners", "собственники", "сколько собственников и как давно квартира в собственности")]


NEW_BUILD_QUESTIONS = [
    ("deadline", "срок сдачи", "когда сдача дома и выдача ключей, были ли переносы"),
    ("condition", "отделка", "какая отделка — черновая, предчистовая или чистовая"),
    ("contract", "договор и эскроу", "по какому договору продаёте — ДДУ с эскроу, и есть ли скидка при ипотеке"),
]


def missing_info(listing: dict, limit: int = 3, features: list[str] | tuple = ()) -> list[dict]:
    """Что спросить у продавца: сначала удобства, о которых просил клиент, потом пробелы в объявлении.
    В новостройке — срок сдачи, отделка, договор (собственников и прописанных там нет)."""
    if listing.get("is_new"):
        out = [{"key": k, "label": FEATURES[k][0], "question": FEATURES[k][2]}
               for k in features if k in FEATURES and feature_status(listing, k) is None]
        out += [{"key": k, "label": lab, "question": q} for k, lab, q in NEW_BUILD_QUESTIONS
                if k == "contract" or listing.get(k) is None]
        return out[:limit]
    out = [{"key": k, "label": FEATURES[k][0], "question": FEATURES[k][2]}
           for k in features if k in FEATURES and feature_status(listing, k) is None]
    out += [{"key": k, "label": lab, "question": q} for k, (lab, q) in MISSING_QUESTIONS.items() if listing.get(k) is None]
    out += [{"key": k, "label": lab, "question": q} for k, lab, q in ALWAYS_ASK]
    return out[:limit]
