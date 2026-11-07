"""Проверка ключа GigaChat: токен, список моделей, пробный вопрос.

  .venv/bin/python -m backend.llm.check          # токен + модели
  .venv/bin/python -m backend.llm.check --chat   # плюс короткий запрос к GIGACHAT_MODEL

Ключ — GIGACHAT_CREDENTIALS в .env («Authorization key» из личного кабинета GigaChat API).
"""
from __future__ import annotations

import argparse
import asyncio
import ssl
import sys
import time

import httpx

from ..config import settings
from .base import LLMError


async def main(chat: bool) -> int:
    from .gigachat import GigaChatClient

    print(f"OAuth: {settings.gigachat_auth_url} (scope {settings.gigachat_scope})")
    print(f"API:   {settings.gigachat_base_url}")
    if not settings.gigachat_credentials:
        print("\n✗ GIGACHAT_CREDENTIALS пуст: получите ключ в личном кабинете GigaChat API и добавьте в .env")
        return 1
    client = GigaChatClient(settings.gigachat_credentials, settings.gigachat_scope, settings.gigachat_model,
                            settings.gigachat_auth_url, settings.gigachat_base_url, settings.gigachat_verify_ssl,
                            settings.gigachat_ca_bundle)
    try:
        await client._get_token()
        print(f"✓ токен получен, действует ещё ~{int((client._token_expires_at - time.time()) / 60)} мин")
        models = await client.models()
        print("✓ модели: " + ", ".join(models))
        if settings.gigachat_model not in models:
            print(f"! GIGACHAT_MODEL={settings.gigachat_model} нет в списке — поменяйте в .env")
        if chat:
            r = await client.chat([{"role": "user", "content": "Ответь одним предложением: ты на связи?"}], max_tokens=50)
            print(f"✓ ответ {settings.gigachat_model}: {r.text.strip()}")
    except (httpx.ConnectError, ssl.SSLError) as exc:
        print(f"✗ нет соединения: {exc}\n  Ошибка сертификата — укажите GIGACHAT_CA_BUNDLE=путь к сертификату НУЦ Минцифры "
              "(или временно GIGACHAT_VERIFY_SSL=false)")
        return 2
    except LLMError as exc:
        print(f"✗ {exc}\n  401 — неверный ключ или scope (физлица — GIGACHAT_API_PERS, ИП и юрлица — GIGACHAT_API_B2B/CORP)")
        return 3
    print("\nГотово. Чтобы агент работал на GigaChat: LLM_PROVIDER=gigachat в .env и перезапустить сервер.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Проверка ключа GigaChat")
    ap.add_argument("--chat", action="store_true", help="пробный запрос к модели")
    sys.exit(asyncio.run(main(ap.parse_args().chat)))
