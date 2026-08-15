import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from FunPayAPI.common.exceptions import RequestFailedError, UnauthorizedError

from funpay_service import (
    BalanceUnavailableError,
    FunPayService,
    NotificationManager,
    RaiseOutcome,
    next_raise_delay,
)
from proxy_utils import ProxyFormatError, normalize_proxy
from storage import Database, SecretCipher, StoredSecretError, read_credentials

logger = logging.getLogger(__name__)
router = Router()
PAGE_SIZE = 6


class Setup(StatesGroup):
    waiting_proxy = State()
    waiting_golden_key = State()


class Reply(StatesGroup):
    waiting_text = State()


class Greeting(StatesGroup):
    waiting_text = State()


class LotEdit(StatesGroup):
    waiting_value = State()


class ConfigurationError(RuntimeError):
    pass


def status_icon(enabled: bool) -> str:
    return "✅" if enabled else "❌"


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
                InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            ],
            [InlineKeyboardButton(text="📋 Мои объявления", callback_data="lots:0")],
            [
                InlineKeyboardButton(
                    text="🔔 Уведомления", callback_data="notifications"
                ),
                InlineKeyboardButton(text="⬆️ Автоподнятие", callback_data="autoraise"),
            ],
            [InlineKeyboardButton(text="👋 Приветствие", callback_data="greeting")],
            [
                InlineKeyboardButton(
                    text="🔄 Сменить аккаунт / прокси", callback_data="reconnect"
                )
            ],
        ]
    )


def lots_keyboard(page: int, pages: int, visible_lots: list) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"lots:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop")
    )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(text="➡️", callback_data=f"lots:{page + 1}")
        )
    rows = [
        [
            InlineKeyboardButton(
                text=f"✏️ Редактировать #{lot.id}", callback_data=f"lot_edit:{lot.id}"
            )
        ]
        for lot in visible_lots
    ]
    rows.extend(
        [navigation, [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notifications_keyboard(row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{status_icon(row['message_notifications_enabled'])} Новые сообщения",
                    callback_data="notify:messages",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{status_icon(row['order_notifications_enabled'])} Новые заказы",
                    callback_data="notify:orders",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧪 Проверить мониторинг",
                    callback_data="notifications:check",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить статус", callback_data="notifications"
                )
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def autoraise_keyboard(row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{status_icon(row['auto_raise_enabled'])} Автоподнятие",
                    callback_data="autoraise:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{status_icon(row['raise_notifications_enabled'])} Уведомлять о поднятии",
                    callback_data="autoraise:notify",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬆️ Поднять сейчас", callback_data="autoraise:now"
                )
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def notifications_text(row) -> str:
    last_success = row["monitor_last_success_at"]
    if last_success:
        monitor_status = (
            "✅ работает, последняя проверка: "
            + last_success.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
        )
    elif row["monitor_last_error"]:
        monitor_status = "❌ ошибка: " + html.escape(row["monitor_last_error"])
    else:
        monitor_status = "⏳ ещё не запускался после обновления"
    return (
        "🔔 <b>Уведомления FunPay</b>\n\n"
        f"Новые сообщения: <b>{'включены' if row['message_notifications_enabled'] else 'выключены'}</b>\n"
        f"Новые заказы: <b>{'включены' if row['order_notifications_enabled'] else 'выключены'}</b>\n"
        f"Мониторинг: <b>{monitor_status}</b>\n\n"
        "В уведомлении о сообщении показывается ник собеседника и кнопка быстрого ответа."
    )


def autoraise_text(row) -> str:
    next_raise = row["next_raise_at"]
    if row["auto_raise_enabled"] and next_raise:
        next_text = next_raise.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    elif row["auto_raise_enabled"]:
        next_text = "при ближайшей проверке"
    else:
        next_text = "—"
    return (
        "⬆️ <b>Автоподнятие лотов</b>\n\n"
        f"Автоподнятие: <b>{'включено' if row['auto_raise_enabled'] else 'выключено'}</b>\n"
        f"Уведомления о поднятии: <b>{'включены' if row['raise_notifications_enabled'] else 'выключены'}</b>\n"
        f"Следующая попытка: <b>{next_text}</b>\n\n"
        "Поднимаются стандартные лоты во всех найденных категориях. "
        "Если FunPay вернёт таймер ожидания, следующая попытка будет назначена по нему."
    )


def greeting_keyboard(row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{status_icon(row['greeting_enabled'])} Автоприветствие",
                    callback_data="greeting:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить текст", callback_data="greeting:edit"
                )
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def greeting_text(row) -> str:
    text = html.escape(row["greeting_text"])
    return (
        "👋 <b>Приветствие новых клиентов</b>\n\n"
        f"Статус: <b>{'включено' if row['greeting_enabled'] else 'выключено'}</b>\n\n"
        f"Текст:\n<blockquote>{text}</blockquote>\n\n"
        "Приветствие отправляется один раз — после первого сообщения нового клиента."
    )


def lot_edit_keyboard(lot_id: int, fields) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Название RU", callback_data=f"lot_field:title_ru:{lot_id}"
                ),
                InlineKeyboardButton(
                    text="Название EN", callback_data=f"lot_field:title_en:{lot_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Описание RU",
                    callback_data=f"lot_field:description_ru:{lot_id}",
                ),
                InlineKeyboardButton(
                    text="Описание EN",
                    callback_data=f"lot_field:description_en:{lot_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Цена", callback_data=f"lot_field:price:{lot_id}"
                ),
                InlineKeyboardButton(
                    text="📦 Количество", callback_data=f"lot_field:amount:{lot_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅' if fields.active else '❌'} Активность",
                    callback_data=f"lot_toggle:active:{lot_id}",
                ),
                InlineKeyboardButton(
                    text=f"{'✅' if fields.deactivate_after_sale else '❌'} После продажи",
                    callback_data=f"lot_toggle:deactivate:{lot_id}",
                ),
            ],
            [InlineKeyboardButton(text="📋 К объявлениям", callback_data="lots:0")],
        ]
    )


