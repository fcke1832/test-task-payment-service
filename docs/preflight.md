# Предзапусковая подготовка

Документ описывает проверки, которые нужно выполнить **до** первого
использования сервиса, перед демонстрацией/приёмкой и после изменений
инфраструктуры, схемы БД или конфигурации.

Порядок важен: сначала проверяются зависимости и тесты, затем PostgreSQL и
RabbitMQ, потом миграции, и только после этого запускаются API и consumer.
`api` и `consumer` в compose не стартуют, пока job `migrate` не завершится с
кодом `0`, — это осознанная защита от работы с несовершенной схемой.

Все команды выполняются из корня репозитория.

## 1. Требования к окружению

| Компонент | Требование |
|---|---|
| Docker Engine | 24+ (Docker Desktop или серверный Docker) |
| Docker Compose | v2, команда `docker compose` |
| Свободная память | минимум ~2 GB |
| Интернет | для первой загрузки образов и зависимостей |
| Python (для локальных проверок) | 3.14 |
| uv | для `uv sync` / `uv run` |
| curl, python3 | для smoke-проверок и разбора JSON |

Порты на хосте:

| Порт | Сервис | Можно изменить |
|---|---|---|
| `8000` | API | `ports` сервиса `api` в `docker-compose.yml` |
| `5433` | PostgreSQL | `POSTGRES_HOST_PORT` |
| `5672` | RabbitMQ AMQP | `ports` сервиса `rabbitmq` |
| `15672` | RabbitMQ Management UI | `ports` сервиса `rabbitmq` |
| `8080` | Dozzle | `DOZZLE_PORT` |
| `9000` | локальный webhook-приёмник (вне compose) | аргумент `--port` |

Проверка Docker и портов:

```bash
docker --version
docker compose version
docker info >/dev/null && echo "docker: OK"

lsof -nP -iTCP:8000 -iTCP:5433 -iTCP:5672 -iTCP:15672 -iTCP:8080 -sTCP:LISTEN
# Linux-альтернатива:
ss -ltnp | grep -E ':(8000|5433|5672|15672|8080)\b'
```

Если порт занят, освободите его или измените соответствующую переменную/порт
до запуска стека.

## 2. Подготовка `.env`

```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

Минимально проверьте:

```dotenv
POSTGRES_USER=app_user
POSTGRES_PASSWORD=app_password
POSTGRES_DB=payment_db
RABBITMQ_DEFAULT_USER=payments
RABBITMQ_DEFAULT_PASS=payments_password
API_KEY=test-api-key-123
```

Для реального окружения замените секреты:

```bash
openssl rand -hex 24   # API_KEY
openssl rand -hex 16   # POSTGRES_PASSWORD / RABBITMQ_DEFAULT_PASS
```

Проверьте, что `.env` не попадёт в Git:

```bash
git check-ignore -v .env
git status --short
```

Ожидается строка из `.gitignore`; `.env` не должен быть в `git status`.

Проверьте синтаксис и подстановку Compose. Команда `docker compose config`
печатает секреты — не публикуйте её вывод:

```bash
docker compose config --quiet && echo "compose config: OK"
docker compose config --services
```

Ожидаемые сервисы: `postgres`, `rabbitmq`, `dozzle`, `migrate`, `api`,
`consumer`.

Помните о различиях локального запуска и Docker:

- в compose `POSTGRES_HOST=postgres`, `POSTGRES_PORT=5432`; значения
  `localhost:5433` из `.env` нужны только для API/worker, запущенных на хосте;
- `RABBITMQ_URL` внутри compose собирается из `RABBITMQ_DEFAULT_USER` и
  `RABBITMQ_DEFAULT_PASS` с хостом `rabbitmq`;
- `APP_ENV=production` для контейнеров задаётся в compose;
- после изменения `.env` контейнеры нужно пересоздать:
  `docker compose up -d --force-recreate api consumer`.

## 3. Зависимости, линтеры и unit-тесты

```bash
make install          # uv sync
make check            # ruff check + ruff format --check + mypy + pytest
```

Эквивалент без Makefile:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
PYTHONPATH=src uv run mypy src
uv run pytest
```

Ожидаемый результат:

- `ruff` не находит ошибок;
- форматирование соответствует;
- `mypy` завершается без ошибок;
- `pytest`: **13 passed** (на текущей версии тестов);
- общий код возврата `make check` — `0`.

Для CI используйте установку строго по lock-файлу:

```bash
make install-frozen   # uv sync --frozen
```

Что покрывают unit-тесты:

