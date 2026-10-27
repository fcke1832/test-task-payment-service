# E2E-тестирование

## 1. Цель и границы

E2E-проверка подтверждает, что весь путь работает целиком: HTTP API →
PostgreSQL → outbox relay → RabbitMQ → consumer → webhook-получатель, включая
идемпотентность, retry, DLQ и восстановление после падения RabbitMQ.

Что проверяется:

- коды и тела ответов API на успешные и ошибочные запросы;
- идемпотентность повторного `POST`;
- фактическая запись платежа и outbox в PostgreSQL;
- публикация события и обработка его consumer'ом;
- доставка webhook, повторы при ошибках и попадание в DLQ;
- сохранность платежа при недоступном RabbitMQ;
- отсутствие дублей при конкурентных запросах.

Что **не** покрывается:

- unit-тесты схем, retry-заголовков и payload — они запускаются отдельно
  (`make check`, 13 тестов);
- нагрузочное и security-тестирование;
- проверка реального платёжного шлюза (он эмулируется).

## 2. Предусловия

1. Выполнена [предзапусковая подготовка](preflight.md): стек запущен,
   миграции применены, `api` healthy, `consumer` работает.
2. Порты `8000`, `9000`, `15672` свободны/доступны.
3. Есть `curl` и `python3`.
4. Для webhook-приёмника используется `scripts/webhook_receiver.py`.
5. Желательно чистое окружение. Если в БД и очередях есть данные прошлых
   прогонов, фиксируйте baseline и проверяйте дельту, а не абсолютные числа.

Проверка готовности:

```bash
docker compose ps -a

export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
curl -fsS -H "X-API-Key: $API_KEY" http://localhost:8000/health
```

Ожидается `{"status":"ok"}` и `api` в состоянии `healthy`.

## 3. Подготовка окружения

### 3.1. Переменные

```bash
export API_KEY="$(sed -n 's/^API_KEY=//p' .env)"
export API_URL="http://localhost:8000"
export RUN_ID="$(date +%Y%m%d-%H%M%S)"
export WEBHOOK_URL="http://host.docker.internal:9000/webhook"

echo "RUN_ID=$RUN_ID"
echo "WEBHOOK_URL=$WEBHOOK_URL"
```

`host.docker.internal` нужен, потому что consumer работает в контейнере, а
приёмник — на хосте. Если consumer запущен локально (без Docker), используйте
`http://localhost:9000/webhook`.

### 3.2. Webhook-приёмник

В отдельном терминале:

```bash
python3 scripts/webhook_receiver.py --port 9000
```

Приёмник печатает каждую доставку: номер, путь, `X-Webhook-Id`,
`X-Event-Type`, статус ответа и payload. Возвращает `200 OK`.

Для сценария retry его нужно перезапустить с флагом:

```bash
python3 scripts/webhook_receiver.py --port 9000 --fail-first 2
```

Счётчик доставок сбрасывается при каждом перезапуске приёмника — это важно
для воспроизводимости.

### 3.3. Детерминированность статуса (опционально)

По умолчанию 10% платежей завершаются `failed` — это эмуляция отказа шлюза.
Для E2E это нормально: webhook должен прийти в обоих случаях. Если нужно
гарантированно получить `succeeded`, временно задайте в `.env`:

```dotenv
PAYMENT_SUCCESS_RATE=1.0
```

и пересоздайте consumer:

```bash
docker compose up -d --force-recreate consumer
```

После прогона верните `0.9` и снова пересоздайте consumer. Не используйте
`1.0` в production.

### 3.4. Правила тестовых данных

- Каждый сценарий использует свой `Idempotency-Key` с префиксом `RUN_ID`.
- Не переиспользуйте ключ между сценариями: ключ уникален навсегда.
- Для проверок «нет нового платежа» сравнивайте количество строк до и после.
- После сценария DLQ фиксируйте baseline очереди: в ней могут быть сообщения
  прошлых прогонов.

## 4. Порядок выполнения

