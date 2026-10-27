# Payment Processing Service

Асинхронный микросервис процессинга платежей. API принимает платёж, атомарно
сохраняет его и outbox-событие в PostgreSQL, worker публикует событие в
RabbitMQ, эмулирует обработку платежа и отправляет клиенту webhook с повторами
и Dead Letter Queue.

Сервис демонстрирует надёжную асинхронную обработку: **at-least-once**,
**outbox pattern**, **идемпотентность** по `Idempotency-Key`, retry с
экспоненциальной задержкой и DLQ для окончательно упавших сообщений.

## Возможности

- `POST /api/v1/payments` — приём платежа с ответом `202 Accepted`;
- `GET /api/v1/payments/{id}` — текущий статус и состояние webhook;
- идемпотентное создание без дублей при повторных и конкурентных запросах;
- outbox relay: платёж не теряется при недоступном RabbitMQ;
- эмуляция внешнего шлюза: 2–5 секунд, 90% успех / 10% отказ;
- webhook с повторами через TTL-очереди и DLQ;
- просмотр логов контейнеров через Dozzle.

## Стек

| Слой | Технологии |
|---|---|
| API | Python 3.14, FastAPI, Pydantic v2 |
| БД | PostgreSQL 16, SQLAlchemy 2.0 async, asyncpg, Alembic |
| Брокер | RabbitMQ 3, FastStream |
| Webhook | httpx |
| Инфраструктура | Docker Compose, Dozzle |
| Качество | uv, ruff, mypy, pytest |

## Документация

| Документ | Содержание |
|---|---|
| [Предзапусковая подготовка](docs/preflight.md) | пошаговая проверка окружения, тестов, миграций, сервисов, БД и RabbitMQ; troubleshooting |
| [Архитектура](docs/architecture.md) | схема проекта, стек, модель данных, топология RabbitMQ, конфигурация, ограничения |
| [API](docs/api.md) | эндпоинты, заголовки, тела запросов/ответов, коды, идемпотентность, webhook, корнеркейсы |
| [E2E-тестирование](docs/e2e-testing.md) | порядок сквозных сценариев: happy path, retry, DLQ, падение RabbitMQ, гонки |

## Быстрый старт

Требования: Docker Engine 24+, Docker Compose v2, свободные порты `8000`,
`5433`, `5672`, `15672`, `8080`.

```bash
test -f .env || cp .env.example .env
# замените API_KEY и пароли перед использованием вне локальной среды

docker compose up --build -d
docker compose ps -a
```

Ожидаемое состояние: `migrate` — `Exited (0)`, `api` — `healthy`,
`postgres` и `rabbitmq` — `healthy`, `consumer` — `Up`.

Проверка API:

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
curl -H "X-API-Key: $API_KEY" http://localhost:8000/health
# {"status":"ok"}
```

Интерфейсы:

- Swagger UI — [http://localhost:8000/docs](http://localhost:8000/docs);
- RabbitMQ Management — [http://localhost:15672](http://localhost:15672)
  (`payments` / `payments_password` по умолчанию);
- Dozzle (логи) — [http://localhost:8080](http://localhost:8080).

Создание платежа и проверка статуса описаны в [docs/api.md](docs/api.md).

## Порядок запуска и развёртывания

### 1. Подготовка

```bash
git clone https://github.com/fcke1832/test-task-payment-service.git
cd test-task-payment-service
test -f .env || cp .env.example .env
chmod 600 .env
```

Сгенерируйте стойкие секреты и внесите их в `.env`:

```bash
openssl rand -hex 24   # API_KEY
openssl rand -hex 16   # POSTGRES_PASSWORD, RABBITMQ_DEFAULT_PASS
```

Проверьте конфигурацию Compose (вывод содержит секреты — не публикуйте его):

```bash
docker compose config --quiet
```

### 2. Запуск

```bash
docker compose up -d --build
```

Compose сам выполняет миграции через одноразовый сервис `migrate`, после чего
запускает `api` и `consumer`. При первом запуске скачиваются образы
PostgreSQL, RabbitMQ, Dozzle и Python-зависимости — это может занять
несколько минут.

### 3. Проверка

```bash
docker compose ps -a
docker compose logs --tail=100 api consumer

