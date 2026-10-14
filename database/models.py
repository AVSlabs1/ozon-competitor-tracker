"""Реляционные модели SQLAlchemy для хранения товаров, истории цен, пользователей и настроек."""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый декларативный класс."""

    pass


class SystemSetting(Base):
    """Таблица параметров конфигурации приложения."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)

    def __repr__(self) -> str:
        return f"<SystemSetting(key='{self.key}', value='{self.value}')>"


class User(Base):
    """Модель авторизованного пользователя (селлера)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    max_products: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    team_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<User(chat_id={self.telegram_chat_id}, team={self.team_chat_id}, active={self.is_active})>"


class Product(Base):
    """Модель отслеживаемого товара с изоляцией по пользователям."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    dumping_threshold: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    target_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    check_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.min, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    price_history: Mapped[List["PriceHistory"]] = relationship(
        "PriceHistory",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="desc(PriceHistory.recorded_at)",
    )

    __table_args__ = (UniqueConstraint("telegram_chat_id", "sku", name="uq_chat_sku"),)

    def __repr__(self) -> str:
        return (
            f"<Product(id={self.id}, chat={self.telegram_chat_id}, "
            f"sku='{self.sku}', threshold={self.dumping_threshold}%)>"
        )


class PriceHistory(Base):
    """Исторический снимок цен, остатков и наличия товара."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    card_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    original_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    is_in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stock_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )

    product: Mapped["Product"] = relationship("Product", back_populates="price_history")

    __table_args__ = (Index("ix_product_recorded_at", "product_id", "recorded_at"),)

    def __repr__(self) -> str:
        return (
            f"<PriceHistory(product_id={self.product_id}, "
            f"price={self.current_price}, stock={self.stock_count})>"
        )