Сценарии выполняются последовательно в одном терминале: переменные `RUN_ID`,
`API_KEY`, `IDEM_*` и `PAYMENT_ID_*` переиспользуются между шагами. Порядок
выбран так, чтобы сначала пройти read-only проверки и happy path, затем
идемпотентность и валидацию, а разрушающие инфраструктурные проверки — в
конце.

| # | Сценарий | Создаёт данные | Зависимости |
|---|---|---|---|
| E2E-01 | Health и аутентификация | нет | — |
| E2E-02 | Успешный платёж, webhook, БД, outbox | платёж | приёмник на `9000` |
| E2E-03 | Идемпотентный повтор | нет (replay) | E2E-02 |
| E2E-04 | Конфликт идемпотентности | нет | E2E-02 |
| E2E-05 | Валидация запросов | нет | — |
| E2E-06 | Ошибки `GET` | нет | — |
| E2E-07 | Retry webhook (500, 500, 200) | платёж | приёмник с `--fail-first 2` |
| E2E-08 | DLQ при недоступном webhook | платёж + сообщение DLQ | — |
| E2E-09 | Outbox при остановленном RabbitMQ | платёж | перезапуск RabbitMQ |
| E2E-10 | Конкурентные запросы (опционально) | платёж | — |

Ориентировочное время: 5–10 минут, из них E2E-09 — до 90 секунд ожидания
восстановления.

## 5. Сценарии

### E2E-01. Health и аутентификация

Цель: проверить, что `/health` защищён ключом и API доступен.

```bash
curl -s -o /dev/null -w 'no_key=%{http_code}\n' "$API_URL/health"
curl -s -o /dev/null -w 'bad_key=%{http_code}\n' \
  -H "X-API-Key: definitely-wrong" "$API_URL/health"
curl -sS -H "X-API-Key: $API_KEY" "$API_URL/health"; echo
```

Ожидается:

```text
no_key=401
bad_key=401
{"status":"ok"}
```

Побочных эффектов нет.

### E2E-02. Успешный платёж, webhook и запись в БД

Цель: пройти полный happy path и увидеть результат во всех хранилищах.

1. Убедитесь, что приёмник из раздела 3.2 запущен без `--fail-first`.
2. Создайте платёж:

```bash
export IDEM_02="$RUN_ID-e2e-02"

BODY_02='{
  "amount": "1500.00",
  "currency": "RUB",
  "description": "E2E happy path",
  "metadata": {"run_id": "'"$RUN_ID"'", "case": "e2e-02"},
  "webhook_url": "'"$WEBHOOK_URL"'"
}'

CREATE_02=$(curl -sS -D /tmp/e2e_02.headers \
  -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEM_02" \
  -d "$BODY_02")

printf '%s\n' "$CREATE_02" | python3 -m json.tool
head -n 1 /tmp/e2e_02.headers
grep -i '^Location:' /tmp/e2e_02.headers

export PAYMENT_ID_02=$(printf '%s' "$CREATE_02" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
echo "PAYMENT_ID_02=$PAYMENT_ID_02"
```

Ожидается `202 Accepted`, `status=pending`, непустой `payment_id`,
`Location: /api/v1/payments/<payment_id>`.

3. Дождитесь финального статуса (обработка 2–5 секунд + outbox):

```bash
for n in $(seq 1 30); do
  BODY=$(curl -sS -H "X-API-Key: $API_KEY" \
    "$API_URL/api/v1/payments/$PAYMENT_ID_02")
  STATUS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  DELIVERED=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhook_delivered"])')
  printf '[%02d] status=%s webhook_delivered=%s\n' "$n" "$STATUS" "$DELIVERED"
  [ "$STATUS" != "pending" ] && [ "$DELIVERED" = "True" ] && break
  sleep 1
done

printf '%s\n' "$BODY" | python3 -m json.tool
```

4. Проверьте ответ `GET`:

- `status` — `succeeded` или `failed` (10% — ожидаемый отказ);
- `processed_at` заполнен;
- `webhook_delivered=true`;
- `webhook_attempts >= 1` (обычно `1`);
- `failure_reason=null` при `succeeded` и
  `simulated_gateway_decline` при отказе шлюза.