def lot_edit_text(fields) -> str:
    def shortened(value: str | None, limit: int = 350) -> str:
        value = (value or "—").strip()
        return value if len(value) <= limit else value[: limit - 1] + "…"

    amount = "не указано" if fields.amount is None else str(fields.amount)
    price = "не указана" if fields.price is None else f"{fields.price:,.2f} ₽"
    return (
        f"✏️ <b>Редактирование объявления #{fields.lot_id}</b>\n\n"
        f"Название RU: <b>{html.escape(shortened(fields.title_ru, 180))}</b>\n"
        f"Название EN: <b>{html.escape(shortened(fields.title_en, 180))}</b>\n"
        f"Описание RU: {html.escape(shortened(fields.description_ru))}\n"
        f"Описание EN: {html.escape(shortened(fields.description_en))}\n"
        f"Цена: <b>{price}</b>\n"
        f"Количество: <b>{amount}</b>\n"
        f"Активно: <b>{'да' if fields.active else 'нет'}</b>\n"
        f"Отключать после продажи: <b>{'да' if fields.deactivate_after_sale else 'нет'}</b>\n\n"
        "Каждое изменение сохраняется сразу в FunPay."
    )


async def delete_sensitive_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def prompt_proxy(message: Message, state: FSMContext) -> None:
    await state.set_state(Setup.waiting_proxy)
    await message.answer(
        "🔐 <b>Подключение FunPay</b>\n\n"
        "Отправьте прокси одним сообщением. Поддерживаются форматы:\n"
        "<code>host:port</code>\n"
        "<code>host:port:user:password</code>\n"
        "<code>http://user:password@host:port</code>\n"
        "<code>socks5://user:password@host:port</code>\n\n"
        "Сообщение с прокси будет удалено после обработки."
    )


