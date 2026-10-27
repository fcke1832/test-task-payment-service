from fastapi import APIRouter

from app.api.deps import ApiKey

router = APIRouter(tags=["health"])


@router.get("/health", summary="Проверка работоспособности")
async def health_check(api_key: ApiKey) -> dict[str, str]:
    """Liveness-проверка. Защищена тем же API-ключом, что и бизнес-эндпоинты."""
    return {"status": "ok"}
