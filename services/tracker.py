"""Сервис координации парсинга, сохранения данных и запуска анализа."""

import asyncio
from datetime import datetime
import logging
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Product
from database.repository import ProductRepository
from parsers.ozon import OzonParser, get_shared_parser
from schemas.tracker import PriceAnalysisResult
from services.analyzer import PriceAnalyzer

logger = logging.getLogger(__name__)


class TrackerService:
    """Оркестратор регулярного мониторинга цен."""

    def __init__(
        self,
        session: AsyncSession,
        parser: Optional[OzonParser] = None,
    ) -> None:
        self._session = session
        self._repo = ProductRepository(session)
        self._parser = parser or get_shared_parser()
        self._analyzer = PriceAnalyzer()

    async def process_sku(
        self,
        chat_id: int,
        sku: str,
        dumping_threshold: Optional[float] = None,
        check_interval_minutes: Optional[int] = None,
    ) -> Optional[PriceAnalysisResult]:
        """Обработка позиции пользователя: сбор данных, запись и расчет дельты."""
        try:
            logger.info("Обработка SKU %s для chat_id %d", sku, chat_id)
            parsed_data = await self._parser.fetch_product_by_sku(sku)

            product = await self._repo.add_or_update_product(
                parsed=parsed_data,
                chat_id=chat_id,
                dumping_threshold=dumping_threshold,
                check_interval_minutes=check_interval_minutes,
            )
            await self._repo.save_price_snapshot(product.id, parsed_data)
            await self._repo.update_last_checked(product.id, datetime.utcnow())
            await self._session.commit()

            latest, previous = await self._repo.get_last_two_snapshots(product.id)
            if not latest:
                return None

            result = self._analyzer.analyze(
                product=product,
                latest=latest,
                previous=previous,
            )

            if result.is_dumping:
                logger.warning(
                    "Зафиксировано снижение цены SKU %s (чат %d): %s%% (%s руб.)",
                    result.sku,
                    chat_id,
                    abs(result.diff_percent),
                    result.diff_rubles,
                )

            return result

        except Exception as exc:
            logger.error(
                "Ошибка обработки SKU %s для chat_id %d: %s", sku, chat_id, exc
            )
            await self._session.rollback()
            return None

    async def process_due_products(
        self, concurrency_limit: int = 3
    ) -> List[PriceAnalysisResult]:
        """Параллельный опрос очереди позиций с контролем конкурентности."""
        due_products = await self._repo.get_due_products()
        if not due_products:
            return []

        logger.info("Опрос очереди товаров: %d позиций", len(due_products))

        semaphore = asyncio.Semaphore(concurrency_limit)

        async def worker(prod: Product) -> Optional[PriceAnalysisResult]:
            async with semaphore:
                try:
                    return await self.process_sku(
                        chat_id=prod.telegram_chat_id,
                        sku=prod.sku,
                    )
                except Exception as exc:
                    logger.error("Сбой задачи обработки SKU %s: %s", prod.sku, exc)
                    return None

        tasks = [worker(p) for p in due_products]
        raw_results = await asyncio.gather(*tasks, return_exceptions=False)

        return [r for r in raw_results if r is not None]
