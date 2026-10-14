"""Схемы валидации данных парсинга и аналитических расчетов."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class PriceChangeType(str, Enum):
    """Классификатор событий изменения цены и остатков."""

    DUMPING = "dumping"
    PRICE_DECREASE = "price_decrease"
    PRICE_INCREASE = "price_increase"
    PRICE_UNCHANGED = "price_unchanged"
    OUT_OF_STOCK = "out_of_stock"
    BACK_IN_STOCK = "back_in_stock"
    INITIAL_CHECK = "initial_check"


class ParsedProduct(BaseModel):
    """Данные товара, полученные парсером."""

    model_config = ConfigDict(frozen=True)

    sku: str
    url: str
    title: str
    card_price: Optional[Decimal] = None
    current_price: Decimal
    original_price: Optional[Decimal] = None
    is_in_stock: bool = True
    stock_count: Optional[int] = None
    parsed_at: datetime = Field(default_factory=datetime.utcnow)


class PriceAnalysisResult(BaseModel):
    """Результат сравнительного анализа параметров товара."""

    model_config = ConfigDict(frozen=True)

    chat_id: int = 0
    sku: str
    title: str
    url: str
    change_type: PriceChangeType
    current_price: Decimal
    current_card_price: Optional[Decimal] = None
    previous_price: Optional[Decimal] = None
    previous_card_price: Optional[Decimal] = None
    diff_rubles: Decimal = Decimal("0.00")
    diff_percent: float = 0.0
    is_dumping: bool = False
    is_in_stock: bool = True

    target_price: Optional[Decimal] = None
    is_target_breached: bool = False

    stock_count: Optional[int] = None
    sales_units: Optional[int] = None
    estimated_revenue: Optional[Decimal] = None
    restock_units: Optional[int] = None

    analyzed_at: datetime = Field(default_factory=datetime.utcnow)
