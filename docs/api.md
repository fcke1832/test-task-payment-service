# API

## 1. Общие сведения

| Параметр | Значение |
|---|---|
| Базовый URL (локально) | `http://localhost:8000` |
| Версия API | `v1` (`/api/v1`) |
| Формат тела | JSON (`application/json`) |
| Аутентификация | заголовок `X-API-Key` |
| Идемпотентность POST | заголовок `Idempotency-Key` |
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| OpenAPI | `http://localhost:8000/openapi.json` |

Эндпоинты:

| Метод | Путь | `X-API-Key` | `Idempotency-Key` | Успех |
|---|---|---|---|---|
| `GET` | `/health` | обязателен | не нужен | `200 OK` |
| `POST` | `/api/v1/payments` | обязателен | обязателен | `202 Accepted` |
| `GET` | `/api/v1/payments/{payment_id}` | обязателен | не нужен | `200 OK` |

Других бизнес-эндпоинтов нет: список платежей, отмена и возврат не
поддерживаются. Неизвестный путь возвращает `404`, неподдерживаемый метод —
`405`.

## 2. Аутентификация

Ключ передаётся в заголовке:

```http
X-API-Key: <значение API_KEY из .env>
```

Особенности:

- ключ один и статический, задаётся переменной `API_KEY`;
- сравнение выполняется в constant-time;
- префикс `Bearer` не используется;
- при отсутствии или неверном ключе возвращается `401` **до** валидации тела:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: ApiKey
Content-Type: application/json
```

```json
{"detail": "Invalid or missing API key"}
```

- `/health` защищён тем же ключом, что и бизнес-эндпоинты;
- Swagger UI не защищён ключом: нажмите **Authorize** и введите значение
  `API_KEY`, чтобы выполнять запросы из UI. В публичном окружении `/docs`,
  `/redoc` и `/openapi.json` стоит закрыть reverse proxy.

## 3. Формат ошибок

Есть три разных формата, которые важно различать.

**Доменная ошибка** (`401`, `404`, `409`, `405`, кастомная `422` для пустого
ключа) — объект с одним строковым `detail`:

```json
{"detail": "Payment not found"}
```

**Ошибка валидации FastAPI/Pydantic** (`422` для тела, пути и заголовков) —
массив с `type`, `loc`, `msg`, `input`:

```json
{
  "detail": [
    {
      "type": "greater_than",
      "loc": ["body", "amount"],
      "msg": "Input should be greater than 0",
      "input": "0",
      "ctx": {"gt": 0}
    }
  ]
}
```

**Внутренняя ошибка** (`500`) не имеет гарантированного JSON-контракта:
необработанное исключение отдаёт стандартный текстовый ответ
`Internal Server Error`. Не парсите `500` как JSON.

## 4. `GET /health`

Liveness-проверка API.

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"

curl -i -H "X-API-Key: $API_KEY" http://localhost:8000/health
```

Успех:

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{"status": "ok"}
```

| Код | Причина |
|---|---|
| `200` | API отвечает |
| `401` | ключ отсутствует или неверен |
| `405` | использован не `GET` |

`/health` — только liveness. Он **не проверяет PostgreSQL и RabbitMQ**:
API может отвечать `200`, даже если БД недоступна, а `POST /payments` в этот
момент вернёт `500`. Для полноценной проверки используйте
[preflight.md](preflight.md).

## 5. Создание платежа

### 5.1. Запрос

```http
POST /api/v1/payments HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-API-Key: <API_KEY>
Idempotency-Key: order-123-attempt-1
```

Заголовки:

| Заголовок | Обязателен | Ограничения | Описание |
|---|---|---|---|
| `X-API-Key` | да | не пустой | статический ключ |
| `Idempotency-Key` | да | 1–255 символов | уникальный ключ запроса; пробелы по краям обрезаются |
| `Content-Type` | да | `application/json` | формат тела |

`Idempotency-Key`, состоящий только из пробелов, отклоняется с `422` и
строковым `detail: "Idempotency-Key must not be blank"`.

Тело:

| Поле | Тип | Обязательно | Ограничения |
|---|---|---|---|
| `amount` | decimal | да | `> 0`, максимум 18 цифр всего и 2 знака после запятой (то есть до 16 цифр в целой части); конечное число |
| `currency` | enum | да | строго `RUB`, `USD` или `EUR` в верхнем регистре |
| `description` | string | да | 1–255 символов, не тримится |
| `metadata` | object | нет | произвольный JSON-объект, по умолчанию `{}`; `null` и массивы не принимаются |
| `webhook_url` | URL | да | только `http://` или `https://`, не длиннее 2048; должен быть доступен **из контейнера consumer** |

