"""Настройки приложения из .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    workshop_provider: str = os.getenv('WORKSHOP_LLM_PROVIDER','deepseek').strip().lower()
    workshop_email: str = 'darkyhat@mail.ru'
    workshop_root: str = os.getenv('WORKSHOP_ROOT', str(ROOT/'backend'/'data'/'workshop'))
    workshop_image: str = os.getenv('WORKSHOP_IMAGE','mesto-workshop:0.4.2')
    workshop_docker: str = os.getenv('WORKSHOP_DOCKER','/usr/bin/docker')
    llm_provider: str = os.getenv("LLM_PROVIDER", "gigachat").strip().lower()
    # Разбор простых сообщений кодом (agent/rule_parser.py): off — всегда модель; shadow — модель, а разбор кодом
    # только сравнивается с ней (журнал rule_parser, /api/metrics); on — простое сообщение без двух запросов к модели.
    rule_parser: str = (os.getenv("RULE_PARSER") or "on").strip().lower()

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_vision_model: str = os.getenv('DEEPSEEK_VISION_MODEL','deepseek-flash')

    gigachat_credentials: str = os.getenv("GIGACHAT_CREDENTIALS", "")
    gigachat_scope: str = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    gigachat_model: str = os.getenv("GIGACHAT_MODEL", "GigaChat-3-Ultra")
    gigachat_auth_url: str = os.getenv(
        "GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    )
    gigachat_base_url: str = os.getenv("GIGACHAT_BASE_URL") or "https://api.giga.chat/v1"
    gigachat_verify_ssl: bool = _bool("GIGACHAT_VERIFY_SSL", True)
    gigachat_ca_bundle: str = os.getenv("GIGACHAT_CA_BUNDLE", "")

    # Время активной работы: ожидание ответа человека в него не входит.
    agent_turn_timeout_s: float = max(1, float(os.getenv("AGENT_TURN_TIMEOUT_S") or 120))
    gigachat_queue_size: int = max(0, int(os.getenv("GIGACHAT_QUEUE_SIZE") or 6))
    gigachat_queue_timeout_s: float = max(.1, float(os.getenv("GIGACHAT_QUEUE_TIMEOUT_S") or 25))
    llm_call_timeout_s: float = max(1, float(os.getenv("LLM_CALL_TIMEOUT_S") or 60))
    sse_queue_capacity: int = max(8,int(os.getenv('SSE_QUEUE_CAPACITY') or 128))
    command_queue_capacity: int = max(2,int(os.getenv('COMMAND_QUEUE_CAPACITY') or 64))
    question_queue_capacity: int = max(2,int(os.getenv('QUESTION_QUEUE_CAPACITY') or 16))
    history_memory_events: int = max(80,int(os.getenv('HISTORY_MEMORY_EVENTS') or 512))
    max_active_dialogues: int = max(1,int(os.getenv('MAX_ACTIVE_DIALOGUES') or 32))
    max_cached_dialogues: int = max(max_active_dialogues+8,int(os.getenv('MAX_CACHED_DIALOGUES') or 64))
    max_dialogue_subscribers: int = max(1,int(os.getenv('MAX_DIALOGUE_SUBSCRIBERS') or 4))
    phone_tts_char_limit: int = max(500,int(os.getenv('PHONE_TTS_CHAR_LIMIT') or 12000))
    phone_calls_per_hour: int = max(1,int(os.getenv('PHONE_CALLS_PER_HOUR') or 5))
    phone_call_max_seconds: int = max(30,min(900,int(os.getenv('PHONE_CALL_MAX_SECONDS') or 300)))

    # МТС Exolve: настоящий звонок продавцу (синтез речи + SIP через WebRTC)
    exolve_api_key: str = os.getenv("EXOLVE_API_KEY", "")
    exolve_voice: int = int(os.getenv("EXOLVE_VOICE", "13") or 13)  # 13 — Кирилл
    seller_phone: str = os.getenv("SELLER_PHONE", "")
    # PIN для звонков на публичном сервере: без него любой посетитель звонил бы за ваш счёт
    phone_pin: str = os.getenv("PHONE_PIN", "")

    # База: пользователи, история диалогов, объявления с признаками
    database_url: str = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'backend' / 'data' / 'app.db'}"
    auth_session_days: int = int(os.getenv("AUTH_SESSION_DAYS", "30") or 30)
    cookie_secure: bool = _bool("COOKIE_SECURE", False)  # true — за HTTPS

    # 2GIS: каталог инфраструктуры и растровая подложка. Без ключа — OpenStreetMap.
    twogis_api_key: str = os.getenv("TWOGIS_API_KEY", "").strip()
    places_budget_s: float = max(0.1, float(os.getenv("PLACES_BUDGET_S") or 12))
    search_budget_s: float = max(1,float(os.getenv('SEARCH_BUDGET_S') or 45))

    # Ночной обход объявлений: время (ЧЧ:ММ, пусто — выключен), сколько страниц выдачи на поиск,
    # сколько пропавших объявлений проверить за раз
    refresh_at: str = os.getenv("REFRESH_AT", "").strip()
    refresh_pages: int = int(os.getenv("REFRESH_PAGES", "3") or 3)
    refresh_check_limit: int = int(os.getenv("REFRESH_CHECK_LIMIT", "40") or 40)


settings = Settings()