- валидация Pydantic-схем (`amount`, `currency`, `metadata`, лишние поля);
- проверка `X-API-Key` для `/health`;
- разбор `x-retry-count` и выбор retry-очереди;
- формирование webhook-payload и стабильность `event_id`;
- экспоненциальный backoff outbox.

Чего unit-тесты **не** покрывают: PostgreSQL, RabbitMQ, реальные HTTP-вызовы
consumer'а и сквозную обработку. Эти проверки — в разделах ниже и в
[e2e-testing.md](e2e-testing.md).

## 4. Проверка инфраструктуры: PostgreSQL и RabbitMQ

Поднимите только инфраструктуру и дождитесь healthcheck'ов:

```bash
docker compose up -d --wait postgres rabbitmq
docker compose ps postgres rabbitmq
```

Ожидается:

| Сервис | Статус |
|---|---|
| `postgres` | `Up ... (healthy)` |
| `rabbitmq` | `Up ... (healthy)` |

Проверка PostgreSQL:

```bash
docker compose exec -T postgres sh -lc \
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
   -c "SELECT current_database(), current_user, version();"'
```

Ожидается `accepting connections` и успешный `SELECT` с именем БД
`payment_db` (или вашим значением `POSTGRES_DB`).

Проверка RabbitMQ:

```bash
docker compose exec rabbitmq rabbitmq-diagnostics -q ping
docker compose exec rabbitmq rabbitmqctl status | head -n 20
```

Ожидается `Ping succeeded` и статус узла без ошибок. Management UI:
`http://localhost:15672`, логин/пароль — `RABBITMQ_DEFAULT_USER` /
`RABBITMQ_DEFAULT_PASS`. На этом шаге очереди ещё могут отсутствовать: их
объявляет consumer при старте.

## 5. Миграции

Примените миграции до запуска API и consumer:

```bash
docker compose run --rm migrate
```

Команда печатает вывод миграции в терминал и возвращает код `0` при успехе.
Если стек уже запускался через `docker compose up`, логи одноразового
контейнера можно посмотреть так:

```bash
docker compose logs --tail=100 migrate
```

Проверьте ревизию:

```bash
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic history
```

Ожидается:

```text
b7c1f4a29e10 (head)
```

История содержит цепочку:

```text
7491e9b8a62e -> b7c1f4a29e10 (head), add processing/webhook fields ...
<base> -> 7491e9b8a62e, initial payments and outbox schema
```

Проверьте отсутствие расхождений между моделями и БД:

```bash
docker compose run --rm migrate alembic check
```

Ожидается `No new upgrade operations detected.` Если команда показывает
операции, значит модели изменились без миграции — не запускайте сервис,
создайте миграцию.

Повторный `alembic upgrade head` безопасен и не должен ничего менять:

```bash
docker compose run --rm migrate
```

Если API уже запущен, ревизию можно посмотреть в контейнере:

```bash
docker compose exec api alembic current
```

**Если миграция упала**, не запускайте `api`/`consumer`: проверьте
`docker compose logs --tail=200 migrate`, healthcheck PostgreSQL и
`POSTGRES_*`. Откат (`make downgrade REV=-1`) может удалить данные — в
production без backup его не выполняют.

## 6. Запуск сервисов

Соберите и запустите весь стек:

```bash
docker compose up -d --build
docker compose ps -a
```

Ожидаемые состояния:

| Сервис | Ожидание |
|---|---|
| `postgres` | `Up ... (healthy)` |
| `rabbitmq` | `Up ... (healthy)` |
| `dozzle` | `Up ...` |
| `migrate` | `Exited (0)` — это нормально |
| `api` | `Up ... (healthy)` |
| `consumer` | `Up ...` (отдельного healthcheck нет) |

Посмотрите логи старта:

```bash
docker compose logs --tail=100 api consumer migrate
```

В логах `api` не должно быть traceback. В логах `consumer` ожидаются сообщения
о запуске relay и объявлении топологии, например:

```text
DLQ declared: payments.new.dlq
Retry queue declared: payments.retry.2
Retry queue declared: payments.retry.3
Outbox relay started
```

Точные формулировки зависят от уровня логирования, но не должно быть
необработанных исключений и циклических перезапусков.

Проверка API:

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"

curl -i -H "X-API-Key: $API_KEY" http://localhost:8000/health
# HTTP/1.1 200 OK
# {"status":"ok"}

curl -s -o /dev/null -w 'without_key=%{http_code}\n' http://localhost:8000/health
# without_key=401
```

Проверка Dozzle:

```bash
curl -fsS -o /dev/null -w 'dozzle=%{http_code}\n' http://localhost:8080/
```

Ожидается `dozzle=200`; в интерфейсе видны контейнеры `payment_api`,
`payment_consumer`, `payment_db`, `payment_rabbit`, `payment_migrate`,
`payment_dozzle`.

## 7. Проверка базы данных

Подключитесь к `psql`:

```bash
docker compose exec postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Или передавайте SQL через stdin:

