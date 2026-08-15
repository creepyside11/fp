import asyncio
import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from FunPayAPI.common.exceptions import (
    RaiseError,
    RequestFailedError,
    UnauthorizedError,
)

from FunPayAPI import Account, Runner, enums
from proxy_utils import proxy_mapping
from storage import DEFAULT_USER_AGENT, Database, SecretCipher, StoredSecretError

logger = logging.getLogger(__name__)
DEFAULT_RAISE_INTERVAL = 4 * 60 * 60
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class BalanceUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class RaiseOutcome:
    category_id: int
    category_name: str
    raised: bool
    wait_seconds: int | None = None
    error: str | None = None


@dataclass(slots=True)
class RuntimeSession:
    fingerprint: str
    account: Account
    runner: Runner
    created_at: float


def next_raise_delay(outcomes: list[RaiseOutcome]) -> int:
    waits = [item.wait_seconds for item in outcomes if item.wait_seconds]
    if any(item.raised for item in outcomes):
        return min(waits) if waits else DEFAULT_RAISE_INTERVAL
    if waits:
        return max(60, min(waits) + 10)
    return 15 * 60


def monitor_error_label(stage: str, error: Exception) -> str:
    if isinstance(error, RequestFailedError):
        body = re.sub(r"<[^>]+>", " ", error.response.text or "")
        body = " ".join(body.split())[:70]
        suffix = f" — {body}" if body else ""
        return f"{stage}: HTTP {error.status_code}{suffix}"
    if isinstance(error, (requests.RequestException, TimeoutError)):
        return f"{stage}: прокси или тайм-аут"
    return f"{stage}: {type(error).__name__}"