Лишние поля запрещены (`extra="forbid"`) — опечатка в имени поля даёт `422`,
а не игнорируется.

Пример:

```json
{
  "amount": "1500.00",
  "currency": "RUB",
  "description": "Оплата заказа 123",
  "metadata": {"order_id": 123, "customer_id": "c-42"},
  "webhook_url": "http://host.docker.internal:9000/webhook"
}
```

`amount` рекомендуется передавать строкой. JSON-число с плавающей точкой
может потерять точность ещё до валидации; строковое представление
`"1500.00"` однозначно.

### 5.2. Успешный ответ `202 Accepted`

```http
HTTP/1.1 202 Accepted
Content-Type: application/json
Location: /api/v1/payments/b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0
```

```json
{
  "payment_id": "b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0",
  "status": "pending",
  "created_at": "2026-10-26T18:00:00.123456Z"
}
```

`202` означает «принято в обработку», а не «платёж проведён». Финальный
статус появляется позже.

Заголовки ответа:

| Заголовок | Когда | Значение |
|---|---|---|
| `Location` | всегда | `/api/v1/payments/<payment_id>` |
| `Idempotency-Replayed` | только при повторе | `true` |

При повторном запросе с тем же ключом и тем же телом возвращается `202` с тем
же `payment_id` и заголовком `Idempotency-Replayed: true`; новый платёж и
новое outbox-событие не создаются. Поле `status` в ответе на повтор — **текущий
статус платежа**, поэтому там может быть уже `succeeded` или `failed`, а не
`pending`.

### 5.3. Коды ответов

| Код | Причина | Пример `detail` |
|---|---|---|
| `202` | платёж создан или повторён | — |
| `401` | отсутствует/неверен `X-API-Key` | `Invalid or missing API key` |
| `409` | `Idempotency-Key` уже использован с другим телом | `Idempotency-Key was already used with a different payload` |
| `422` | неверное тело, заголовок или отсутствует `Idempotency-Key` | массив ошибок Pydantic или строка для пустого ключа |
| `500` | ошибка БД/конфигурации | без гарантированного JSON |
| `405` | использован не `POST` | `Method Not Allowed` |

Пример `409`:

```json
{"detail": "Idempotency-Key was already used with a different payload"}
```