5. Проверьте лог приёмника: должна быть доставка с `status=200`, тем же
   `X-Webhook-Id` и событием `payment.succeeded` или `payment.failed`.

6. Проверьте БД (подставьте значения):

```sql
SELECT id, status, amount, currency, webhook_delivered, webhook_attempts,
       failure_reason, created_at, processed_at
FROM payments
WHERE id = '<PAYMENT_ID_02>';
```

```sql
SELECT status, attempts, last_error, published_at
FROM outbox
WHERE payload->>'payment_id' = '<PAYMENT_ID_02>';
```

Ожидается `outbox.status='published'`, `published_at` заполнен,
`attempts >= 1`.

7. Проверьте очереди:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
```

`payments.new`, `payments.retry.*`, `payments.new.dlq` должны быть пустыми
(`messages_ready=0`), у `payments.new` — один consumer.

### E2E-03. Идемпотентный повтор

Цель: убедиться, что повтор с тем же ключом и телом не создаёт новый платёж,
новое событие и повторный webhook.

1. Зафиксируйте текущее число доставок в логе приёмника (или
   `webhook_attempts` из E2E-02).
2. Повторите тот же POST с `$IDEM_02` и `$BODY_02`:

```bash
curl -sS -D /tmp/e2e_03.headers \
  -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEM_02" \
  -d "$BODY_02" > /tmp/e2e_03.json

head -n 1 /tmp/e2e_03.headers
grep -i '^Idempotency-Replayed:' /tmp/e2e_03.headers
python3 -m json.tool < /tmp/e2e_03.json
```

Ожидается:

- `202 Accepted`;
- `Idempotency-Replayed: true`;
- тот же `payment_id`, что и в E2E-02;
- `status` — текущий (может быть уже `succeeded`/`failed`, это нормально);
- в логе приёмника нет новой доставки;
- `webhook_attempts` не изменился.

3. Проверьте отсутствие дублей:

```sql
SELECT count(*) FROM payments WHERE idempotency_key = '<IDEM_02>';

SELECT count(*) FROM outbox
WHERE payload->>'payment_id' = '<PAYMENT_ID_02>';
```

Ожидается `1` и `1`.

4. Повторите запрос ещё раз — результат должен быть стабильным (тот же
   `payment_id`, `Idempotency-Replayed: true`).

### E2E-04. Конфликт идемпотентности

Цель: тот же ключ с другим телом должен дать `409`, не создавая записей.

```bash
curl -sS -D /tmp/e2e_04.headers -o /tmp/e2e_04.json \
  -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEM_02" \
  -d '{
    "amount": "999.00",
    "currency": "RUB",
    "description": "E2E conflict",
    "metadata": {"run_id": "'"$RUN_ID"'", "case": "e2e-04"},
    "webhook_url": "'"$WEBHOOK_URL"'"
  }'

head -n 1 /tmp/e2e_04.headers
python3 -m json.tool < /tmp/e2e_04.json
```

Ожидается `409 Conflict` и:

```json
{"detail": "Idempotency-Key was already used with a different payload"}
```

Количество платежей по ключу и событий outbox не меняется. Новый webhook не
отправляется.

### E2E-05. Валидация запросов

Цель: проверить, что некорректные данные не доходят до БД.

Базовый пример:

```bash
curl -s -o /tmp/e2e_05.json -w '%{http_code}\n' \
  -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $RUN_ID-e2e-05-amount" \
  -d '{
    "amount": "0",
    "currency": "RUB",
    "description": "Invalid amount",
    "webhook_url": "'"$WEBHOOK_URL"'"
  }'
