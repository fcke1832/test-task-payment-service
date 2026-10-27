from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.messaging.broker import RETRY_QUEUES
from app.messaging.consumer import (
    build_webhook_payload,
    read_retry_count,
    retry_queue_for_attempt,
)
from app.models.enums import CurrencyEnum, PaymentStatusEnum
from app.models.payment import Payment


def _payment(status: PaymentStatusEnum) -> Payment:
    return Payment(
        id=uuid4(),
        amount=Decimal("42.50"),
        currency=CurrencyEnum.USD,
        description="Test payment",
        metadata_={"order_id": 7},
        status=status,
        idempotency_key="test-key",
        webhook_url="https://example.com/webhook",
        webhook_delivered=False,
        webhook_attempts=0,
        created_at=datetime.now(UTC),
        processed_at=datetime.now(UTC),
    )


def test_read_retry_count_defaults_and_parses() -> None:
    assert read_retry_count(None) == 1
    assert read_retry_count({}) == 1
    assert read_retry_count({"x-retry-count": 3}) == 3
    assert read_retry_count({"x-retry-count": "2"}) == 2
    assert read_retry_count({"x-retry-count": "bad"}) == 1
    assert read_retry_count({"x-retry-count": -5}) == 1


def test_retry_queue_is_selected_by_attempt() -> None:
    if not RETRY_QUEUES:
        return
    assert retry_queue_for_attempt(2).name == RETRY_QUEUES[0].name
    assert retry_queue_for_attempt(999).name == RETRY_QUEUES[-1].name


def test_webhook_payload_contains_stable_event_id() -> None:
    payment = _payment(PaymentStatusEnum.SUCCEEDED)
    payload = build_webhook_payload(payment)
    assert payload["payment_id"] == str(payment.id)
    assert payload["event"] == "payment.succeeded"
    assert payload["event_id"] == f"{payment.id}:succeeded"
    assert payload["amount"] == "42.50"
    assert payload["metadata"] == {"order_id": 7}