Пример `422` для отсутствующего заголовка:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["header", "Idempotency-Key"],
      "msg": "Field required",
      "input": null
    }
  ]
}
```

Пример `422` для суммы:

```json
{
  "detail": [
    {
      "type": "greater_than",
      "loc": ["body", "amount"],
      "msg": "Input should be greater than 0",
      "input": "0",
      "ctx": {"gt": 0}
    }
  ]
}
```

Если одновременно отсутствует ключ и некорректно тело, возвращается `401`:
аутентификация проверяется раньше валидации тела.

### 5.4. Пример с curl

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
export API_URL="http://localhost:8000"
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

## 6. Получение платежа

### 6.1. Запрос

```http
GET /api/v1/payments/{payment_id} HTTP/1.1
X-API-Key: <API_KEY>
```

`payment_id` — UUID. Регистр не важен, но формат должен быть UUID; строка
`not-a-uuid` даёт `422`, а валидный, но несуществующий UUID — `404`.

### 6.2. Успешный ответ `200 OK`

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

Поля:

| Поле | Тип | Описание |
|---|---|---|
| `payment_id` | UUID | идентификатор платежа |
| `amount` | string (decimal) | сумма с 2 знаками; в JSON строка, не число |
| `currency` | enum | `RUB`, `USD`, `EUR` |
| `description` | string | назначение платежа |
| `metadata` | object | метаданные, переданные при создании |
| `status` | enum | `pending`, `succeeded`, `failed` |
| `idempotency_key` | string | ключ, с которым создан платёж |
| `webhook_url` | string | нормализованный URL уведомления |
| `webhook_delivered` | boolean | был ли webhook принят получателем (`2xx`) |
| `webhook_attempts` | integer | число выполненных HTTP-попыток webhook |
| `failure_reason` | string \| null | причина отказа; `null` при успехе и в `pending` |
| `created_at` | datetime | время создания, UTC |
| `processed_at` | datetime \| null | время завершения обработки; `null` пока `pending` |

Особенности:

- `amount` в JSON — строка (`"1500.00"`); не используйте `float` для денег;
- `webhook_attempts` растёт и после финального статуса, пока идут retry;
  `webhook_delivered=false` при этом означает, что уведомление ещё не принято;
- `failure_reason` для эмуляции отказа шлюза равен
  `simulated_gateway_decline`; при окончательном сбое обработки туда
  записывается строка вида `ИмяИсключения: текст` (обрезается до 2000
  символов);
- `processed_at` заполняется одновременно с `succeeded`/`failed`;
- ответ содержит `idempotency_key` и `webhook_url` — ограничивайте доступ к
  API.

### 6.3. Коды ответов

| Код | Причина |
|---|---|
| `200` | платёж найден |
| `401` | отсутствует/неверен `X-API-Key` |
| `404` | платежа с таким UUID нет |
| `422` | `payment_id` не является UUID |
| `500` | ошибка БД |

## 7. Статусы платежа

| Статус | Значение |
|---|---|
| `pending` | платёж создан, событие ещё не обработано consumer'ом |
| `succeeded` | эмуляция шлюза завершилась успешно |
| `failed` | шлюз отклонил платёж (10% по умолчанию) или обработка окончательно не удалась |

Переходы:

```text
pending --(consumer: успех)--> succeeded
pending --(consumer: отказ)--> failed
pending --(исчерпаны попытки, платёж всё ещё pending)--> failed
```

Обратного перехода нет. Повторная доставка сообщения не меняет финальный
статус: consumer обрабатывает шлюз только для `pending` и затем повторяет
только webhook. Отдельного эндпоинта для смены статуса нет.

## 8. Идемпотентность

### 8.1. Правила

- `Idempotency-Key` обязателен для `POST` и уникален глобально.
- Ограничение длины `1–255` проверяется до обрезки пробелов; затем ключ
  обрезается. Ключ хранится в БД бессрочно: **один ключ — один платёж**. Для
  нового платежа нужен новый ключ, даже если предыдущий завершился `failed`.
- Пробелы по краям ключа обрезаются; ключ `" key "` эквивалентен `"key"`.
- Повтор с тем же ключом и тем же телом возвращает существующий платёж,
  `202` и `Idempotency-Replayed: true`. Outbox-событие и webhook повторно не
  создаются.
- Повтор с тем же ключом и другим телом возвращает `409 Conflict`.
- Срок жизни идемпотентности не задан; очистки старых ключей нет.

### 8.2. Что считается «тем же телом»

Сравниваются:

| Поле | Правило сравнения |
|---|---|
| `amount` | числовое равенство (`1500`, `1500.0`, `1500.00` — одно и то же) |
| `currency` | точное значение enum |
| `description` | точное строковое равенство (пробелы и регистр важны) |
| `metadata` | равенство Python-объектов: порядок ключей не важен |
| `webhook_url` | равенство нормализованных строк |

Нормализация URL выполняется Pydantic `HttpUrl`: схема и хост приводятся к
нижнему регистру, пустой путь становится `/`. Поэтому
`https://example.com` и `https://example.com/` считаются одним URL, а
`/hook` и `/hook/` — разными.

Тонкости сравнения `metadata`: значения сравниваются как Python-объекты,
поэтому `1` и `1.0` равны, `true` и `1` тоже равны, а строка `"1"` и число
`1` — нет. Если важна строгая различимость типов, не полагайтесь на
идемпотентность для такой проверки.

### 8.3. Конкурентные запросы

Идемпотентность обеспечена уникальным индексом БД, а не только
предварительным `SELECT`:

- два одновременных запроса с одинаковым ключом и одинаковым телом: оба
  могут получить `202` с одним `payment_id`; в БД будет одна строка и одно
  событие; заголовок `Idempotency-Replayed` может быть только у одного
  ответа;
- два одновременных запроса с одинаковым ключом и разным телом: один создаст
  платёж (`202`), второй получит `409`;
- проигравший гонку запрос откатывает транзакцию, перечитывает платёж и
  возвращает replay либо `409`.

### 8.4. Пример повтора и конфликта