```

Ожидается `422`; тело содержит массив `detail` с `loc=["body","amount"]`.

Проверьте остальные случаи (каждый — отдельный запрос с уникальным ключом):

| Случай | Ожидание |
|---|---|
| без `X-API-Key` | `401` |
| без `Idempotency-Key` | `422`, `loc=["header","Idempotency-Key"]` |
| `Idempotency-Key: "   "` | `422`, строковый `detail` о пустом ключе |
| `amount: "-1.00"` | `422` |
| `amount: "10.999"` | `422` |
| `currency: "GBP"` или `"rub"` | `422` |
| `description: ""` | `422` |
| лишнее поле `webhook` | `422` |
| `webhook_url: "ftp://example.com/hook"` | `422` |
| `metadata: null` | `422` |
| `payment_id = "not-a-uuid"` в `GET` | `422` |

После серии проверок убедитесь, что число платежей и outbox не выросло:

```sql
SELECT count(*) FROM payments;
SELECT count(*) FROM outbox;
```

Ожидается то же количество, что и до E2E-05.

### E2E-06. Ошибки получения платежа

```bash
curl -s -o /dev/null -w 'unknown=%{http_code}\n' \
  -H "X-API-Key: $API_KEY" \
  "$API_URL/api/v1/payments/00000000-0000-0000-0000-000000000000"

curl -s -o /dev/null -w 'invalid=%{http_code}\n' \
  -H "X-API-Key: $API_KEY" \
  "$API_URL/api/v1/payments/not-a-uuid"

curl -s -o /dev/null -w 'no_key=%{http_code}\n' \
  "$API_URL/api/v1/payments/$PAYMENT_ID_02"
```

Ожидается `unknown=404`, `invalid=422`, `no_key=401`.

### E2E-07. Retry webhook: 500, 500, 200

Цель: проверить TTL-очереди, счётчик попыток и успешную доставку с третьей
попытки.

1. Остановите текущий приёмник (Ctrl+C) и запустите его заново, чтобы
   сбросить счётчик:

```bash
python3 scripts/webhook_receiver.py --port 9000 --fail-first 2
```

2. Создайте новый платёж с новым ключом:

```bash
export IDEM_07="$RUN_ID-e2e-07"

CREATE_07=$(curl -sS -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEM_07" \
  -d '{
    "amount": "250.00",
    "currency": "USD",
    "description": "E2E webhook retry",
    "metadata": {"run_id": "'"$RUN_ID"'", "case": "e2e-07"},
    "webhook_url": "'"$WEBHOOK_URL"'"
  }')

