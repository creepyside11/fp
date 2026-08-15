import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

import asyncpg
from cryptography.fernet import Fernet, InvalidToken


class StoredSecretError(RuntimeError):
    pass


class SecretCipher:
    """Encrypt stored credentials using a key derived from the bot token."""

    def __init__(self, bot_token: str):
        digest = hashlib.sha256(f"funpay-telegram-bot:v1:{bot_token}".encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except (InvalidToken, ValueError) as error:
            raise StoredSecretError(
                "Не удалось расшифровать сохранённые данные."
            ) from error


@dataclass(slots=True)
class StoredCredentials:
    proxy_url: str
    golden_key: str


class Database:
    SETTINGS: ClassVar[frozenset[str]] = frozenset(
        {
            "message_notifications_enabled",
            "order_notifications_enabled",
            "auto_raise_enabled",
            "raise_notifications_enabled",
            "greeting_enabled",
        }
    )

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
        pool = self._pool()
        await pool.execute(
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
        migrations = (
            (
                "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS "
                "message_notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            ),
            (
                "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS "
                "order_notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            ),
            (
                "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS "
                "auto_raise_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            ),
            (
                "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS "
                "raise_notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            ),
            (
                "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS "
                "greeting_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            ),
            (
                "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS "
                "greeting_text TEXT NOT NULL DEFAULT "
                "'Здравствуйте! Спасибо за обращение. Скоро отвечу вам.'"
            ),
            "ALTER TABLE funpay_bot_users ADD COLUMN IF NOT EXISTS next_raise_at TIMESTAMPTZ",
        )
        for statement in migrations:
            await pool.execute(statement)
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS funpay_notification_chats (
                telegram_id BIGINT NOT NULL REFERENCES funpay_bot_users(telegram_id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL,
                interlocutor_username TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, chat_id)
            )
            """
        )
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS funpay_seen_messages (
                telegram_id BIGINT NOT NULL REFERENCES funpay_bot_users(telegram_id) ON DELETE CASCADE,
                message_id BIGINT NOT NULL,
                chat_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, message_id)
            )
            """
        )
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS funpay_greeted_clients (
                telegram_id BIGINT NOT NULL REFERENCES funpay_bot_users(telegram_id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, chat_id)
            )
            """
        )
        await pool.execute(
            "DELETE FROM funpay_seen_messages WHERE created_at < NOW() - INTERVAL '90 days'"
        )

    async def get_user(self, telegram_id: int) -> asyncpg.Record | None:
        return await self._pool().fetchrow(
            "SELECT * FROM funpay_bot_users WHERE telegram_id = $1", telegram_id
        )

    async def save_proxy(
        self, telegram_id: int, username: str | None, encrypted_proxy: str
    ) -> None:
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
                auto_raise_enabled = FALSE,
                next_raise_at = NULL,
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
                auto_raise_enabled = FALSE,
                next_raise_at = NULL,
                updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id,
        )

    async def delete_user(self, telegram_id: int) -> None:
        await self._pool().execute(
            "DELETE FROM funpay_bot_users WHERE telegram_id = $1", telegram_id
        )

    async def set_setting(self, telegram_id: int, setting: str, enabled: bool) -> None:
        if setting not in self.SETTINGS:
            raise ValueError("Unknown setting")
        extra = (
            ", next_raise_at = NOW()"
            if setting == "auto_raise_enabled" and enabled
            else ""
        )
        await self._pool().execute(
            f"UPDATE funpay_bot_users SET {setting} = $2{extra}, updated_at = NOW() WHERE telegram_id = $1",
            telegram_id,
            enabled,
        )

    async def set_next_raise(self, telegram_id: int, next_raise_at: datetime) -> None:
        await self._pool().execute(
            "UPDATE funpay_bot_users SET next_raise_at = $2, updated_at = NOW() WHERE telegram_id = $1",
            telegram_id,
            next_raise_at,
        )

    async def set_greeting_text(self, telegram_id: int, greeting_text: str) -> None:
        await self._pool().execute(
            """
            UPDATE funpay_bot_users
            SET greeting_text = $2, updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id,
            greeting_text,
        )

    async def list_background_users(self) -> list[asyncpg.Record]:
        return await self._pool().fetch(
            """
            SELECT * FROM funpay_bot_users
            WHERE encrypted_proxy IS NOT NULL
              AND encrypted_golden_key IS NOT NULL
              AND (
                  message_notifications_enabled
                  OR order_notifications_enabled
                  OR auto_raise_enabled
                  OR greeting_enabled
              )
            """
        )

    async def claim_message(
        self, telegram_id: int, message_id: int, chat_id: int | str
    ) -> bool:
        inserted = await self._pool().fetchval(
            """
            INSERT INTO funpay_seen_messages (telegram_id, message_id, chat_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id, message_id) DO NOTHING
            RETURNING message_id
            """,
            telegram_id,
            message_id,
            str(chat_id),
        )
        return inserted is not None

    async def claim_greeting(self, telegram_id: int, chat_id: int | str) -> bool:
        inserted = await self._pool().fetchval(
            """
            INSERT INTO funpay_greeted_clients (telegram_id, chat_id)
            VALUES ($1, $2)
            ON CONFLICT (telegram_id, chat_id) DO NOTHING
            RETURNING chat_id
            """,
            telegram_id,
            str(chat_id),
        )
        return inserted is not None

    async def release_greeting(self, telegram_id: int, chat_id: int | str) -> None:
        await self._pool().execute(
            "DELETE FROM funpay_greeted_clients WHERE telegram_id = $1 AND chat_id = $2",
            telegram_id,
            str(chat_id),
        )

    async def save_chat_target(
        self, telegram_id: int, chat_id: int | str, interlocutor_username: str | None
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO funpay_notification_chats (telegram_id, chat_id, interlocutor_username)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id, chat_id) DO UPDATE SET
                interlocutor_username = EXCLUDED.interlocutor_username,
                updated_at = NOW()
            """,
            telegram_id,
            str(chat_id),
            interlocutor_username,
        )

    async def get_chat_target(
        self, telegram_id: int, chat_id: str
    ) -> asyncpg.Record | None:
        return await self._pool().fetchrow(
            """
            SELECT chat_id, interlocutor_username
            FROM funpay_notification_chats
            WHERE telegram_id = $1 AND chat_id = $2
            """,
            telegram_id,
            chat_id,
        )


async def read_credentials(
    telegram_id: int, database: Database, cipher: SecretCipher
) -> StoredCredentials | None:
    row = await database.get_user(telegram_id)
    if not row or not row["encrypted_proxy"] or not row["encrypted_golden_key"]:
        return None
    return StoredCredentials(
        proxy_url=cipher.decrypt(row["encrypted_proxy"]),
        golden_key=cipher.decrypt(row["encrypted_golden_key"]),
    )
