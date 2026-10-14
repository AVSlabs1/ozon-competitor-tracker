"""Обработчики команд и диалогов Telegram-бота с контролем доступа селлеров и командными чатами."""

import asyncio
from datetime import datetime
from decimal import Decimal
import logging
import re
from typing import List, Optional

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config.settings import settings
from database.models import Product
from database.repository import ProductRepository
from database.session import async_session_factory
from services.tracker import TrackerService

logger = logging.getLogger(__name__)
router = Router()


class AddProductFSM(StatesGroup):
    """Шаги диалога добавления товара."""

    waiting_for_input = State()
    waiting_for_threshold = State()


class EditProductFSM(StatesGroup):
    """Шаги изменения параметров существующего товара."""

    waiting_for_custom_threshold = State()
    waiting_for_custom_interval = State()


async def extract_sku_from_text(raw_text: str) -> Optional[str]:
    """Извлечение артикула SKU из текста, прямой ссылки или короткой ссылки ozon.ru/t/."""
    if "ozon.ru/t/" in raw_text:
        url_match = re.search(r"https?://[^\s]+", raw_text)
        if url_match:
            short_url = url_match.group(0)
            try:
                cmd = [
                    "curl",
                    "-s",
                    "-i",
                    "-m",
                    "10",
                    "-A",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
                ]
                if settings.PROXY_SERVER:
                    cmd.extend(["-x", settings.PROXY_SERVER])
                cmd.append(short_url)

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode("utf-8", errors="ignore")

                loc_match = re.search(r"location:\s*([^\r\n]+)", output, re.IGNORECASE)
                if loc_match:
                    target_location = loc_match.group(1).strip()
                    sku_match = re.search(r"(\d{8,12})", target_location)
                    if sku_match:
                        return sku_match.group(1)
            except Exception as exc:
                logger.error(
                    "Сбой curl при распаковке короткой ссылки %s: %s", short_url, exc
                )

    direct_match = re.search(r"(\d{8,12})", raw_text)
    if direct_match:
        return direct_match.group(1)

    return None


async def safe_delete_message(msg: Optional[Message]) -> None:
    """Безопасное удаление объекта сообщения."""
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass


async def safe_delete_by_id(message: Message, message_id: Optional[int]) -> None:
    """Безопасное удаление сообщения по его ID через клиент бота."""
    if message.bot and message_id:
        try:
            await message.bot.delete_message(
                chat_id=message.chat.id, message_id=message_id
            )
        except Exception:
            pass


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Добавить товар"), KeyboardButton(text="Мои товары")],
            [KeyboardButton(text="Командный чат")],
        ],
        resize_keyboard=True,
    )


def get_threshold_inline_keyboard(sku: Optional[str] = None) -> InlineKeyboardMarkup:
    """Кнопки выбора порога демпинга."""
    prefix = f"ethresh_{sku}_" if sku else "thresh_"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3%", callback_data=f"{prefix}3"),
                InlineKeyboardButton(text="5%", callback_data=f"{prefix}5"),
                InlineKeyboardButton(text="10%", callback_data=f"{prefix}10"),
                InlineKeyboardButton(text="Свой ввод", callback_data=f"{prefix}custom"),
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"view_{sku}" if sku else "back_to_list"
                )
            ],
        ]
    )


def get_product_interval_inline_keyboard(sku: str) -> InlineKeyboardMarkup:
    """Кнопки выбора интервала проверки товара."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="15 мин", callback_data=f"eint_{sku}_15"),
                InlineKeyboardButton(text="30 мин", callback_data=f"eint_{sku}_30"),
                InlineKeyboardButton(text="1 час", callback_data=f"eint_{sku}_60"),
            ],
            [
                InlineKeyboardButton(text="3 часа", callback_data=f"eint_{sku}_180"),
                InlineKeyboardButton(text="6 часов", callback_data=f"eint_{sku}_360"),
                InlineKeyboardButton(
                    text="Свой ввод", callback_data=f"eint_{sku}_custom"
                ),
            ],
            [InlineKeyboardButton(text="Назад", callback_data=f"view_{sku}")],
        ]
    )


def build_products_list_keyboard(products: List[Product]) -> InlineKeyboardMarkup:
    """Список товаров с переходом в карточку."""
    buttons = []
    for p in products:
        short_title = p.title[:26].strip() + "..."
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{short_title} ({p.dumping_threshold}% | {p.check_interval_minutes}м)",
                    callback_data=f"view_{p.sku}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_single_product_keyboard(sku: str) -> InlineKeyboardMarkup:
    """Панель управления карточкой товара."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Порог демпинга (%)", callback_data=f"menu_thresh_{sku}"
                ),
                InlineKeyboardButton(
                    text="Мин. цена (₽)", callback_data=f"menu_target_{sku}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Интервал", callback_data=f"menu_interval_{sku}"
                ),
                InlineKeyboardButton(
                    text="Отчет Excel (.xlsx)", callback_data=f"excel_{sku}"
                ),
            ],
            [
                InlineKeyboardButton(text="Удалить товар", callback_data=f"del_{sku}"),
            ],
            [
                InlineKeyboardButton(
                    text="К списку товаров", callback_data="back_to_list"
                ),
            ],
        ]
    )


