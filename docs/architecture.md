# Архитектура проекта

## 1. Назначение и границы сервиса

Сервис принимает платёж по HTTP, сохраняет его и событие на обработку в одной
транзакции PostgreSQL, затем асинхронно обрабатывает платёж и уведомляет
клиента webhook'ом. Главная задача архитектуры — **не потерять платёж или
событие при сбоях** между PostgreSQL и RabbitMQ и **не создать дубль** при
повторном запросе клиента.

Что сервис делает:

- принимает `POST /api/v1/payments` и отвечает `202 Accepted`;
- эмулирует внешний платёжный шлюз (задержка 2–5 секунд, 90% успех / 10% отказ);
- отправляет webhook на `webhook_url` с повторами и Dead Letter Queue;
- отдаёт текущее состояние платежа через `GET /api/v1/payments/{id}`.

Чего сервис не делает:

- не проводит реальные деньги и не интегрирован с настоящим шлюзом;
- не поддерживает отмену, возврат и список платежей;
- не подписывает webhook HMAC-подписью;
- не обслуживает несколько клиентов с разными API-ключами (ключ один и
  статический);
- не гарантирует exactly-once — только at-least-once.

## 2. Стек

| Слой | Технология | Версия / ограничение | Роль |
|---|---|---|---|
| Язык | Python | 3.14 (`requires-python >= 3.14`) | runtime API и worker |
| Web / API | FastAPI | >= 0.162 | HTTP-роутинг, OpenAPI, зависимости |
| Валидация | Pydantic v2 + pydantic-settings | >= 2.0 | схемы запросов/ответов, настройки |
| ORM / БД | SQLAlchemy 2.0 async + asyncpg | >= 2.0 / >= 0.32 | асинхронная работа с PostgreSQL |
| БД | PostgreSQL | 16 (`postgres:16-alpine`) | `payments`, `outbox`, состояние webhook |
| Брокер | RabbitMQ | 3 (`rabbitmq:3-management-alpine`) | основная очередь, retry-очереди, DLQ |
| Работа с брокером | FastStream + aio-pika | >= 0.7.8 | топология, consumer, публикация |
| HTTP-клиент | httpx | >= 0.28 | отправка webhook'ов |
| Миграции | Alembic | >= 1.20 | версионирование схемы БД |
| Контейнеризация | Docker + Docker Compose v2 | Engine 24+ | запуск всего стека |
| Логи | Dozzle | `amir20/dozzle:v11.3.5` | web-просмотр логов контейнеров |
| Пакеты | uv | lock-файл `uv.lock` | установка зависимостей |
| Качество | ruff, mypy, pytest | dev-группа | линтеры, типы, unit-тесты |

## 3. Схема проекта

Логическая схема потоков:

```text
                       POST /api/v1/payments
                  (X-API-Key, Idempotency-Key)
                                  |
                                  v
                 +----------------------------------+
                 |        api (FastAPI :8000)       |
                 |  валидация -> идемпотентность    |
                 +----------------+-----------------+
                                  |
                                  | одна транзакция:
                                  | INSERT payments + outbox
                                  v
                 +----------------------------------+
                 |           PostgreSQL 16          |
                 |    payments        |   outbox    |
                 +----+---------------------+-------+
                      |                     ^
      SELECT ... FOR UPDATE SKIP LOCKED      |
      (outbox relay, каждые ~1 c)            | UPDATE статуса
                      v                     | и webhook-состояния
                 +----------------------------------+
                 |      consumer (worker)           |
                 |  outbox relay  +  consumer       |
                 |  prefetch=1, 2-5 c, 90/10        |
                 +----+------------------------+----+
                      |                        |
       publish        |                        | POST webhook
       payment.created|                        | (таймаут 10 c)
                      v                        v
       +----------------------------+   +----------------------+
       |         RabbitMQ           |   | webhook_url          |
       | payments.exchange          |   | (приёмник/внешний    |
       |   -> payments.new          |   |  сервис)             |
       | payments.retry.2/3 (TTL)   |   +----------------------+
       | payments.new.dlq           |
       +--------------+-------------+
                      |
                      | deliver payment.created
                      v
                 consumer (тот же worker)
```

