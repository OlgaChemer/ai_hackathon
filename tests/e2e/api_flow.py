"""Сквозной прогон через HTTP API (как браузер): создать диалог, слушать ленту, отвечать на карточки.

  .venv/bin/python -m tests.e2e.api_flow --base http://127.0.0.1:8790 --query "Двушка в Казани до 9 млн, наличными"
Печатает ход сделки и итог; код выхода 0 — сделка дошла до подписания.
"""
from __future__ import annotations

import argparse
import json
import threading
import time

import httpx

ANSWERS = {
    "payment": (True, {"payment": "cash"}),
    "family": (True, {"children": 1, "youngest_age": 4, "matcap": 737200}),
    "housing": (True, {"housing": "both"}),
    "call": (True, {"mode": "auto"}),
}


def run(base: str, query: str, answers: dict | None = None, timeout: float = 120) -> dict:
    answers = {**ANSWERS, **(answers or {})}
    c = httpx.Client(base_url=base, timeout=30)
    c.get("/api/me")
    sid = c.post("/api/session", json={"query": query}).json()["id"]
    seen: dict = {"client": c, "sid": sid, "kinds": [], "messages": [], "steps": set(), "results": [], "errors": []}
    shown: list = []

    def answer(kind: str, ev: dict):
        time.sleep(0.2)
        approved, data = answers.get(kind, (True, {}))
        if kind == "selection":
            data = {"action": "proceed", "liked": [shown[0]["id"]] if shown else [], "disliked": []}
        r = c.post(f"/api/session/{sid}/confirm", json={"kind": kind, "approved": approved, "data": {**data, "_label": "e2e"}})
        if r.status_code != 200:
            seen["errors"].append(f"confirm {kind}: {r.status_code} {r.text[:120]}")

    start = time.time()
    with c.stream("GET", f"/api/chat/{sid}/stream", timeout=None) as r:
        for line in r.iter_lines():
            if time.time() - start > timeout:
                seen["errors"].append("таймаут")
                break
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            t = ev["type"]
            if t == "message":
                seen["messages"].append((ev["role"], ev["text"]))
            elif t == "step" and ev["status"] == "active":
                seen["steps"].add(ev["step"])
            elif t == "result":
                seen["results"].append(ev["kind"])
                if ev["kind"] == "selection":
                    shown[:] = ev["data"]["cards"]
            elif t == "confirm":
                seen["kinds"].append(ev["kind"])
                threading.Thread(target=answer, args=(ev["kind"], ev)).start()
            elif t == "error":
                seen["errors"].append(ev["text"])
                break
            if t == "message" and ev["role"] == "agent" and "Сделка оформлена" in ev["text"]:
                break
    seen["steps"] = sorted(seen["steps"])
    return seen


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    ap.add_argument("--query", default="Двушка в Казани до 9 млн, наличными")
    a = ap.parse_args()
    res = run(a.base, a.query)
    for role, text in res["messages"]:
        print(f"{role:>5}: {text[:140]}")
    print("карточки:", res["kinds"])
    print("панели:", res["results"], "шаги:", res["steps"])
    print("ошибки:", res["errors"])
    raise SystemExit(0 if "sign" in res["kinds"] and not res["errors"] else 1)
