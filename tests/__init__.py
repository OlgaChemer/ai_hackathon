
# Тесты не должны зависеть от собранных геоданных городов (backend/geodata/cities): без них агент идёт в 2ГИС/Overpass,
# и именно это проверяют тесты поиска объектов рядом. Реальные файлы проверяет tests/test_geodata.RealDataTest.
import os as _os
import tempfile as _tempfile
_os.environ.setdefault("GEODATA_DIR", _tempfile.mkdtemp(prefix="geodata-empty-"))
# Сценарные LLM-тесты проверяют контракт модели. Активный локальный разбор
# отдельно проверяют RuleIntegrationTest и тесты 18 реальных городов.
_os.environ.setdefault('RULE_PARSER','shadow')