Сообщения и очереди:

```text
payments.exchange --(payment.created)--> payments.new --> consumer
       ^                                                    |
       | TTL истёк                                          | webhook не доставлен
       |                                                    v
payments.retry.2 / payments.retry.3  <---------- publish retry
       |
       | после MAX_PROCESSING_ATTEMPTS
       v
payments.dead.exchange --(payment.dead)--> payments.new.dlq
```

Последовательность успешного сценария и сценария с повтором:

```mermaid
sequenceDiagram
    autonumber
    participant C as Клиент
    participant A as API
    participant DB as PostgreSQL
    participant R as Outbox relay
    participant MQ as RabbitMQ
    participant W as Consumer
    participant H as Webhook receiver

    C->>A: POST /api/v1/payments
    A->>DB: BEGIN; INSERT payment; INSERT outbox; COMMIT
    A-->>C: 202 Accepted + Location
    R->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    R->>MQ: publish payment.created (persistent)
    R->>DB: UPDATE outbox SET status=published
    MQ->>W: deliver payments.new
    W->>DB: SELECT payment FOR UPDATE
    W->>W: sleep 2-5 c, успех 90%
    W->>DB: UPDATE status, processed_at; COMMIT
    W->>H: POST webhook
    alt получатель ответил 2xx
        H-->>W: 200 OK
        W->>DB: UPDATE webhook_delivered=true; COMMIT
    else ошибка, не-2xx или таймаут
        H-->>W: 5xx / timeout
        W->>DB: UPDATE webhook_attempts; COMMIT
        W->>MQ: publish payments.retry.N (x-retry-count)
        Note over MQ: сообщение лежит в retry-очереди TTL,<br/>затем возвращается в payments.exchange
        MQ->>W: deliver снова
    end
    Note over W,MQ: после исчерпания попыток сообщение уходит в payments.new.dlq
```

## 4. Компоненты

| Компонент | Контейнер | Ответственность |
|---|---|---|
| `api` | `payment_api` | HTTP API: аутентификация, валидация, идемпотентное создание платежа и outbox в одной транзакции, чтение платежа |
| `consumer` | `payment_consumer` | Один процесс worker'а: outbox relay (публикация в RabbitMQ) и consumer платежей (эмуляция шлюза, webhook, retry) |
| `postgres` | `payment_db` | `payments`, `outbox`, служебная таблица `alembic_version` |
| `rabbitmq` | `payment_rabbit` | Обменники, основная очередь, retry-очереди с TTL, DLQ |
| `migrate` | `payment_migrate` | Одноразовый job `alembic upgrade head`; gate для старта `api` и `consumer` |
| `dozzle` | `payment_dozzle` | Web-интерфейс логов контейнеров через Docker socket (read-only) |
| `webhook receiver` | вне compose | Локальный `scripts/webhook_receiver.py` или внешний сервис клиента |

`api` и `consumer` используют один и тот же образ `payment-service:local`, но
разные команды запуска. `migrate` — тот же образ с командой
`alembic upgrade head` и `restart: "no"`.

## 5. Жизненный цикл платежа

```text
                 POST /api/v1/payments
                          |
                          v
                      +---------+
                      | pending |
                      +----+----+
                           |
             consumer: 2-5 c, 90% успех / 10% отказ
                           |
              +------------+------------+
              |                         |
              v                         v
        +-----------+             +----------+
        | succeeded |             |  failed  |
        +-----+-----+             +-----+----+
              |                         |
              | webhook 2xx             | webhook 2xx
              v                         v
      webhook_delivered=true     webhook_delivered=true
```