```bash
docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT current_database(), current_user;
SQL
```

### 7.1. Таблицы

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;
```

Ожидается три таблицы: `alembic_version`, `outbox`, `payments`.

### 7.2. Колонки

```sql
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('payments', 'outbox')
ORDER BY table_name, ordinal_position;
```

В `payments` обязательны: `id`, `amount`, `currency`, `description`,
`metadata`, `status`, `idempotency_key`, `webhook_url`, `webhook_delivered`,
`webhook_attempts`, `failure_reason`, `created_at`, `processed_at`.

В `outbox` обязательны: `id`, `event_type`, `payload`, `status`, `attempts`,
`last_error`, `next_attempt_at`, `created_at`, `published_at`.

### 7.3. Enum'ы

```sql
SELECT t.typname,
       string_agg(e.enumlabel, ', ' ORDER BY e.enumsortorder) AS labels
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.typname IN (
  'currency_type', 'payment_status_type', 'outbox_status_type'
)
GROUP BY t.typname
ORDER BY t.typname;
```

Ожидается:

| Enum | Значения |
|---|---|
| `currency_type` | `RUB, USD, EUR` |
| `payment_status_type` | `pending, succeeded, failed` |
| `outbox_status_type` | `pending, published, failed` |

Значения должны быть в нижнем регистре для статусов — это результат второй
миграции. Если видны `PENDING`/`SUCCEEDED`/`FAILED`, схема не домигрирована.

### 7.4. Индексы и ограничения

```sql
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;
```

Ожидается:

- уникальный индекс по `payments.idempotency_key`
  (`ix_payments_idempotency_key`, `UNIQUE`);
- индекс `ix_payments_status`;
- индекс `ix_outbox_status`;
- индекс `ix_outbox_next_attempt_at`.

```sql
SELECT conrelid::regclass AS table_name, conname, contype
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace
ORDER BY 1, 2;
```

Ожидается check-ограничение `ck_payments_amount_positive` (`contype = 'c'`)
и первичные ключи таблиц. Внешних ключей между `payments` и `outbox` нет по
проекту.

### 7.5. Данные

До первого платежа:

```sql
SELECT
  (SELECT count(*) FROM payments) AS payments,
  (SELECT count(*) FROM outbox) AS outbox,
  (SELECT count(*) FROM outbox WHERE status = 'pending') AS outbox_pending;
```

Ожидается `0, 0, 0` (или данные предыдущих прогонов — тогда
`outbox >= payments`, потому что на каждый платёж создаётся событие).

После успешной обработки платежа инварианты:

- `payments.status` — `succeeded` или `failed`, `processed_at` заполнен;
- `outbox.status='published'`, `published_at` заполнен, `attempts >= 1`;
- `payments.webhook_delivered=true`, если получатель ответил `2xx`.

Проверить последние платежи и события:

```sql
SELECT id, status, amount, currency, webhook_delivered, webhook_attempts,
       created_at, processed_at
FROM payments
ORDER BY created_at DESC
LIMIT 10;

SELECT id, event_type, status, attempts, last_error, next_attempt_at,
       published_at
FROM outbox
ORDER BY created_at DESC
LIMIT 10;
```

## 8. Проверка RabbitMQ

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers

docker compose exec rabbitmq rabbitmqctl list_exchanges name type

docker compose exec rabbitmq rabbitmqctl list_bindings
```

Ожидаемые очереди (при стандартных настройках):

| Очередь | Consumer'ы | Назначение |
|---|---|---|
| `payments.new` | `1` | основная очередь |
| `payments.new.dlq` | `0` | Dead Letter Queue |
| `payments.retry.2` | `0` | retry через 1 секунду |
| `payments.retry.3` | `0` | retry через 2 секунды |

Если `payments.new` отсутствует или `consumers=0`, consumer не подключился
или упал — смотрите `docker compose logs --tail=200 consumer`. Если
отсутствуют retry-очереди/DLQ, проверьте те же логи и аргументы
`MAX_PROCESSING_ATTEMPTS` / `RETRY_BASE_DELAY_SECONDS`.

Топология в Management UI: `Queues` и `Exchanges` на
`http://localhost:15672`. У `payments.new` должен быть consumer; у DLQ и
retry-очередей — нет.

## 9. Smoke-проверка API и платежа

