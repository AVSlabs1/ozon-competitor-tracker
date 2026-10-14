"""Сервис анализа динамики цен, остатков и расчета показателей реализации."""

from decimal import Decimal
from typing import Optional

from database.models import PriceHistory, Product
from schemas.tracker import PriceAnalysisResult, PriceChangeType


class PriceAnalyzer:
    """Анализатор динамики цен, объема реализации и таргетных порогов."""

    @staticmethod
    def analyze(
        product: Product,
        latest: PriceHistory,
        previous: Optional[PriceHistory],
    ) -> PriceAnalysisResult:
        """Сравнение двух замеров параметров позиции."""
        chat_id = product.telegram_chat_id
        effective_curr = (
            latest.card_price if latest.card_price else latest.current_price
        )

        is_target_breached = False
        if product.target_price is not None and effective_curr <= product.target_price:
            is_target_breached = True

        if previous is None:
            return PriceAnalysisResult(
                chat_id=chat_id,
                sku=product.sku,
                title=product.title,
                url=product.url,
                change_type=PriceChangeType.INITIAL_CHECK,
                current_price=latest.current_price,
                current_card_price=latest.card_price,
                previous_price=None,
                previous_card_price=None,
                diff_rubles=Decimal("0.00"),
                diff_percent=0.0,
                is_dumping=False,
                is_in_stock=latest.is_in_stock,
                target_price=product.target_price,
                is_target_breached=is_target_breached,
                stock_count=latest.stock_count,
            )

        sales_units: Optional[int] = None
        estimated_revenue: Optional[Decimal] = None
        restock_units: Optional[int] = None

        if previous.stock_count is not None and latest.stock_count is not None:
            if previous.stock_count > latest.stock_count:
                sales_units = previous.stock_count - latest.stock_count
                estimated_revenue = Decimal(sales_units) * effective_curr
            elif latest.stock_count > previous.stock_count:
                restock_units = latest.stock_count - previous.stock_count

        if previous.is_in_stock and not latest.is_in_stock:
            return PriceAnalysisResult(
                chat_id=chat_id,
                sku=product.sku,
                title=product.title,
                url=product.url,
                change_type=PriceChangeType.OUT_OF_STOCK,
                current_price=latest.current_price,
                current_card_price=latest.card_price,
                previous_price=previous.current_price,
                previous_card_price=previous.card_price,
                diff_rubles=Decimal("0.00"),
                diff_percent=0.0,
                is_dumping=False,
                is_in_stock=False,
                target_price=product.target_price,
                is_target_breached=is_target_breached,
                stock_count=0,
                sales_units=sales_units,
                estimated_revenue=estimated_revenue,
            )

        if not previous.is_in_stock and latest.is_in_stock:
            return PriceAnalysisResult(
                chat_id=chat_id,
                sku=product.sku,
                title=product.title,
                url=product.url,
                change_type=PriceChangeType.BACK_IN_STOCK,
                current_price=latest.current_price,
                current_card_price=latest.card_price,
                previous_price=previous.current_price,
                previous_card_price=previous.card_price,
                diff_rubles=Decimal("0.00"),
                diff_percent=0.0,
                is_dumping=False,
                is_in_stock=True,
                target_price=product.target_price,
                is_target_breached=is_target_breached,
                stock_count=latest.stock_count,
                restock_units=restock_units,
            )

        effective_prev = (
            previous.card_price if previous.card_price else previous.current_price
        )
        diff_rub = effective_curr - effective_prev

        if effective_prev > Decimal("0"):
            percent_change = float((diff_rub / effective_prev) * Decimal("100"))
        else:
            percent_change = 0.0

        percent_abs = abs(percent_change)
        threshold = product.dumping_threshold or 5.0

        if diff_rub < Decimal("0"):
            if percent_abs >= threshold:
                change_type = PriceChangeType.DUMPING
                is_dumping = True
            else:
                change_type = PriceChangeType.PRICE_DECREASE
                is_dumping = False
        elif diff_rub > Decimal("0"):
            change_type = PriceChangeType.PRICE_INCREASE
            is_dumping = False
        else:
            change_type = PriceChangeType.PRICE_UNCHANGED
            is_dumping = False

        return PriceAnalysisResult(
            chat_id=chat_id,
            sku=product.sku,
            title=product.title,
            url=product.url,
            change_type=change_type,
            current_price=latest.current_price,
            current_card_price=latest.card_price,
            previous_price=previous.current_price,
            previous_card_price=previous.card_price,
            diff_rubles=diff_rub,
            diff_percent=round(percent_change, 2),
            is_dumping=is_dumping,
            is_in_stock=latest.is_in_stock,
            target_price=product.target_price,
            is_target_breached=is_target_breached,
            stock_count=latest.stock_count,
            sales_units=sales_units,
            estimated_revenue=estimated_revenue,
            restock_units=restock_units,
        )