async def check_access(message: Message) -> bool:
    """Проверка прав доступа в личных сообщениях с селлером и синхронизация профиля."""
    if message.chat.type != ChatType.PRIVATE:
        return False

    chat_id = message.chat.id
    uname = message.from_user.username if message.from_user else None
    fname = message.from_user.full_name if message.from_user else None

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        # Всегда сохраняем актуальный юзернейм и имя из Telegram
        await repo.sync_user_profile(chat_id=chat_id, username=uname, full_name=fname)
        await session.commit()
        allowed = await repo.is_user_allowed(chat_id)

    if allowed:
        return True

    admin_link = (
        f"@{settings.ADMIN_USERNAME}" if settings.ADMIN_USERNAME else "администратору"
    )
    await message.answer(
        f"Доступ ограничен.\n\n"
        f"Сервис закрытого мониторинга доступен по подписке.\n\n"
        f"Ваш ID: <code>{message.chat.id}</code>\n"
        f"Для оформления доступа обратитесь к {admin_link}.",
        reply_markup=ReplyKeyboardRemove(),
    )

    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if admin_id and message.bot:
        user_name = f"@{uname}" if uname else "Без юзернейма"
        await message.bot.send_message(
            chat_id=admin_id,
            text=(
                f"Запрос доступа в систему:\n\n"
                f"Пользователь: {user_name}\n"
                f"Имя: {fname or 'Не указано'}\n"
                f"Chat ID: <code>{message.chat.id}</code>"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Активировать доступ",
                            callback_data=f"admin_grant_{message.chat.id}",
                        )
                    ]
                ]
            ),
        )

    return False


async def render_product_card(message: Message, chat_id: int, sku: str) -> None:
    """Отображение карточки товара."""
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        product = await repo.get_by_sku(chat_id=chat_id, sku=sku)
        latest, _ = (
            await repo.get_last_two_snapshots(product.id) if product else (None, None)
        )

    if not product:
        await message.edit_text("Товар не найден.")
        return

    price_str = (
        f"<b>{latest.current_price:,.0f} ₽</b>".replace(",", " ")
        if latest
        else "нет данных"
    )
    card_str = (
        f" ({latest.card_price:,.0f} ₽ по карте)".replace(",", " ")
        if (latest and latest.card_price)
        else ""
    )
    target_str = (
        f"<b>{product.target_price:,.0f} ₽</b>".replace(",", " ")
        if product.target_price
        else "не задана"
    )

    text = (
        f'Товар: <a href="{product.url}">{product.title}</a>\n\n'
        f"Актуальная цена: {price_str}{card_str}\n"
        f"Порог демпинга: <b>{product.dumping_threshold}%</b>\n"
        f"Минимальная планка: {target_str}\n"
        f"Интервал опроса: <b>{product.check_interval_minutes} мин.</b>\n\n"
        "Параметры карточки:"
    )

    await message.edit_text(
        text,
        reply_markup=build_single_product_keyboard(product.sku),
        disable_web_page_preview=False,
    )


# --- Управление командным рабочим чатом селлера ---


@router.message(F.text == "Командный чат", F.chat.type == ChatType.PRIVATE)
async def handle_team_chat_menu(message: Message) -> None:
    """Меню подключения рабочего чата с копированием команды в один клик."""
    if not await check_access(message):
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        user = await repo.get_user(message.chat.id)

    team_id = user.team_chat_id if user else None
    status_str = (
        f"Подключен (ID: <code>{team_id}</code>)" if team_id else "Не подключен"
    )
    connect_cmd = f"/connect {message.chat.id}"

    text = (
        f"Подключение командного чата:\n\n"
        f"Статус: <b>{status_str}</b>\n\n"
        f"Инструкция:\n"
        f"1. Добавьте бота в рабочий чат вашей команды.\n"
        f"2. Нажмите на команду ниже, чтобы скопировать её:\n"
        f"<code>{connect_cmd}</code>\n"
        f"3. Отправьте скопированную команду в группу."
    )

    buttons = []
    if message.bot:
        bot_user = await message.bot.get_me()
        if bot_user.username:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Добавить бота в группу",
                        url=f"https://t.me/{bot_user.username}?startgroup=true",
                    )
                ]
            )

    if team_id:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Отключить командный чат", callback_data="disconnect_team_chat"
                )
            ]
        )

    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "disconnect_team_chat")
