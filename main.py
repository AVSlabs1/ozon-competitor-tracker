"""Точка входа: параллельный запуск планировщика мониторинга и Telegram-бота."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alerts.handlers import router as bot_router
from alerts.telegram import TelegramNotifier
from config.settings import settings
from database.session import async_session_factory, init_db
from parsers.ozon import OzonParser, get_shared_parser
from services.tracker import TrackerService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tracker")

logging.getLogger("aiogram.dispatcher").setLevel(logging.CRITICAL)
logging.getLogger("apscheduler.executors.default").setLevel(logging.CRITICAL)


async def run_monitoring_cycle(notifier: TelegramNotifier, parser: OzonParser) -> None:
    """Выполнение цикла мониторинга и пакетная отправка алертов без спама."""
    try:
        async with async_session_factory() as session:
            tracker = TrackerService(session, parser=parser)
            results = await tracker.process_due_products()
            if results:
                await notifier.send_batch_alerts(results)
    except asyncio.CancelledError:
        pass


async def main() -> None:
    """Инициализация компонентов, запуск браузерного пула и поллинга бота."""
    logger.info("Инициализация сервиса мониторинга")

    await init_db()

    parser = get_shared_parser()
    await parser.start()
    logger.info("Пул браузера Playwright успешно запущен")

    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(bot_router)

    notifier = TelegramNotifier()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_monitoring_cycle,
        trigger="interval",
        minutes=1,
        args=[notifier, parser],
        id="monitoring_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Планировщик персональных интервалов запущен")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=True,
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        pass
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        await notifier.close()
        await bot.session.close()
        await parser.close()
        logger.info("Сервис остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