Повтор с тем же телом:

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

Ожидается `202`, тот же `payment_id`, `Idempotency-Replayed: true`.

Тот же ключ с другой суммой:

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

Ожидается `409 Conflict`.

## 9. Webhook

### 9.1. Когда отправляется

Webhook отправляется consumer'ом после того, как финальный статус платежа
зафиксирован в БД. Уведомление отправляется и для `payment.succeeded`, и для
`payment.failed` — отказ шлюза тоже является результатом, о котором нужно
сообщить.

### 9.2. Тело

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

| Поле | Описание |
|---|---|
| `event_id` | стабильный ID уведомления: `<payment_id>:<status>` |
| `event` | `payment.succeeded` или `payment.failed` |
| `payment_id` | UUID платежа |
| `status` | `succeeded` или `failed` |
| `amount` | сумма строкой с 2 знаками |
| `currency` | валюта |
| `description` | назначение платежа |
| `metadata` | метаданные платежа |
| `failure_reason` | причина отказа или `null` |
| `created_at` | время создания, ISO 8601 с `+00:00` |
| `processed_at` | время обработки, ISO 8601 с `+00:00` |

### 9.3. Заголовки

| Заголовок | Значение |
|---|---|
| `Content-Type` | `application/json` |
| `User-Agent` | `payment-service/1.0` |
| `X-Event-Type` | `payment.succeeded` или `payment.failed` |
| `X-Payment-Id` | UUID платежа |
| `X-Webhook-Id` | стабильный ID, равен `event_id` |

### 9.4. Ответ получателя

- любой `2xx` — успех, `webhook_delivered=true`;
- `3xx` — ошибка: httpx не следует редиректам по умолчанию;
- `4xx`/`5xx` — ошибка;
- таймаут или сетевая ошибка — ошибка.

Таймаут запроса — `WEBHOOK_TIMEOUT_SECONDS` (по умолчанию 10 секунд).
Тело ответа получателя не анализируется: важен только статус.

### 9.5. Повторы и DLQ

По умолчанию `MAX_PROCESSING_ATTEMPTS=3` — первая попытка и два повтора:

| Попытка | Задержка перед попыткой | Механизм |
|---|---|---|
| 1 | — | сразу из `payments.new` |
| 2 | 1 секунда | `payments.retry.2` |
| 3 | 2 секунды | `payments.retry.3` |

После третьей неудачи сообщение уходит в `payments.new.dlq` и автоматически
не вычитывается. Платёж при этом остаётся в финальном статусе, а
`webhook_delivered=false`, `webhook_attempts=3`.

Если сообщение оказалось в DLQ, после исправления получателя его можно
переопубликовать: тело сообщения — в `payments.exchange` с routing key
`payment.created` и заголовком `x-retry-count: 1`. Если платёж уже имеет
финальный статус, повторно выполнится только webhook; если `pending` —
полная обработка.

### 9.6. Гарантии и дедупликация

Доставка **at-least-once**: один и тот же webhook может прийти повторно,
например если consumer упал после получения `2xx`, но до коммита
`webhook_delivered=true`, или если получатель обработал запрос, но ответ
потерялся по таймауту. Получатель должен быть идемпотентным и
дедуплицировать по `X-Webhook-Id` / `event_id`.

HMAC-подписи нет. В production стоит добавить подпись и ротацию секрета либо
доверять только сети/TLS и allowlist'у адресов.

## 10. Корнеркейсы

### 10.1. Запрос и валидация

| Ситуация | Поведение |
|---|---|
| Нет `X-API-Key` или ключ неверен | `401`, тело не валидируется |
| Нет `Idempotency-Key` | `422`, `loc=["header","Idempotency-Key"]` |
| `Idempotency-Key` из пробелов | `422`, строковый `detail` (не массив) |
| `Idempotency-Key` длиннее 255 символов | `422` |
| `amount = 0`, отрицательная сумма, `10.999`, `NaN`, `Infinity` | `422` |
| В `amount` больше 16 цифр в целой части | `422` (`decimal_whole_digits`) |
| `amount` передан JSON-числом с плавающей точкой | Может быть округлён; используйте строку |
| `currency = "rub"` или неподдерживаемая валюта | `422` |
| `description` пустой или длиннее 255 | `422` |
| `description` из одних пробелов | Формально проходит `min_length`; не полагайтесь на это |
| Лишнее/опечатанное поле в теле | `422` (`extra_forbidden`) |
| `metadata: null`, массив или скаляр | `422`; нужен JSON-объект |
| `webhook_url` со схемой `ftp://`, относительный URL | `422` |
| `webhook_url` длиннее 2048 | `422` |
| `webhook_url = http://localhost:...` при consumer в Docker | URL валиден, но `localhost` указывает на контейнер; webhook не доставится |
| Некорректный JSON | `422` |
| Неверный `payment_id` в пути | `422` (`uuid_parsing`) |
| Неизвестный путь / неверный метод | `404` / `405` |