async def handle_disconnect_team_chat(callback: CallbackQuery) -> None:
    """Отключение командного чата селлера."""
    await callback.answer("Командный чат отключен")
    if not callback.message:
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.link_team_chat(chat_id=callback.message.chat.id, team_chat_id=None)
        await session.commit()

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Командный чат отключен. Уведомления будут поступать только вам лично."
        )


@router.message(CommandStart(), F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def handle_group_start(message: Message) -> None:
    """Приветственное сообщение при добавлении бота в группу."""
    await message.answer(
        "Бот успешно добавлен в группу.\n\n"
        "Чтобы привязать этот чат к учетной записи селлера, отправьте команду:\n"
        "<code>/connect ВАШ_ID</code>\n"
        "(Ваш личный ID указан в личных сообщениях с ботом в разделе «Командный чат»)."
    )


@router.message(Command("connect"))
async def handle_connect_group(message: Message) -> None:
    """Привязка рабочей группы селлера через команду /connect."""
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer(
            "Данная команда отправляется внутри группы Telegram.\n"
            "Добавьте бота в группу и отправьте <code>/connect ВАШ_ID</code>."
        )
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(
            "Формат: <code>/connect ВАШ_ID</code>\n(ID указан в боте в разделе «Командный чат»)."
        )
        return

    owner_id = int(parts[1])
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        user = await repo.get_user(owner_id)
        if not user or not user.is_active:
            await message.answer(
                "Учетная запись селлера с указанным ID не найдена или деактивирована."
            )
            return

        await repo.link_team_chat(chat_id=owner_id, team_chat_id=message.chat.id)
        await session.commit()

    await message.answer(
        f"Рабочий чат успешно подключен к учетной записи <code>{owner_id}</code>.\n"
        f"Сюда будут поступать оперативные оповещения об изменении цен и остатков."
    )


# --- Команды администратора ---


async def render_admin_user_card(message: Message, target_chat_id: int) -> None:
    """Чистая отрисовка карточки управления селлером."""
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        user = await repo.get_user(target_chat_id)
        prod_count = await repo.count_user_products(target_chat_id)

    if not user:
        await message.edit_text("Пользователь не найден.")
        return

    status = "Активен" if user.is_active else "Отключен"
    uname = f"@{user.username}" if user.username else "Не указан"
    team = f"<code>{user.team_chat_id}</code>" if user.team_chat_id else "Не подключен"

    text = (
        f"Карточка клиента: <code>{user.telegram_chat_id}</code>\n\n"
        f"Юзернейм: {uname}\n"
        f"Статус: <b>{status}</b>\n"
        f"Тарифный лимит: <b>{user.max_products} позиций</b>\n"
        f"Активных товаров: <b>{prod_count} шт.</b>\n"
        f"Командный чат: {team}\n\n"
        "Управление лимитом и доступом:"
    )

    toggle_text = "Деактивировать доступ" if user.is_active else "Активировать доступ"
    toggle_data = (
        f"admin_revoke_{user.telegram_chat_id}"
        if user.is_active
        else f"admin_grant_{user.telegram_chat_id}"
    )

    buttons = [
        [
            InlineKeyboardButton(
                text="Лимит 10", callback_data=f"adm_lim_{user.telegram_chat_id}_10"
            ),
            InlineKeyboardButton(
                text="Лимит 25", callback_data=f"adm_lim_{user.telegram_chat_id}_25"
            ),
            InlineKeyboardButton(
                text="Лимит 50", callback_data=f"adm_lim_{user.telegram_chat_id}_50"
            ),
        ],
        [
            InlineKeyboardButton(
                text="Лимит 100", callback_data=f"adm_lim_{user.telegram_chat_id}_100"
            ),
            InlineKeyboardButton(
                text="Безлимит (500)",
                callback_data=f"adm_lim_{user.telegram_chat_id}_500",
            ),
        ],
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
        [
            InlineKeyboardButton(
                text="К списку пользователей", callback_data="adm_back_panel"
            )
        ],
    ]

    await message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def handle_admin_panel(message: Message) -> None:
    """Панель администрирования со списком пользователей и кнопками."""
    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if message.chat.id != admin_id:
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        # Синхронизируем профиль самого админа
        await repo.sync_user_profile(
            chat_id=message.chat.id,
            username=message.from_user.username if message.from_user else None,
            full_name=message.from_user.full_name if message.from_user else None,
        )
        await session.commit()

        users_overview = await repo.get_users_overview()
        metrics = await repo.get_system_metrics()

    stats_text = (
        "Панель администрирования\n\n"
        f"Пользователей: <b>{metrics['total_users']}</b> (активных: <b>{metrics['active_users']}</b>)\n"
        f"Позиций на мониторинге: <b>{metrics['total_products']}</b>\n"
        f"Снимков цен в базе: <b>{metrics['total_snapshots']}</b>\n\n"
        "Пользователи системы:\n"
    )

    lines = []
    buttons = []
    for u, prod_count in users_overview:
        # Строгое отображение @username (ID: ...)
        if u.username:
            user_display = f'<a href="https://t.me/{u.username}">@{u.username}</a>'
            btn_title = f"@{u.username}"
        elif u.full_name:
            user_display = (
                f'<a href="tg://user?id={u.telegram_chat_id}">{u.full_name}</a>'
            )
            btn_title = u.full_name[:18]
        else:
            user_display = f'<a href="tg://user?id={u.telegram_chat_id}">ID: {u.telegram_chat_id}</a>'
            btn_title = f"ID: {u.telegram_chat_id}"

        lines.append(f"{user_display} (ID: <code>{u.telegram_chat_id}</code>)")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Управление: {btn_title}",
                    callback_data=f"adm_user_{u.telegram_chat_id}",
                )
            ]
        )

    action_buttons = [
        [
            InlineKeyboardButton(
                text="⚡ Запустить опрос сейчас", callback_data="adm_force_run"
            ),
            InlineKeyboardButton(
                text="💾 Скачать бэкап БД", callback_data="adm_backup_db"
            ),
        ],
        [
            InlineKeyboardButton(
                text="⚠️ Очистить всю базу данных", callback_data="adm_wipe_confirm"
            ),
        ],
    ]

    full_text = stats_text + ("\n".join(lines) if lines else "Список пуст")
    await message.answer(
        full_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=action_buttons + buttons),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "adm_wipe_confirm")
