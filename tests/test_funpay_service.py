import asyncio
import unittest
from types import SimpleNamespace

from funpay_service import (
    BalanceUnavailableError,
    FunPayService,
    NotificationManager,
    RaiseOutcome,
    next_raise_delay,
)
from FunPayAPI import enums


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


class RaiseScheduleTests(unittest.TestCase):
    def test_success_uses_default_interval(self):
        outcomes = [RaiseOutcome(1, "Game", True)]
        self.assertEqual(next_raise_delay(outcomes), 4 * 60 * 60)

    def test_cooldown_uses_funpay_wait(self):
        outcomes = [RaiseOutcome(1, "Game", False, wait_seconds=125)]
        self.assertEqual(next_raise_delay(outcomes), 135)


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


class FakeFunPay:
    def __init__(self, latest=None, first_client_message=False):
        self.latest = latest
        self.first_client_message = first_client_message
        self.replies = []

    async def latest_message(self, account, chat_id, chat_name):
        return self.latest

    async def is_first_client_message(self, account, message):
        return self.first_client_message

    async def send_message(self, account, chat_id, text, chat_name):
        self.replies.append((chat_id, text, chat_name))


class NotificationTests(unittest.TestCase):
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
        row = {
            "telegram_id": 99,
            "message_notifications_enabled": True,
            "order_notifications_enabled": True,
            "greeting_enabled": False,
            "greeting_text": "Hello",
        }

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
        row = {
            "telegram_id": 99,
            "message_notifications_enabled": True,
            "order_notifications_enabled": True,
            "greeting_enabled": False,
            "greeting_text": "Hello",
        }

        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))
        asyncio.run(manager._deliver_events(row, SimpleNamespace(id=42), [event]))

        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Existing chat message", bot.sent[0][1])

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