Статус меняется только из `pending` в `succeeded` или `failed` и фиксируется
до отправки webhook. Повторная доставка сообщения из RabbitMQ не запускает
эмуляцию шлюза заново: consumer видит финальный статус и повторяет только
webhook. Если после исчерпания попыток платёж всё ещё `pending` (например,
сбой БД), consumer помечает его `failed` и записывает `failure_reason`.

## 6. Обработка платежа по шагам

1. **Приём запроса.** `api` проверяет `X-API-Key` (constant-time сравнение),
   валидирует тело Pydantic-схемой и требует `Idempotency-Key`.
2. **Идемпотентность.** Сервис ищет платёж по `idempotency_key`. Если запись
   есть и поля совпадают — возвращает её с `Idempotency-Replayed: true`; если
   поля отличаются — `409 Conflict`.
3. **Запись.** В одной транзакции создаются `payments` (`status=pending`) и
   `outbox` (`event_type=payment.created`, `payload` с `payment_id`, `amount`,
   `currency`). `flush()` нужен, чтобы получить UUID платежа до создания
   события. При гонке двух запросов уникальный индекс по `idempotency_key`
   пропускает только одного; проигравший откатывается, перечитывает платёж и
   либо возвращает его как replay, либо отдаёт `409`.
4. **Ответ.** Клиент получает `202 Accepted`, `payment_id`, `status=pending`,
   `created_at` и заголовок `Location`.
5. **Outbox relay.** Отдельная корутина в worker'е раз в
   `OUTBOX_POLL_INTERVAL` секунд выбирает пачку `pending`-событий с
   `next_attempt_at <= now`, блокируя строки `FOR UPDATE SKIP LOCKED`.
6. **Публикация.** Событие публикуется в `payments.exchange` с routing key
   `payment.created`, `persist=True`, publisher confirms и
   `on_return_raises=True`. Только после подтверждения брокера строка
   помечается `published`, записывается `published_at`.
7. **Ошибка публикации.** `attempts += 1`, `last_error` заполняется,
   `next_attempt_at` сдвигается на экспоненциальный backoff
   (`min(OUTBOX_MAX_BACKOFF_SECONDS, 2^attempts)`); цикл прерывается, чтобы не
   блокировать остальные события. Событие остаётся `pending` и будет
   опубликовано позже. Отдельного максимума попыток у outbox нет.
8. **Доставка consumer'у.** RabbitMQ кладёт сообщение в `payments.new`.
   Consumer работает с `prefetch=1`, то есть обрабатывает сообщения
   последовательно.
9. **Обработка.** Consumer блокирует строку платежа `FOR UPDATE`. Если статус
   `pending`, эмулирует шлюз (2–5 секунд, 90/10), записывает
   `status`, `processed_at`, `failure_reason` и коммитит **до** webhook.
10. **Webhook.** Если `webhook_delivered=false`, consumer увеличивает
    `webhook_attempts`, отправляет POST на `webhook_url` с таймаутом
    `WEBHOOK_TIMEOUT_SECONDS` и при `2xx` ставит `webhook_delivered=true`.
    При ошибке счётчик попыток коммитится, а сообщение уходит в retry-очередь
    с `x-retry-count = attempt + 1`.
11. **Retry.** Retry-очередь не имеет consumer'а: RabbitMQ держит сообщение
    `TTL` и через dead-letter-exchange возвращает его в
    `payments.exchange`. Задержка: `RETRY_BASE_DELAY_SECONDS * 2^(attempt-2)`
    (по умолчанию 1 c, затем 2 c).
12. **DLQ.** После `MAX_PROCESSING_ATTEMPTS` неудач (по умолчанию 3) handler
    пробрасывает исключение; `AckPolicy.REJECT_ON_ERROR` отклоняет сообщение
    без requeue, и через `payments.dead.exchange` оно попадает в
    `payments.new.dlq`. DLQ не вычитывается автоматически.

## 7. Модель данных

Связи между таблицами на уровне БД нет: `outbox` — самостоятельная очередь
событий, а ссылка на платёж хранится в JSON-поле `payload.payment_id`.
Логически одному платежу соответствует одно событие `payment.created`.

