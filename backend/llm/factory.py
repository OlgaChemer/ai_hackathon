"""Выбор провайдера по LLM_PROVIDER (deepseek | gigachat). Без модели агент не работает — подмен нет."""
from __future__ import annotations

from functools import lru_cache

from ..config import settings
from .base import LLMClient, LLMError


def build_llm(provider: str | None = None) -> LLMClient:
    """Клиент модели. Нет ключа или неизвестный провайдер — LLMError с понятной причиной."""
    provider = (provider or settings.llm_provider).lower()
    if provider == "deepseek":
        from .deepseek import DeepSeekClient

        return DeepSeekClient(
            settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model,
            timeout=settings.llm_call_timeout_s,
        )
    if provider == "gigachat":
        from .gigachat import GigaChatClient

        return GigaChatClient(
            credentials=settings.gigachat_credentials,
            scope=settings.gigachat_scope,
            model=settings.gigachat_model,
            auth_url=settings.gigachat_auth_url,
            base_url=settings.gigachat_base_url,
            verify_ssl=settings.gigachat_verify_ssl,
            ca_bundle=settings.gigachat_ca_bundle,
            timeout=settings.llm_call_timeout_s,
            max_waiters=settings.gigachat_queue_size,
            queue_timeout=settings.gigachat_queue_timeout_s,
        )
    raise LLMError(f"Неизвестный LLM_PROVIDER: {provider} (нужен deepseek или gigachat)")


@lru_cache
def get_provider(provider: str) -> LLMClient:
    """Один клиент/очередь на провайдера во всём приложении, включая мастерскую."""
    return build_llm(provider)


@lru_cache
def get_vision_provider(provider:str) -> LLMClient:
    if provider=='gigachat':return get_provider(provider)  # общая очередь тарифа
    if provider=='deepseek':
        from .deepseek import DeepSeekClient
        return DeepSeekClient(settings.deepseek_api_key,settings.deepseek_base_url,
            settings.deepseek_vision_model,timeout=settings.llm_call_timeout_s)
    raise LLMError('Не настроена модель для анализа фотографий.')


@lru_cache
def get_llm() -> LLMClient:
    return get_provider(settings.llm_provider)
