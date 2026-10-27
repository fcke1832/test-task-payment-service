"""Worker: outbox relay + consumer платежей в одном процессе."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from faststream import FastStream

from app.core.config import settings
from app.db.session import engine
from app.messaging import consumer as _consumer  # noqa: F401
from app.messaging.broker import (
    DEAD_EXCHANGE,
    DEAD_QUEUE,
    DEAD_ROUTING_KEY,
    RETRY_QUEUES,
    broker,
)
from app.messaging.outbox import publish_pending_events

logger = logging.getLogger(__name__)

_outbox_task: asyncio.Task[None] | None = None


async def declare_topology() -> None:
    """Объявить retry-очереди и DLQ после подключения к RabbitMQ.

    Основные exchange/queue объявляет subscriber. Retry-очереди и DLQ не имеют
    consumer'ов, поэтому объявляются здесь явно.
    """
    dead_exchange = await broker.declare_exchange(DEAD_EXCHANGE)
    dead_queue = await broker.declare_queue(DEAD_QUEUE)
    await dead_queue.bind(dead_exchange, routing_key=DEAD_ROUTING_KEY)
    logger.info(f"DLQ declared: {DEAD_QUEUE.name}")

    for queue in RETRY_QUEUES:
        await broker.declare_queue(queue)
        logger.info(f"Retry queue declared: {queue.name}")


async def outbox_poll_loop() -> None:
    while True:
        try:
            await publish_pending_events()
        except asyncio.CancelledError:
            logger.info("Outbox relay cancelled")
            raise
        except Exception:
            logger.exception("Outbox relay iteration failed")

        await asyncio.sleep(settings.OUTBOX_POLL_INTERVAL)


async def start_outbox_relay() -> None:
    global _outbox_task
    if _outbox_task is None or _outbox_task.done():
        _outbox_task = asyncio.create_task(
            outbox_poll_loop(),
            name="outbox-relay",
        )
        logger.info("Outbox relay started")


async def stop_outbox_relay() -> None:
    global _outbox_task
    if _outbox_task is None:
        return
    _outbox_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _outbox_task
    _outbox_task = None
    logger.info("Outbox relay stopped")


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    try:
        yield
    finally:
        await stop_outbox_relay()
        await engine.dispose()
        logger.info("Worker stopped")


faststream_app = FastStream(
    broker,
    lifespan=lifespan,
    after_startup=[declare_topology, start_outbox_relay],
)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(faststream_app.run())


if __name__ == "__main__":
    main()
