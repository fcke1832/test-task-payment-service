from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.enums import CurrencyEnum
from app.schemas.payment import PaymentCreate


def test_payment_create_parses_metadata_and_webhook() -> None:
    data = PaymentCreate.model_validate(
        {
            "amount": "125.50",
            "currency": "RUB",
            "description": "Order #42",
            "metadata": {"order_id": 42},
            "webhook_url": "https://example.com/webhook",
        }
    )
    assert data.amount == Decimal("125.50")
    assert data.currency is CurrencyEnum.RUB
    assert data.metadata == {"order_id": 42}
    assert str(data.webhook_url) == "https://example.com/webhook"


@pytest.mark.parametrize("amount", ["0", "-1.00", "10.999"])
def test_payment_create_rejects_invalid_amount(amount: str) -> None:
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate(
            {
                "amount": amount,
                "currency": "USD",
                "description": "Invalid amount",
                "webhook_url": "https://example.com/webhook",
            }
        )


def test_payment_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate(
            {
                "amount": "10.00",
                "currency": "EUR",
                "description": "Typo",
                "webhook_url": "https://example.com/webhook",
                "webhook": "https://example.com/other",
            }
        )


def test_payment_create_defaults_metadata_to_empty_dict() -> None:
    data = PaymentCreate.model_validate(
        {
            "amount": "10.00",
            "currency": "EUR",
            "description": "No metadata",
            "webhook_url": "https://example.com/webhook",
        }
    )
    assert data.metadata == {}