### 10.2. Идемпотентность

| Ситуация | Поведение |
|---|---|
| Тот же ключ и тело, платёж ещё `pending` | `202`, тот же `payment_id`, `Idempotency-Replayed: true` |
| Тот же ключ и тело, платёж уже завершён | `202`, тот же `payment_id`, в ответе финальный статус |
| Тот же ключ, другое тело | `409`, новый платёж не создаётся |
| `https://example.com` и `https://example.com/` | Считаются одним URL — replay |
| `"1500"` и `"1500.00"` в `amount` | Считаются одной суммой — replay |
| `metadata` с другим порядком ключей | Считается тем же телом — replay |
| `metadata` `1` и `"1"` | Разные значения — `409` |
| Одновременные запросы с одним ключом и телом | Все могут получить `202` с одним `payment_id`; строка и outbox — одна |
| Одновременные запросы с одним ключом и разным телом | Один `202`, остальные `409` |
| Повтор после `failed` | Replay/`409`; повторной обработки и нового webhook нет |

### 10.3. Инфраструктура и доставка

| Ситуация | Поведение |
|---|---|
| PostgreSQL недоступен | `POST`/`GET` возвращают `500`; `/health` при этом может отвечать `200` |
| RabbitMQ недоступен | `POST` всё равно `202`: платёж и outbox уже в PostgreSQL; outbox ретраит публикацию с backoff |
| Consumer остановлен | Платёж остаётся `pending`; outbox может быть `published`, сообщение ждёт в `payments.new` |
| Webhook отвечает `3xx` | Считается ошибкой, уходит в retry |
| Webhook отвечает `204` | Успех (`2xx`) |
| Webhook не отвечает 10 секунд | Таймаут, retry |
| Получатель обработал запрос, но ответ потерян | Повторная доставка; дедупликация по `X-Webhook-Id` |
| Consumer упал после `2xx`, но до коммита | Повторная доставка (at-least-once) |
| Исчерпаны попытки webhook | Сообщение в `payments.new.dlq`; автоматического повтора нет |
| Платёж «завис» в `pending` после DLQ | Consumer пытается пометить его `failed`; проверьте `failure_reason` |
| Публикация outbox не удалась | Событие остаётся `pending`, `attempts` растёт, `last_error` заполнен |
| Outbox долго `pending` | Проверьте consumer, RabbitMQ и `last_error`; максимум backoff — 60 секунд |

### 10.4. Прочее

| Ситуация | Поведение |
|---|---|
| Браузерный запрос с другого origin | CORS не настроен; используйте серверный вызов или reverse proxy |
| Большой запрос | Лимита в приложении нет; `413` может вернуть reverse proxy |
| Rate limit | Не реализован |
| `GET /docs`, `/openapi.json` | Доступны без `X-API-Key` |
| `500` | Тело не гарантирует JSON; смотрите логи API |
| Пагинация/список платежей | Не реализованы |

## 11. Проверка сценариев в Swagger UI

Swagger UI отдаётся самим API (`/docs`), поэтому браузерные запросы идут на тот
же origin — CORS не мешает. Сама страница `/docs` не защищена ключом: без
`X-API-Key` запросы будут получать `401`, но открыть и изучить схемы можно.

Ниже — порядок действий для ручной проверки в браузере. Для сценариев с
webhook понадобится терминал: приёмник webhook и просмотр его логов.

### 11.1. Подготовка

1. Убедитесь, что стек запущен:

   ```bash
   docker compose ps -a
   curl -fsS -H "X-API-Key: $(sed -n 's/^API_KEY=//p' .env)" \
     http://localhost:8000/health
   ```

