from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.service import DeliveryService
from storage.repository import REACTIONS, RecommendationRepository


PRIVACY_TEXT = (
    "Храню локально ваш Telegram ID, реакции и журнал отправок; имена и тексты сообщений не сохраняю. "
    "Рабочая база шифруется, ключ хранится отдельно. Данные не передаются в LLM. "
    "Telegram получает сообщения и нажатия согласно своим условиям. "
    "/delete_my_data удаляет реакции, журнал и профиль вкуса из локальной базы. "
    "После удаления рекомендации могут повториться; сообщения в Telegram остаются. "
    "Для прекращения ежедневных отправок остановите контейнер бота."
)


def create_dispatcher(service: DeliveryService, repository: RecommendationRepository,
                      allowed_user_id: int) -> Dispatcher:
    if allowed_user_id <= 0:
        raise ValueError("a positive private Telegram user ID is required")
    dispatcher = Dispatcher()

    def authorized(message: Message) -> bool:
        return bool(message.chat.type == "private" and message.chat.id == allowed_user_id
                    and message.from_user and not message.from_user.is_bot
                    and message.from_user.id == allowed_user_id)

    async def start(message: Message):
        if authorized(message):
            await message.answer(
                "Один аромат в сутки: /today — получить сегодня, /wishlist — хочу попробовать, "
                "/known — уже знаю и купил. Ежедневная отправка — по времени в конфигурации. "
                "/privacy — о данных, /delete_my_data — удалить мои данные.\n\n" + PRIVACY_TEXT)

    async def privacy(message: Message):
        if authorized(message):
            await message.answer(PRIVACY_TEXT)

    async def today(message: Message):
        if not authorized(message):
            return
        result = await service.deliver(allowed_user_id)
        text = {
            "sent": "", "reserved": "Выдача на сегодня зарезервирована. Результат отправки ещё не подтверждён.",
            "uncertain": "Исход отправки на сегодня неясен. Повтор отключён, чтобы избежать дубля.",
            "empty": "На сегодня нет подходящего аромата с доступными данными.",
            "failed": "Telegram отклонил отправку. Можно повторить /today позже.",
        }[result.status]
        # A successful new send is the card itself. Repeated /today needs a status message.
        if result.status == "sent" and not result.delivered:
            text = "Карточка на сегодня уже отправлена."
        if text:
            await message.answer(text)

    async def wishlist(message: Message):
        await show_list(message, ["want"], "Хочу попробовать")

    async def known(message: Message):
        await show_list(message, ["known", "bought"], "Уже знаю и купил")

    async def show_list(message: Message, reactions: list[str], title: str):
        if not authorized(message):
            return
        perfumes = repository.list_feedback(allowed_user_id, reactions)
        lines = [title] + [f"• {p.name} — {p.house}" for p in perfumes]
        if not perfumes:
            lines.append("Список пока пуст.")
        # Split on whole entries so a growing wishlist stays within Telegram's limit.
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await message.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await message.answer(chunk)

    async def delete_my_data(message: Message):
        if authorized(message):
            repository.delete_user_data(allowed_user_id)
            await message.answer("Локальные реакции, журнал и профиль удалены. Рекомендации могут повторяться. "
                                 "Сообщения в Telegram остаются. Для остановки рассылки остановите контейнер.")

    async def feedback(query: CallbackQuery):
        message = query.message
        if (query.from_user.id != allowed_user_id or query.from_user.is_bot
                or not isinstance(message, Message) or message.chat.type != "private"
                or message.chat.id != allowed_user_id):
            await query.answer("Недоступно")
            return
        parts = (query.data or "").split(":")
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in REACTIONS:
            await query.answer("Неизвестная кнопка")
            return
        accepted = repository.feedback(allowed_user_id, int(parts[1]), parts[2], message.message_id)
        await query.answer("Сохранено" if accepted else "Карточка не подтверждена для этого сообщения")

    for command, handler in (("start", start), ("privacy", privacy), ("today", today),
                             ("wishlist", wishlist), ("known", known), ("delete_my_data", delete_my_data)):
        dispatcher.message.register(handler, Command(command))
    dispatcher.callback_query.register(feedback, F.data.startswith("feedback:"))
    return dispatcher