async def show_menu(message: Message, username: str | None = None) -> None:
    greeting = f", <b>{html.escape(username)}</b>" if username else ""
    await message.answer(
        f"✅ FunPay подключён{greeting}. Выберите действие:",
        reply_markup=menu_keyboard(),
    )


async def load_account(
    telegram_id: int, database: Database, cipher: SecretCipher, funpay: FunPayService
):
    credentials = await read_credentials(telegram_id, database, cipher)
    if not credentials:
        raise StoredSecretError("Аккаунт ещё не подключён.")
    return await funpay.account(credentials.golden_key, credentials.proxy_url)


async def report_funpay_error(
    target: Message,
    telegram_id: int,
    error: Exception,
    database: Database,
    state: FSMContext | None = None,
) -> None:
    if isinstance(error, UnauthorizedError):
        await database.clear_key(telegram_id)
        if state:
            await state.set_state(Setup.waiting_golden_key)
        await target.answer(
            "❌ GOLDEN_KEY недействителен или сессия FunPay завершена. Отправьте новый GOLDEN_KEY."
        )
        return
    if isinstance(error, BalanceUnavailableError):
        await target.answer(
            "❌ FunPay открыл профиль, но не отдал блок баланса. "
            "Попробуйте создать или активировать хотя бы одно объявление и повторить проверку."
        )
        return
    if isinstance(error, StoredSecretError):
        await target.answer(
            f"❌ {html.escape(str(error))} Используйте /reset для переподключения."
        )
        return
    if isinstance(error, (requests.RequestException, RequestFailedError, TimeoutError)):
        await target.answer(
            "❌ FunPay не ответил через сохранённый прокси. Проверьте прокси или используйте /reset."
        )
        return
    logger.exception("Unexpected FunPay request failure", exc_info=error)
    await target.answer(
        "❌ Не удалось получить данные FunPay. Попробуйте ещё раз позже."
    )


