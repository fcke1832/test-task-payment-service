from app.core.config import settings
from app.messaging.outbox import outbox_backoff_seconds


def test_outbox_backoff_grows_exponentially() -> None:
    assert outbox_backoff_seconds(1) <= outbox_backoff_seconds(2)
    assert outbox_backoff_seconds(2) <= outbox_backoff_seconds(3)
    assert outbox_backoff_seconds(100) <= settings.OUTBOX_MAX_BACKOFF_SECONDS
