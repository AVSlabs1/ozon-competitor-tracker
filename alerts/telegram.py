"""Модуль отправки уведомлений в Telegram с фильтрацией шума и поддержкой командных чатов."""

from collections import defaultdict
from datetime import timedelta
import logging
from typing import Dict, List, Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import settings
from database.repository import ProductRepository
from database.session import async_session_factory
from schemas.tracker import PriceAnalysisResult, PriceChangeType

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Сервис отправки оповещений о динамике цен с защитой от спама."""

    def __init__(self) -> None:
        self._bot: Optional[Bot] = None
        if (
            settings.TELEGRAM_BOT_TOKEN
            and settings.TELEGRAM_BOT_TOKEN != "your_bot_token_here"
        ):
            self._bot = Bot(
                token=settings.TELEGRAM_BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        else:
            logger.warning("TELEGRAM_BOT_TOKEN не задан. Уведомления отключены")

    def _format_single_alert(self, result: PriceAnalysisResult) -> str:
        """Формирование лаконичного отчета для селлера."""
        msk_time = result.analyzed_at + timedelta(hours=3)
        formatted_time = msk_time.strftime("%d.%m в %H:%M (МСК)")

        if result.current_card_price:
            price_display = f"По Ozon Карте: <b>{result.current_card_price:,.0f} ₽</b> (базовая: {result.current_price:,.0f} ₽)".replace(
                ",", " "
            )
        else:
            price_display = f"Цена: <b>{result.current_price:,.0f} ₽</b>".replace(
                ",", " "
            )

        sales_block = ""
        if result.sales_units and result.estimated_revenue:
            rev_formatted = f"{result.estimated_revenue:,.0f} ₽".replace(",", " ")
            sales_block = (
                f"\nРеализация за период: ~{result.sales_units} шт.\n"
                f"Расчетный объем: ~{rev_formatted}\n"
            )
        elif result.restock_units:
            sales_block = f"\nПоступление на склад: +{result.restock_units} шт.\n"

        stock_info = (
            f" (остаток: <b>{result.stock_count} шт.</b>)"
            if result.stock_count is not None
            else ""
        )

        target_alert = ""
        if result.is_target_breached and result.target_price:
            target_str = f"{result.target_price:,.0f} ₽".replace(",", " ")
            target_alert = (
                f"\nВнимание: цена ниже установленного минимума!\n"
                f"Минимальная планка: <b>{target_str}</b>\n"
            )

        if result.change_type == PriceChangeType.DUMPING:
            header = "<b>Снижение цены ниже порога</b>"
            body = (
                f"{price_display}\n"
                f"Дельта: <b>{result.diff_rubles:,.0f} ₽</b> ({result.diff_percent}%)\n"
                f"Предыдущая цена: <s>{result.previous_card_price or result.previous_price} ₽</s>"
                f"{stock_info}{target_alert}{sales_block}"
            )
        elif result.change_type == PriceChangeType.PRICE_INCREASE:
            header = "<b>Повышение цены</b>"
            body = (
                f"{price_display}\n"
                f"Дельта: <b>+{result.diff_rubles:,.0f} ₽</b> (+{result.diff_percent}%)\n"
                f"Предыдущая цена: {result.previous_card_price or result.previous_price} ₽"
                f"{stock_info}{target_alert}{sales_block}"
            )
        elif result.change_type == PriceChangeType.OUT_OF_STOCK:
            header = "<b>Товар закончился на складе</b>"
            body = f"Статус: Нет в наличии{sales_block}"
        elif result.change_type == PriceChangeType.BACK_IN_STOCK:
            header = "<b>Товар снова в наличии</b>"
            body = f"{price_display}{stock_info}{target_alert}{sales_block}"
        elif result.change_type == PriceChangeType.PRICE_UNCHANGED:
            header = "<b>Цена без изменений</b>"
            body = f"{price_display}{stock_info}{target_alert}{sales_block}"
        else:
            header = "<b>Товар добавлен на мониторинг</b>"
            body = f"{price_display}{stock_info}"

        return (
            f"{header}\n\n"
            f'Товар: <a href="{result.url}">{result.title}</a>\n\n'
            f"{body}\n\n"
            f"<i>{formatted_time}</i>"
        )

    def _format_batch_digest(self, results: List[PriceAnalysisResult]) -> str:
        """Формирование сводного дайджеста при массовом изменении цен."""
        msk_time = results[0].analyzed_at + timedelta(hours=3)
        formatted_time = msk_time.strftime("%d.%m в %H:%M (МСК)")

        lines = [f"<b>Сводка изменений по {len(results)} позициям:</b>\n"]

        for i, res in enumerate(results, start=1):
            price_val = f"{res.current_card_price or res.current_price:,.0f} ₽".replace(
                ",", " "
            )

            if res.change_type == PriceChangeType.DUMPING:
                tag = f"Снижение (-{abs(res.diff_percent)}%)"
            elif res.change_type == PriceChangeType.PRICE_INCREASE:
                tag = f"Повышение (+{res.diff_percent}%)"
            elif res.change_type == PriceChangeType.OUT_OF_STOCK:
                tag = "Закончился"
            elif res.change_type == PriceChangeType.BACK_IN_STOCK:
                tag = "Появился"
            else:
                tag = "Изменение"

            short_title = res.title[:35].strip() + "..."
            alert_tag = " [Ниже себестоимости]" if res.is_target_breached else ""

            lines.append(
                f'{i}. {tag}: <a href="{res.url}">{short_title}</a>\n'
                f"   Актуальная цена: <b>{price_val}</b>{alert_tag}"
            )

        lines.append(f"\n<i>{formatted_time}</i>")
        return "\n".join(lines)

    async def send_batch_alerts(self, results: List[PriceAnalysisResult]) -> None:
        """Пакетная отправка алертов владельцу и в рабочий чат команды."""
        if not self._bot:
            return

        actionable_results: List[PriceAnalysisResult] = []
        for r in results:
            if r.change_type == PriceChangeType.INITIAL_CHECK:
                continue
            if (
                r.change_type == PriceChangeType.PRICE_UNCHANGED
                and not r.is_target_breached
                and not r.sales_units
            ):
                continue
            actionable_results.append(r)

        if not actionable_results:
            return

        grouped_by_user: Dict[int, List[PriceAnalysisResult]] = defaultdict(list)
        for r in actionable_results:
            target_chat = r.chat_id or settings.TELEGRAM_ALERT_CHAT_ID
            if target_chat:
                grouped_by_user[target_chat].append(r)

        for chat_id, user_results in grouped_by_user.items():
            text = (
                self._format_single_alert(user_results[0])
                if len(user_results) == 1
                else self._format_batch_digest(user_results)
            )

            # 1. Отправка личного сообщения селлеру
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    disable_web_page_preview=False,
                )
                logger.info(
                    "Отправлен алерт (%d событий) селлеру %d",
                    len(user_results),
                    chat_id,
                )
            except Exception as exc:
                logger.error("Ошибка отправки сообщения селлеру %d: %s", chat_id, exc)

            # 2. Дублирование в командный рабочий чат селлера (если подключен)
            async with async_session_factory() as session:
                repo = ProductRepository(session)
                team_chat_id = await repo.get_user_team_chat(chat_id)

            if team_chat_id and team_chat_id != chat_id:
                try:
                    await self._bot.send_message(
                        chat_id=team_chat_id,
                        text=f"Рабочий чат команды:\n\n{text}",
                        disable_web_page_preview=False,
                    )
                    logger.info("Алерт дублирован в командный чат %d", team_chat_id)
                except Exception as exc:
                    logger.error(
                        "Ошибка дублирования в командный чат %d: %s", team_chat_id, exc
                    )

    async def close(self) -> None:
        """Закрытие HTTP-клиента бота."""
        if self._bot:
            await self._bot.session.close()