def format_raise_results(outcomes: list[RaiseOutcome]) -> str:
    if not outcomes:
        return "📭 Стандартные лоты для поднятия не найдены. Валютные лоты FunPay поднимать не позволяет."
    lines = ["⬆️ <b>Результат поднятия</b>\n"]
    for item in outcomes:
        name = html.escape(item.category_name)
        if item.raised:
            lines.append(f"✅ {name}: подняты")
        elif item.wait_seconds:
            minutes = max(1, (item.wait_seconds + 59) // 60)
            lines.append(f"⏳ {name}: повтор через ~{minutes} мин.")
        else:
            lines.append(f"❌ {name}: не удалось поднять")
    return "\n".join(lines)


@router.message(CommandStart())
async def command_start(
    message: Message, state: FSMContext, database: Database
) -> None:
    await state.clear()
    row = await database.get_user(message.from_user.id)
    if row and row["encrypted_proxy"] and row["encrypted_golden_key"]:
        await show_menu(message, row["funpay_username"])
    elif row and row["encrypted_proxy"]:
        await state.set_state(Setup.waiting_golden_key)
        await message.answer(
            "Прокси уже сохранён. Отправьте <b>GOLDEN_KEY</b> одним сообщением.\n\n"
            "Сообщение будет удалено сразу после обработки."
        )
    else:
        await prompt_proxy(message, state)


@router.message(Command("reset"))
async def command_reset(
    message: Message, state: FSMContext, database: Database
) -> None:
    await database.delete_user(message.from_user.id)
    await state.clear()
    await message.answer("Сохранённое подключение и настройки удалены.")
    await prompt_proxy(message, state)


@router.message(Setup.waiting_proxy, F.text)
async def receive_proxy(
    message: Message,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    raw_proxy = message.text.strip()
    await delete_sensitive_message(message)
    status = await message.answer("⏳ Проверяю прокси…")
    try:
        proxy_url = normalize_proxy(raw_proxy)
        await funpay.check_proxy(proxy_url)
    except ProxyFormatError as error:
        await status.edit_text(
            f"❌ {html.escape(str(error))}\n\nОтправьте прокси ещё раз."
        )
        return
    except (requests.RequestException, asyncio.TimeoutError):
        await status.edit_text(
            "❌ Прокси не отвечает или не может открыть FunPay. Проверьте данные и отправьте его ещё раз."
        )
        return

    await database.save_proxy(
        message.from_user.id, message.from_user.username, cipher.encrypt(proxy_url)
    )
    await state.set_state(Setup.waiting_golden_key)
    await status.edit_text(
        "✅ Прокси работает.\n\n"
        "Теперь отправьте <b>GOLDEN_KEY</b> одним сообщением. "
        "Его можно взять из cookie авторизованной сессии FunPay.\n\n"
        "Сообщение будет удалено сразу после обработки."
    )


@router.message(Setup.waiting_golden_key, F.text)
async def receive_golden_key(
    message: Message,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    golden_key = message.text.strip()
    await delete_sensitive_message(message)
    status = await message.answer("⏳ Проверяю GOLDEN_KEY…")
    if not 16 <= len(golden_key) <= 512 or any(char.isspace() for char in golden_key):
        await status.edit_text(
            "❌ GOLDEN_KEY выглядит некорректно. Отправьте ключ ещё раз."
        )
        return

    row = await database.get_user(message.from_user.id)
    if not row or not row["encrypted_proxy"]:
        await status.edit_text("Сначала нужно добавить прокси. Используйте /reset.")
        await state.clear()
        return

    try:
        proxy_url = cipher.decrypt(row["encrypted_proxy"])
        account = await funpay.account(golden_key, proxy_url)
    except UnauthorizedError:
        await status.edit_text(
            "❌ FunPay отклонил GOLDEN_KEY. Проверьте ключ и отправьте его ещё раз."
        )
        return
    except (requests.RequestException, RequestFailedError, StoredSecretError):
        await status.edit_text(
            "❌ Не удалось подключиться к FunPay через сохранённый прокси. Используйте /reset и проверьте данные."
        )
        return
    except Exception as error:
        logger.exception("Golden key validation failed", exc_info=error)
        await status.edit_text(
            "❌ Не удалось проверить аккаунт. Попробуйте ещё раз позже."
        )
        return

    await database.save_account(
        message.from_user.id,
        message.from_user.username,
        cipher.encrypt(golden_key),
        account.id,
        account.username,
    )
    await state.clear()
    await status.edit_text(
        f"✅ Аккаунт <b>{html.escape(account.username)}</b> подключён. "
        "Уведомления о новых сообщениях и заказах включены.",
        reply_markup=menu_keyboard(),
    )


@router.callback_query(F.data == "menu")
async def callback_menu(
    callback: CallbackQuery, state: FSMContext, database: Database
) -> None:
    await callback.answer()
    await state.clear()
    row = await database.get_user(callback.from_user.id)
    username = row["funpay_username"] if row else None
    await callback.message.edit_text(
        f"✅ FunPay подключён{f', <b>{html.escape(username)}</b>' if username else ''}. "
        "Выберите действие:",
        reply_markup=menu_keyboard(),
    )


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "reconnect")
async def callback_reconnect(
    callback: CallbackQuery, state: FSMContext, database: Database
) -> None:
    await callback.answer()
    await database.delete_user(callback.from_user.id)
    await state.clear()
    await prompt_proxy(callback.message, state)


@router.callback_query(F.data == "balance")
async def callback_balance(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Получаю баланс…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    try:
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        balance = await funpay.balance(account)
        text = (
            "💰 <b>Баланс FunPay</b>\n\n"
            f"🇷🇺 RUB: <b>{balance.total_rub:,.2f} ₽</b> "
            f"(доступно {balance.available_rub:,.2f} ₽)\n"
            f"🇺🇸 USD: <b>${balance.total_usd:,.2f}</b> "
            f"(доступно ${balance.available_usd:,.2f})\n"
            f"🇪🇺 EUR: <b>€{balance.total_eur:,.2f}</b> "
            f"(доступно €{balance.available_eur:,.2f})"
        )
        await callback.message.answer(text, reply_markup=menu_keyboard())
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data == "profile")
async def callback_profile(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Получаю профиль…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    try:
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        profile = await funpay.profile(account)
        lots_count = len(profile.get_lots())
        text = (
            "👤 <b>Профиль FunPay</b>\n\n"
            f"Никнейм: <b>{html.escape(profile.username)}</b>\n"
            f"ID: <code>{profile.id}</code>\n"
            f"Статус: {'🟢 онлайн' if profile.online else '⚪️ офлайн'}\n"
            f"Блокировка: {'🔴 да' if profile.banned else '🟢 нет'}\n"
            f"Активные продажи: <b>{account.active_sales}</b>\n"
            f"Активные покупки: <b>{account.active_purchases}</b>\n"
            f"Объявлений: <b>{lots_count}</b>\n"
            f'Фото: <a href="{html.escape(profile.profile_photo, quote=True)}">открыть</a>\n'
            f'Профиль: <a href="https://funpay.com/users/{profile.id}/">FunPay</a>'
        )
        await callback.message.answer(
            text, reply_markup=menu_keyboard(), disable_web_page_preview=True
        )
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data.startswith("lots:"))
async def callback_lots(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Получаю объявления…")
    try:
        requested_page = max(0, int(callback.data.split(":", 1)[1]))
    except (ValueError, IndexError):
        requested_page = 0
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)

    try:
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        profile = await funpay.profile(account)
        lots = profile.get_lots()
        if not lots:
            await callback.message.answer(
                "📭 У аккаунта нет опубликованных объявлений.",
                reply_markup=menu_keyboard(),
            )
            return

        pages = (len(lots) + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(requested_page, pages - 1)
        chunks = [
            f"📋 <b>Объявления {html.escape(profile.username)}</b> — {len(lots)} шт.\n"
        ]
        visible_lots = lots[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        for index, lot in enumerate(visible_lots, start=page * PAGE_SIZE + 1):
            category = getattr(
                getattr(lot, "subcategory", None), "fullname", "Без категории"
            )
            description = (lot.description or "Без названия").strip()
            if len(description) > 180:
                description = description[:177] + "…"
            server = f" · {html.escape(lot.server)}" if lot.server else ""
            chunks.append(
                f"\n<b>{index}. {html.escape(description)}</b>\n"
                f"{html.escape(category)}{server}\n"
                f"Цена: <b>{lot.price:,.2f} ₽</b> · "
                f'<a href="{html.escape(lot.public_link, quote=True)}">открыть</a>'
            )
        text = "\n".join(chunks)
        keyboard = lots_keyboard(page, pages, visible_lots)
        if callback.message.text and callback.message.text.startswith("📋"):
            await callback.message.edit_text(
                text, reply_markup=keyboard, disable_web_page_preview=True
            )
        else:
            await callback.message.answer(
                text, reply_markup=keyboard, disable_web_page_preview=True
            )
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data.startswith("lot_edit:"))
async def callback_lot_edit(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Загружаю объявление…")
    try:
        lot_id = int(callback.data.rsplit(":", 1)[1])
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        fields = await funpay.lot_fields(account, lot_id)
        await state.clear()
        await callback.message.answer(
            lot_edit_text(fields), reply_markup=lot_edit_keyboard(lot_id, fields)
        )
    except (ValueError, IndexError):
        await callback.message.answer("❌ Некорректный ID объявления.")
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data.startswith("lot_field:"))
async def callback_lot_field(callback: CallbackQuery, state: FSMContext) -> None:
    prompts = {
        "title_ru": "Введите новое название на русском.",
        "title_en": "Введите новое название на английском.",
        "description_ru": "Введите новое описание на русском.",
        "description_en": "Введите новое описание на английском.",
        "price": "Введите новую цену, например: 199.90",
        "amount": "Введите количество целым числом или «-», чтобы очистить.",
    }
    try:
        _, field, lot_id_text = callback.data.split(":", 2)
        lot_id = int(lot_id_text)
        prompt = prompts[field]
    except (ValueError, KeyError):
        await callback.answer("Некорректное поле", show_alert=True)
        return
    await state.set_state(LotEdit.waiting_value)
    await state.update_data(lot_id=lot_id, lot_field=field)
    await callback.answer()
    await callback.message.answer(
        f"✏️ {prompt}\n\nДля текстового поля отправьте <code>-</code>, чтобы очистить его.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Отмена", callback_data=f"lot_edit:{lot_id}"
                    )
                ]
            ]
        ),
    )


