"""
Точка входа воркера (consumer + outbox relay).

Запускает:
1. FastStream consumer для обработки событий из RabbitMQ
2. Outbox relay - фоновый цикл для публикации событий из БД в RabbitMQ

Graceful shutdown:
При SIGTERM (остановка контейнера) relay отменяется, FastStream дообрабатывает
текущее сообщение и только потом закрывает соединение с брокером.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from faststream import FastStream

from app.core.config import settings
from app.messaging.broker import broker
from app.messaging.consumer import router
from app.messaging.outbox import publish_pending_events

logger: logging.Logger = logging.getLogger(__name__)


async def outbox_poll_loop() -> None:
    """Периодически вычитывает Outbox и публикует события."""
    while True:
        try:
            await publish_pending_events()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox relay iteration failed")

        await asyncio.sleep(settings.OUTBOX_POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastStream) -> AsyncIterator[None]:
    """Запускает relay при старте и корректно останавливает при shutdown."""
    task: asyncio.Task[None] = asyncio.create_task(outbox_poll_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


faststream_app: FastStream = FastStream(
    broker,
    lifespan=lifespan,
    title="Payment worker",
    version="1.0.0",
)

faststream_app.include_router(router)


def main() -> None:
    """Точка входа воркера."""
    logging.basicConfig(level=logging.INFO)
    faststream_app.run()


if __name__ == "__main__":
    main()
