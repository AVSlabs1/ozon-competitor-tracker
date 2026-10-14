"""Базовый абстрактный интерфейс для парсеров маркетплейсов."""

from abc import ABC, abstractmethod
from schemas.tracker import ParsedProduct


class BaseParser(ABC):
    """Контракт парсера карточек товаров маркетплейсов."""

    async def start(self) -> None:
        """Инициализация сетевых ресурсов или пула браузера."""
        pass

    async def close(self) -> None:
        """Освобождение сетевых ресурсов и остановка браузера."""
        pass

    @abstractmethod
    async def fetch_product_by_sku(self, sku: str) -> ParsedProduct:
        """Получение данных о товаре по его SKU."""
        pass