### `payments`

| Колонка | Тип | Ограничения / смысл |
|---|---|---|
| `id` | `uuid` | PK, генерируется приложением |
| `amount` | `numeric(18,2)` | `CHECK amount > 0`, сумма с 2 знаками |
| `currency` | enum `currency_type` | `RUB`, `USD`, `EUR` |
| `description` | `varchar(255)` | назначение платежа |
| `metadata` | `jsonb` | `NOT NULL DEFAULT '{}'`, произвольный JSON-объект |
| `status` | enum `payment_status_type` | `pending` / `succeeded` / `failed`, индекс |
| `idempotency_key` | `varchar(255)` | `NOT NULL`, уникальный индекс |
| `webhook_url` | `varchar(2048)` | URL уведомления |
| `webhook_delivered` | `boolean` | `NOT NULL DEFAULT false` |
| `webhook_attempts` | `integer` | `NOT NULL DEFAULT 0`, число HTTP-попыток |
| `failure_reason` | `text` | причина отказа шлюза или обработки |
| `created_at` | `timestamptz` | `DEFAULT now()` |
| `processed_at` | `timestamptz` | заполняется после обработки шлюзом |

Индексы и ограничения: PK `payments_pkey`, уникальный индекс
`ix_payments_idempotency_key`, индекс `ix_payments_status`, check-ограничение
`ck_payments_amount_positive`.

### `outbox`

| Колонка | Тип | Ограничения / смысл |
|---|---|---|
| `id` | `uuid` | PK |
| `event_type` | `varchar(100)` | сейчас всегда `payment.created` |
| `payload` | `jsonb` | `payment_id`, `amount`, `currency` |
| `status` | enum `outbox_status_type` | `pending` / `published` / `failed`, индекс |
| `attempts` | `integer` | число попыток публикации |
| `last_error` | `text` | ошибка последней публикации |
| `next_attempt_at` | `timestamptz` | время следующей попытки, индекс |
| `created_at` | `timestamptz` | `DEFAULT now()` |
| `published_at` | `timestamptz` | время успешной публикации |

Значение `outbox.status = failed` объявлено в enum, но текущий relay его не
выставляет: при ошибке событие остаётся `pending` с backoff. Это важно при
мониторинге — «зависшее» событие выглядит как `pending` с растущим
`attempts` и заполненным `last_error`.

### Enum'ы

| Enum | Значения |
|---|---|
| `currency_type` | `RUB`, `USD`, `EUR` |
| `payment_status_type` | `pending`, `succeeded`, `failed` |
| `outbox_status_type` | `pending`, `published`, `failed` |

## 8. Топология RabbitMQ

| Объект | Тип | Ключ / аргументы | Назначение |
|---|---|---|---|
| `payments.exchange` | direct, durable | routing key `payment.created` | приём событий из outbox и retry-очередей |
| `payments.new` | durable queue | `x-dead-letter-exchange=payments.dead.exchange`, `x-dead-letter-routing-key=payment.dead` | основная очередь, один consumer |
| `payments.retry.2` … `payments.retry.N` | durable queue без consumer'а | `x-message-ttl`, DLX обратно в `payments.exchange` | задержка перед 2-й, 3-й, … попыткой |
| `payments.dead.exchange` | direct, durable | routing key `payment.dead` | приём отклонённых сообщений |
| `payments.new.dlq` | durable queue без consumer'а | — | Dead Letter Queue для разбора |

Параметры retry-очередей:

| Попытка | Очередь | Задержка при базе 1 c |
|---|---|---|
| 1 | `payments.new` | сразу |
| 2 | `payments.retry.2` | 1 c |
| 3 | `payments.retry.3` | 2 c |

