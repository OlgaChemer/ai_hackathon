"""Сервер для сквозной проверки без сети: настоящий backend.main, но
- модель — PolicyLLM (правила ведут агента обычным ходом сделки),
- Циан — сохранённые настоящие объявления из tests/e2e/data (Казань вторичка и новостройки, Москва; 2 комнаты),
  отдаются как «живая» выдача, страница объявления — «активно»,
- карта (OpenStreetMap) — недоступна, как без сети.

Запуск: .venv/bin/python -m tests.e2e.server --port 8790   (база — во временном файле)
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def patch() -> None:
    from backend import geo, main
    from backend.sources import cian
    from backend.sources.base import SourceUnavailable

    from .policy_llm import PolicyLLM

    llm = PolicyLLM()
    main.get_llm = lambda: llm

    async def fetch_page(city, rooms, budget, page, okrug=None, new=False):
        if page > 1:
            raise SourceUnavailable("ответ 302 (страниц больше нет)")
        path = DATA / f"{cian.REGIONS[city][0]}_{rooms}{'_new' if new else ''}.json"
        if not path.exists():
            raise SourceUnavailable("e2e: для этого города нет сохранённых объявлений")
        items = json.loads(path.read_text(encoding="utf-8"))["listings"]
        return [x for x in items if not budget or x["price"] <= budget * 1.05], "e2e"

    async def check_page(url):
        return {"alive": True, "note": "e2e: страница открывается", "offer": None}

    async def no_overpass(query, timeout):
        raise geo.httpx.ConnectError("e2e: без сети")

    cian._fetch_page = fetch_page
    cian.check_listing_page = check_page
    geo._overpass = no_overpass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/e2e.db")
    patch()
    import uvicorn

    from backend.main import app
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
