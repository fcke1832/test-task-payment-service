# Makefile — частые команды проекта test-task-payment-service.
#
# Быстрый старт:
#   make install     # зависимости через uv
#   make up          # весь стек в Docker (Postgres, RabbitMQ, API, consumer)
#   make check       # линтеры + форматирование + типы + тесты
#
# Переменные можно переопределять, например:
#   make api PORT=8001
#   make migration m="add payment method"
#   make logs s=consumer

SHELL := bash
.DEFAULT_GOAL := help

UV ?= uv
COMPOSE ?= docker compose

# Приложение ожидает пакет app из src (см. Dockerfile / migrations/env.py).
PYTHONPATH ?= src
export PYTHONPATH

HOST ?= 0.0.0.0
PORT ?= 8000
WEBHOOK_PORT ?= 9000
API_KEY ?= test-api-key-123

.DELETE_ON_ERROR:

# ---------------------------------------------------------------------------
# Справка
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Список доступных команд
	@awk 'BEGIN {FS = ":.*##"; printf "\nИспользование:\n  make \033[36m<команда>\033[0m\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Окружение и зависимости
# ---------------------------------------------------------------------------

.PHONY: install
install: ## Поставить/обновить зависимости (uv sync)
	$(UV) sync

.PHONY: install-frozen
install-frozen: ## Установить зависимости строго по uv.lock (как в CI)
	$(UV) sync --frozen

.PHONY: lock
lock: ## Пересобрать uv.lock после изменения pyproject.toml
	$(UV) lock

.PHONY: env
env: ## Создать .env из .env.example (если .env ещё нет)
	@if [ -f .env ]; then \
		echo ".env уже существует, не перезаписываю"; \
	else \
		cp .env.example .env; \
		echo "Создан .env — проверьте значения POSTGRES_* и API_KEY"; \
	fi

# ---------------------------------------------------------------------------
# Локальный запуск (uv + .env, инфраструктура в Docker)
# ---------------------------------------------------------------------------

.PHONY: api
api: ## Запустить API с автоперезагрузкой (PORT=8000)
	$(UV) run uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

.PHONY: worker
worker: ## Запустить consumer + outbox relay
	$(UV) run python -m app.messaging.worker

.PHONY: webhook
webhook: ## Поднять локальный приёмник webhook (WEBHOOK_PORT=9000)
	$(UV) run python scripts/webhook_receiver.py --port $(WEBHOOK_PORT)

.PHONY: health
health: ## Проверить /health у локального API (API_KEY=...)
	@curl -fsS -H "X-API-Key: $(API_KEY)" http://localhost:$(PORT)/health
	@echo

# ---------------------------------------------------------------------------
# Миграции Alembic
# ---------------------------------------------------------------------------

.PHONY: migrate
migrate: ## Применить все миграции (alembic upgrade head)
	$(UV) run alembic upgrade head

.PHONY: migrate-docker
migrate-docker: ## Применить миграции в контейнере migrate
	$(COMPOSE) run --rm migrate

.PHONY: migration
migration: ## Новая автогенерируемая миграция: make migration m="short message"
	@test -n "$(m)" || { echo "Укажите сообщение: make migration m=\"short message\""; exit 1; }
	$(UV) run alembic revision --autogenerate -m "$(m)"

.PHONY: revision
revision: ## Пустая миграция вручную: make revision m="short message"
	@test -n "$(m)" || { echo "Укажите сообщение: make revision m=\"short message\""; exit 1; }
	$(UV) run alembic revision -m "$(m)"

.PHONY: downgrade
downgrade: ## Откатить миграции (REV=-1 или конкретная ревизия)
	$(UV) run alembic downgrade $(or $(REV),-1)

.PHONY: current
current: ## Показать текущую ревизию БД
	$(UV) run alembic current

.PHONY: history
history: ## Показать историю миграций
	$(UV) run alembic history --verbose

# ---------------------------------------------------------------------------
# Качество кода и тесты
# ---------------------------------------------------------------------------

.PHONY: lint
lint: ## Проверить код ruff (без изменений)
	$(UV) run ruff check .

.PHONY: lint-fix
lint-fix: ## Исправить автоисправимые замечания ruff
	$(UV) run ruff check --fix .

.PHONY: format
format: ## Отформатировать код ruff format
	$(UV) run ruff format .

.PHONY: format-check
format-check: ## Проверить форматирование без изменений
	$(UV) run ruff format --check .

.PHONY: typecheck
typecheck: ## Проверить типы mypy
	$(UV) run mypy src

.PHONY: test
test: ## Запустить тесты
	$(UV) run pytest

.PHONY: test-v
test-v: ## Запустить тесты подробно (-vv)
	$(UV) run pytest -vv

.PHONY: test-file
test-file: ## Один файл/тест: make test-file f=tests/test_outbox.py
	@test -n "$(f)" || { echo "Укажите путь: make test-file f=tests/test_outbox.py"; exit 1; }
	$(UV) run pytest $(f)

.PHONY: check
check: lint format-check typecheck test ## Полная проверка: lint + format + mypy + pytest

.PHONY: fix
fix: lint-fix format ## Автоисправление и форматирование

# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------

.PHONY: build
build: ## Собрать образ приложения
	$(COMPOSE) build

.PHONY: up
up: ## Поднять весь стек в фоне (с пересборкой)
	$(COMPOSE) up -d --build

.PHONY: up-deps
up-deps: ## Поднять только Postgres и RabbitMQ
	$(COMPOSE) up -d --wait postgres rabbitmq

.PHONY: down
down: ## Остановить и удалить контейнеры (данные сохраняются)
	$(COMPOSE) down

.PHONY: down-v
down-v: ## Остановить стек и удалить тома с данными
	$(COMPOSE) down -v

.PHONY: restart
restart: ## Перезапустить сервисы: make restart s="api" (по умолчанию все)
	$(COMPOSE) restart $(s)

.PHONY: stop-app
stop-app: ## Остановить API и consumer (чтобы запустить их локально)
	$(COMPOSE) stop api consumer

.PHONY: ps
ps: ## Показать статус контейнеров
	$(COMPOSE) ps

.PHONY: logs
logs: ## Логи в реальном времени: make logs s=api
	$(COMPOSE) logs -f --tail=100 $(s)

.PHONY: logs-api
logs-api: ## Логи API
	$(COMPOSE) logs -f --tail=100 api

.PHONY: logs-consumer
logs-consumer: ## Логи consumer
	$(COMPOSE) logs -f --tail=100 consumer

.PHONY: shell
shell: ## Shell внутри контейнера: make shell s=api
	$(COMPOSE) exec $(or $(s),api) bash

.PHONY: db-shell
db-shell: ## psql внутри контейнера postgres
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

.PHONY: rabbit-queues
rabbit-queues: ## Список очередей RabbitMQ
	$(COMPOSE) exec rabbitmq rabbitmqctl list_queues name messages consumers

# ---------------------------------------------------------------------------
# Очистка
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Удалить кеши Python/линтеров и артефакты тестов
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov coverage.xml

.PHONY: clean-venv
clean-venv: ## Удалить локальное окружение .venv
	rm -rf .venv