@router.message(LotEdit.waiting_value, F.text)
async def receive_lot_value(
    message: Message,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    data = await state.get_data()
    lot_id = data.get("lot_id")
    field = data.get("lot_field")
    value = message.text.strip()
    if not lot_id or field not in {
        "title_ru",
        "title_en",
        "description_ru",
        "description_en",
        "price",
        "amount",
    }:
        await state.clear()
        await message.answer(
            "Контекст редактирования потерян. Откройте объявление заново."
        )
        return

    try:
        if field in {"title_ru", "title_en"} and len(value) > 200:
            raise ValueError("Название не должно быть длиннее 200 символов.")
        if field in {"description_ru", "description_en"} and len(value) > 10000:
            raise ValueError("Описание не должно быть длиннее 10 000 символов.")

        account = await load_account(message.from_user.id, database, cipher, funpay)
        fields = await funpay.lot_fields(account, int(lot_id))
        if field == "price":
            try:
                price = float(value.replace(",", "."))
            except ValueError as error:
                raise ValueError("Введите цену числом, например 199.90.") from error
            if price <= 0:
                raise ValueError("Цена должна быть больше нуля.")
            fields.price = price
        elif field == "amount":
            if value == "-":
                fields.amount = None
            else:
                try:
                    amount = int(value)
                except ValueError as error:
                    raise ValueError("Введите количество целым числом.") from error
                if amount < 0:
                    raise ValueError("Количество не может быть отрицательным.")
                fields.amount = amount
        else:
            setattr(fields, field, "" if value == "-" else value)

        await funpay.save_lot(account, fields)
        fields = await funpay.lot_fields(account, int(lot_id))
        await state.clear()
        await message.answer(
            "✅ Изменение сохранено в FunPay.\n\n" + lot_edit_text(fields),
            reply_markup=lot_edit_keyboard(int(lot_id), fields),
        )
    except ValueError as error:
        await message.answer(f"❌ {html.escape(str(error))} Попробуйте ещё раз.")
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(message, message.from_user.id, error, database, state)


@router.callback_query(F.data.startswith("lot_toggle:"))
async def callback_lot_toggle(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    try:
        _, toggle, lot_id_text = callback.data.split(":", 2)
        lot_id = int(lot_id_text)
        if toggle not in {"active", "deactivate"}:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    await callback.answer("Сохраняю…")
    try:
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        fields = await funpay.lot_fields(account, lot_id)
        if toggle == "active":
            fields.active = not fields.active
        else:
            fields.deactivate_after_sale = not fields.deactivate_after_sale
        await funpay.save_lot(account, fields)
        fields = await funpay.lot_fields(account, lot_id)
        await callback.message.edit_text(
            lot_edit_text(fields), reply_markup=lot_edit_keyboard(lot_id, fields)
        )
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data == "notifications")
async def callback_notifications(callback: CallbackQuery, database: Database) -> None:
    await callback.answer()
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        notifications_text(row), reply_markup=notifications_keyboard(row)
    )


