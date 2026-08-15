import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from funpay_service import (
    BalanceUnavailableError,
    FunPayService,
    NotificationManager,
    RaiseOutcome,
    next_raise_delay,
)
from FunPayAPI import enums
from storage import DEFAULT_USER_AGENT


class FakeLot:
    def __init__(self, lot_id, subcategory=None):
        self.id = lot_id
        self.subcategory = subcategory


class FakeSubcategory:
    type = "common"
    id = 10


class FakeProfile:
    def __init__(self, lot_ids, with_subcategory=False):
        subcategory = FakeSubcategory() if with_subcategory else None
        self._lots = [FakeLot(item, subcategory) for item in lot_ids]

    def get_lots(self):
        return self._lots


class FakeAccount:
    id = 42

    def __init__(self, lot_ids, working_id=None, public_ids=None):
        self.profile = FakeProfile(lot_ids, with_subcategory=public_ids is not None)
        self.working_id = working_id
        self.public_ids = public_ids or []
        self.requested_ids = []

    def get_user(self, user_id):
        return self.profile

    def get_subcategory_public_lots(self, subcategory_type, subcategory_id):
        return [FakeLot(item) for item in self.public_ids]

    def get_balance(self, lot_id):
        self.requested_ids.append(lot_id)
        if lot_id == self.working_id:
            return {"balance": 100}
        raise RuntimeError("lot unavailable")


class BalanceTests(unittest.TestCase):
    def test_uses_real_account_lot_before_old_default(self):
        account = FakeAccount([555, 777], working_id=555)
        result = asyncio.run(FunPayService().balance(account))
        self.assertEqual(result, {"balance": 100})
        self.assertEqual(account.requested_ids, [555])

    def test_prefers_another_sellers_public_lot(self):
        account = FakeAccount([555], working_id=999, public_ids=[555, 999])
        result = asyncio.run(FunPayService().balance(account))
        self.assertEqual(result, {"balance": 100})
        self.assertEqual(account.requested_ids, [999])

    def test_tries_more_than_one_account_lot(self):
        account = FakeAccount([555, 777], working_id=777)
        result = asyncio.run(FunPayService().balance(account))
        self.assertEqual(result, {"balance": 100})
        self.assertEqual(account.requested_ids, [555, 777])

    def test_raises_clear_error_when_no_candidate_works(self):
        account = FakeAccount([], working_id=None)
        with self.assertRaises(BalanceUnavailableError):
            asyncio.run(FunPayService().balance(account))


class AccountTests(unittest.TestCase):
    def test_account_uses_browser_user_agent_by_default(self):
        initiated = SimpleNamespace(id=42)
        with patch("funpay_service.Account") as account_class:
            account_class.return_value.get.return_value = initiated
            result = asyncio.run(
                FunPayService().account("golden-key", "http://127.0.0.1:8080")
            )

        self.assertIs(result, initiated)
        account_class.assert_called_once_with(
            "golden-key",
            user_agent=DEFAULT_USER_AGENT,
            requests_timeout=15,
            proxy={
                "http": "http://127.0.0.1:8080",
                "https": "http://127.0.0.1:8080",
            },
        )


class RaiseScheduleTests(unittest.TestCase):
    def test_success_uses_default_interval(self):
        outcomes = [RaiseOutcome(1, "Game", True)]
        self.assertEqual(next_raise_delay(outcomes), 4 * 60 * 60)

    def test_cooldown_uses_funpay_wait(self):
        outcomes = [RaiseOutcome(1, "Game", False, wait_seconds=125)]
        self.assertEqual(next_raise_delay(outcomes), 135)


class ChatHistoryServiceTests(unittest.TestCase):
    def test_only_requests_ten_most_recent_chat_histories(self):
        class Account:
            def __init__(self):
                self.requested_chat_ids = []

            def request_chats(self):
                return [
                    SimpleNamespace(id=index, name=f"Buyer {index}")
                    for index in range(15)
                ]

            def get_chats_histories(self, chat_names):
                self.requested_chat_ids = list(chat_names)
                return {chat_id: [] for chat_id in chat_names}

        account = Account()
        chats, histories = asyncio.run(FunPayService().chat_histories(account))

        self.assertEqual(len(chats), 10)
        self.assertEqual(account.requested_chat_ids, list(range(10)))
        self.assertEqual(set(histories), set(range(10)))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, telegram_id, text, **kwargs):
        self.sent.append((telegram_id, text, kwargs))


