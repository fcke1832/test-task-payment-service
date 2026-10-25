from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session


# Зависимость для получения сессии БД
async def get_db() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Зависимость для проверки API Key
async def verify_api_key(x_api_key: Annotated[str, Header()]) -> str:
    if x_api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )
    return x_api_key


# Типизированные алиасы для удобства
DbSession = Annotated[AsyncSession, Depends(get_db)]
ApiKey = Annotated[str, Depends(verify_api_key)]