@router.callback_query(F.data == "notifications:check")
async def callback_check_notifications(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Проверяю FunPay…")
    try:
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        chats, histories = await funpay.chat_histories(account)
        await database.mark_monitor_success(callback.from_user.id)
        row = await database.get_user(callback.from_user.id)
        message_count = sum(len(messages) for messages in histories.values())
        await callback.message.edit_text(
            notifications_text(row)
            + "\n\n"
            + f"🧪 Доступно чатов: <b>{len(chats)}</b>, получено сообщений для проверки: <b>{message_count}</b>.",
            reply_markup=notifications_keyboard(row),
        )
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await database.mark_monitor_error(
            callback.from_user.id, type(error).__name__
        )
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data == "greeting")
async def callback_greeting(
    callback: CallbackQuery, state: FSMContext, database: Database
) -> None:
    await callback.answer()
    await state.clear()
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        greeting_text(row), reply_markup=greeting_keyboard(row)
    )


@router.callback_query(F.data == "greeting:toggle")
async def callback_greeting_toggle(callback: CallbackQuery, database: Database) -> None:
    row = await database.get_user(callback.from_user.id)
    enabled = not row["greeting_enabled"]
    await database.set_setting(callback.from_user.id, "greeting_enabled", enabled)
    await callback.answer(
        "Приветствие включено" if enabled else "Приветствие выключено"
    )
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        greeting_text(row), reply_markup=greeting_keyboard(row)
    )


