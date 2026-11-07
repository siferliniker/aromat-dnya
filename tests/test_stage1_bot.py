import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import Chat, Message, Update, User
from sqlalchemy import text

from bot.app import create_dispatcher
from bot.scheduler import create_scheduler
from bot.service import DeliveryService
from storage.repository import RecommendationRepository


OWNER = 777000123
NOW = datetime(2026, 11, 7, 4, tzinfo=timezone.utc)


class FakeSession(BaseSession):
    """No external HTTP: exercise the real Dispatcher and Bot method layer."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 300

    async def close(self):
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            self.next_id += 1
            return Message(
                message_id=self.next_id, date=NOW,
                chat=Chat(id=method.chat_id, type="private"), text=method.text,
                from_user=User(id=bot.id, is_bot=True, first_name="Synthetic bot"),
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Synthetic bot", username="qa_aromat_bot")
        raise AssertionError(f"Unexpected outbound Telegram method: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Network download is forbidden in QA")
        yield b""  # This is an async-generator interface, never executed.


def command(text_value, *, user=OWNER, chat=OWNER, chat_type="private", update_id=1):
    return Update.model_validate({
        "update_id": update_id,
        "message": {"message_id": update_id, "date": NOW, "text": text_value,
                    "entities": [{"type": "bot_command", "offset": 0, "length": len(text_value.split()[0])}],
                    "from": {"id": user, "is_bot": False, "first_name": "Synthetic user"},
                    "chat": {"id": chat, "type": chat_type}},
    })


def callback(data, message_id, *, user=OWNER, chat=OWNER, chat_type="private", update_id=10):
    return Update.model_validate({
        "update_id": update_id,
        "callback_query": {"id": f"qa-{update_id}", "chat_instance": "qa-instance", "data": data,
                           "from": {"id": user, "is_bot": False, "first_name": "Synthetic user"},
                           "message": {"message_id": message_id, "date": NOW, "text": "QA card",
                                       "chat": {"id": chat, "type": chat_type}}},
    })


@pytest.fixture
def harness(qa_engine, qa_config):
    session = FakeSession()
    bot = Bot("123456:synthetic-test-token-no-live-access", session=session)
    repository = RecommendationRepository(qa_engine, qa_config.scoring)
    service = DeliveryService(repository, bot, timezone="Asia/Omsk")
    dispatcher = create_dispatcher(service, repository, allowed_user_id=OWNER)
    return SimpleNamespace(session=session, bot=bot, repository=repository, service=service, dispatcher=dispatcher)


def send_commands(harness, *updates):
    async def run():
        for update in updates:
            await harness.dispatcher.feed_update(harness.bot, update)
    asyncio.run(run())


def db_rows(engine, table):
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT * FROM {table}")).mappings().all()


def cards(harness):
    return [call for call in harness.session.calls if isinstance(call, SendMessage) and call.reply_markup is not None]


def test_start_help_and_privacy_commands_use_real_dispatcher_without_delivering(harness, qa_engine):
    send_commands(harness, command("/start"), command("/privacy", update_id=2))
    text_value = "\n".join(call.text for call in harness.session.calls if isinstance(call, SendMessage))
    assert all(name in text_value for name in ["/today", "/wishlist", "/known", "/delete_my_data"])
    assert not db_rows(qa_engine, "sent_log")


@pytest.mark.parametrize("user,chat,chat_type", [
    (OWNER + 1, OWNER + 1, "private"), (OWNER + 1, OWNER, "private"),
    (OWNER, -100456, "supergroup"), (OWNER, OWNER + 1, "private"),
])
def test_owner_only_requires_private_chat_and_matching_from_user(harness, qa_seed, qa_engine, user, chat, chat_type):
    qa_seed()
    send_commands(harness, command("/today", user=user, chat=chat, chat_type=chat_type))
    assert not db_rows(qa_engine, "sent_log")
    assert not cards(harness)


def test_today_sends_exactly_one_card_with_four_bound_feedback_buttons(harness, qa_seed, qa_engine):
    qa_seed()
    send_commands(harness, command("/today"), command("/today", update_id=2))
    assert len(cards(harness)) == 1
    buttons = [button for row in cards(harness)[0].reply_markup.inline_keyboard for button in row]
    assert {button.text for button in buttons} == {"Хочу попробовать", "Уже знаю", "Не моё", "Купил"}
    assert len({button.callback_data for button in buttons}) == 4
    log = db_rows(qa_engine, "sent_log")
    assert len(log) == 1 and log[0]["status"] == "sent"
    assert log[0]["telegram_message_id"] is not None


def test_callbacks_are_bound_to_the_sent_message_and_feedback_changes_lists(harness, qa_seed, qa_engine):
    qa_seed()
    send_commands(harness, command("/today"))
    card = cards(harness)[0]
    buttons = {button.text: button.callback_data for row in card.reply_markup.inline_keyboard for button in row}
    message_id = db_rows(qa_engine, "sent_log")[0]["telegram_message_id"]
    send_commands(harness,
        callback(buttons["Хочу попробовать"], message_id + 10),
        callback(buttons["Хочу попробовать"], message_id, user=OWNER + 1, update_id=11),
        callback(buttons["Хочу попробовать"], message_id, chat=-100456, chat_type="supergroup", update_id=12),
    )
    assert not db_rows(qa_engine, "user_feedback")
    send_commands(harness,
        callback(buttons["Хочу попробовать"], message_id, update_id=13),
        command("/wishlist", update_id=14),
    )
    assert db_rows(qa_engine, "user_feedback")[0]["reaction"] == "want"
    perfume = db_rows(qa_engine, "perfumes")
    sent_id = db_rows(qa_engine, "sent_log")[0]["perfume_id"]
    sent_name = next(row["name"] for row in perfume if row["id"] == sent_id)
    assert sent_name in harness.session.calls[-1].text
    send_commands(harness,
        callback(buttons["Купил"], message_id, update_id=15),
        command("/known", update_id=16),
    )
    assert len(db_rows(qa_engine, "user_feedback")) == 1
    assert db_rows(qa_engine, "user_feedback")[0]["reaction"] == "bought"
    assert sent_name in harness.session.calls[-1].text


def test_delete_command_removes_feedback_and_sent_log(harness, qa_seed, qa_engine):
    qa_seed()
    send_commands(harness, command("/today"), command("/delete_my_data", update_id=2))
    assert not db_rows(qa_engine, "sent_log")
    assert not db_rows(qa_engine, "user_feedback")
    assert db_rows(qa_engine, "perfumes")


class FailingSender:
    def __init__(self, error, engine):
        self.error = error
        self.engine = engine
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        assert len(db_rows(self.engine, "sent_log")) == 1
        assert db_rows(self.engine, "sent_log")[0]["status"] == "reserved"
        raise self.error


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramForbiddenError])
def test_confirmed_telegram_rejection_releases_only_current_reservation(qa_engine, qa_config, qa_seed, error_type):
    qa_seed()
    request = SendMessage(chat_id=OWNER, text="QA")
    sender = FailingSender(error_type(method=request, message="synthetic refusal"), qa_engine)
    repository = RecommendationRepository(qa_engine, qa_config.scoring)
    result = asyncio.run(DeliveryService(repository, sender, timezone="Asia/Omsk").deliver(OWNER, now=NOW))
    assert result.status == "failed"
    assert not db_rows(qa_engine, "sent_log")


@pytest.mark.parametrize("error", [
    TimeoutError("synthetic timeout"),
    TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="QA"), message="synthetic disconnect"),
    RuntimeError("synthetic unknown outcome"),
])
def test_uncertain_telegram_outcome_persists_and_prevents_retry(qa_engine, qa_config, qa_seed, error):
    qa_seed()
    sender = FailingSender(error, qa_engine)
    repository = RecommendationRepository(qa_engine, qa_config.scoring)
    service = DeliveryService(repository, sender, timezone="Asia/Omsk")
    async def run():
        first = await service.deliver(OWNER, now=NOW)
        second = await service.deliver(OWNER, now=NOW)
        return first, second
    first, second = asyncio.run(run())
    assert first.status == second.status == "uncertain"
    assert sender.calls == 1
    assert db_rows(qa_engine, "sent_log")[0]["status"] == "uncertain"


def test_scheduler_runs_daily_at_configured_omsk_time_without_starting_network(harness, qa_config):
    scheduler = create_scheduler(harness.service, qa_config.delivery, OWNER)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    trigger = jobs[0].trigger
    assert str(trigger.timezone) == "Asia/Omsk"
    start = datetime(2026, 11, 7, 3, 59, tzinfo=timezone.utc)
    first = trigger.get_next_fire_time(None, start)
    second = trigger.get_next_fire_time(first, first)
    assert first.astimezone(timezone.utc) == datetime(2026, 11, 7, 4, tzinfo=timezone.utc)
    assert second.astimezone(timezone.utc) == datetime(2026, 11, 8, 4, tzinfo=timezone.utc)


def test_delivery_calendar_uses_omsk_and_overlapping_calls_do_not_send_twice(qa_engine, qa_config, qa_seed):
    qa_seed()
    repository = RecommendationRepository(qa_engine, qa_config.scoring)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Sender:
            calls = 0
            async def send_message(self, **kwargs):
                self.calls += 1
                assert db_rows(qa_engine, "sent_log")[0]["status"] == "reserved"
                entered.set()
                await release.wait()
                return SimpleNamespace(message_id=555)
        sender = Sender()
        service = DeliveryService(repository, sender, timezone="Asia/Omsk")
        # Already the next calendar day in Omsk, still November 6 in UTC.
        now = datetime(2026, 11, 6, 18, 1, tzinfo=timezone.utc)
        active = asyncio.create_task(service.deliver(OWNER, now=now))
        await asyncio.wait_for(entered.wait(), timeout=3)
        overlapping = await service.deliver(OWNER, now=now)
        release.set()
        completed = await active
        return sender.calls, overlapping, completed
    count, overlapping, completed = asyncio.run(run())
    assert count == 1
    assert overlapping.status == "reserved"
    assert completed.status == "sent"
    stored = db_rows(qa_engine, "sent_log")
    assert len(stored) == 1
    assert str(stored[0]["local_date"]) == "2026-11-07"


def test_cancelled_sender_keeps_uncertain_reservation_before_propagating(qa_engine, qa_config, qa_seed):
    qa_seed()
    sender = FailingSender(asyncio.CancelledError(), qa_engine)
    repository = RecommendationRepository(qa_engine, qa_config.scoring)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(DeliveryService(repository, sender, timezone="Asia/Omsk").deliver(OWNER, now=NOW))
    assert db_rows(qa_engine, "sent_log")[0]["status"] == "uncertain"