Полный набор сценариев — в [e2e-testing.md](e2e-testing.md). Здесь минимальная
проверка, что сервис «жив».

### 9.1. Webhook-приёмник

В отдельном терминале:

```bash
python3 scripts/webhook_receiver.py --port 9000
# или: make webhook
```

Приёмник слушает `0.0.0.0:9000` и отвечает `200 OK`. Для consumer в Docker
URL приёмника на хосте: `http://host.docker.internal:9000/webhook`. Для
локального worker'а — `http://localhost:9000/webhook`.

### 9.2. Создание платежа

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
export API_URL="http://localhost:8000"
export IDEMPOTENCY_KEY="preflight-$(date +%s)-$RANDOM"

CREATE_RESPONSE=$(curl -sS -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d '{
    "amount": "100.00",
    "currency": "RUB",
    "description": "Preflight smoke test",
    "metadata": {"source": "preflight"},
    "webhook_url": "http://host.docker.internal:9000/webhook"
  }')

printf '%s\n' "$CREATE_RESPONSE" | python3 -m json.tool

export PAYMENT_ID=$(printf '%s' "$CREATE_RESPONSE" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
echo "PAYMENT_ID=$PAYMENT_ID"
```

Ожидается `202 Accepted`, `status=pending`, непустой `payment_id`.

### 9.3. Ожидание результата

```bash
for n in $(seq 1 30); do
  BODY=$(curl -sS -H "X-API-Key: $API_KEY" \
    "$API_URL/api/v1/payments/$PAYMENT_ID")
  STATUS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  DELIVERED=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhook_delivered"])')
  printf '[%02d] status=%s webhook_delivered=%s\n' "$n" "$STATUS" "$DELIVERED"

  if [ "$STATUS" != "pending" ] && [ "$DELIVERED" = "True" ]; then
    break
  fi
  sleep 1
done

printf '%s\n' "$BODY" | python3 -m json.tool
```

Ожидается переход в `succeeded` или `failed` за несколько секунд (обработка
2–5 секунд плюс задержка outbox). `failed` в 10% случаев — это эмуляция
отказа шлюза, а не ошибка сервиса. В обоих случаях webhook должен быть
доставлен: в логе приёмника появится строка `status=200`, а в ответе
`GET` — `webhook_delivered=true` и `webhook_attempts >= 1`.

Если приёмник не запущен, платёж всё равно обработается, но webhook уйдёт в
retry и затем в DLQ — для чистого smoke-теста сначала запустите приёмник.

### 9.4. Идемпотентность

Повторите тот же POST с тем же `Idempotency-Key` и тем же телом. Ожидается
`202`, тот же `payment_id` и заголовок `Idempotency-Replayed: true`.

Тот же ключ с другой суммой должен вернуть `409 Conflict`. Подробные команды —
в [api.md](api.md#84-пример-повтора-и-конфликта).

### 9.5. Проверка результата в БД и RabbitMQ

```sql
SELECT id, status, webhook_delivered, webhook_attempts, failure_reason,
       processed_at
FROM payments
WHERE id = '<PAYMENT_ID>';
```

```sql
SELECT status, attempts, last_error, published_at
FROM outbox
WHERE payload->>'payment_id' = '<PAYMENT_ID>';
```

Ожидается `status` платежа `succeeded`/`failed`, `webhook_delivered=true`,
`outbox.status='published'` и `published_at` заполнен.

Очереди после успешного smoke-теста:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
```

`payments.new`, `payments.retry.*`, `payments.new.dlq` должны быть пустыми
(`messages_ready=0`), у `payments.new` — один consumer.

## 10. Production-проверки перед публикацией

- `.env` с боевыми секретами, права `600`, файл не в Git;
- заменены `API_KEY`, `POSTGRES_PASSWORD`, `RABBITMQ_DEFAULT_PASS`;
- `docker compose config --quiet` проходит;
- наружу не опубликованы `5433`, `5672`, `15672`, `8080`; для production
  привяжите порты к `127.0.0.1` или закройте firewall'ом;
- API стоит за reverse proxy с TLS; наружу нужен только `443` (и `80` для
  ACME);
- `/docs`, `/redoc`, `/openapi.json` закрыты прокси или отключены;
- Dozzle не опубликован в интернет: он подключён к Docker socket и без
  аутентификации показывает логи всех контейнеров;
- webhook-получатель доступен с сервера; для произвольных URL клиентов
  настроен allowlist доменов (снижение SSRF-риска);
- настроен мониторинг: `/health`, длины `payments.new`, `payments.retry.*`,
  `payments.new.dlq`, ошибки consumer, свободное место на volume;
- проверен backup и, желательно, restore на тестовом окружении.

Backup PostgreSQL:

```bash
docker compose exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > payment_db.dump
```

Restore (перезаписывает данные; сначала остановите API и consumer):

```bash
docker compose stop api consumer
cat payment_db.dump | docker compose exec -T postgres sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists'
docker compose start api consumer
```

## 11. Если проверка не прошла

| Симптом | Что проверить |
|---|---|
| `bind: address already in use` | занятый порт (`lsof`/`ss`), изменить порт в `.env`/compose |
| `migrate` завершился с ошибкой | `docker compose logs migrate`, healthcheck PostgreSQL, `POSTGRES_*` |
| API отвечает `401` | заголовок `X-API-Key`, значение `API_KEY`, пересоздать `api` после изменения `.env` |
| API отвечает `422` | правила полей из [api.md](api.md#5-создание-платежа), наличие `Idempotency-Key` |
| API отвечает `500` | логи API, доступность PostgreSQL, `alembic current` |
| Платёж долго `pending` | consumer, `outbox.status/attempts/last_error`, очереди RabbitMQ |
| Webhook: `All connection attempts failed` | приёмник запущен; из Docker — `host.docker.internal`, а не `localhost`; firewall |
| Webhook доставлен, но `webhook_delivered=false` | падение consumer между `2xx` и commit — ожидаемая at-least-once; проверьте логи |
| Consumer перезапускается | `RABBITMQ_URL`, доступность RabbitMQ, аргументы retry-очередей, миграции |
| RabbitMQ UI не открывается | контейнер `rabbitmq`, порт `15672`, логин/пароль; после смены credentials нужен пересозданный volume или `rabbitmqctl change_password` |
| Dozzle не показывает логи | контейнер `dozzle`, `/var/run/docker.sock`, порт, логи |
| Сменили пароль БД, но он не применился | volume уже инициализирован: `ALTER USER` или пересоздание данных |
| После смены TTL consumer не стартует | удалите retry-очереди с конфликтующими аргументами или пересоздайте RabbitMQ volume |

## 12. Остановка и удаление данных

Остановить, сохранив данные:

```bash
docker compose stop
docker compose start
```

Перезапустить отдельный сервис:

```bash
docker compose restart consumer
docker compose restart api
```

Пересобрать после изменения кода:

```bash
docker compose up -d --build
```

Удалить контейнеры и сеть, сохранив данные:

```bash
docker compose down
```

Удалить **все** данные PostgreSQL и RabbitMQ:

```bash
docker compose down -v
```

Удалить образ:

```bash
docker image rm payment-service:local
```

`down -v` необратимо удаляет volume'ы. В production сначала сделайте backup.

## 13. Итоговый чек-лист

- [ ] Docker и Compose доступны, `docker info` без ошибок.
- [ ] Свободны порты `8000`, `5433`, `5672`, `15672`, `8080`.
- [ ] `.env` создан, права `600`, секреты не в Git.
- [ ] `docker compose config --quiet` проходит.
- [ ] `make check` зелёный: ruff, форматирование, mypy, `13 passed`.
- [ ] `postgres` и `rabbitmq` — `healthy`.
- [ ] `migrate` завершился с кодом `0`, `alembic current` — `b7c1f4a29e10 (head)`.
- [ ] `alembic check` — `No new upgrade operations detected.`
- [ ] В БД есть `payments`, `outbox`, `alembic_version`; enum'ы в нижнем регистре.
- [ ] Уникальный индекс `idempotency_key` и check `amount > 0` присутствуют.
- [ ] `docker compose ps -a`: `api` healthy, `consumer` Up, `migrate` Exited (0).
- [ ] В логах API/consumer нет traceback; топология RabbitMQ объявлена.
- [ ] `/health` возвращает `200` с ключом и `401` без ключа.
- [ ] Dozzle открывается и показывает логи контейнеров.
- [ ] Очереди: `payments.new` с одним consumer, `payments.new.dlq` и retry — без consumer'ов.
- [ ] Smoke: `POST` → `202`, `GET` → финальный статус, webhook доставлен.
- [ ] Повтор с тем же ключом и телом → тот же `payment_id` и `Idempotency-Replayed: true`.
- [ ] Повтор с тем же ключом и другим телом → `409`.
- [ ] `outbox.status='published'` после обработки, очереди пусты.
- [ ] Production-чеклист из раздела 10 выполнен.
- [ ] Для релиза прогнаны сценарии [e2e-testing.md](e2e-testing.md).

## 14. Связанные документы

- [Архитектура](architecture.md)
- [API](api.md)
- [E2E-тестирование](e2e-testing.md)
- [README проекта](../README.md)
