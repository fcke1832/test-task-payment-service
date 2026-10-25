from fastapi import FastAPI

from app.api.v1.payments import router as payments_router

app: FastAPI = FastAPI(
    title="Payment Processing Service",
    description="Асинхронный сервис процессинга платежей",
    version="1.0.0",
)

app.include_router(payments_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint для мониторинга."""
    return {"status": "healthy"}