class FunPayService:
    def __init__(self, concurrency: int = 8):
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _thread(self, function, *args):
        async with self._semaphore:
            return await asyncio.to_thread(function, *args)

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

        await self._thread(request)

    async def account(
        self, golden_key: str, proxy_url: str, user_agent: str | None = None
    ) -> Account:
        def request() -> Account:
            return Account(
                golden_key,
                user_agent=user_agent or DEFAULT_USER_AGENT,
                requests_timeout=15,
                proxy=proxy_mapping(proxy_url),
            ).get()

        return await self._thread(request)

    async def profile(self, account: Account) -> Any:
        return await self._thread(account.get_user, account.id)

    async def balance(self, account: Account) -> Any:
        """Use a real account lot because FunPayAPI's default lot ID can disappear."""

        def request() -> Any:
            profile = account.get_user(account.id)
            candidates: list[int] = []
            own_lots = profile.get_lots()
            own_ids = {str(lot.id) for lot in own_lots}

            # A public lot owned by somebody else is the most reliable page: FunPay
            # renders the payment-method selector (and its balance data) there.
            checked_subcategories: set[tuple[Any, int]] = set()
            for lot in own_lots:
                subcategory = getattr(lot, "subcategory", None)
                if subcategory is None:
                    continue
                marker = (subcategory.type, subcategory.id)
                if marker in checked_subcategories:
                    continue
                checked_subcategories.add(marker)
                try:
                    public_lots = account.get_subcategory_public_lots(
                        subcategory.type, subcategory.id
                    )
                except UnauthorizedError:
                    raise
                except Exception:  # noqa: BLE001 - try another documented lot source
                    public_lots = []
                for public_lot in public_lots:
                    if str(public_lot.id) not in own_ids:
                        try:
                            candidates.append(int(public_lot.id))
                        except (TypeError, ValueError):
                            continue
                        break
                if candidates or len(checked_subcategories) == 3:
                    break

            # Own lots and the legacy library default remain fallbacks.
            for lot in own_lots:
                try:
                    lot_id = int(lot.id)
                except (TypeError, ValueError):
                    continue
                if lot_id not in candidates:
                    candidates.append(lot_id)
                if len(candidates) == 3:
                    break
            if 18853876 not in candidates:
                candidates.append(18853876)

            last_error: Exception | None = None
            for lot_id in candidates:
                try:
                    balance = account.get_balance(lot_id)
                    if balance is not None:
                        return balance
                except UnauthorizedError:
                    raise
                except Exception as error:  # noqa: BLE001 - normalize parser/API failures
                    last_error = error
            raise BalanceUnavailableError(
                "FunPay не вернул блок баланса ни для одного доступного объявления."
            ) from last_error

        return await self._thread(request)

    async def send_message(
        self, account: Account, chat_id: int | str, text: str, chat_name: str | None
    ) -> Any:
        if isinstance(chat_id, str) and chat_id.isdigit():
            chat_id = int(chat_id)
        return await self._thread(account.send_message, chat_id, text, chat_name)

    async def latest_message(
        self, account: Account, chat_id: int | str, chat_name: str | None
    ) -> Any | None:
        def request() -> Any | None:
            normalized_chat_id: int | str = chat_id
            if isinstance(normalized_chat_id, str) and normalized_chat_id.isdigit():
                normalized_chat_id = int(normalized_chat_id)
            messages = account.get_chat_history(
                normalized_chat_id, interlocutor_username=chat_name
            )
            return messages[-1] if messages else None

        return await self._thread(request)

    async def is_first_client_message(self, account: Account, message: Any) -> bool:
        def request() -> bool:
            chat_id: int | str = message.chat_id
            if isinstance(chat_id, str) and chat_id.isdigit():
                chat_id = int(chat_id)
            history = account.get_chat_history(
                chat_id, interlocutor_username=message.chat_name
            )
            client_messages = [
                item
                for item in history
                if item.author_id == message.author_id
                and item.author_id not in (0, account.id)
            ]
            return bool(client_messages) and client_messages[0].id == message.id

        return await self._thread(request)

    async def lot_fields(self, account: Account, lot_id: int) -> Any:
        return await self._thread(account.get_lot_fields, lot_id)

    async def save_lot(self, account: Account, lot_fields: Any) -> None:
        await self._thread(account.save_lot, lot_fields)

    async def poll_events(self, runner: Runner) -> list[Any]:
        def request() -> list[Any]:
            for attempt in range(2):
                try:
                    return runner.parse_updates(runner.get_updates())
                except UnauthorizedError:
                    raise
                except RequestFailedError as error:
                    if attempt:
                        raise
                    if error.status_code == 400:
                        runner.account.get(update_phpsessid=True)
                    elif error.status_code in TRANSIENT_HTTP_STATUSES:
                        time.sleep(2)
                    else:
                        raise
            return []

        return await self._thread(request)

    async def chat_histories(
        self, account: Account
    ) -> tuple[list[Any], dict[int | str, list[Any]]]:
        """Fetch current chats and their message IDs without relying on Runner events."""

        def request() -> tuple[list[Any], dict[int | str, list[Any]]]:
            chats: list[Any] = []
            for attempt in range(2):
                try:
                    chats = account.request_chats()
                    break
                except UnauthorizedError:
                    raise
                except RequestFailedError as error:
                    if attempt:
                        raise
                    if error.status_code == 400:
                        account.get(update_phpsessid=True)
                    elif error.status_code in TRANSIENT_HTTP_STATUSES:
                        time.sleep(2)
                    else:
                        raise

            # New and recently active conversations are always at the top.
            # Ten histories fit into one runner request and avoid requesting
            # all 50 conversations every few seconds through the proxy.
            chats = chats[:10]
            histories: dict[int | str, list[Any]] = {}
            failed_batches = 0

            # FunPay's runner endpoint is designed for packs of up to ten chat
            # objects. A failed pack is retried chat-by-chat so one malformed
            # conversation cannot disable notifications for every other chat.
            for offset in range(0, len(chats), 10):
                batch = chats[offset : offset + 10]
                chat_names = {chat.id: chat.name for chat in batch}
                try:
                    for attempt in range(2):
                        try:
                            histories.update(account.get_chats_histories(chat_names))
                            break
                        except UnauthorizedError:
                            raise
                        except RequestFailedError as error:
                            if attempt:
                                raise
                            if error.status_code == 400:
                                account.get(update_phpsessid=True)
                            elif error.status_code in TRANSIENT_HTTP_STATUSES:
                                time.sleep(2)
                            else:
                                raise
                    continue
                except UnauthorizedError:
                    raise
                except RequestFailedError as error:
                    if error.status_code in TRANSIENT_HTTP_STATUSES:
                        raise
                    failed_batches += 1
                except Exception:  # noqa: BLE001 - retry each chat independently
                    failed_batches += 1

                for chat in batch:
                    try:
                        histories[chat.id] = account.get_chat_history(
                            chat.id, interlocutor_username=chat.name
                        )
                    except UnauthorizedError:
                        raise
                    except Exception:
                        logger.exception("Failed to fetch FunPay chat %s", chat.id)

            if chats and not histories and failed_batches:
                raise RuntimeError("FunPay chat histories are unavailable")
            return chats, histories

        return await self._thread(request)

    async def raise_all(self, account: Account) -> list[RaiseOutcome]:
        def request() -> list[RaiseOutcome]:
            profile = account.get_user(account.id)
            categories: dict[int, str] = {}
            for lot in profile.get_lots():
                subcategory = getattr(lot, "subcategory", None)
                category = getattr(subcategory, "category", None)
                if (
                    subcategory is not None
                    and subcategory.type is enums.SubCategoryTypes.COMMON
                    and category is not None
                ):
                    categories[category.id] = category.name

            outcomes: list[RaiseOutcome] = []
            for category_id, category_name in categories.items():
                try:
                    account.raise_lots(category_id)
                    outcomes.append(RaiseOutcome(category_id, category_name, True))
                except UnauthorizedError:
                    raise
                except RaiseError as error:
                    outcomes.append(
                        RaiseOutcome(
                            category_id,
                            category_name,
                            False,
                            wait_seconds=error.wait_time,
                            error=error.short_str(),
                        )
                    )
                except Exception as error:  # noqa: BLE001 - report failure per category
                    outcomes.append(
                        RaiseOutcome(
                            category_id, category_name, False, error=str(error)[:300]
                        )
                    )
            return outcomes

        return await self._thread(request)