@router.callback_query(F.data == "greeting:edit")
async def callback_greeting_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Greeting.waiting_text)
    await callback.answer()
    await callback.message.answer(
        "✏️ Отправьте новый текст приветствия (до 1000 символов).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отмена", callback_data="greeting")]
            ]
        ),
    )


@router.message(Greeting.waiting_text, F.text)
async def receive_greeting_text(
    message: Message, state: FSMContext, database: Database
) -> None:
    value = message.text.strip()
    if not value:
        await message.answer("Приветствие не может быть пустым.")
        return
    if len(value) > 1000:
        await message.answer("Приветствие слишком длинное. Максимум — 1000 символов.")
        return
    await database.set_greeting_text(message.from_user.id, value)
    await state.clear()
    row = await database.get_user(message.from_user.id)
    await message.answer(
        "✅ Приветствие сохранено.\n\n" + greeting_text(row),
        reply_markup=greeting_keyboard(row),
    )


@router.callback_query(F.data.startswith("notify:"))
async def callback_toggle_notification(
    callback: CallbackQuery, database: Database
) -> None:
    await callback.answer()
    row = await database.get_user(callback.from_user.id)
    setting = (
        "message_notifications_enabled"
        if callback.data == "notify:messages"
        else "order_notifications_enabled"
    )
    await database.set_setting(callback.from_user.id, setting, not row[setting])
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        notifications_text(row), reply_markup=notifications_keyboard(row)
    )


@router.callback_query(F.data == "autoraise")
async def callback_autoraise(callback: CallbackQuery, database: Database) -> None:
    await callback.answer()
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        autoraise_text(row), reply_markup=autoraise_keyboard(row)
    )


@router.callback_query(F.data == "autoraise:toggle")
async def callback_toggle_autoraise(
    callback: CallbackQuery, database: Database
) -> None:
    row = await database.get_user(callback.from_user.id)
    enabled = not row["auto_raise_enabled"]
    await database.set_setting(callback.from_user.id, "auto_raise_enabled", enabled)
    await callback.answer(
        "Автоподнятие включено" if enabled else "Автоподнятие выключено"
    )
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        autoraise_text(row), reply_markup=autoraise_keyboard(row)
    )