2. Для happy path и retry запустите приёмник webhook в отдельном терминале:

   ```bash
   python3 scripts/webhook_receiver.py --port 9000
   ```

   Для consumer в Docker URL приёмника —
   `http://host.docker.internal:9000/webhook`. Если worker запущен локально,
   используйте `http://localhost:9000/webhook`.

3. Откройте Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs).

4. Нажмите **Authorize** (замок в правом верхнем углу).

5. В поле значения вставьте `API_KEY` из `.env` — **без** префикса `Bearer`.
   Нажмите **Authorize**, затем **Close**.

6. Убедитесь, что у защищённых эндпоинтов появился значок замка. Теперь
   Swagger UI автоматически добавляет `X-API-Key` в запросы.

Подготовьте два-три уникальных `Idempotency-Key`, например
`swagger-20261027-01`, `swagger-20261027-02`. Для нового платежа всегда
используйте новый ключ.

### 11.2. Сценарий A: health и аутентификация

1. Разверните `GET /health`.
2. Нажмите **Try it out**.
3. Нажмите **Execute**.
4. Ожидается `200` и тело `{"status":"ok"}`.

Проверка `401`:

1. Нажмите **Authorize** → **Logout** → **Close**.
2. Снова выполните `GET /health`.
3. Ожидается `401` и тело `{"detail":"Invalid or missing API key"}`.
4. Верните ключ через **Authorize**, чтобы продолжить сценарии.

### 11.3. Сценарий B: создание платежа

1. Разверните `POST /api/v1/payments`.
2. Нажмите **Try it out**.
3. Заполните обязательный заголовок `Idempotency-Key`, например
   `swagger-20261027-01`.
4. В поле Request body подставьте:

   ```json
   {
     "amount": "1500.00",
     "currency": "RUB",
     "description": "Swagger happy path",
     "metadata": {"source": "swagger", "run": "01"},
     "webhook_url": "http://host.docker.internal:9000/webhook"
   }
   ```

5. Нажмите **Execute**.
6. Ожидается `202 Accepted` и тело:

   ```json
   {
     "payment_id": "b7f9a5a1-4a4d-4fd8-a9c2-8f7c3db8f7d0",
     "status": "pending",
     "created_at": "2026-10-26T18:00:00.123456Z"
   }
   ```

7. Скопируйте `payment_id` — он понадобится для `GET`.
8. В ответе также будет заголовок `Location`. Если он не виден в блоке
   ответа, откройте DevTools → **Network** → запрос `payments` →
   **Response Headers** (см. раздел 11.12).

Если приёмник не запущен, платёж всё равно создастся, но webhook уйдёт в
retry и затем в DLQ. Для чистого happy path приёмник должен работать.

### 11.4. Сценарий C: получение и ожидание результата

1. Разверните `GET /api/v1/payments/{payment_id}`.
2. Нажмите **Try it out**.
3. Вставьте `payment_id` из предыдущего шага.
4. Нажмите **Execute**. Сразу после создания ожидается `status=pending`,
   `processed_at=null`.
5. Подождите 2–5 секунд и нажмите **Execute** ещё раз (Swagger не обновляет
   ответ автоматически).
6. Повторяйте, пока статус не станет `succeeded` или `failed`.

Что проверить в ответе:

- `status` — `succeeded` или `failed` (10% отказов — ожидаемая эмуляция);
- `processed_at` заполнен;
- `webhook_delivered=true`;
- `webhook_attempts >= 1` (обычно `1`);
- `failure_reason=null` при успехе или `simulated_gateway_decline` при отказе.

`webhook_delivered` может стать `true` на мгновение позже, чем финальный
статус. Если статус уже финальный, а флаг ещё `false`, нажмите **Execute**
ещё раз через секунду — это нормальная асинхронность.

### 11.5. Сценарий D: идемпотентный повтор

1. Вернитесь к `POST /api/v1/payments` → **Try it out**.
2. Оставьте **тот же** `Idempotency-Key`, что в сценарии B.
3. Восстановите **точно то же** тело запроса (сумма, валюта, описание,
   metadata, webhook_url).
4. Нажмите **Execute**.
5. Ожидается `202` и **тот же** `payment_id`.
6. Проверьте заголовок `Idempotency-Replayed: true`. Если Swagger его не
   показывает, смотрите DevTools → Network (раздел 11.12).