Количество retry-очередей — `MAX_PROCESSING_ATTEMPTS - 1`. Очередь
`payments.retry.K` создаётся для попытки `K`; задержка
`RETRY_BASE_DELAY_SECONDS * 2^(K-2)`. Публикация выполняется в default
exchange с именем очереди, поэтому сообщение попадает прямо в неё. После
истечения TTL RabbitMQ возвращает сообщение в `payments.exchange` с routing
key `payment.created`.

Основную очередь `payments.new` и её exchange объявляет подписчик
(`@broker.subscriber`). `payments.new.dlq`, `payments.dead.exchange` и
retry-очереди объявляются в `declare_topology()` worker'а после подключения к
RabbitMQ.

## 9. Гарантии доставки и идемпотентность

| Гарантия | Механизм | Ограничение |
|---|---|---|
| Платёж и событие не расходятся | одна транзакция `payments` + `outbox` | — |
| Событие не теряется при падении API | outbox + повторная публикация relay | возможны дубли публикации (at-least-once) |
| Событие доставлено consumer'у | publisher confirms, `persist=True`, durable очереди | брокер должен быть доступен; при недоступности relay ретраит с backoff |
| Повторный POST не создаёт дубль | уникальный индекс `idempotency_key` + pre-check + обработка `IntegrityError` | ключ глобальный, без срока жизни; сравнение тела осознанно строгое |
| Webhook доставлен | до 3 попыток, TTL-очереди, DLQ | возможны повторные доставки; получатель должен дедуплицировать |
| Платёж не остаётся `pending` после окончательного сбоя | `_mark_failed_if_pending` при уходе в DLQ | выполняется best-effort, ошибка только логируется |
| Обработка не меняет финальный статус | проверка `status == pending` под `FOR UPDATE` | — |

Exactly-once не гарантируется ни для события, ни для webhook. Повторная
доставка webhook'а возможна, если consumer упал после получения `2xx`, но до
коммита `webhook_delivered=true`. Ключ дедупликации на стороне получателя —
`X-Webhook-Id` (он же `event_id` в теле), значение стабильно между повторами.

## 10. Конфигурация

Все настройки читаются из переменных окружения и файла `.env`
(`pydantic-settings`, `case_sensitive=True`). Значения по умолчанию для
локального запуска — в `.env.example`. В Docker Compose часть переменных
переопределяется (см. примечания ниже).

| Переменная | Значение в `.env.example` | Где применяется | Описание |
|---|---|---|---|
| `POSTGRES_USER` | `app_user` | API, consumer, migrate, postgres | пользователь PostgreSQL |
| `POSTGRES_PASSWORD` | `app_password` | API, consumer, migrate, postgres | пароль PostgreSQL |
| `POSTGRES_HOST` | `localhost` | локальный запуск | в compose переопределяется на `postgres` |
| `POSTGRES_PORT` | `5433` | локальный запуск | в compose переопределяется на `5432` |
| `POSTGRES_DB` | `payment_db` | API, consumer, migrate, postgres | имя БД |
| `POSTGRES_ECHO` | `false` | API, consumer | логировать SQL-запросы |
| `POSTGRES_HOST_PORT` | `5433` | compose | порт PostgreSQL на хосте |
| `RABBITMQ_DEFAULT_USER` | `payments` | RabbitMQ, compose | пользователь RabbitMQ |
| `RABBITMQ_DEFAULT_PASS` | `payments_password` | RabbitMQ, compose | пароль RabbitMQ |
| `RABBITMQ_URL` | `amqp://payments:payments_password@localhost:5672/` | локальный запуск | в compose собирается из `RABBITMQ_DEFAULT_*` и хоста `rabbitmq` |
| `DOZZLE_PORT` | `8080` | compose | порт Dozzle на хосте |
| `API_KEY` | `test-api-key-123` | API + healthcheck | статический ключ `X-API-Key` |
| `APP_ENV` | `development` | API, consumer | в compose для контейнеров задаётся `production` |
| `OUTBOX_POLL_INTERVAL` | `1.0` | consumer | период опроса outbox, секунды, `> 0` |
| `OUTBOX_BATCH_SIZE` | `100` | consumer | размер пачки, 1–1000 |
| `OUTBOX_MAX_BACKOFF_SECONDS` | `60` | consumer | потолок backoff публикации, `> 0` |
| `MAX_PROCESSING_ATTEMPTS` | `3` | consumer | всего попыток обработки, 1–10 |
| `RETRY_BASE_DELAY_SECONDS` | `1.0` | consumer | база экспоненциальной задержки, `> 0` |
| `PAYMENT_SUCCESS_RATE` | `0.9` | consumer | доля успешных эмуляций, 0–1 |
| `PROCESSING_MIN_SECONDS` | `2.0` | consumer | минимальная задержка шлюза, `>= 0` |
| `PROCESSING_MAX_SECONDS` | `5.0` | consumer | максимальная задержка шлюза, `>= 0`, не меньше минимума |
| `WEBHOOK_TIMEOUT_SECONDS` | `10.0` | consumer | таймаут webhook-запроса, `> 0` |

