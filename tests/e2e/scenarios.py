"""Четыре полные сделки через HTTP API против tests.e2e.server: до подписания, без ошибок.

  .venv/bin/python -m tests.e2e.scenarios --base http://127.0.0.1:8790
"""
from __future__ import annotations

import argparse

from .api_flow import run

CASES = [
    ("Казань, наличные", "Двушка в Казани до 9 млн, вторичка, наличными"),
    ("Семья, маткапитал, копит на взнос", "Ищу 2-комнатную в Казани до 9 млн, ипотека, на взнос ещё копим, сын 4 года, есть маткапитал"),
    ("Москва, ипотека, взнос есть", "Двушка в Москве до 20 млн, ипотека, взнос есть, детей нет"),
    ("Казань, новостройки и вторичка", "Двушка в Казани до 9 млн, новостройки и вторичка, наличными"),
]


def main(base: str) -> int:
    bad = 0
    for name, query in CASES:
        r = run(base, query, timeout=150)
        good = "sign" in r["kinds"] and not r["errors"]
        bad += not good
        print("✓" if good else "✗", name, "| карточки:", r["kinds"], "| панели:", sorted(set(r["results"])), "| ошибки:", r["errors"])
        if not good:
            for role, text in r["messages"][-6:]:
                print("    ", role, text[:160])
    print(f"ИТОГО: {len(CASES) - bad}/{len(CASES)}")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    raise SystemExit(main(ap.parse_args().base))