export PAYMENT_ID_07=$(printf '%s' "$CREATE_07" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
echo "PAYMENT_ID_07=$PAYMENT_ID_07"
```

3. Дождитесь трёх попыток и успешной доставки (до ~60 секунд):

```bash
for n in $(seq 1 60); do
  BODY=$(curl -sS -H "X-API-Key: $API_KEY" \
    "$API_URL/api/v1/payments/$PAYMENT_ID_07")
  STATUS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  DELIVERED=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhook_delivered"])')
  ATTEMPTS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhook_attempts"])')
  printf '[%02d] status=%s delivered=%s attempts=%s\n' \
    "$n" "$STATUS" "$DELIVERED" "$ATTEMPTS"
  [ "$DELIVERED" = "True" ] && break
  sleep 1
done

printf '%s\n' "$BODY" | python3 -m json.tool
```

Ожидается:

- `status` — `succeeded` или `failed` (финальный);
- `webhook_delivered=true`;
- `webhook_attempts=3`.

4. В логе приёмника:

```text
delivery=1 ... status=500 ...
delivery=2 ... status=500 ...
delivery=3 ... status=200 ...
```

У всех трёх доставок одинаковый `X-Webhook-Id` и одно и то же событие.

5. Проверьте, что retry-очереди пусты:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
```

`payments.retry.2` и `payments.retry.3` — `messages_ready=0`.

6. Верните приёмник в обычный режим (`--fail-first` без аргумента/с флагом)
   перед следующими сценариями, где webhook должен доставляться сразу.

Примечание: если приёмник ответит ошибкой и на третью попытку, сообщение
уйдёт в DLQ, а `webhook_delivered` останется `false` — это уже сценарий
E2E-08, а не успешный retry.

### E2E-08. Dead Letter Queue

Цель: после исчерпания попыток сообщение должно оказаться в
`payments.new.dlq`, а платёж — не «зависнуть» в `pending`.

1. Зафиксируйте baseline DLQ:

```bash
DLQ_BEFORE=$(docker compose exec -T rabbitmq \
  rabbitmqctl list_queues name messages_ready 2>/dev/null \
  | awk '$1=="payments.new.dlq" {print $2}')
DLQ_BEFORE=${DLQ_BEFORE:-0}
echo "DLQ_BEFORE=$DLQ_BEFORE"
```

2. Создайте платёж с заведомо недоступным webhook (порт `9999`):

```bash
export IDEM_08="$RUN_ID-e2e-08"

CREATE_08=$(curl -sS -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEM_08" \
  -d '{
    "amount": "77.77",
    "currency": "EUR",
    "description": "E2E DLQ",
    "metadata": {"run_id": "'"$RUN_ID"'", "case": "e2e-08"},
    "webhook_url": "http://host.docker.internal:9999/unreachable"
  }')

export PAYMENT_ID_08=$(printf '%s' "$CREATE_08" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
echo "PAYMENT_ID_08=$PAYMENT_ID_08"
```

3. Дождитесь исчерпания попыток (до ~60 секунд; каждая попытка может ждать
   таймаут webhook):

```bash
for n in $(seq 1 60); do
  BODY=$(curl -sS -H "X-API-Key: $API_KEY" \
    "$API_URL/api/v1/payments/$PAYMENT_ID_08")
  STATUS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  DELIVERED=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhook_delivered"])')
  ATTEMPTS=$(printf '%s' "$BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhook_attempts"])')
  printf '[%02d] status=%s delivered=%s attempts=%s\n' \
    "$n" "$STATUS" "$DELIVERED" "$ATTEMPTS"
  [ "$ATTEMPTS" -ge 3 ] && break
  sleep 1
done

printf '%s\n' "$BODY" | python3 -m json.tool
```

Ожидается:

- `status` — финальный (`succeeded`/`failed`), не `pending`;
- `processed_at` заполнен;
- `webhook_delivered=false`;
- `webhook_attempts=3`.

Дайте RabbitMQ пару секунд на маршрутизацию отклонённого сообщения:

```bash
sleep 2
```

4. Проверьте DLQ:

```bash
DLQ_AFTER=$(docker compose exec -T rabbitmq \
  rabbitmqctl list_queues name messages_ready 2>/dev/null \
  | awk '$1=="payments.new.dlq" {print $2}')
DLQ_AFTER=${DLQ_AFTER:-0}
echo "DLQ_BEFORE=$DLQ_BEFORE DLQ_AFTER=$DLQ_AFTER"
```

Ожидается `DLQ_AFTER = DLQ_BEFORE + 1`, у DLQ `consumers=0`.

5. Разберите сообщение в Management UI:
   `Queues` → `payments.new.dlq` → `Get messages`. Выберите
   `Nack message requeue true`, чтобы только прочитать сообщение и не удалить
   его. В payload будет событие `payment.created` с `payment_id`.

6. Если DLQ нужна пустой для следующего прогона, очистите её **после**
   фиксации результата:

```bash
docker compose exec rabbitmq rabbitmqctl purge_queue payments.new.dlq
```

`purge_queue` удаляет все сообщения DLQ, включая чужие. В production так не
делайте без разбора.

### E2E-09. Outbox при недоступном RabbitMQ

Цель: доказать, что API не теряет платёж при падении брокера, а outbox
публикует событие после восстановления.

1. Убедитесь, что приёмник на `9000` запущен в обычном режиме (не
   `--fail-first`), чтобы после восстановления webhook доставился.
2. Остановите RabbitMQ:

```bash
docker compose stop rabbitmq
docker compose ps rabbitmq
```

3. Создайте платёж — API должен продолжить работу:

```bash
export IDEM_09="$RUN_ID-e2e-09"

CREATE_09=$(curl -sS -X POST "$API_URL/api/v1/payments" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $IDEM_09" \
  -d '{
    "amount": "321.00",
    "currency": "RUB",
    "description": "E2E outbox recovery",
    "metadata": {"run_id": "'"$RUN_ID"'", "case": "e2e-09"},
    "webhook_url": "'"$WEBHOOK_URL"'"
  }')

echo "$CREATE_09" | python3 -m json.tool

export PAYMENT_ID_09=$(printf '%s' "$CREATE_09" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["payment_id"])')
```

Ожидается `202` и `payment_id`: API пишет только в PostgreSQL и не зависит от
RabbitMQ.

4. Убедитесь, что платёж `pending`, а outbox не опубликован:

```bash
curl -sS -H "X-API-Key: $API_KEY" \
  "$API_URL/api/v1/payments/$PAYMENT_ID_09" | python3 -m json.tool
```

```sql
SELECT status, attempts, last_error, next_attempt_at
FROM outbox
WHERE payload->>'payment_id' = '<PAYMENT_ID_09>';
```

Через несколько секунд ожидается `status='pending'`, `attempts >= 1`,
заполненный `last_error` (ошибка соединения) и `next_attempt_at` в будущем.

5. Запустите RabbitMQ:

```bash
docker compose start rabbitmq
docker compose ps rabbitmq
```

6. Дождитесь восстановления. Backoff outbox может достигать
   `OUTBOX_MAX_BACKOFF_SECONDS` (60 c), поэтому опрашивайте до 90 секунд:

```bash
for i in $(seq 1 45); do
  OUTBOX_STATUS=$(docker compose exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
     "SELECT status FROM outbox ORDER BY created_at DESC LIMIT 1;"')
  PAYMENT_STATE=$(docker compose exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
     "SELECT status || chr(58) || webhook_delivered::text \
      FROM payments ORDER BY created_at DESC LIMIT 1;"')
  printf '[%02d] outbox=%s payment=%s\n' "$i" "$OUTBOX_STATUS" "$PAYMENT_STATE"

  case "$PAYMENT_STATE" in
    succeeded:true|failed:true)
      [ "$OUTBOX_STATUS" = "published" ] && break
      ;;
  esac
  sleep 2
done
```

Запрос «последняя строка» корректен при последовательном выполнении
сценариев. Если параллельно создавались другие платежи, фильтруйте по
`payment_id`.

7. Проверьте итог:

- `outbox.status='published'`, `published_at` заполнен;
- платёж в `succeeded`/`failed`, `processed_at` заполнен;
- `webhook_delivered=true` (приёмник запущен);
- очереди пусты, `payments.new` имеет consumer.

8. Проверьте логи consumer: ошибки соединения во время простоя RabbitMQ
   ожидаемы, после восстановления не должно быть циклических падений.

### E2E-10. Конкурентные запросы (опционально)

Цель: проверить идемпотентность под гонкой.

1. Отправьте 5 параллельных запросов с одним ключом и одним телом:

```bash
export IDEM_10="$RUN_ID-e2e-10"

for i in 1 2 3 4 5; do
  curl -sS -o "/tmp/e2e_10_$i.json" -w "%{http_code}\n" \
    -X POST "$API_URL/api/v1/payments" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -H "Idempotency-Key: $IDEM_10" \
    -d '{
      "amount": "42.00",
      "currency": "USD",
      "description": "E2E concurrency",
      "metadata": {"run_id": "'"$RUN_ID"'", "case": "e2e-10"},
      "webhook_url": "'"$WEBHOOK_URL"'"
    }' &
done
wait
```

2. Проверьте идентификаторы:

```bash
python3 - <<'PY'
import json
import pathlib

ids = {
    json.loads(pathlib.Path(f"/tmp/e2e_10_{i}.json").read_text())["payment_id"]
    for i in range(1, 6)
}
print("unique_ids=", ids)
assert len(ids) == 1, "race created more than one payment"
PY

export PAYMENT_ID_10=$(python3 -c \
  'import json,pathlib; print(json.loads(pathlib.Path("/tmp/e2e_10_1.json").read_text())["payment_id"])')
echo "PAYMENT_ID_10=$PAYMENT_ID_10"
```

Все ответы должны быть `202` с одним `payment_id`. В БД — одна строка и одно
outbox-событие:

```sql
SELECT count(*) FROM payments WHERE idempotency_key = '<IDEM_10>';

SELECT count(*) FROM outbox
WHERE payload->>'payment_id' = '<PAYMENT_ID_10>';
```

Одна из доставок webhook (не более одной), `webhook_attempts` не должен
показывать серию дублей.

Примечание: гонка может не воспроизвестись на быстром последовательном
выполнении; сам факт одинакового результата всё равно проверяет механизм.

## 6. Проверка после прогона

1. Все тестовые платежи в финальном статусе:

```sql
SELECT status, count(*)
FROM payments
WHERE metadata->>'run_id' = '<RUN_ID>'
GROUP BY status;
```

Не должно быть `pending` (кроме платежей, созданных вне сценариев).

2. Нет «зависших» outbox-событий:

```sql
SELECT status, count(*)
FROM outbox
GROUP BY status;
```

Для завершённых сценариев `pending` быть не должно.

3. Очереди пусты, кроме ожидаемых сообщений DLQ:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged consumers
```

4. В логах consumer нет неожиданных traceback. Ошибки webhook во время
   E2E-07/E2E-08 и ошибки соединения во время E2E-09 — ожидаемы.

```bash
docker compose logs --since=30m consumer | grep -iE 'traceback|critical' || true
```

5. Проверьте, что API после всех сценариев отвечает:

```bash
curl -fsS -H "X-API-Key: $API_KEY" "$API_URL/health"
```

## 7. Очистка

Остановите приёмник (Ctrl+C). Удалите временные файлы:

```bash
rm -f /tmp/e2e_*.json /tmp/e2e_*.headers
```

Если нужна чистая среда (удалит **все** данные PostgreSQL и RabbitMQ):

```bash
docker compose down -v
docker compose up -d --build
```

После пересоздания повторите [preflight.md](preflight.md) — миграции
применятся автоматически, но health и очереди нужно проверить снова.

Если данные нужны, оставьте их и используйте `RUN_ID` для фильтрации. Не
удаляйте строки из `payments`/`outbox` вручную в production: это ломает
соответствие событий и платежей.

## 8. Критерии приёмки E2E

- [ ] E2E-01: `/health` — `401` без ключа, `401` с неверным, `200` с верным.
- [ ] E2E-02: `POST` → `202`; `GET` → финальный статус; webhook доставлен;
      `outbox.status='published'`.
- [ ] E2E-03: повтор — тот же `payment_id`, `Idempotency-Replayed: true`,
      одна строка платежа и одно событие, без нового webhook.
- [ ] E2E-04: другой body с тем же ключом → `409`, без новых записей.
- [ ] E2E-05: некорректные запросы → `401`/`422`, БД не меняется.
- [ ] E2E-06: неизвестный UUID → `404`, невалидный → `422`.
- [ ] E2E-07: `webhook_attempts=3`, `webhook_delivered=true`, в логе
      `500, 500, 200`, retry-очереди пусты.
- [ ] E2E-08: `webhook_attempts=3`, `webhook_delivered=false`, DLQ выросла
      на 1, платёж не `pending`.
- [ ] E2E-09: при остановленном RabbitMQ `POST` → `202`, outbox `pending`;
      после старта — `published`, платёж обработан, webhook доставлен.
- [ ] E2E-10 (опционально): все параллельные ответы `202` с одним
      `payment_id`, одна строка и одно событие.
- [ ] В логах нет необработанных исключений; очереди после прогона пусты
      (кроме ожидаемой DLQ).

Результаты удобно фиксировать таблицей:

| Сценарий | Статус | Доказательство |
|---|---|---|
| E2E-01 | pass/fail | коды ответов |
| E2E-02 | pass/fail | JSON `GET`, лог приёмника, SQL |
| … | … | … |

## 9. Связанные документы

- [Предзапусковая подготовка](preflight.md)
- [API](api.md)
- [Архитектура](architecture.md)
- [README проекта](../README.md)