Особенности:

- настройки читаются при старте процесса; `docker compose restart` может не
  подхватить изменённый `.env` — используйте `up -d --force-recreate`;
- внутри compose `POSTGRES_HOST=postgres`, `POSTGRES_PORT=5432`, а
  `RABBITMQ_URL` собирается из логина/пароля и хоста `rabbitmq`;
- `APP_ENV=production` для `api` и `consumer` задаётся в compose и не зависит
  от значения в `.env`;
- `POSTGRES_HOST_PORT` влияет только на публикацию порта на хосте;
- изменение `DOZZLE_PORT` применяется пересозданием Dozzle:
  `docker compose up -d --force-recreate dozzle`;
- изменение `POSTGRES_PASSWORD` или пароля RabbitMQ после первого создания
  volume не меняет уже созданного пользователя — нужен `ALTER USER` /
  `rabbitmqctl change_password` или пересоздание данных;
- изменение `RETRY_BASE_DELAY_SECONDS` меняет аргументы TTL-очередей, а
  RabbitMQ не позволяет переобъявить очередь с другими аргументами: удалите
  retry-очереди или пересоздайте RabbitMQ volume;
- уменьшение `MAX_PROCESSING_ATTEMPTS` оставляет «лишние» retry-очереди без
  consumer'а — их можно удалить вручную;
- порт API `8000` задан в `docker-compose.yml`; для смены отредактируйте
  `ports` сервиса `api`.

## 11. Структура репозитория

```text
src/app/
  main.py                  # FastAPI app, lifespan, подключение роутеров
  api/
    deps.py                # сессия БД, проверка X-API-Key
    v1/
      health.py            # GET /health (под API-ключом)
      payments.py          # POST/GET платежей
  core/config.py           # настройки и валидация
  db/session.py            # async engine/session
  models/
    base.py                # DeclarativeBase
    enums.py               # валюты, статусы платежа и outbox
    payment.py             # Payment
    outbox.py              # Outbox
  schemas/payment.py       # Pydantic-схемы API
  services/
    payments.py            # идемпотентное создание в транзакции
    errors.py              # доменные ошибки
  messaging/
    broker.py              # exchanges, очереди, retry, DLQ
    outbox.py              # outbox relay
    consumer.py            # consumer, webhook, retry
    schemas.py             # событие payment.created
    worker.py              # entrypoint worker'а
migrations/
  env.py
  versions/
    7491e9b8a62e_init_payments_and_outbox_tables.py
    b7c1f4a29e10_add_processing_and_outbox_retry_fields.py
scripts/webhook_receiver.py
tests/                     # unit-тесты (без PostgreSQL и RabbitMQ)
docs/                      # эта документация
docker-compose.yml
Dockerfile
Makefile
.env.example
alembic.ini
pyproject.toml
uv.lock
```

## 12. Развёртывание

Все сервисы запускаются в одной сети Docker Compose. Внутри сети доступны
DNS-имена `postgres`, `rabbitmq`, `api`, `consumer`.