class NotificationManager:
    def __init__(
        self,
        bot: Bot,
        database: Database,
        cipher: SecretCipher,
        funpay: FunPayService,
        poll_interval: float = 12.0,
    ):
        self.bot = bot
        self.database = database
        self.cipher = cipher
        self.funpay = funpay
        self.poll_interval = poll_interval
        self.sessions: dict[int, RuntimeSession] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                rows = await self.database.list_background_users()
                active_ids = {row["telegram_id"] for row in rows}
                for telegram_id in set(self.sessions) - active_ids:
                    self.sessions.pop(telegram_id, None)
                await asyncio.gather(
                    *(self._process_user(row) for row in rows), return_exceptions=True
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background FunPay loop failed")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _session(self, row: Any) -> RuntimeSession:
        telegram_id = row["telegram_id"]
        encrypted_proxy = row["encrypted_proxy"]
        encrypted_key = row["encrypted_golden_key"]
        user_agent = row["funpay_user_agent"] or DEFAULT_USER_AGENT
        settings_fingerprint = ":".join(
            str(bool(row[name]))
            for name in (
                "message_notifications_enabled",
                "order_notifications_enabled",
                "auto_raise_enabled",
                "raise_notifications_enabled",
                "greeting_enabled",
            )
        )
        fingerprint = hashlib.sha256(
            f"{encrypted_proxy}:{encrypted_key}:{user_agent}:{settings_fingerprint}".encode()
        ).hexdigest()
        cached = self.sessions.get(telegram_id)
        if (
            cached
            and cached.fingerprint == fingerprint
            and time.monotonic() - cached.created_at < 50 * 60
        ):
            return cached

        proxy_url = self.cipher.decrypt(encrypted_proxy)
        golden_key = self.cipher.decrypt(encrypted_key)
        account = await self.funpay.account(
            golden_key,
            proxy_url,
            user_agent,
        )
        session = RuntimeSession(
            fingerprint,
            account,
            Runner(account, disable_message_requests=True),
            time.monotonic(),
        )
        self.sessions[telegram_id] = session
        return session

    async def _process_user(self, row: Any) -> None:
        telegram_id = row["telegram_id"]
        try:
            session = await self._session(row)
            monitor_errors: list[str] = []
            monitor_succeeded = False
            # Runner determines changes by the visible text/time in a chat
            # bookmark. It can miss an already existing chat or two equal
            # consecutive messages. Persistent message-ID cursors are the
            # authoritative source and run independently from order parsing.
            if row["message_notifications_enabled"] or row["greeting_enabled"]:
                try:
                    await self._poll_chat_histories(row, session.account)
                    monitor_succeeded = True
                except UnauthorizedError:
                    raise
                except Exception as error:
                    monitor_errors.append(monitor_error_label("сообщения", error))
                    logger.exception(
                        "FunPay message polling failed for Telegram user %s",
                        telegram_id,
                    )

            if row["order_notifications_enabled"]:
                try:
                    events = await self.funpay.poll_events(session.runner)
                    await self._deliver_events(row, session.account, events)
                    monitor_succeeded = True
                except UnauthorizedError:
                    raise
                except Exception as error:
                    monitor_errors.append(monitor_error_label("заказы", error))
                    logger.exception(
                        "FunPay order polling failed for Telegram user %s",
                        telegram_id,
                    )

            if (
                row["message_notifications_enabled"]
                or row["order_notifications_enabled"]
                or row["greeting_enabled"]
            ):
                await self.database.mark_monitor_result(
                    telegram_id,
                    monitor_succeeded,
                    "; ".join(monitor_errors) if monitor_errors else None,
                )

            now = datetime.now(timezone.utc)
            next_raise_at = row["next_raise_at"]
            if row["auto_raise_enabled"] and (
                next_raise_at is None or next_raise_at <= now
            ):
                outcomes = await self.funpay.raise_all(session.account)
                delay = next_raise_delay(outcomes)
                await self.database.set_next_raise(
                    telegram_id, now + timedelta(seconds=delay)
                )
                if row["raise_notifications_enabled"] and any(
                    item.raised for item in outcomes
                ):
                    await self._send_raise_notification(telegram_id, outcomes)
        except UnauthorizedError:
            self.sessions.pop(telegram_id, None)
            await self.database.mark_monitor_error(telegram_id, "UnauthorizedError")
            await self.database.clear_key(telegram_id)
            await self._safe_send(
                telegram_id,
                "⚠️ Сессия FunPay завершена. Откройте /start и отправьте новый GOLDEN_KEY.",
            )
        except StoredSecretError:
            self.sessions.pop(telegram_id, None)
            await self.database.mark_monitor_error(telegram_id, "StoredSecretError")
            logger.warning(
                "Stored credentials cannot be decrypted for Telegram user %s",
                telegram_id,
            )
        except Exception as error:
            await self.database.mark_monitor_error(telegram_id, type(error).__name__)
            logger.exception(
                "FunPay background processing failed for Telegram user %s", telegram_id
            )

    async def _poll_chat_histories(self, row: Any, account: Account) -> None:
        chats, histories = await self.funpay.chat_histories(account)
        telegram_id = row["telegram_id"]
        saved_cursors = await self.database.get_chat_cursors(telegram_id)
        changed_cursors: dict[int | str, int] = {}

        for chat in chats:
            messages = sorted(histories.get(chat.id, []), key=lambda item: item.id)
            if not messages:
                continue

            chat_key = str(chat.id)
            previous_id = saved_cursors.get(chat_key)
            latest_id = messages[-1].id
            if previous_id is None:
                # Do not replay up to fifty old conversations on the first
                # deployment. An unread chat is the one important exception:
                # deliver its latest incoming message immediately.
                candidates = [messages[-1]] if chat.unread else []
            else:
                candidates = [message for message in messages if message.id > previous_id]

            for message in candidates:
                await self._handle_message(row, account, message)

            if previous_id is None or latest_id > previous_id:
                changed_cursors[chat.id] = latest_id

        await self.database.save_chat_cursors(telegram_id, changed_cursors)

    async def _deliver_events(
        self, row: Any, account: Account, events: list[Any]
    ) -> None:
        new_message_chat_ids: set[str] = set()

        # Process concrete messages first. Runner also emits a generic
        # LAST_CHAT_MESSAGE_CHANGED event for the same chat.
        for event in events:
            if event.type is enums.EventTypes.NEW_MESSAGE:
                new_message_chat_ids.add(str(event.message.chat_id))
                await self._handle_message(row, account, event.message)
            elif (
                event.type is enums.EventTypes.NEW_ORDER
                and row["order_notifications_enabled"]
            ):
                order = event.order
                description = order.description.strip()
                if len(description) > 1800:
                    description = description[:1797] + "…"
                await self._safe_send(
                    row["telegram_id"],
                    "🛒 <b>Новый заказ FunPay</b>\n\n"
                    f"Заказ: <code>#{html.escape(order.id)}</code>\n"
                    f"Покупатель: <b>{html.escape(order.buyer_username)}</b>\n"
                    f"Товар: {html.escape(description)}\n"
                    f"Сумма: <b>{order.price:,.2f} ₽</b>\n"
                    f'<a href="https://funpay.com/orders/{html.escape(order.id, quote=True)}/">Открыть заказ</a>',
                )

        # FunPayAPI can emit only a chat-change event when history parsing
        # fails. Fetching the last message explicitly fixes notifications for
        # already existing chats. INITIAL_CHAT covers messages received during
        # worker startup; the database deduplicates them across restarts.
        for event in events:
            is_initial_unread = (
                event.type is enums.EventTypes.INITIAL_CHAT and event.chat.unread
            )
            is_changed = event.type is enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED
            if not (is_initial_unread or is_changed):
                continue
            if str(event.chat.id) in new_message_chat_ids:
                continue
            message = await self.funpay.latest_message(
                account, event.chat.id, event.chat.name
            )
            if message is not None:
                await self._handle_message(row, account, message)

    async def _handle_message(self, row: Any, account: Account, message: Any) -> None:
        if message.by_bot or message.author_id in (0, account.id):
            return
        if not await self.database.claim_message(
            row["telegram_id"], message.id, message.chat_id
        ):
            return

        username = message.author or message.chat_name or "Неизвестный пользователь"
        if row["message_notifications_enabled"]:
            await self.database.save_chat_target(
                row["telegram_id"], message.chat_id, username
            )
            if message.text:
                message_text = message.text.strip()
                if len(message_text) > 3000:
                    message_text = message_text[:2997] + "…"
                body = html.escape(message_text)
            elif message.image_link:
                body = f'<a href="{html.escape(message.image_link, quote=True)}">Изображение</a>'
            else:
                body = "Сообщение без текста"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"↩️ Ответить {username}"[:60],
                            callback_data=f"reply:{message.chat_id}",
                        )
                    ]
                ]
            )
            await self._safe_send(
                row["telegram_id"],
                "💬 <b>Новое сообщение FunPay</b>\n\n"
                f"От: <b>{html.escape(username)}</b>\n"
                f"Сообщение: {body}",
                reply_markup=keyboard,
            )

        if row["greeting_enabled"] and await self.funpay.is_first_client_message(
            account, message
        ):
            claimed = await self.database.claim_greeting(
                row["telegram_id"], message.chat_id
            )
            if claimed:
                try:
                    await self.funpay.send_message(
                        account,
                        message.chat_id,
                        row["greeting_text"],
                        username,
                    )
                except Exception:
                    await self.database.release_greeting(
                        row["telegram_id"], message.chat_id
                    )
                    logger.exception(
                        "Failed to greet FunPay chat %s for Telegram user %s",
                        message.chat_id,
                        row["telegram_id"],
                    )

    async def _send_raise_notification(
        self, telegram_id: int, outcomes: list[RaiseOutcome]
    ) -> None:
        names = [html.escape(item.category_name) for item in outcomes if item.raised]
        await self._safe_send(
            telegram_id,
            "⬆️ <b>Лоты автоматически подняты</b>\n\n"
            + "\n".join(f"• {name}" for name in names),
        )

    async def _safe_send(self, telegram_id: int, text: str, **kwargs) -> None:
        try:
            await self.bot.send_message(telegram_id, text, **kwargs)
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.info("Cannot deliver Telegram notification to %s", telegram_id)