Поле `status` в ответе на повтор — текущий статус платежа. Если платёж уже
обработан, там будет `succeeded`/`failed`, а не `pending`; это нормально.
Новый платёж, новое outbox-событие и повторный webhook не создаются.

### 11.6. Сценарий E: конфликт идемпотентности

1. В том же `POST` оставьте тот же `Idempotency-Key`.
2. Измените только `amount`, например на `"999.00"`.
3. Нажмите **Execute**.
4. Ожидается `409 Conflict`:

   ```json
   {"detail": "Idempotency-Key was already used with a different payload"}
   ```

5. Убедитесь, что новый `payment_id` не появился. Для нового платежа нужен
   новый ключ.

### 11.7. Сценарий F: ошибки валидации

Для каждого случая удобно начинать с рабочего тела из сценария B, менять одно
поле и использовать **новый** `Idempotency-Key`. Некорректное тело не создаёт
платёж, но новый ключ избавляет от случайного `409`.

| Что изменить в Swagger | Ожидание |
|---|---|
| `amount: "0"` | `422`, `loc=["body","amount"]` |
| `amount: "-1.00"` | `422` |
| `amount: "10.999"` | `422` (больше 2 знаков после запятой) |
| `currency: "GBP"` или `"rub"` | `422` |
| `description: ""` | `422` |
| `metadata: null` | `422` |
| добавить лишнее поле `"unexpected": "x"` | `422` (`extra_forbidden`) |
| `webhook_url: "ftp://example.com/hook"` | `422` |
| очистить `Idempotency-Key` | `422` |
| нажать **Authorize → Logout** и выполнить запрос | `401` |

Для пустого `Idempotency-Key` Swagger UI может не отправить пустой заголовок.
Введите один пробел — API обрежет его и вернёт `422` со строковым
`detail: "Idempotency-Key must not be blank"`. Отсутствие заголовка даёт
`422` с массивом ошибок; если UI не позволяет отправить запрос без него,
проверьте этот случай через `curl`.

Ожидаемый формат `422`:

```json
{
  "detail": [
    {
      "type": "greater_than",
      "loc": ["body", "amount"],
      "msg": "Input should be greater than 0",
      "input": "0",
      "ctx": {"gt": 0}
    }
  ]
}
```

### 11.8. Сценарий G: ошибки GET

1. `GET /api/v1/payments/{payment_id}` с валидным, но несуществующим UUID:
   `00000000-0000-0000-0000-000000000000` → `404`,
   `{"detail":"Payment not found"}`.
2. Строка `not-a-uuid` в качестве `payment_id` → `422` (`uuid_parsing`).
3. **Authorize → Logout** и повторный GET → `401`.

### 11.9. Сценарий H: retry webhook (500, 500, 200)

1. Остановите приёмник (Ctrl+C) и запустите его заново, чтобы сбросить
   счётчик:

   ```bash
   python3 scripts/webhook_receiver.py --port 9000 --fail-first 2
   ```

2. В Swagger создайте новый платёж с новым ключом и тем же
   `webhook_url`, что в сценарии B.
3. В `GET` повторяйте **Execute**, пока `webhook_delivered` не станет `true`
   (обычно 5–15 секунд).
4. Ожидается `webhook_attempts=3`, `webhook_delivered=true`, финальный статус.
5. В терминале приёмника должны быть строки:

   ```text
   delivery=1 ... status=500 ...
   delivery=2 ... status=500 ...
   delivery=3 ... status=200 ...
   ```

   У всех трёх доставок одинаковый `X-Webhook-Id`.

6. В RabbitMQ UI (`http://localhost:15672`) → **Queues** проверьте, что
   `payments.retry.2` и `payments.retry.3` пусты.
7. Верните приёмник в обычный режим (перезапустите без `--fail-first`),
   прежде чем продолжать.

Если третья попытка тоже неуспешна, сообщение уйдёт в DLQ, а
`webhook_delivered` останется `false` — это сценарий I.

### 11.10. Сценарий I: Dead Letter Queue

1. Убедитесь, что приёмник webhook на `9000` не нужен для этого URL.
2. В Swagger создайте платёж с новым ключом и заведомо недоступным адресом:

   ```json
   {
     "amount": "77.77",
     "currency": "EUR",
     "description": "Swagger DLQ",
     "metadata": {"source": "swagger", "run": "dlq"},
     "webhook_url": "http://host.docker.internal:9999/unreachable"
   }
   ```