| Сервис | Образ / сборка | Порты на хосте | Тома | Healthcheck / условие старта |
|---|---|---|---|---|
| `postgres` | `postgres:16-alpine` | `5433 -> 5432` | `postgres_data:/var/lib/postgresql/data` | `pg_isready`, 10 ретраев |
| `rabbitmq` | `rabbitmq:3-management-alpine` | `5672`, `15672` | `rabbitmq_data:/var/lib/rabbitmq` | `rabbitmq-diagnostics ping` + проверка порта |
| `migrate` | `payment-service:local` | — | — | стартует после `postgres` healthy, `restart: "no"` |
| `api` | `payment-service:local` | `8000` | — | после `migrate` exit 0 и `postgres` healthy; HTTP healthcheck |
| `consumer` | `payment-service:local` | — | — | после `migrate`, `rabbitmq` и `postgres` healthy; своего healthcheck нет |
| `dozzle` | `amir20/dozzle:v11.3.5` | `8080` | `/var/run/docker.sock` (ro) | нет |

`api` и `consumer` не стартуют, пока `migrate` не завершится с кодом `0` —
это защита от работы с несовершенной схемой. `consumer` получает
`extra_hosts: host.docker.internal:host-gateway`, чтобы webhook на приёмник,
запущенный на хосте, был доступен из контейнера.

Dockerfile собирает образ на `ghcr.io/astral-sh/uv:python3.14-bookworm-slim`,
ставит зависимости по `uv.lock` без dev-группы и задаёт
`PYTHONPATH=/app/src`. Один и тот же образ используется для `api`,
`consumer` и `migrate` с разными командами.

## 13. Ограничения и направления развития

- **Webhook без подписи.** HMAC и ротация секрета не реализованы; получатель
  не может криптографически проверить источник.
- **Один статический API-ключ.** Нет таблицы клиентов, ротации и отзыва
  ключей; ключ не привязан к платежу.
- **Один consumer, `prefetch=1`.** Обработка последовательная; при росте
  нагрузки нужен пул consumer'ов и отдельный deployment для outbox relay.
- **Outbox relay в процессе consumer.** Падение worker'а останавливает и
  публикацию, и обработку. SQL уже использует `SKIP LOCKED`, поэтому relay
  можно вынести без изменения схемы.
- **Нет метрик и трейсинга.** Prometheus/OpenTelemetry не подключены;
  мониторинг строится на health, длинах очередей, логах и состоянии outbox.
- **Нет автоматической очистки.** DLQ и outbox растут; нужна политика
  ретенции и алертинг по длине `payments.new.dlq` и «зависшим» `pending`
  событиям outbox.
- **Networking / SSRF.** `webhook_url` задаёт клиент; в публичном окружении
  нужен allowlist доменов, запрет приватных адресов и DNS-rebinding-защита.
- **Dozzle и Docker socket.** Без аутентификации Dozzle показывает логи всех
  контейнеров, а доступ к Docker API даёт широкие права на хост. Не
  публикуйте его в интернет.
- **Swagger UI и `/openapi.json` не защищены API-ключом.** Закрывайте их
  reverse proxy или отключайте в production.
- **Нет rate limiting и квот.** Один клиент может занять API/очередь.
- **Нет интеграционных тестов в репозитории.** Unit-тесты не проверяют
  PostgreSQL и RabbitMQ; сквозные сценарии описаны в
  [e2e-testing.md](e2e-testing.md).
- **Ограничения хранения.** `metadata` не ограничен по размеру (кроме лимитов
  запроса/прокси), а `idempotency_key` хранится бессрочно.
- **Уязвимость данных в GET.** Ответ `GET /payments/{id}` содержит
  `idempotency_key` и `webhook_url`; доступ должен быть ограничен.

## 14. Связанные документы

- [Предзапусковая подготовка](preflight.md)
- [API](api.md)
- [E2E-тестирование](e2e-testing.md)
- [README проекта](../README.md)