async def handle_admin_wipe_confirm(callback: CallbackQuery) -> None:
    """Запрос подтверждения полной очистки базы данных."""
    await callback.answer()
    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if not callback.message or callback.message.chat.id != admin_id:
        return

    text = (
        "Внимание: полная очистка базы данных!\n\n"
        "Это действие безвозвратно сотрет всех пользователей, товары и историю замеров.\n\n"
        "Вы уверены?"
    )
    buttons = [
        [
            InlineKeyboardButton(
                text="Да, удалить все данные", callback_data="adm_wipe_execute"
            )
        ],
        [InlineKeyboardButton(text="Отмена", callback_data="adm_back_panel")],
    ]
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )


@router.callback_query(F.data == "adm_wipe_execute")
async def handle_admin_wipe_execute(callback: CallbackQuery) -> None:
    """Выполнение полной очистки базы данных."""
    await callback.answer("Очистка базы данных...")
    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if not callback.message or callback.message.chat.id != admin_id:
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.wipe_database()
        if admin_id:
            await repo.grant_user_access(chat_id=admin_id)
        await session.commit()

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "База данных успешно и полностью очищена.\nСтруктура пересоздана с нуля."
        )


@router.callback_query(F.data.startswith("adm_user_"))
async def handle_admin_user_card(callback: CallbackQuery) -> None:
    """Карточка управления селлером с мгновенным снятием подсветки кнопки."""
    await callback.answer()
    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if (
        not callback.message
        or callback.message.chat.id != admin_id
        or not callback.data
    ):
        return

    target_chat_id = int(callback.data.split("_")[2])
    if isinstance(callback.message, Message):
        await render_admin_user_card(callback.message, target_chat_id)


@router.callback_query(F.data.startswith("adm_lim_"))
async def handle_admin_change_limit(callback: CallbackQuery) -> None:
    """Изменение лимита товаров селлера админом без зависания кнопок."""
    await callback.answer()
    if (
        not callback.data
        or not callback.message
        or not isinstance(callback.message, Message)
    ):
        return

    parts = callback.data.split("_")
    target_chat_id = int(parts[2])
    new_limit = int(parts[3])

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.update_user_max_products(
            chat_id=target_chat_id, max_products=new_limit
        )
        await session.commit()

    if callback.bot:
        try:
            await callback.bot.send_message(
                chat_id=target_chat_id,
                text=f"Параметры вашей подписки обновлены.\nДоступный лимит отслеживаемых позиций: <b>{new_limit} шт.</b>",
            )
        except Exception:
            pass

    await render_admin_user_card(callback.message, target_chat_id)


