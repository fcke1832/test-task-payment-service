import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.api.v1.payments import router as payments_router
from app.db.session import engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("Payment API started")
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("Payment API stopped")


app = FastAPI(
    title="Payment Processing Service",
    description=(
        "Асинхронный сервис процессинга платежей: API создаёт платёж и "
        "outbox-событие, consumer обрабатывает его и отправляет webhook."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(payments_router, prefix="/api/v1")
