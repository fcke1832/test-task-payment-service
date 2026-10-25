from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.APP_ENV == "development",  # Логируем SQL только в dev
    pool_pre_ping=True,  # Проверяем соединение перед использованием
)


async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Не обновляем объекты после коммита
)