@router.callback_query(F.data == "adm_back_panel")
async def handle_admin_back_to_panel(callback: CallbackQuery) -> None:
    """Возврат в панель администрирования со снятием подсветки кнопки."""
    await callback.answer()
    if callback.message and isinstance(callback.message, Message):
        await handle_admin_panel(callback.message)


@router.callback_query(F.data == "adm_backup_db")
async def handle_admin_backup_db(callback: CallbackQuery) -> None:
    """Отправка резервной копии базы данных администратору."""
    await callback.answer("Создание резервной копии...")
    if not callback.message:
        return

    try:
        import os
        from aiogram.types import FSInputFile

        db_path = "tracker.db"
        if os.path.exists(db_path):
            doc = FSInputFile(
                path=db_path,
                filename=f"tracker_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.db",
            )
            await callback.message.answer_document(
                document=doc,
                caption="Резервная копия базы данных SQLite (tracker.db)",
            )
        else:
            await callback.message.answer(
                "Файл базы данных tracker.db не найден на диске."
            )
    except Exception as exc:
        await callback.message.answer(f"Ошибка создания бэкапа: {exc}")


@router.callback_query(F.data == "adm_force_run")
async def handle_admin_force_run(callback: CallbackQuery) -> None:
    """Внеочередной принудительный запуск цикла мониторинга."""
    await callback.answer("Запуск внеочередной проверки...")
    if callback.message and isinstance(callback.message, Message):
        status_msg = await callback.message.answer(
            "Запущен внеочередной сбор данных по позициям..."
        )
        try:
            from parsers.ozon import get_shared_parser
            from alerts.telegram import TelegramNotifier

            parser = get_shared_parser()
            notifier = TelegramNotifier()
            async with async_session_factory() as session:
                tracker = TrackerService(session, parser=parser)
                results = await tracker.process_due_products()
                if results:
                    await notifier.send_batch_alerts(results)
            await status_msg.edit_text(
                f"Внеочередной цикл завершен. Проверено позиций: {len(results)}."
            )
        except Exception as exc:
            await status_msg.edit_text(f"Сбой выполнения цикла: {exc}")


@router.message(Command("pm"), F.chat.type == ChatType.PRIVATE)
async def handle_pm_command(message: Message) -> None:
    """Отправка личного сообщения селлеру от имени бота."""
    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if message.chat.id != admin_id:
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: <code>/pm ID_ПОЛЬЗОВАТЕЛЯ Текст сообщения</code>")
        return

    target_id = int(parts[1])
    msg_text = parts[2]

    if message.bot:
        try:
            await message.bot.send_message(
                chat_id=target_id,
                text=f"Сообщение от администрации сервиса:\n\n{msg_text}",
            )
            await message.answer(
                f"Сообщение успешно доставлено пользователю {target_id}."
            )
        except Exception as exc:
            await message.answer(
                f"Не удалось доставить сообщение пользователю {target_id}: {exc}"
            )


@router.message(Command("broadcast"), F.chat.type == ChatType.PRIVATE)
async def handle_broadcast_command(message: Message) -> None:
    """Рассылка системного сообщения пользователям."""
    admin_id = settings.ADMIN_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID
    if message.chat.id != admin_id:
        return

    text_to_send = (
        message.text.replace("/broadcast", "", 1).strip() if message.text else ""
    )
    if not text_to_send:
        await message.answer("Формат: <code>/broadcast Текст сообщения</code>")
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        users = await repo.get_all_users()

    sent_count = 0
    if message.bot:
        for u in users:
            if u.is_active:
                try:
                    await message.bot.send_message(
                        chat_id=u.telegram_chat_id,
                        text=f"Информационное сообщение:\n\n{text_to_send}",
                    )
                    sent_count += 1
                except Exception:
                    pass

    await message.answer(f"Сообщение доставлено {sent_count} пользователям.")


# --- Выгрузка Excel ---


@router.callback_query(F.data.startswith("excel_"))
async def handle_export_excel(callback: CallbackQuery) -> None:
    """Генерация и отправка отчета Excel."""
    await callback.answer("Формирование отчета...")
    if not callback.data or not callback.message:
        return

    sku = callback.data.split("_")[1]
    chat_id = callback.message.chat.id

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        excel_bytes, filename = await repo.export_product_history_excel(
            chat_id=chat_id, sku=sku
        )

    if not excel_bytes:
        await callback.message.answer("История замеров по данной позиции пуста.")
        return

    document = BufferedInputFile(
        file=excel_bytes,
        filename=filename,
    )
    await callback.message.answer_document(
        document=document,
        caption=f"Отчет истории цен: <code>{filename}</code>",
    )


