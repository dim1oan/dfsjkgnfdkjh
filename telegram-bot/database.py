"""
Асинхронная настройка SQLAlchemy 2.0.

Экспортирует:
    engine            — async-движок
    async_session     — фабрика сессий
    get_session()     — асинхронный контекст-менеджер сессии
    init_db()         — создание таблиц при первом запуске
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings
from models import Base

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Контекст-менеджер сессии БД.

    Использование:
        async with get_session() as session:
            ...
    Автоматически коммитит при успехе и откатывает при исключении.
    """
    session = async_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Создаёт все таблицы, если они ещё не существуют."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
