import asyncio
import base64
import hashlib
import html
import logging
import os
from dataclasses import dataclass
from typing import Any

import asyncpg
import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cryptography.fernet import Fernet, InvalidToken
from FunPayAPI import Account
from FunPayAPI.common.exceptions import RequestFailedError, UnauthorizedError

from proxy_utils import ProxyFormatError, normalize_proxy, proxy_mapping


logger = logging.getLogger(__name__)
router = Router()
PAGE_SIZE = 6


class Setup(StatesGroup):
    waiting_proxy = State()
    waiting_golden_key = State()


class ConfigurationError(RuntimeError):
    pass


class StoredSecretError(RuntimeError):
    pass


class SecretCipher:
    """Encrypts stored credentials without requiring a third environment variable."""

    def __init__(self, bot_token: str):
        digest = hashlib.sha256(f"funpay-telegram-bot:v1:{bot_token}".encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except (InvalidToken, ValueError) as error:
            raise StoredSecretError("Не удалось расшифровать сохранённые данные.") from error


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=5,
            command_timeout=20,
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database is not connected")
        return self.pool

    async def init(self) -> None:
        await self._pool().execute(
            """
            CREATE TABLE IF NOT EXISTS funpay_bot_users (
                telegram_id BIGINT PRIMARY KEY,
                telegram_username TEXT,
                encrypted_proxy TEXT,
                encrypted_golden_key TEXT,
                funpay_user_id BIGINT,
                funpay_username TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    async def get_user(self, telegram_id: int) -> asyncpg.Record | None:
        return await self._pool().fetchrow(
            "SELECT * FROM funpay_bot_users WHERE telegram_id = $1", telegram_id
        )

    async def save_proxy(self, telegram_id: int, username: str | None, encrypted_proxy: str) -> None:
        await self._pool().execute(
            """
            INSERT INTO funpay_bot_users (
                telegram_id, telegram_username, encrypted_proxy, encrypted_golden_key,
                funpay_user_id, funpay_username
            ) VALUES ($1, $2, $3, NULL, NULL, NULL)
            ON CONFLICT (telegram_id) DO UPDATE SET
                telegram_username = EXCLUDED.telegram_username,
                encrypted_proxy = EXCLUDED.encrypted_proxy,
                encrypted_golden_key = NULL,
                funpay_user_id = NULL,
                funpay_username = NULL,
                updated_at = NOW()
            """,
            telegram_id,
            username,
            encrypted_proxy,
        )

    async def save_account(
        self,
        telegram_id: int,
        username: str | None,
        encrypted_golden_key: str,
        funpay_user_id: int,
        funpay_username: str,
    ) -> None:
        await self._pool().execute(
            """
            UPDATE funpay_bot_users SET
                telegram_username = $2,
                encrypted_golden_key = $3,
                funpay_user_id = $4,
                funpay_username = $5,
                updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id,
            username,
            encrypted_golden_key,
            funpay_user_id,
            funpay_username,
        )

    async def clear_key(self, telegram_id: int) -> None:
        await self._pool().execute(
            """
            UPDATE funpay_bot_users SET
                encrypted_golden_key = NULL,
                funpay_user_id = NULL,
                funpay_username = NULL,
                updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id,
        )

    async def delete_user(self, telegram_id: int) -> None:
        await self._pool().execute(
            "DELETE FROM funpay_bot_users WHERE telegram_id = $1", telegram_id
        )


@dataclass(slots=True)
class StoredCredentials:
    proxy_url: str
    golden_key: str


class FunPayService:
    def __init__(self, concurrency: int = 8):
        self._semaphore = asyncio.Semaphore(concurrency)

    async def check_proxy(self, proxy_url: str) -> None:
        def request() -> None:
            response = requests.get(
                "https://funpay.com/",
                proxies=proxy_mapping(proxy_url),
                timeout=12,
                allow_redirects=True,
            )
            if response.status_code == 407 or response.status_code >= 500:
                raise requests.RequestException(f"HTTP {response.status_code}")

        async with self._semaphore:
            await asyncio.to_thread(request)

    async def account(self, golden_key: str, proxy_url: str) -> Account:
        def request() -> Account:
            return Account(
                golden_key,
                requests_timeout=15,
                proxy=proxy_mapping(proxy_url),
            ).get()

        async with self._semaphore:
            return await asyncio.to_thread(request)

    async def profile(self, account: Account) -> Any:
        async with self._semaphore:
            return await asyncio.to_thread(account.get_user, account.id)

    async def balance(self, account: Account) -> Any:
        async with self._semaphore:
            return await asyncio.to_thread(account.get_balance)


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
                InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            ],
            [InlineKeyboardButton(text="📋 Мои объявления", callback_data="lots:0")],
            [InlineKeyboardButton(text="🔄 Сменить аккаунт / прокси", callback_data="reconnect")],
        ]
    )


def lots_keyboard(page: int, pages: int) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"lots:{page - 1}"))
    navigation.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"lots:{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[navigation, [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]]
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
        f"✅ FunPay подключён{greeting}. Выберите действие:", reply_markup=menu_keyboard()
    )


async def read_credentials(
    telegram_id: int, db: Database, cipher: SecretCipher
) -> StoredCredentials | None:
    row = await db.get_user(telegram_id)
    if not row or not row["encrypted_proxy"] or not row["encrypted_golden_key"]:
        return None
    return StoredCredentials(
        proxy_url=cipher.decrypt(row["encrypted_proxy"]),
        golden_key=cipher.decrypt(row["encrypted_golden_key"]),
    )


async def load_account(
    telegram_id: int, db: Database, cipher: SecretCipher, funpay: FunPayService
) -> Account:
    credentials = await read_credentials(telegram_id, db, cipher)
    if not credentials:
        raise StoredSecretError("Аккаунт ещё не подключён.")
    return await funpay.account(credentials.golden_key, credentials.proxy_url)


async def report_funpay_error(
    target: Message, telegram_id: int, error: Exception, db: Database, state: FSMContext | None = None
) -> None:
    if isinstance(error, UnauthorizedError):
        await db.clear_key(telegram_id)
        if state:
            await state.set_state(Setup.waiting_golden_key)
        await target.answer(
            "❌ GOLDEN_KEY недействителен или сессия FunPay завершена. "
            "Отправьте новый GOLDEN_KEY."
        )
        return
    if isinstance(error, StoredSecretError):
        await target.answer(f"❌ {html.escape(str(error))} Используйте /reset для переподключения.")
        return
    if isinstance(error, (requests.RequestException, RequestFailedError, TimeoutError)):
        await target.answer(
            "❌ FunPay не ответил через сохранённый прокси. Проверьте прокси или используйте /reset."
        )
        return
    logger.exception("Unexpected FunPay request failure", exc_info=error)
    await target.answer("❌ Не удалось получить данные FunPay. Попробуйте ещё раз позже.")


@router.message(CommandStart())
async def command_start(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    row = await db.get_user(message.from_user.id)
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
async def command_reset(message: Message, state: FSMContext, db: Database) -> None:
    await db.delete_user(message.from_user.id)
    await state.clear()
    await message.answer("Сохранённое подключение удалено.")
    await prompt_proxy(message, state)


@router.message(Setup.waiting_proxy, F.text)
async def receive_proxy(
    message: Message,
    state: FSMContext,
    db: Database,
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
        await status.edit_text(f"❌ {html.escape(str(error))}\n\nОтправьте прокси ещё раз.")
        return
    except (requests.RequestException, asyncio.TimeoutError):
        await status.edit_text(
            "❌ Прокси не отвечает или не может открыть FunPay. Проверьте данные и отправьте его ещё раз."
        )
        return

    await db.save_proxy(
        message.from_user.id,
        message.from_user.username,
        cipher.encrypt(proxy_url),
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
    db: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    golden_key = message.text.strip()
    await delete_sensitive_message(message)
    status = await message.answer("⏳ Проверяю GOLDEN_KEY…")
    if not 16 <= len(golden_key) <= 512 or any(char.isspace() for char in golden_key):
        await status.edit_text("❌ GOLDEN_KEY выглядит некорректно. Отправьте ключ ещё раз.")
        return

    row = await db.get_user(message.from_user.id)
    if not row or not row["encrypted_proxy"]:
        await status.edit_text("Сначала нужно добавить прокси. Используйте /reset.")
        await state.clear()
        return

    try:
        proxy_url = cipher.decrypt(row["encrypted_proxy"])
        account = await funpay.account(golden_key, proxy_url)
    except UnauthorizedError:
        await status.edit_text("❌ FunPay отклонил GOLDEN_KEY. Проверьте ключ и отправьте его ещё раз.")
        return
    except (requests.RequestException, RequestFailedError, StoredSecretError):
        await status.edit_text(
            "❌ Не удалось подключиться к FunPay через сохранённый прокси. Используйте /reset и проверьте данные."
        )
        return
    except Exception as error:
        logger.exception("Golden key validation failed", exc_info=error)
        await status.edit_text("❌ Не удалось проверить аккаунт. Попробуйте ещё раз позже.")
        return

    await db.save_account(
        message.from_user.id,
        message.from_user.username,
        cipher.encrypt(golden_key),
        account.id,
        account.username,
    )
    await state.clear()
    await status.edit_text(
        f"✅ Аккаунт <b>{html.escape(account.username)}</b> подключён.",
        reply_markup=menu_keyboard(),
    )


@router.callback_query(F.data == "menu")
async def callback_menu(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    row = await db.get_user(callback.from_user.id)
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
async def callback_reconnect(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    await db.delete_user(callback.from_user.id)
    await state.clear()
    await prompt_proxy(callback.message, state)


@router.callback_query(F.data == "balance")
async def callback_balance(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Получаю баланс…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    try:
        account = await load_account(callback.from_user.id, db, cipher, funpay)
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
    except Exception as error:
        await report_funpay_error(callback.message, callback.from_user.id, error, db, state)


@router.callback_query(F.data == "profile")
async def callback_profile(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    cipher: SecretCipher,
    funpay: FunPayService,
) -> None:
    await callback.answer("Получаю профиль…")
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    try:
        account = await load_account(callback.from_user.id, db, cipher, funpay)
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
            f"Фото: <a href=\"{html.escape(profile.profile_photo, quote=True)}\">открыть</a>\n"
            f"Профиль: <a href=\"https://funpay.com/users/{profile.id}/\">FunPay</a>"
        )
        await callback.message.answer(text, reply_markup=menu_keyboard(), disable_web_page_preview=True)
    except Exception as error:
        await report_funpay_error(callback.message, callback.from_user.id, error, db, state)


@router.callback_query(F.data.startswith("lots:"))
async def callback_lots(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
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
        account = await load_account(callback.from_user.id, db, cipher, funpay)
        profile = await funpay.profile(account)
        lots = profile.get_lots()
        if not lots:
            await callback.message.answer("📭 У аккаунта нет опубликованных объявлений.", reply_markup=menu_keyboard())
            return

        pages = (len(lots) + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(requested_page, pages - 1)
        chunks = [f"📋 <b>Объявления {html.escape(profile.username)}</b> — {len(lots)} шт.\n"]
        for index, lot in enumerate(lots[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], start=page * PAGE_SIZE + 1):
            category = getattr(getattr(lot, "subcategory", None), "fullname", "Без категории")
            description = (lot.description or "Без названия").strip()
            if len(description) > 180:
                description = description[:177] + "…"
            server = f" · {html.escape(lot.server)}" if lot.server else ""
            chunks.append(
                f"\n<b>{index}. {html.escape(description)}</b>\n"
                f"{html.escape(category)}{server}\n"
                f"Цена: <b>{lot.price:,.2f} ₽</b> · "
                f"<a href=\"{html.escape(lot.public_link, quote=True)}\">открыть</a>"
            )
        text = "\n".join(chunks)
        keyboard = lots_keyboard(page, pages)
        if callback.message.text and callback.message.text.startswith("📋"):
            await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
        else:
            await callback.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception as error:
        await report_funpay_error(callback.message, callback.from_user.id, error, db, state)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Используйте /start для открытия меню или /reset для переподключения.")


def required_environment() -> tuple[str, str]:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    missing = [name for name, value in (("BOT_TOKEN", bot_token), ("DATABASE_URL", database_url)) if not value]
    if missing:
        raise ConfigurationError(f"Missing required environment variables: {', '.join(missing)}")
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

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            db=database,
            cipher=cipher,
            funpay=funpay,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await database.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