# --- Пользовательские диалоги ---


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def handle_start(message: Message, state: FSMContext) -> None:
    """Стартовое меню с проверкой доступа."""
    await state.clear()
    if not await check_access(message):
        return

    await message.answer(
        "Сервис мониторинга цен конкурентов Ozon активен.\n\n"
        "Выберите необходимое действие в меню:",
        reply_markup=get_main_keyboard(),
    )


@router.message(F.text == "Добавить товар", F.chat.type == ChatType.PRIVATE)
async def handle_add_product_button(message: Message, state: FSMContext) -> None:
    """Запуск добавления товара."""
    if not await check_access(message):
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        user = await repo.get_user(message.chat.id)
        current_count = await repo.count_user_products(message.chat.id)

    max_allowed = user.max_products if user else 10
    if current_count >= max_allowed:
        admin_link = (
            f"@{settings.ADMIN_USERNAME}"
            if settings.ADMIN_USERNAME
            else "администратору"
        )
        await message.answer(
            f"Достигнут лимит активных позиций ({current_count} из {max_allowed}).\n"
            f"Для изменения параметров тарифа обратитесь к {admin_link}."
        )
        return

    await state.set_state(AddProductFSM.waiting_for_input)
    prompt_msg = await message.answer(
        "Отправьте ссылку на товар Ozon или числовой артикул (SKU):"
    )
    await state.update_data(prompt_msg_id=prompt_msg.message_id)


_last_menu_messages: dict[int, int] = {}


@router.message(F.text == "Мои товары", F.chat.type == ChatType.PRIVATE)
async def handle_list_products(message: Message) -> None:
    """Вывод списка позиций селлера."""
    if not await check_access(message):
        return

    prev_msg_id = _last_menu_messages.get(message.chat.id)
    if prev_msg_id and message.bot:
        try:
            await message.bot.delete_message(
                chat_id=message.chat.id, message_id=prev_msg_id
            )
        except Exception:
            pass

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        products = await repo.get_active_products(chat_id=message.chat.id)

    if not products:
        msg = await message.answer("Список отслеживаемых товаров пуст.")
        _last_menu_messages[message.chat.id] = msg.message_id
        return

    msg = await message.answer(
        "Отслеживаемые позиции:\n"
        "Выберите товар для просмотра деталей и экспорта отчета:",
        reply_markup=build_products_list_keyboard(products),
    )
    _last_menu_messages[message.chat.id] = msg.message_id


@router.callback_query(F.data == "back_to_list")
async def handle_back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к перечню товаров со снятием подсветки."""
    await callback.answer()
    await state.clear()
    msg = callback.message if isinstance(callback.message, Message) else None
    if not msg:
        return

    chat_id = msg.chat.id
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        products = await repo.get_active_products(chat_id=chat_id)

    if not products:
        await msg.edit_text("Список отслеживаемых товаров пуст.")
        return

    await msg.edit_text(
        "Отслеживаемые позиции:\n" "Выберите товар для просмотра деталей:",
        reply_markup=build_products_list_keyboard(products),
    )


@router.callback_query(F.data.startswith("view_"))
async def handle_view_product(callback: CallbackQuery, state: FSMContext) -> None:
    """Просмотр карточки товара."""
    await callback.answer()
    await state.clear()
    if not callback.data:
        return

    sku = callback.data.split("_")[1]
    msg = callback.message if isinstance(callback.message, Message) else None
    if not msg:
        return

    await render_product_card(message=msg, chat_id=msg.chat.id, sku=sku)


@router.callback_query(F.data.startswith("menu_thresh_"))
async def handle_menu_threshold(callback: CallbackQuery) -> None:
    """Меню изменения порога демпинга."""
    await callback.answer()
    if not callback.data:
        return
    sku = callback.data.split("_")[2]
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg:
        await msg.edit_text(
            "Настройка порога демпинга (%):\n"
            "Выберите процент снижения цены для оповещения:",
            reply_markup=get_threshold_inline_keyboard(sku=sku),
        )


@router.callback_query(F.data.startswith("menu_target_"))
async def handle_menu_target_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню настройки себестоимости."""
    await callback.answer()
    if not callback.data or not callback.message:
        return
    sku = callback.data.split("_")[2]
    await state.set_state(EditProductFSM.waiting_for_custom_threshold)
    await state.update_data(sku=sku, is_target_price_input=True)

    msg = callback.message if isinstance(callback.message, Message) else None
    if msg:
        await msg.edit_text(
            "Настройка минимальной цены (порог себестоимости):\n\n"
            "При падении цены конкурента ниже данного значения будет отправлено экстренное уведомление.\n\n"
            "Введите сумму в рублях (например: <code>1200</code>) или <code>0</code> для отключения:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад", callback_data=f"view_{sku}")]
                ]
            ),
        )


