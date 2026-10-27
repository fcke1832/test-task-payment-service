# Payment Processing Service

Асинхронный микросервис процессинга платежей: API принимает платёж, атомарно
сохраняет его и outbox-событие в PostgreSQL, worker публикует событие в RabbitMQ,
эмулирует обработку платежа и отправляет клиенту webhook с повторами и DLQ.

Стек: **FastAPI + Pydantic v2**, **SQLAlchemy 2.0 async + asyncpg**,
**PostgreSQL 16**, **RabbitMQ 3 + FastStream**, **Alembic**, **Docker Compose**,
**Dozzle** для просмотра логов контейнеров.

---

## Содержание

- [1. Что делает сервис](#1-что-делает-сервис)
- [2. Требования](#2-требования)
- [3. Архитектура](#3-архитектура)
- [4. Быстрый старт](#4-быстрый-старт)
- [5. Пошаговое развёртывание](#5-пошаговое-развёртывание)
- [6. Проверка после запуска](#6-проверка-после-запуска)
- [7. Работа с API](#7-работа-с-api)
- [8. Webhook](#8-webhook)
- [9. Обработка, retry и DLQ](#9-обработка-retry-и-dlq)
- [10. Мониторинг и диагностика](#10-мониторинг-и-диагностика)
- [11. Проверка основных сценариев](#11-проверка-основных-сценариев)
- [12. Управление сервисом](#12-управление-сервисом)
- [13. Конфигурация](#13-конфигурация)
- [14. Локальный запуск без Docker](#14-локальный-запуск-без-docker)
- [15. Тесты и линтеры](#15-тесты-и-линтеры)
- [16. Структура проекта](#16-структура-проекта)
- [17. Production-развёртывание](#17-production-развёртывание)
- [18. Troubleshooting](#18-troubleshooting)
- [19. Остановка и удаление данных](#19-остановка-и-удаление-данных)
- [20. Ограничения](#20-ограничения)
- [21. Чек-лист приёмки](#21-чек-лист-приёмки)

---

## 1. Что делает сервис

1. Клиент вызывает `POST /api/v1/payments` с `X-API-Key` и `Idempotency-Key`.
2. API в одной транзакции создаёт платёж (`payments`) и событие (`outbox`).
3. Клиент сразу получает `202 Accepted` со статусом `pending`.
4. Outbox relay забирает событие и публикует его в RabbitMQ.
5. Один consumer получает событие, эмулирует внешний платёжный шлюз
   (2–5 секунд, 90% успех / 10% отказ), сохраняет итоговый статус.
6. Consumer отправляет webhook на `webhook_url`.
7. Если webhook не доставлен, сообщение повторяется через TTL-очереди с
   экспоненциальной задержкой.
8. После исчерпания попыток сообщение остаётся в Dead Letter Queue.

Ключевые гарантии:

- **At-least-once** доставка события и webhook.
- **Outbox pattern** — событие не теряется при падении процесса между БД и
  RabbitMQ.
- **Идемпотентность API** — защита от повторных POST.
- **Retry** — до 3 попыток обработки сообщения по умолчанию.
- **DLQ** — сообщение не теряется после окончательного сбоя.
- **Статический API-ключ** — `X-API-Key` для API и `/health`.

---

## 2. Требования

Для запуска через Docker:

- Docker Engine **24+** или Docker Desktop;
- Docker Compose **v2** (`docker compose ...`);
- свободные порты на хосте:
  - `8000` — API;
  - `5433` — PostgreSQL (порт на хосте, внутри контейнера `5432`);
  - `5672` — RabbitMQ AMQP;
  - `15672` — RabbitMQ Management UI;
  - `8080` — Dozzle (просмотр логов контейнеров);
- минимум ~2 GB свободной оперативной памяти;
- доступ в интернет для первой загрузки Docker-образов.

Для локального webhook-приёмника и разбора JSON:

- `curl`;
- `python3` (стандартная библиотека, без дополнительных пакетов).

Для локальной разработки без Docker-контейнеров API/consumer:

- Python **3.14**;
- [uv](https://docs.astral.sh/uv/);
- запущенные PostgreSQL и RabbitMQ (можно поднять только их через compose).

Проверить Docker:

```bash
docker --version
docker compose version
docker info
```

---

## 3. Архитектура

```text
                 POST /api/v1/payments
                          |
                          v
                  +---------------+        одна транзакция
                  |   FastAPI     |------------------------+
                  +---------------+                        |
                          |                                v
                          |                       +----------------+
                          |                       |   PostgreSQL   |
                          |                       | payments       |
                          |                       | outbox         |
                          |                       +----------------+
                          |                                ^
                          |                                | FOR UPDATE SKIP LOCKED
                          v                       +----------------+
                   (HTTP 202 Accepted)            |  outbox relay  |
                                                  |  (worker)      |
                                                  +----------------+
                                                           |
                                      publish payment.created (persist)
                                                           v
                                                  payments.exchange
                                                           |
                                                           v
                                                    payments.new
                                                           |
                                                           v
                                                  +----------------+
                                                  |    consumer    |
                                                  | 2-5s, 90/10    |
                                                  +----------------+
                                                     |          |
                                       webhook (2xx?) |          | ошибка
                                                     v          v
                                              клиентский URL   retry TTL queue
                                                                  |
                                                                  | 1s / 2s / ...
                                                                  v
                                                          payments.new
                                                                  |
                                                      после 3 попыток
                                                                  v
                                                 payments.dead.exchange
                                                                  |
                                                                  v
                                                          payments.new.dlq
```

Компоненты:

| Компонент | Ответственность |
|---|---|
| `api` | HTTP API, идемпотентное создание платежа и outbox в одной транзакции |
| `consumer` | Outbox relay + единственный consumer платежей, webhook, retry |
| `postgres` | `payments`, `outbox`, состояние webhook |
| `rabbitmq` | Основная очередь, retry-очереди, DLQ |
| `dozzle` | Web-интерфейс логов всех контейнеров |
| `migrate` | Одноразовый запуск `alembic upgrade head` перед стартом API/consumer |

`payments.new` — основная очередь, `payments.new.dlq` — Dead Letter Queue.
Retry-очереди не имеют consumer'ов: RabbitMQ сам возвращает сообщение в основной
exchange после истечения TTL.

---

## 4. Быстрый старт

Если нужен минимальный набор команд:

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps -a
```

Проверка API:

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
curl -H "X-API-Key: $API_KEY" http://localhost:8000/health
# {"status":"ok"}
```

Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs).
RabbitMQ Management UI: [http://localhost:15672](http://localhost:15672),
пользователь/пароль по умолчанию `payments` / `payments_password`.
Dozzle (логи контейнеров): [http://localhost:8080](http://localhost:8080).

Перед использованием в реальной среде обязательно поменяйте `API_KEY` и
пароли в `.env`. Подробный порядок действий — ниже.

---

## 5. Пошаговое развёртывание

### 5.1. Подготовить каталог проекта

Скопируйте проект на сервер или клонируйте репозиторий, затем перейдите в него:

```bash
cd test-task-payment-service
```

### 5.2. Создать `.env`

```bash
cp .env.example .env
```

`.env` содержит локальные секреты и уже добавлен в `.gitignore`. Откройте файл
и проверьте как минимум:

```dotenv
POSTGRES_PASSWORD=app_password
RABBITMQ_DEFAULT_PASS=payments_password
API_KEY=test-api-key-123
```

Для локальной разработки важно, чтобы совпадали:

```dotenv
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
RABBITMQ_URL=amqp://payments:payments_password@localhost:5672/
```

Для Docker-контейнеров `POSTGRES_HOST` и `POSTGRES_PORT` переопределяются
внутри `docker-compose.yml` на `postgres:5432`, а URL RabbitMQ собирается из
`RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS`. То есть `.env` используется
Compose для подстановки значений, но внутренние адреса контейнеров заданы
Compose.

Рекомендуется сгенерировать стойкие значения, например:

```bash
openssl rand -hex 24   # для API_KEY
openssl rand -hex 16   # для паролей
```

Если меняете пароль RabbitMQ, обновите и `RABBITMQ_URL` для локального запуска.

Не коммитьте `.env` в Git.

### 5.3. Проверить конфигурацию Compose

```bash
docker compose config
```

Команда показывает итоговую конфигурацию с подставленными значениями. Не
публикуйте её вывод: там есть пароли.

### 5.4. Собрать и запустить

```bash
docker compose up --build -d
```

Первый запуск может занять несколько минут: скачиваются образы PostgreSQL,
RabbitMQ, Dozzle и Python-зависимости.

### 5.5. Проверить контейнеры

```bash
docker compose ps -a
```

Ожидаемое состояние:

| Сервис | Ожидаемый статус |
|---|---|
| `postgres` | `Up ... (healthy)` |
| `rabbitmq` | `Up ... (healthy)` |
| `dozzle` | `Up ...` |
| `migrate` | `Exited (0)` — это нормально |
| `api` | `Up ... (healthy)` |
| `consumer` | `Up ...` |

`migrate` — одноразовый job. Если он завершился с кодом `0`, миграции
применились. Если `api` или `consumer` не стартовали, смотрите их логи (раздел
[«Troubleshooting»](#18-troubleshooting)).

### 5.6. Проверить миграции

```bash
docker compose logs --tail=100 migrate
docker compose exec api alembic current
```

Вторая команда должна показать актуальную ревизию (например,
`b7c1f4a29e10 (head)`).

### 5.7. Проверить API

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
curl -i -H "X-API-Key: $API_KEY" http://localhost:8000/health
```

Ожидается `200 OK` и тело:

```json
{"status":"ok"}
```

Без заголовка `X-API-Key` health вернёт `401` — это ожидаемое поведение.

### 5.8. Проверить RabbitMQ

Management UI: [http://localhost:15672](http://localhost:15672).

Логин и пароль — значения `RABBITMQ_DEFAULT_USER` и
`RABBITMQ_DEFAULT_PASS` из `.env` (по умолчанию `payments` /
`payments_password`).

В разделе `Queues` должны быть:

- `payments.new` — с одним consumer;
- `payments.new.dlq` — без consumer;
- `payments.retry.2`, `payments.retry.3` — без consumer (при стандартных
  настройках).

CLI-проверка:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
```

### 5.9. Проверить Dozzle

Откройте [http://localhost:8080](http://localhost:8080). В интерфейсе должны
быть контейнеры `payment_api`, `payment_consumer`, `payment_db`,
`payment_rabbit`, `payment_migrate` и `payment_dozzle`.

Используется закреплённый образ `amir20/dozzle:v11.3.5`. Dozzle читает логи
через смонтированный Docker-сокет:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

Сокет смонтирован только для чтения, но сам Docker API даёт широкий доступ к
хосту. Не публикуйте Dozzle в интернет без аутентификации и reverse proxy.
Порт на хосте задаётся переменной `DOZZLE_PORT` (по умолчанию `8080`).

Если интерфейс открылся, но логов нет, проверьте:

```bash
docker compose logs dozzle
docker compose ps -a
```

---

## 6. Проверка после запуска

Короткий smoke-test:

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
export API_URL="http://localhost:8000"

curl -fsS -H "X-API-Key: $API_KEY" "$API_URL/health"
curl -fsS -o /dev/null -w 'dozzle_http=%{http_code}\n' http://localhost:8080/
docker compose ps -a
docker compose logs --tail=50 api consumer
```

Если все три шага успешны, можно создавать платежи (раздел
[«Работа с API»](#7-работа-с-api)).

---

## 7. Работа с API

API требует два обязательных заголовка:

- `X-API-Key` — статический ключ из `.env`;
- `Idempotency-Key` — уникальный ключ конкретного запроса на создание платежа.

Для GET нужен только `X-API-Key`.

Подготовьте переменные в терминале:

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
export API_URL="http://localhost:8000"
```

### 7.1. Создание платежа

```bash
export IDEMPOTENCY_KEY="order-$(date +%s)-$RANDOM"

CREATE_RESPONSE=$(curl -sS -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d '{
    "amount": "1500.00",
    "currency": "RUB",
    "description": "Оплата заказа 123",
    "metadata": {"order_id": 123, "customer_id": "c-42"},
    "webhook_url": "http://host.docker.internal:9000/webhook"
  }')

printf '%s\n' "$CREATE_RESPONSE" | python3 -m json.tool
export PAYMENT_ID=$(printf '%s' "$CREATE_RESPONSE" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
echo "PAYMENT_ID=$PAYMENT_ID"
```

Ответ `202 Accepted`:

```json
{
  "payment_id": "b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0",
  "status": "pending",
  "created_at": "2026-10-26T18:00:00.123456Z"
}
```

В заголовках ответа также будет:

- `Location: /api/v1/payments/<payment_id>`;
- `Idempotency-Replayed` — только при повторном запросе.

Поля запроса:

| Поле | Тип | Обязательно | Ограничения |
|---|---|---|---|
| `amount` | decimal | да | больше 0, не более 2 знаков после запятой |
| `currency` | enum | да | `RUB`, `USD`, `EUR` |
| `description` | string | да | 1–255 символов |
| `metadata` | object | нет | произвольный JSON, по умолчанию `{}` |
| `webhook_url` | URL | да | `http://` или `https://`, не длиннее 2048 |

`webhook_url` должен быть доступен **из контейнера consumer**. Для приёмника на
хосте используйте `http://host.docker.internal:9000/...` (см. раздел
[«Webhook»](#8-webhook)). Для внешнего сервиса — публичный URL.

Если приёмник ещё не запущен, платёж всё равно будет создан и обработан, но
webhook уйдёт в retry и затем в DLQ. Для чистого smoke-теста сначала запустите
приёмник из раздела 8.1.

### 7.2. Получение платежа

```bash
curl -sS -H "X-API-Key: $API_KEY" \
  "$API_URL/api/v1/payments/$PAYMENT_ID" | python3 -m json.tool
```

Пример ответа после обработки:

```json
{
  "payment_id": "b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0",
  "amount": "1500.00",
  "currency": "RUB",
  "description": "Оплата заказа 123",
  "metadata": {"order_id": 123, "customer_id": "c-42"},
  "status": "succeeded",
  "idempotency_key": "order-123-attempt-1",
  "webhook_url": "http://host.docker.internal:9000/webhook",
  "webhook_delivered": true,
  "webhook_attempts": 1,
  "failure_reason": null,
  "created_at": "2026-10-26T18:00:00.123456Z",
  "processed_at": "2026-10-26T18:00:03.987654Z"
}
```

Статусы:

| Статус | Значение |
|---|---|
| `pending` | платёж создан, ещё не обработан consumer'ом |
| `succeeded` | эмуляция шлюза завершилась успешно |
| `failed` | шлюз отклонил платёж (10% по умолчанию) или обработка окончательно не удалась |

Поля webhook-состояния:

- `webhook_delivered` — был ли webhook успешно принят получателем;
- `webhook_attempts` — сколько HTTP-попыток было сделано;
- `failure_reason` — причина отказа шлюза, если платёж `failed` (например,
  `simulated_gateway_decline`).

### 7.3. Ожидание результата

Обработка занимает 2–5 секунд плюс задержка outbox relay (по умолчанию до ~1
секунды). Можно опрашивать платёж:

```bash
for n in $(seq 1 15); do
  BODY=$(curl -sS -H "X-API-Key: $API_KEY" \
    "$API_URL/api/v1/payments/$PAYMENT_ID")
  STATUS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  printf '[%02d] status=%s\n' "$n" "$STATUS"

  if [ "$STATUS" != "pending" ]; then
    printf '%s\n' "$BODY" | python3 -m json.tool
    break
  fi
  sleep 2
done
```

Помните: 10% платежей завершаются статусом `failed` — это эмуляция отказа
внешнего шлюза, а не ошибка сервиса.

### 7.4. Идемпотентность

Повторный POST с тем же `Idempotency-Key` и тем же телом:

```bash
curl -i -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d '{
    "amount": "1500.00",
    "currency": "RUB",
    "description": "Оплата заказа 123",
    "metadata": {"order_id": 123, "customer_id": "c-42"},
    "webhook_url": "http://host.docker.internal:9000/webhook"
  }'
```

Вернётся тот же `payment_id` с `202 Accepted` и заголовком
`Idempotency-Replayed: true`; новый платёж создан не будет.

Тот же ключ с другим телом вернёт `409 Conflict`:

```bash
curl -i -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d '{
    "amount": "999.00",
    "currency": "RUB",
    "description": "Другая сумма",
    "metadata": {"order_id": 123, "customer_id": "c-42"},
    "webhook_url": "http://host.docker.internal:9000/webhook"
  }'
```

Идемпотентность реализована на уровне уникального индекса БД, поэтому она
работает и при одновременных запросах.

### 7.5. Ошибки API

| Код | Причина | Что проверить |
|---|---|---|
| `401` | неверный или отсутствующий `X-API-Key` | значение `API_KEY` в `.env` и заголовок |
| `404` | платёж с таким ID не найден | UUID из ответа на создание |
| `409` | `Idempotency-Key` уже использован с другим телом | используйте новый ключ для нового платежа |
| `422` | ошибка валидации | сумму, валюту, URL, наличие `Idempotency-Key` |
| `500` | внутренняя ошибка (обычно БД/конфигурация) | `docker compose logs api`, миграции |

Пример `422`:

```json
{
  "detail": [
    {
      "type": "decimal_parsing",
      "loc": ["body", "amount"],
      "msg": "Input should be a valid decimal"
    }
  ]
}
```

### 7.6. Swagger UI

Откройте [http://localhost:8000/docs](http://localhost:8000/docs), нажмите
**Authorize** и введите значение `API_KEY` (без префикса `Bearer`).
Swagger UI и `/openapi.json` — инфраструктурные страницы; в публичном окружении
их стоит закрыть reverse proxy или отключить.

---

## 8. Webhook

### 8.1. Локальный приёмник

В отдельном терминале:

```bash
python3 scripts/webhook_receiver.py --port 9000
```

Скрипт слушает `0.0.0.0:9000`, печатает входящие запросы и возвращает `200 OK`.
Для проверки retry:

```bash
python3 scripts/webhook_receiver.py --port 9000 --fail-first 2
```

С `--fail-first 2` первые две доставки получат `500`, третья — `200`.
Счётчик доставок сбрасывается при перезапуске приёмника.

Если consumer запущен в Docker, URL приёмника на хосте:

```text
http://host.docker.internal:9000/webhook
```

Если consumer запущен локально (раздел 14), используйте:

```text
http://localhost:9000/webhook
```

`localhost` внутри контейнера указывает на сам контейнер, а не на хост — это
самая частая причина ошибки `All connection attempts failed`.

### 8.2. Формат уведомления

Webhook отправляется после того, как результат платежа зафиксирован в БД:

```json
{
  "event_id": "b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0:succeeded",
  "event": "payment.succeeded",
  "payment_id": "b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0",
  "status": "succeeded",
  "amount": "1500.00",
  "currency": "RUB",
  "description": "Оплата заказа 123",
  "metadata": {"order_id": 123, "customer_id": "c-42"},
  "failure_reason": null,
  "created_at": "2026-10-26T18:00:00.123456+00:00",
  "processed_at": "2026-10-26T18:00:03.987654+00:00"
}
```

Заголовки:

| Заголовок | Значение |
|---|---|
| `X-Event-Type` | `payment.succeeded` или `payment.failed` |
| `X-Payment-Id` | UUID платежа |
| `X-Webhook-Id` | стабильный ID уведомления, равен `event_id` |
| `Content-Type` | `application/json` |

Доставка **at-least-once**. Получатель должен быть идемпотентным: одна и та же
доставка может прийти повторно (например, если consumer упал после отправки, но
до сохранения `webhook_delivered=true`). Ключ дедупликации — `X-Webhook-Id` или
`event_id`.

### 8.3. Ответ получателя

- любой `2xx` — успех, `webhook_delivered=true`;
- сетевой таймаут, connection error или не-2xx — ошибка, сообщение уходит в
  retry.

Таймаут webhook по умолчанию — 10 секунд (`WEBHOOK_TIMEOUT_SECONDS`).

---

## 9. Обработка, retry и DLQ

### 9.1. Жизненный цикл платежа

```text
pending
   |
   | consumer: 2-5s, 90/10
   v
succeeded / failed  +  processed_at
   |
   | webhook 2xx
   v
webhook_delivered=true
```

Результат шлюза сохраняется **до** отправки webhook. Поэтому при повторной
доставке сообщения consumer не вызывает шлюз заново и не меняет статус — он
только повторяет webhook.

### 9.2. Попытки

По умолчанию `MAX_PROCESSING_ATTEMPTS=3`: первая попытка + два повтора.

| Попытка | Задержка перед попыткой | Механизм |
|---|---|---|
| 1 | — | сразу из `payments.new` |
| 2 | 1 секунда | TTL-очередь `payments.retry.2` |
| 3 | 2 секунды | TTL-очередь `payments.retry.3` |

Задержка вычисляется как
`RETRY_BASE_DELAY_SECONDS * 2^(attempt - 2)`. При стандартной базе 1 секунда
получаются 1s и 2s, затем 4s и так далее — количество очередей зависит от
`MAX_PROCESSING_ATTEMPTS`.

Сообщение в retry-очереди не обрабатывается consumer'ом: RabbitMQ держит его
TTL, после чего возвращает в `payments.exchange` с routing key
`payment.created`.

### 9.3. Dead Letter Queue

После третьей неудачной попытки сообщение отклоняется с `requeue=False` и через
`payments.dead.exchange` попадает в `payments.new.dlq`.

DLQ **не вычитывается автоматически** — сообщения остаются там для разбора.
Если на момент окончательного сбоя платёж всё ещё `pending`, consumer помечает
его `failed` и записывает `failure_reason`.

Типичные причины попадания в DLQ:

- webhook-получатель недоступен или стабильно отвечает не-2xx;
- ошибка БД при обработке;
- некорректное/несуществующее событие.

Посмотреть очередь:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
```

В Management UI: `Queues` → `payments.new.dlq` → `Get messages`.

Ручной повтор после исправления получателя:

1. Убедитесь, что webhook-получатель снова доступен.
2. Откройте `payments.new.dlq` в RabbitMQ UI.
3. Скопируйте payload через `Get messages`, не удаляя сообщение (в UI выберите
   `Nack message requeue true`).
4. Опубликуйте тело сообщения в exchange `payments.exchange` с routing key
   `payment.created` и заголовком `x-retry-count: 1`.
5. Consumer обработает событие заново. Если платёж уже имеет финальный статус,
   повторно будет отправлен только webhook; если `pending` — выполнится полная
   обработка.

Если нужно повторить именно эмуляцию шлюза для уже завершённого платежа,
сбросьте состояние вручную (осторожно, только в тестовой среде):

Откройте `psql`:

```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

И выполните SQL, подставив ID платежа:

```sql
UPDATE payments
SET status = 'pending',
    processed_at = NULL,
    webhook_delivered = false,
    webhook_attempts = 0,
    failure_reason = NULL
WHERE id = '<payment_id>';
```

После этого опубликуйте событие заново способом выше. Не используйте этот
запрос в production без понимания последствий.

---

## 10. Мониторинг и диагностика

### 10.1. Логи

Основной способ — Dozzle: [http://localhost:8080](http://localhost:8080).
В интерфейсе можно выбрать контейнер, искать по тексту, смотреть логи в
реальном времени и скачивать их. Порт задаётся `DOZZLE_PORT`.

CLI-команды остаются полезны для быстрого просмотра из терминала.

Все сервисы:

```bash
docker compose logs -f --tail=100
```

Только API и consumer:

```bash
docker compose logs -f --tail=100 api consumer
```

Последние логи миграций:

```bash
docker compose logs --tail=100 migrate
```

Логи с временными метками:

```bash
docker compose logs --timestamps --tail=200 consumer
```

### 10.2. Статус контейнеров

```bash
docker compose ps -a
```

`migrate` должен быть `Exited (0)`. `api`, `postgres` и `rabbitmq` — `healthy`.
`consumer` и `dozzle` отдельных healthcheck не имеют; проверяйте их логи и
доступность интерфейсов.

### 10.3. Health API

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
curl -i -H "X-API-Key: $API_KEY" http://localhost:8000/health
```

`/health` защищён тем же API-ключом, что и бизнес-эндпоинты.

### 10.4. RabbitMQ

Через CLI:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
docker compose exec rabbitmq rabbitmqctl list_exchanges name type
```

Через UI: [http://localhost:15672](http://localhost:15672).

Полезно смотреть:

- `payments.new` — есть ли consumer и растёт ли `messages_ready`;
- `payments.retry.*` — временно накопленные retry-сообщения;
- `payments.new.dlq` — окончательно упавшие сообщения.

### 10.5. База данных

Открыть `psql`:

```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Последние платежи:

```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, status, amount, currency, webhook_delivered, webhook_attempts,
       created_at, processed_at
FROM payments
ORDER BY created_at DESC
LIMIT 10;
"'
```

Состояние outbox:

```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, event_type, status, attempts, last_error, next_attempt_at
FROM outbox
ORDER BY created_at DESC
LIMIT 10;
"'
```

Нормальное состояние:

- `payments.status` — `pending`, `succeeded` или `failed`;
- `outbox.status` — `published` для опубликованных событий;
- `outbox.attempts` — число попыток публикации;
- `outbox.last_error` — причина последней ошибки публикации, если была;
- `next_attempt_at` — когда relay попробует снова.

### 10.6. Проверка миграций

```bash
docker compose exec api alembic current
docker compose exec api alembic history
```

Если API не запущен:

```bash
docker compose run --rm migrate alembic current
```

---

## 11. Проверка основных сценариев

### 11.1. Успешный платёж и webhook

1. Запустите приёмник:

   ```bash
   python3 scripts/webhook_receiver.py --port 9000
   ```

2. Создайте платёж с `webhook_url = http://host.docker.internal:9000/webhook`.
3. Подождите 5–10 секунд.
4. Проверьте платёж:

   ```bash
   curl -sS -H "X-API-Key: $API_KEY" \
     "$API_URL/api/v1/payments/$PAYMENT_ID" | python3 -m json.tool
   ```

Ожидается `webhook_delivered=true` и `webhook_attempts=1` (если получатель
ответил `200` с первой попытки). Статус `succeeded`/`failed` зависит от
эмуляции 90/10.

### 11.2. Retry webhook

1. Запустите приёмник, который первые два раза отвечает ошибкой:

   ```bash
   python3 scripts/webhook_receiver.py --port 9000 --fail-first 2
   ```

2. Создайте новый платёж с новым `Idempotency-Key`.
3. В логах приёмника будут доставки `500`, `500`, `200`.
4. Через ~10 секунд проверьте платёж: `webhook_attempts=3`,
   `webhook_delivered=true`.
5. Проверьте, что retry-очереди пусты:

   ```bash
   docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
   ```

### 11.3. Dead Letter Queue

1. Создайте платёж с заведомо недоступным webhook:

   ```bash
   export IDEMPOTENCY_KEY="dlq-$(date +%s)-$RANDOM"

   CREATE_RESPONSE=$(curl -sS -X POST "$API_URL/api/v1/payments" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
     -d '{
       "amount": "77.77",
       "currency": "USD",
       "description": "DLQ test",
       "metadata": {},
       "webhook_url": "http://host.docker.internal:9999/unreachable"
     }')
   printf '%s\n' "$CREATE_RESPONSE" | python3 -m json.tool
   export PAYMENT_ID=$(printf '%s' "$CREATE_RESPONSE" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
   ```

2. Подождите 15–20 секунд.
3. Проверьте платёж: `webhook_delivered=false`, `webhook_attempts=3`.
4. Проверьте DLQ:

   ```bash
   docker compose exec rabbitmq rabbitmqctl list_queues \
     name messages_ready messages_unacknowledged consumers
   ```

   В `payments.new.dlq` должно быть `messages_ready=1`, `consumers=0`.

5. Разберите сообщение через RabbitMQ UI и при необходимости переопубликуйте
   после исправления получателя (раздел 9.3).

### 11.4. Outbox при недоступном RabbitMQ

Этот тест проверяет, что API не теряет платёж при падении брокера.

1. Остановите RabbitMQ:

   ```bash
   docker compose stop rabbitmq
   ```

2. Создайте платёж. API продолжит отвечать `202`, потому что пишет только в
   PostgreSQL.
3. Проверьте outbox:

   ```bash
   docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
   SELECT status, attempts, last_error, next_attempt_at
   FROM outbox
   ORDER BY created_at DESC
   LIMIT 1;
   "'
   ```

   Событие будет `pending`, `attempts` будет расти, `last_error` — содержать
   ошибку соединения.

4. Запустите RabbitMQ обратно:

   ```bash
   docker compose start rabbitmq
   ```

5. Consumer переподключится, outbox relay повторит публикацию. Подождите
   10–45 секунд и проверьте снова: `outbox.status=published`, платёж
   обработан.

Если RabbitMQ был недоступен долго, backoff outbox может достигать
`OUTBOX_MAX_BACKOFF_SECONDS` (по умолчанию 60 секунд).

---

## 12. Управление сервисом

### 12.1. Остановить, не удаляя данные

```bash
docker compose stop
```

Запустить снова:

```bash
docker compose start
```

### 12.2. Перезапустить отдельный сервис

```bash
docker compose restart consumer
docker compose restart api
```

### 12.3. Пересобрать после изменения кода

```bash
docker compose up -d --build
```

Только выбранные сервисы:

```bash
docker compose up -d --build api consumer
```

### 12.4. Применить миграции вручную

```bash
docker compose run --rm migrate alembic upgrade head
```

Обычно это не требуется: сервис `migrate` запускается автоматически при
`docker compose up`.

### 12.5. Полностью пересоздать окружение

```bash
docker compose down
docker compose up -d --build
```

Данные в volume сохраняются.

---

## 13. Конфигурация

Все настройки читаются из переменных окружения и `.env`. Значения по умолчанию
для Docker/локального запуска находятся в `.env.example`.

| Переменная | Значение в `.env.example` | Где применяется | Описание |
|---|---|---|---|
| `POSTGRES_USER` | `app_user` | API, consumer, migrate | пользователь PostgreSQL |
| `POSTGRES_PASSWORD` | `app_password` | API, consumer, migrate | пароль PostgreSQL |
| `POSTGRES_HOST` | `localhost` | локальный запуск | в compose переопределяется на `postgres` |
| `POSTGRES_PORT` | `5433` | локальный запуск | в compose переопределяется на `5432` |
| `POSTGRES_DB` | `payment_db` | API, consumer, migrate | имя БД |
| `POSTGRES_ECHO` | `false` | API, consumer | логировать SQL-запросы |
| `POSTGRES_HOST_PORT` | `5433` | compose | порт PostgreSQL на хосте |
| `RABBITMQ_DEFAULT_USER` | `payments` | RabbitMQ + compose | пользователь RabbitMQ |
| `RABBITMQ_DEFAULT_PASS` | `payments_password` | RabbitMQ + compose | пароль RabbitMQ |
| `RABBITMQ_URL` | `amqp://payments:...@localhost:5672/` | локальный запуск | в compose URL собирается из `RABBITMQ_DEFAULT_*` |
| `DOZZLE_PORT` | `8080` | compose / Dozzle | порт Dozzle на хосте |
| `API_KEY` | `test-api-key-123` | API + healthcheck | статический API-ключ |
| `APP_ENV` | `development` | API/consumer | в compose для контейнеров задаётся `production` |
| `OUTBOX_POLL_INTERVAL` | `1.0` | consumer | период опроса outbox, сек |
| `OUTBOX_BATCH_SIZE` | `100` | consumer | размер пачки outbox |
| `OUTBOX_MAX_BACKOFF_SECONDS` | `60` | consumer | максимум backoff публикации outbox |
| `MAX_PROCESSING_ATTEMPTS` | `3` | consumer | всего попыток обработки сообщения |
| `RETRY_BASE_DELAY_SECONDS` | `1.0` | consumer | база экспоненциальной задержки |
| `PAYMENT_SUCCESS_RATE` | `0.9` | consumer | доля успешных эмуляций шлюза |
| `PROCESSING_MIN_SECONDS` | `2.0` | consumer | минимальная задержка шлюза |
| `PROCESSING_MAX_SECONDS` | `5.0` | consumer | максимальная задержка шлюза |
| `WEBHOOK_TIMEOUT_SECONDS` | `10.0` | consumer | таймаут HTTP-запроса webhook |

После изменения переменных пересоздайте затронутые контейнеры:

```bash
docker compose up -d --force-recreate api consumer
```

Особенности:

- настройки читаются при старте процесса; `restart` без пересоздания может не
  подхватить изменённый `.env`;
- изменение `API_KEY` требует пересоздания `api` (healthcheck тоже использует
  этот ключ);
- изменение `DOZZLE_PORT` применяется пересозданием Dozzle:

  ```bash
  docker compose up -d --force-recreate dozzle
  ```
- изменение `POSTGRES_PASSWORD` после первого создания volume не меняет пароль
  уже существующего пользователя БД: нужен `ALTER USER` или пересоздание данных;
- изменение `RETRY_BASE_DELAY_SECONDS` меняет аргументы TTL-очередей. RabbitMQ
  не позволяет переобъявить очередь с другими аргументами: удалите retry-очереди
  или пересоздайте RabbitMQ volume;
- увеличение `MAX_PROCESSING_ATTEMPTS` добавляет новые retry-очереди; уменьшение
  оставляет старые очереди в RabbitMQ без consumer'а (их можно удалить вручную);
- `POSTGRES_HOST_PORT` влияет только на проброс порта на хост;
- чтобы изменить порт API `8000`, отредактируйте `ports` в
  [docker-compose.yml](docker-compose.yml).

---

## 14. Локальный запуск без Docker

Этот вариант удобен для разработки: PostgreSQL и RabbitMQ — в Docker, API и
consumer — локальными процессами Python.

### 14.1. Подготовка

```bash
uv sync
cp .env.example .env
docker compose up -d postgres rabbitmq
```

Убедитесь, что в `.env` для локального запуска:

```dotenv
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
RABBITMQ_URL=amqp://payments:payments_password@localhost:5672/
```

Если полный Docker-стек уже запущен, остановите его API/consumer, чтобы не
занимать порт `8000`:

```bash
docker compose stop api consumer
```

### 14.2. Миграции

```bash
uv run alembic upgrade head
```

`migrations/env.py` сам добавляет `src` в `sys.path`, поэтому команда работает
из корня проекта.

### 14.3. API

В первом терминале:

```bash
PYTHONPATH=src uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 14.4. Consumer + outbox relay

Во втором терминале:

```bash
PYTHONPATH=src uv run python -m app.messaging.worker
```

Worker одновременно запускает outbox relay и consumer.

### 14.5. Webhook при локальном worker

Если и API, и consumer запущены локально, используйте URL:

```text
http://localhost:9000/webhook
```

Приёмник:

```bash
python3 scripts/webhook_receiver.py --port 9000
```

Проверка health и создание платежа — как в разделах 6–7.

---

## 15. Тесты и линтеры

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
PYTHONPATH=src uv run mypy src
```

Unit-тесты не требуют PostgreSQL и RabbitMQ: проверяются Pydantic-схемы,
API-key dependency, парсинг retry-заголовков и формирование webhook payload.

Для полной проверки поведения используйте сценарии из раздела
[«Проверка основных сценариев»](#11-проверка-основных-сценариев).

---

## 16. Структура проекта

```text
src/app/
  api/deps.py              # сессия БД, X-API-Key
  api/v1/payments.py       # POST/GET эндпоинты
  api/v1/health.py         # health (тоже под API-ключом)
  core/config.py           # настройки
  db/session.py            # async engine/session
  models/                  # Payment, Outbox, enum'ы
  schemas/payment.py       # Pydantic-схемы API
  services/payments.py     # идемпотентное создание в транзакции
  messaging/broker.py      # топология RabbitMQ
  messaging/outbox.py      # outbox relay
  messaging/consumer.py    # consumer платежей, retry, webhook
  messaging/worker.py      # entrypoint worker'а
  main.py                  # FastAPI app
migrations/                # Alembic
scripts/webhook_receiver.py
tests/
docker-compose.yml
Dockerfile
.env.example
```

Таблицы:

- `payments` — платёж, статус, webhook-состояние, даты;
- `outbox` — события для публикации, попытки и backoff.

---

## 17. Production-развёртывание

Ниже — минимальные рекомендации для запуска на сервере.

1. Установите Docker Engine и Compose plugin.
2. Скопируйте проект на сервер, создайте `.env` с боевыми секретами:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

3. Замените как минимум:
   - `API_KEY` — длинный случайный ключ;
   - `POSTGRES_PASSWORD`;
   - `RABBITMQ_DEFAULT_PASS`;
   - при необходимости `RABBITMQ_URL` для локального запуска.

4. Запустите:

   ```bash
   docker compose up -d --build
   ```

5. Проверьте `docker compose ps -a`, health API и миграции.

6. **Не публикуйте наружу** порты PostgreSQL (`5433`), RabbitMQ AMQP (`5672`),
   RabbitMQ Management UI (`15672`) и Dozzle (`8080`). В текущем compose они
   проброшены на хост для удобства. Для production:
   - закройте их firewall'ом;
   - либо уберите/привяжите к `127.0.0.1` соответствующие `ports` в
     [docker-compose.yml](docker-compose.yml).

   Dozzle особенно чувствителен к публикации: он подключён к
   `/var/run/docker.sock` и без аутентификации показывает логи всех контейнеров.
   Используйте SSH-туннель, VPN или reverse proxy с аутентификацией.

7. Поставьте API за reverse proxy с TLS (Nginx/Caddy/Traefik). Наружу обычно
   нужен только `443` (и, при необходимости, `80` для ACME).

8. Ограничьте доступ к `/docs` и `/openapi.json`, если API публичный.

9. Webhook-получатель должен быть доступен **с сервера** по указанному
   `webhook_url`. Если клиенты передают произвольные URL, рассмотрите allowlist
   доменов — это снижает риск SSRF.

10. Настройте мониторинг:
    - health API;
    - длину `payments.new`, `payments.retry.*`, `payments.new.dlq`;
    - ошибки в логах consumer;
    - свободное место на volume PostgreSQL/RabbitMQ.

### 17.1. Backup и restore PostgreSQL

Backup:

```bash
docker compose exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > payment_db.dump
```

Restore (осторожно, перезапишет данные). Сначала остановите API и consumer,
чтобы не было активных соединений:

```bash
docker compose stop api consumer
cat payment_db.dump | docker compose exec -T postgres sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists'
docker compose start api consumer
```

Проверьте backup на тестовом окружении перед восстановлением в production.

---

## 18. Troubleshooting

### 18.1. Порт уже занят

Симптом: контейнер не стартует, `bind: address already in use`.

Проверьте занятые порты:

```bash
lsof -i :8000 -i :5433 -i :5672 -i :15672 -i :8080
# Linux-альтернатива:
ss -ltnp | grep -E ':(8000|5433|5672|15672|8080)'
```

Варианты:

- остановить конфликтующий процесс;
- изменить `POSTGRES_HOST_PORT` в `.env`;
- изменить `ports` для `api`/`rabbitmq` в `docker-compose.yml` и пересобрать
  запуск;
- изменить `DOZZLE_PORT` в `.env` и пересоздать только Dozzle:
  `docker compose up -d --force-recreate dozzle`.

### 18.2. `migrate` завершился с ошибкой

```bash
docker compose logs --tail=200 migrate
```

Частые причины:

- PostgreSQL ещё не готов (healthcheck не прошёл);
- неверные `POSTGRES_*`;
- ошибка миграции.

Если миграция упала, `api` и `consumer` не запустятся — это ожидаемая защита от
работы с несовместимой схемой.

### 18.3. API отвечает `401`

- проверьте заголовок `X-API-Key`;
- сверьте значение с `API_KEY` в `.env`;
- после изменения `.env` пересоздайте API:

  ```bash
  docker compose up -d --force-recreate api
  ```

### 18.4. API отвечает `422`

Проверьте:

- передан ли `Idempotency-Key`;
- `amount` — положительное число не более чем с двумя знаками после запятой;
- `currency` — `RUB`, `USD` или `EUR`;
- `webhook_url` — валидный http/https URL;
- нет ли опечаток/лишних полей в JSON (лишние поля запрещены).

### 18.5. API отвечает `500`

```bash
docker compose logs --tail=200 api
docker compose ps -a
docker compose exec api alembic current
```

Чаще всего причина — недоступная БД или неприменённые миграции.

### 18.6. Платёж долго остаётся `pending`

1. Проверьте consumer:

   ```bash
   docker compose logs --tail=200 consumer
   ```

2. Проверьте outbox:

   ```bash
   docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
   SELECT status, attempts, last_error, next_attempt_at
   FROM outbox ORDER BY created_at DESC LIMIT 5;
   "'
   ```

3. Проверьте очереди RabbitMQ:

   ```bash
   docker compose exec rabbitmq rabbitmqctl list_queues \
     name messages_ready messages_unacknowledged consumers
   ```

Если `outbox.status=pending` и `attempts` растёт — consumer не может
подключиться к RabbitMQ или публикация отклоняется. Смотрите `last_error` и
логи `consumer`.

### 18.7. Webhook: `All connection attempts failed`

- Убедитесь, что приёмник запущен и слушает `0.0.0.0`.
- Для приёмника на хосте из Docker используйте
  `http://host.docker.internal:9000/...`, а не `localhost`.
- Для локального worker используйте `localhost`.
- Проверьте, что порт не закрыт firewall'ом.
- Если приёмник в другом контейнере, используйте имя сервиса и внутренний порт.

### 18.8. Webhook доставлен, но `webhook_delivered=false`

Это возможно, если consumer упал после получения `2xx`, но до commit'а
`webhook_delivered=true`. Сообщение будет повторено — это ожидаемая
at-least-once семантика. Получатель должен дедуплицировать по `X-Webhook-Id`.

### 18.9. Consumer перезапускается

```bash
docker compose logs --tail=200 consumer
```

Проверьте:

- доступность RabbitMQ;
- `RABBITMQ_URL` / `RABBITMQ_DEFAULT_*`;
- совпадение аргументов retry-очередей (после изменения TTL может потребоваться
  удалить очереди и перезапустить consumer);
- миграции БД.

### 18.10. RabbitMQ UI не открывается

- UI доступен на `http://localhost:15672`;
- проверьте `docker compose ps rabbitmq`;
- логин/пароль — `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS`;
- если volume RabbitMQ создавался с другими credentials, env-переменные не
  переопределяют уже созданного пользователя: используйте `rabbitmqctl
  change_password` или пересоздайте volume.

### 18.11. Dozzle не показывает логи

```bash
docker compose ps -a dozzle
docker compose logs dozzle
ls -l /var/run/docker.sock
```

Проверьте:

- контейнер `dozzle` запущен и порт `8080` не занят;
- Docker-сокет существует и доступен;
- в `docker-compose.yml` смонтирован
  `/var/run/docker.sock:/var/run/docker.sock:ro`;
- для rootless Docker путь к сокету может отличаться от
  `/var/run/docker.sock`;
- если порт изменён через `DOZZLE_PORT`, интерфейс доступен на новом порту;
- в production доступ к Dozzle не закрыт firewall'ом.

### 18.12. Полный сброс (удалит все данные)

Используйте, только если данные не нужны:

```bash
docker compose down -v
docker compose up -d --build
```

---

## 19. Остановка и удаление данных

Остановить и удалить контейнеры/сеть, сохранив данные:

```bash
docker compose down
```

Остановить и удалить данные PostgreSQL/RabbitMQ:

```bash
docker compose down -v
```

Удалить собранный образ:

```bash
docker image rm payment-service:local
```

Проверить оставшиеся volume:

```bash
docker volume ls | grep test-task-payment-service
```

---

## 20. Ограничения

- Webhook не подписывается HMAC: в production стоит добавить подпись и
  ротацию секрета.
- API-ключ один и статический; для нескольких клиентов нужна таблица ключей и
  их ротация.
- Outbox relay запущен в процессе consumer; для больших объёмов его лучше
  вынести в отдельный deployment. SQL уже использует `SKIP LOCKED`.
- Один consumer и `prefetch=1` — обработка сообщений последовательная.
- Метрики и трейсинг (Prometheus/OpenTelemetry) не подключены. Для DLQ стоит
  настроить алертинг по длине очереди.
- Swagger UI и OpenAPI не защищены `X-API-Key`; закрывайте их на уровне
  reverse proxy/сети.
- `POSTGRES_PASSWORD`/RabbitMQ credentials после первого создания volume
  требуют отдельного изменения пользователя или пересоздания данных.
- Dozzle без дополнительной настройки не имеет аутентификации, а доступ к
  Docker-сокету эквивалентен широким правам на хост. Не публикуйте его в
  интернет; используйте VPN, SSH-туннель или reverse proxy с аутентификацией.

---

## 21. Чек-лист приёмки

- [ ] `.env` создан, секреты не закоммичены.
- [ ] `docker compose config` проходит без ошибок.
- [ ] `docker compose ps -a`: `migrate` — `Exited (0)`, `api` — healthy.
- [ ] Dozzle открывается на `http://localhost:8080` и показывает логи
      контейнеров.
- [ ] `curl /health` с `X-API-Key` возвращает `200`.
- [ ] `POST /api/v1/payments` возвращает `202` и `payment_id`.
- [ ] Повторный POST с тем же `Idempotency-Key` возвращает тот же `payment_id`.
- [ ] Повторный POST с другим телом возвращает `409`.
- [ ] `GET /api/v1/payments/{id}` показывает `pending`, затем
      `succeeded`/`failed`.
- [ ] Webhook приходит на приёмник; `webhook_delivered=true`.
- [ ] При `--fail-first 2` webhook доставляется с третьей попытки.
- [ ] При недоступном webhook после 3 попыток сообщение есть в
      `payments.new.dlq`.
- [ ] В outbox после обработки `status=published`.
- [ ] `docker compose logs consumer` не содержит необработанных ошибок.