@router.callback_query(F.data == "autoraise:notify")
async def callback_toggle_raise_notifications(
    callback: CallbackQuery, database: Database
) -> None:
    row = await database.get_user(callback.from_user.id)
    enabled = not row["raise_notifications_enabled"]
    await database.set_setting(
        callback.from_user.id, "raise_notifications_enabled", enabled
    )
    await callback.answer(
        "Уведомления включены" if enabled else "Уведомления выключены"
    )
    row = await database.get_user(callback.from_user.id)
    await callback.message.edit_text(
        autoraise_text(row), reply_markup=autoraise_keyboard(row)
    )


@router.callback_query(F.data == "autoraise:now")
async def callback_raise_now(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Поднимаю лоты…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    try:
        account = await load_account(callback.from_user.id, database, cipher, funpay)
        outcomes = await funpay.raise_all(account)
        delay = next_raise_delay(outcomes)
        await database.set_next_raise(
            callback.from_user.id,
            datetime.now(timezone.utc) + timedelta(seconds=delay),
        )
        await callback.message.answer(
            format_raise_results(outcomes), reply_markup=menu_keyboard()
        )
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(
            callback.message, callback.from_user.id, error, database, state
        )


@router.callback_query(F.data.startswith("reply:"))
async def callback_reply(
    callback: CallbackQuery, state: FSMContext, database: Database
) -> None:
    chat_id = callback.data.split(":", 1)[1]
    target = await database.get_chat_target(callback.from_user.id, chat_id)
    username = target["interlocutor_username"] if target else "собеседнику"
    await state.set_state(Reply.waiting_text)
    await state.update_data(funpay_chat_id=chat_id, funpay_username=username)
    await callback.answer()
    await callback.message.answer(
        f"✍️ Введите ответ для <b>{html.escape(username or 'собеседника')}</b>:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отмена", callback_data="reply_cancel")]
            ]
        ),
    )


@router.callback_query(F.data == "reply_cancel")
async def callback_reply_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Ответ отменён")
    await callback.message.edit_text(
        "Отправка ответа отменена.", reply_markup=menu_keyboard()
    )


@router.message(Reply.waiting_text, F.text)
async def send_funpay_reply(
    message: Message,
    state: FSMContext,
    database: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    reply_text = message.text.strip()
    if not reply_text:
        await message.answer("Ответ не может быть пустым.")
        return
    if len(reply_text) > 4000:
        await message.answer("Ответ слишком длинный. Максимум — 4000 символов.")
        return
    data = await state.get_data()
    chat_id = data.get("funpay_chat_id")
    username = data.get("funpay_username")
    if not chat_id:
        await state.clear()
        await message.answer(
            "Контекст ответа потерян. Дождитесь нового сообщения FunPay."
        )
        return

    status = await message.answer("⏳ Отправляю ответ в FunPay…")
    try:
        account = await load_account(message.from_user.id, database, cipher, funpay)
        await funpay.send_message(account, chat_id, reply_text, username)
        await state.clear()
        await status.edit_text(
            f"✅ Ответ для <b>{html.escape(username or 'собеседника')}</b> отправлен.",
            reply_markup=menu_keyboard(),
        )
    except Exception as error:  # noqa: BLE001 - centralized FunPay error reporting
        await report_funpay_error(message, message.from_user.id, error, database, state)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Используйте /start для открытия меню или /reset для переподключения."
    )


def required_environment() -> tuple[str, str]:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    missing = [
        name
        for name, value in (("BOT_TOKEN", bot_token), ("DATABASE_URL", database_url))
        if not value
    ]
    if missing:
        raise ConfigurationError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return bot_token, database_url


async def main() -> None:
    bot_token, database_url = required_environment()
    database = Database(database_url)
    await database.connect()
    await database.init()

    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    cipher = SecretCipher(bot_token)
    funpay = FunPayService()
    notifications = NotificationManager(bot, database, cipher, funpay)
    background_task = asyncio.create_task(
        notifications.run(), name="funpay-notifications"
    )

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            database=database,
            cipher=cipher,
            funpay=funpay,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        notifications.stop()
        background_task.cancel()
        await asyncio.gather(background_task, return_exceptions=True)
        await database.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