3. Через `GET` дождитесь `webhook_attempts=3` (может занять до минуты, если
   соединение висит до таймаута). Ожидается `webhook_delivered=false`,
   финальный статус и заполненный `processed_at`.
4. Откройте RabbitMQ Management UI:
   [http://localhost:15672](http://localhost:15672), войдите под
   `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS`.
5. **Queues** → `payments.new.dlq` → **Get messages**. Выберите
   `Nack message requeue true`, чтобы прочитать сообщение, но не удалить его.
6. В payload будет событие `payment.created` с вашим `payment_id`.
7. Если в DLQ уже были сообщения прошлых прогонов, ориентируйтесь на прирост
   `messages_ready`, а не на абсолютное число.
8. Очищать DLQ можно только в тестовом окружении и после фиксации результата
   (кнопка **Purge** или `rabbitmqctl purge_queue`).

### 11.11. Дополнительно: outbox при остановленном RabbitMQ

Этот сценарий уже не совсем «только Swagger»: брокер нужно останавливать
терминалом.

1. В терминале: `docker compose stop rabbitmq`.
2. В Swagger создайте платёж с новым ключом. API всё равно ответит `202`, так
   как пишет в PostgreSQL.
3. В `GET` платёж останется `pending`.
4. В терминале: `docker compose start rabbitmq`.
5. Повторяйте `GET` до 90 секунд: outbox повторяет публикацию с backoff,
   поэтому восстановление может занять до `OUTBOX_MAX_BACKOFF_SECONDS`.
6. После восстановления статус станет `succeeded`/`failed`, а
   `webhook_delivered` — `true`, если приёмник запущен.

Состояние outbox и очередей в Swagger не отображается: проверяйте его в
RabbitMQ UI, Dozzle или SQL. Подробный сценарий — E2E-09 в
[e2e-testing.md](e2e-testing.md).

### 11.12. Что видно в Swagger, а что нет

Swagger UI показывает:

- HTTP-код и тело ответа;
- схему запроса и обязательные параметры;
- иногда — заголовки ответа в блоке **Response headers**.

Swagger UI **не показывает**:

- асинхронную обработку и webhook-доставку — только через `GET` и лог
  приёмника;
- состояние outbox, таблиц и очередей — через SQL, RabbitMQ UI, Dozzle;
- retry-очереди и DLQ — через RabbitMQ UI;
- причину `500` — через логи API.

Если `Location` или `Idempotency-Replayed` не видны:

1. Откройте DevTools браузера (F12) → **Network**.
2. Выполните запрос в Swagger.
3. Выберите запрос `payments`.
4. Откройте **Headers** → **Response Headers**.
5. Найдите `location` и `idempotency-replayed` (регистр не важен).

Особенности:

- Swagger UI и API на одном origin, поэтому CORS не мешает;
- `/docs` не защищён ключом — не используйте production-ключ в общем браузере;
- Swagger UI не является нагрузочным инструментом; гонки надёжнее проверять по
  E2E-10;
- после **Logout** все последующие запросы будут `401`, пока не выполните
  **Authorize** снова.

### 11.13. Чек-лист проверки в Swagger

- [ ] Без ключа `/health` возвращает `401`.
- [ ] С ключом `/health` возвращает `200` и `{"status":"ok"}`.
- [ ] `POST /payments` возвращает `202` и `payment_id`.
- [ ] `GET /payments/{id}` показывает `pending`, затем `succeeded`/`failed`.
- [ ] Повтор с тем же ключом и телом — тот же `payment_id` и
      `Idempotency-Replayed: true`.
- [ ] Тот же ключ с другим телом — `409`.
- [ ] Некорректные поля — `422`; запрос без ключа — `401`.
- [ ] Неизвестный UUID — `404`, невалидный — `422`.
- [ ] Retry: `webhook_attempts=3`, `webhook_delivered=true`.
- [ ] DLQ: `webhook_attempts=3`, `webhook_delivered=false`, сообщение в
      `payments.new.dlq`.
- [ ] После сценариев выполнен **Logout**; вкладка с тестовым ключом закрыта.

Swagger UI удобен для проверки контракта и ручных сценариев, но не заменяет
сквозной прогон: асинхронную обработку, webhook, retry и DLQ проверяйте по
[e2e-testing.md](e2e-testing.md).
