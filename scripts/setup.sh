#!/usr/bin/env bash
# Установка одной командой: окружение, пакеты, модель распознавания речи, .env из шаблона.
#   ./scripts/setup.sh            — модель small (по умолчанию)
#   WHISPER_MODEL=base ./scripts/setup.sh — модель поменьше (~140 МБ), если интернет медленный
#   SKIP_WHISPER=1 ./scripts/setup.sh     — без модели (голос будет распознавать браузер)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "→ Создаю виртуальное окружение .venv"
  python3 -m venv .venv
fi

echo "→ Ставлю пакеты из requirements.txt"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ Создал .env из .env.example — впишите DEEPSEEK_API_KEY (или настройки GigaChat)"
fi

if [ "${SKIP_WHISPER:-0}" != "1" ]; then
  echo "→ Скачиваю модель распознавания речи"
  .venv/bin/python -m backend.stt download "${WHISPER_MODEL:-small}"
fi

echo
echo "Готово. Запуск: .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000"
