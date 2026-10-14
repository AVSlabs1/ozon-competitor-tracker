"""Управление асинхронными сессиями SQLAlchemy и автомиграциями SQLite."""

from typing import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import settings
from database.models import Base

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Инициализация таблиц базы данных и автоматическое накатывание новых колонок."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        migrations = [
            "ALTER TABLE products ADD COLUMN target_price NUMERIC(12, 2)",
            "ALTER TABLE products ADD COLUMN dumping_threshold REAL DEFAULT 5.0",
            "ALTER TABLE products ADD COLUMN check_interval_minutes INTEGER DEFAULT 30",
            "ALTER TABLE products ADD COLUMN last_checked_at DATETIME DEFAULT '1970-01-01 00:00:00'",
            "ALTER TABLE price_history ADD COLUMN stock_count INTEGER",
            "ALTER TABLE users ADD COLUMN max_products INTEGER DEFAULT 10",
            "ALTER TABLE users ADD COLUMN team_chat_id BIGINT",
        ]
        for query in migrations:
            try:
                await conn.execute(text(query))
            except Exception:
                pass


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Генератор асинхронных сессий для внедрения зависимостей."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