class FakeDatabase:
    def __init__(self):
        self.targets = []
        self.seen = set()
        self.greeted = set()
        self.cursors = {}

    async def save_chat_target(self, telegram_id, chat_id, username):
        self.targets.append((telegram_id, chat_id, username))

    async def claim_message(self, telegram_id, message_id, chat_id):
        key = (telegram_id, message_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    async def claim_greeting(self, telegram_id, chat_id):
        key = (telegram_id, str(chat_id))
        if key in self.greeted:
            return False
        self.greeted.add(key)
        return True

    async def release_greeting(self, telegram_id, chat_id):
        self.greeted.discard((telegram_id, str(chat_id)))

    async def get_chat_cursors(self, telegram_id):
        return dict(self.cursors)

    async def save_chat_cursors(self, telegram_id, cursors):
        self.cursors.update({str(chat_id): value for chat_id, value in cursors.items()})


class FakeFunPay:
    def __init__(
        self, latest=None, first_client_message=False, chats=None, histories=None
    ):
        self.latest = latest
        self.first_client_message = first_client_message
        self.replies = []
        self.chats = chats or []
        self.histories = histories or {}

    async def latest_message(self, account, chat_id, chat_name):
        return self.latest

    async def is_first_client_message(self, account, message):
        return self.first_client_message

    async def send_message(self, account, chat_id, text, chat_name):
        self.replies.append((chat_id, text, chat_name))

    async def chat_histories(self, account):
        return self.chats, self.histories


class NotificationTests(unittest.TestCase):
    @staticmethod
    def row():
        return {
            "telegram_id": 99,
            "message_notifications_enabled": True,
            "order_notifications_enabled": True,
            "greeting_enabled": False,
            "greeting_text": "Hello",
        }

    def test_message_notification_escapes_text_and_has_reply_target(self):
        bot = FakeBot()
        database = FakeDatabase()
        manager = NotificationManager(bot, database, None, None)
        message = SimpleNamespace(
            id=500,
            by_bot=False,
            author_id=7,
            author="<Buyer>",
            chat_name="Buyer",
            chat_id=123,
            text="<b>hello</b>",
            image_link=None,
        )
        event = SimpleNamespace(type=enums.EventTypes.NEW_MESSAGE, message=message)
        row = self.row()

        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))

        self.assertEqual(database.targets, [(99, 123, "<Buyer>")])
        _, text, kwargs = bot.sent[0]
        self.assertIn("&lt;Buyer&gt;", text)
        self.assertIn("&lt;b&gt;hello&lt;/b&gt;", text)
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "reply:123"
        )

    def test_existing_chat_change_fetches_latest_message_and_deduplicates(self):
        bot = FakeBot()
        database = FakeDatabase()
        latest = SimpleNamespace(
            id=501,
            by_bot=False,
            author_id=7,
            author="Buyer",
            chat_name="Buyer",
            chat_id=123,
            text="Existing chat message",
            image_link=None,
        )
        manager = NotificationManager(bot, database, None, FakeFunPay(latest=latest))
        chat = SimpleNamespace(id=123, name="Buyer", unread=True)
        event = SimpleNamespace(
            type=enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED, chat=chat
        )
        row = self.row()

        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))
        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))

        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Existing chat message", bot.sent[0][1])

    def test_direct_history_poll_detects_existing_chat_and_equal_text(self):
        bot = FakeBot()
        database = FakeDatabase()
        chat = SimpleNamespace(id=123, name="Buyer", unread=True)
        first = SimpleNamespace(
            id=501,
            by_bot=False,
            author_id=7,
            author="Buyer",
            chat_name="Buyer",
            chat_id=123,
            text="Same text",
            image_link=None,
        )
        second = SimpleNamespace(
            id=502,
            by_bot=False,
            author_id=7,
            author="Buyer",
            chat_name="Buyer",
            chat_id=123,
            text="Same text",
            image_link=None,
        )
        funpay = FakeFunPay(chats=[chat], histories={123: [first]})
        manager = NotificationManager(bot, database, None, funpay)

        asyncio.run(manager._poll_chat_histories(self.row(), SimpleNamespace(id=42)))
        funpay.histories = {123: [first, second]}
        asyncio.run(manager._poll_chat_histories(self.row(), SimpleNamespace(id=42)))

        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(database.cursors, {"123": 502})

    def test_direct_history_poll_baselines_read_chats_without_replay(self):
        bot = FakeBot()
        database = FakeDatabase()
        chat = SimpleNamespace(id=123, name="Buyer", unread=False)
        old = SimpleNamespace(
            id=500,
            by_bot=False,
            author_id=7,
            author="Buyer",
            chat_name="Buyer",
            chat_id=123,
            text="Old message",
            image_link=None,
        )
        funpay = FakeFunPay(chats=[chat], histories={123: [old]})
        manager = NotificationManager(bot, database, None, funpay)

        asyncio.run(manager._poll_chat_histories(self.row(), SimpleNamespace(id=42)))

        self.assertEqual(bot.sent, [])
        self.assertEqual(database.cursors, {"123": 500})

    def test_greeting_is_sent_once_for_first_client_message(self):
        bot = FakeBot()
        database = FakeDatabase()
        funpay = FakeFunPay(first_client_message=True)
        manager = NotificationManager(bot, database, None, funpay)
        message = SimpleNamespace(
            id=777,
            by_bot=False,
            author_id=7,
            author="Buyer",
            chat_name="Buyer",
            chat_id=321,
            text="Hi",
            image_link=None,
        )
        event = SimpleNamespace(type=enums.EventTypes.NEW_MESSAGE, message=message)
        row = {
            "telegram_id": 99,
            "message_notifications_enabled": False,
            "order_notifications_enabled": False,
            "greeting_enabled": True,
            "greeting_text": "Welcome!",
        }

        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))
        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))

        self.assertEqual(funpay.replies, [(321, "Welcome!", "Buyer")])


if __name__ == "__main__":
    unittest.main()