export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
curl -fsS -H "X-API-Key: $API_KEY" http://localhost:8000/health
```

Полный чек-лист, проверка миграций, схемы БД и топологии RabbitMQ —
в [docs/preflight.md](docs/preflight.md). Сквозные сценарии — в
[docs/e2e-testing.md](docs/e2e-testing.md).

### 4. Production-развёртывание

Перед публикацией:

- замените `API_KEY`, `POSTGRES_PASSWORD`, `RABBITMQ_DEFAULT_PASS`;
- не публикуйте наружу порты `5433`, `5672`, `15672`, `8080` (в текущем
  compose они проброшены для удобства);
- поставьте API за reverse proxy с TLS; наружу нужен только `443` (и `80`
  для ACME);
- закройте `/docs`, `/redoc`, `/openapi.json`, если API публичный;
- для произвольных `webhook_url` настройте allowlist доменов (снижение
  SSRF-риска);
- Dozzle не должен быть доступен из интернета: он подключён к Docker socket
  и без аутентификации показывает логи всех контейнеров;
- настройте мониторинг health, длин очередей, DLQ, ошибок consumer и места на
  volume;
- проверьте backup PostgreSQL.

Подробные команды backup/restore и production-чек-лист —
в [docs/preflight.md](docs/preflight.md#10-production-проверки-перед-публикацией).

### 5. Обновление

```bash
docker compose up -d --build
```

Миграции применяются автоматически. После изменения `.env` пересоздайте
затронутые контейнеры:

```bash
docker compose up -d --force-recreate api consumer
```

### 6. Остановка

```bash
docker compose stop          # остановить, сохранив данные
docker compose start         # запустить снова
docker compose down          # удалить контейнеры и сеть, данные сохранить
docker compose down -v       # удалить контейнеры и все данные (необратимо)
```

## Локальный запуск без Docker

PostgreSQL и RabbitMQ поднимаются в Docker, API и worker — локальными
процессами:

```bash
uv sync
test -f .env || cp .env.example .env
docker compose up -d --wait postgres rabbitmq

uv run alembic upgrade head
PYTHONPATH=src uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Во втором терминале:

```bash
PYTHONPATH=src uv run python -m app.messaging.worker
```

В `.env` для локального запуска должны быть `POSTGRES_HOST=localhost`,
`POSTGRES_PORT=5433` и `RABBITMQ_URL` с хостом `localhost`. Webhook для
локального worker'а указывайте на `http://localhost:9000/webhook`, приёмник:

```bash
python3 scripts/webhook_receiver.py --port 9000
```

## Тесты и проверки

```bash
make install
make check       # ruff check + ruff format --check + mypy + pytest
```

Или по отдельности:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
PYTHONPATH=src uv run mypy src
```

Unit-тесты не требуют PostgreSQL и RabbitMQ: проверяются схемы, API-key
dependency, retry-заголовки, webhook payload и backoff outbox. Сквозные
сценарии выполняются отдельно по [docs/e2e-testing.md](docs/e2e-testing.md).

## Структура

```text
src/app/
  api/            # HTTP-слой, зависимости, эндпоинты
  core/           # конфигурация
  db/             # async engine/session
  models/         # Payment, Outbox, enum'ы
  schemas/        # Pydantic-схемы
  services/       # бизнес-логика создания платежа
  messaging/      # broker, outbox relay, consumer, worker
migrations/       # Alembic
scripts/          # webhook-приёмник
tests/            # unit-тесты
docs/             # документация
docker-compose.yml
Dockerfile
```

Подробное описание компонентов и потоков — в
[docs/architecture.md](docs/architecture.md).

## Ограничения

- webhook не подписывается HMAC;
- API-ключ один и статический;
- один consumer с `prefetch=1` — обработка последовательная;
- метрики и трейсинг не подключены, DLQ не вычитывается автоматически;
- Swagger UI и `/openapi.json` не защищены API-ключом.

Полный список и направления развития — в
[docs/architecture.md](docs/architecture.md#13-ограничения-и-направления-развития).