@router.callback_query(F.data.startswith("menu_interval_"))
async def handle_menu_interval(callback: CallbackQuery) -> None:
    """Меню настройки периодичности замера."""
    await callback.answer()
    if not callback.data:
        return
    sku = callback.data.split("_")[2]
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg:
        await msg.edit_text(
            "Интервал мониторинга позиции:\n" "Укажите периодичность проверки:",
            reply_markup=get_product_interval_inline_keyboard(sku=sku),
        )


@router.callback_query(F.data.startswith("ethresh_"))
async def handle_edit_threshold_choice(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Выбор порога демпинга."""
    await callback.answer()
    if not callback.data:
        return
    parts = callback.data.split("_")
    sku = parts[1]
    choice = parts[2]
    msg = callback.message if isinstance(callback.message, Message) else None
    if not msg:
        return

    chat_id = msg.chat.id

    if choice == "custom":
        await state.set_state(EditProductFSM.waiting_for_custom_threshold)
        await state.update_data(
            sku=sku, origin_msg_id=msg.message_id, is_target_price_input=False
        )
        prompt = await callback.message.answer("Введите порог демпинга в процентах (от 1 до 100):")  # type: ignore
        await state.update_data(prompt_msg_id=prompt.message_id)
        return

    new_threshold = float(choice)
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.update_product_threshold(
            chat_id=chat_id, sku=sku, threshold=new_threshold
        )
        await session.commit()

    await render_product_card(message=msg, chat_id=chat_id, sku=sku)


@router.message(EditProductFSM.waiting_for_custom_threshold)
async def process_custom_threshold_text(message: Message, state: FSMContext) -> None:
    """Обработка ввода числового значения порога или себестоимости."""
    data = await state.get_data()
    sku = data["sku"]
    prompt_msg_id = data.get("prompt_msg_id")
    is_target_input = data.get("is_target_price_input", False)

    await safe_delete_message(message)
    await safe_delete_by_id(message, prompt_msg_id)

    raw_text = (
        (message.text or "").replace(",", ".").replace("₽", "").replace("%", "").strip()
    )
    try:
        val = float(raw_text)
        if val < 0:
            raise ValueError
    except ValueError:
        return

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        if is_target_input:
            target_val = Decimal(str(val)) if val > 0 else None
            await repo.update_product_target_price(
                chat_id=message.chat.id, sku=sku, target_price=target_val
            )
            confirm_text = (
                f"Минимальная цена установлена: <b>{val:,.0f} ₽</b>".replace(",", " ")
                if val > 0
                else "Минимальная цена отключена"
            )
        else:
            await repo.update_product_threshold(
                chat_id=message.chat.id, sku=sku, threshold=val
            )
            confirm_text = f"Порог демпинга обновлен: <b>{val}%</b>"
        await session.commit()

    await state.clear()
    sent_confirm = await message.answer(confirm_text)
    await asyncio.sleep(2)
    await safe_delete_message(sent_confirm)


@router.callback_query(F.data.startswith("eint_"))
async def handle_edit_interval_choice(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Выбор интервала опроса позиции."""
    await callback.answer()
    if not callback.data:
        return
    parts = callback.data.split("_")
    sku = parts[1]
    choice = parts[2]
    msg = callback.message if isinstance(callback.message, Message) else None
    if not msg:
        return

    chat_id = msg.chat.id

    if choice == "custom":
        await state.set_state(EditProductFSM.waiting_for_custom_interval)
        await state.update_data(sku=sku, origin_msg_id=msg.message_id)
        prompt = await callback.message.answer("Укажите интервал проверки в минутах:")  # type: ignore
        await state.update_data(prompt_msg_id=prompt.message_id)
        return

    new_interval = int(choice)
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.update_product_interval(
            chat_id=chat_id, sku=sku, minutes=new_interval
        )
        await session.commit()

    await render_product_card(message=msg, chat_id=chat_id, sku=sku)


@router.message(EditProductFSM.waiting_for_custom_interval)
async def process_custom_interval_text(message: Message, state: FSMContext) -> None:
    """Обработка ввода интервала в минутах."""
    data = await state.get_data()
    sku = data["sku"]
    prompt_msg_id = data.get("prompt_msg_id")

    await safe_delete_message(message)
    await safe_delete_by_id(message, prompt_msg_id)

    raw_text = (message.text or "").strip()
    if not raw_text.isdigit() or int(raw_text) < 1:
        return

    val = int(raw_text)
    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.update_product_interval(
            chat_id=message.chat.id, sku=sku, minutes=val
        )
        await session.commit()

    await state.clear()
    sent_confirm = await message.answer(f"Интервал обновлен: <b>{val} мин.</b>")
    await asyncio.sleep(2)
    await safe_delete_message(sent_confirm)


@router.callback_query(F.data.startswith("del_"))
async def handle_delete_product(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление позиции из мониторинга."""
    await callback.answer("Товар удален из мониторинга", show_alert=True)
    if not callback.data:
        return

    sku = callback.data.split("_")[1]
    msg = callback.message if isinstance(callback.message, Message) else None
    chat_id = msg.chat.id if msg else 0

    async with async_session_factory() as session:
        repo = ProductRepository(session)
        await repo.delete_by_sku(chat_id=chat_id, sku=sku)
        await session.commit()

    await handle_back_to_list(callback, state)


@router.message(AddProductFSM.waiting_for_input)
async def process_product_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода ссылки или SKU."""
    if not await check_access(message):
        return

    data = await state.get_data()
    prompt_id = data.get("prompt_msg_id")

    await safe_delete_message(message)
    await safe_delete_by_id(message, prompt_id)

    raw_text = (message.text or "").strip()
    sku = await extract_sku_from_text(raw_text)

    if not sku:
        err = await message.answer(
            "Не удалось идентифицировать артикул. Отправьте ссылку или SKU:"
        )
        await state.update_data(prompt_msg_id=err.message_id)
        return

    await state.update_data(sku=sku)
    await state.set_state(AddProductFSM.waiting_for_threshold)

    thresh_msg = await message.answer(
        f"Артикул <code>{sku}</code> идентифицирован.\n\n"
        "Укажите порог фиксации демпинга (%):",
        reply_markup=get_threshold_inline_keyboard(),
    )
    await state.update_data(prompt_msg_id=thresh_msg.message_id)


@router.callback_query(
    AddProductFSM.waiting_for_threshold, F.data.startswith("thresh_")
)
async def process_threshold_callback(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Выбор порога демпинга при добавлении."""
    await callback.answer()
    if not callback.data:
        return

    choice = callback.data.split("_")[1]
    msg = callback.message if isinstance(callback.message, Message) else None

    if choice == "custom":
        if msg:
            await msg.edit_text("Введите порог демпинга в процентах (от 1 до 100):")
        return

    threshold_val = float(choice)
    if msg:
        await _finalize_add_product(msg, state, threshold_val)


@router.message(AddProductFSM.waiting_for_threshold)
async def process_threshold_text(message: Message, state: FSMContext) -> None:
    """Ввод порога демпинга текстом."""
    data = await state.get_data()
    prompt_id = data.get("prompt_msg_id")

    await safe_delete_message(message)
    await safe_delete_by_id(message, prompt_id)

    raw_text = (message.text or "").replace(",", ".").replace("%", "").strip()
    try:
        threshold_val = float(raw_text)
        if threshold_val <= 0 or threshold_val > 100:
            raise ValueError
    except ValueError:
        return

    status_msg = await message.answer("Сбор первичных метрик с Ozon...")
    await _finalize_add_product(status_msg, state, threshold_val)


async def _finalize_add_product(
    message: Message, state: FSMContext, threshold: float
) -> None:
    """Сохранение позиции и первичная инициализация карточки."""
    data = await state.get_data()
    sku = data["sku"]
    await state.clear()

    await message.edit_text(f"Инициализация артикула {sku}...")

    async with async_session_factory() as session:
        tracker = TrackerService(session)
        result = await tracker.process_sku(
            chat_id=message.chat.id,
            sku=sku,
            dumping_threshold=threshold,
            check_interval_minutes=30,
        )

    if not result:
        await message.edit_text(
            "Не удалось получить данные по указанной позиции. Попробуйте позже."
        )
        return

    card_str = (
        f" ({result.current_card_price:,.0f} ₽ по карте)".replace(",", " ")
        if result.current_card_price
        else ""
    )
    await message.edit_text(
        f"Позиция успешно добавлена на мониторинг.\n\n"
        f'Товар: <a href="{result.url}">{result.title}</a>\n'
        f"Актуальная цена: <b>{result.current_price:,.0f} ₽</b>{card_str}\n".replace(
            ",", " "
        )
        + f"Порог демпинга: <b>{threshold}%</b>\n"
        f"Интервал опроса: <b>30 мин.</b>\n\n"
        f"Уведомления будут поступать при изменении параметров.",
        disable_web_page_preview=False,
    )
