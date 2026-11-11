from html import escape
from math import isfinite

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.service import DeliveryService
from bot.cards import visible_utf16_units
from collectors.permissions import PermissionDenied
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
                      allowed_user_id: int, *, opinion_pipeline=None) -> Dispatcher:
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
                "/known — уже знаю и купил, /blogs — доверенные блогеры и веса. "
                "Ежедневная отправка — по времени в конфигурации. "
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
            "unavailable": "Карточка недоступна: данные или разрешения изменились либо текст не помещается. "
                           "Отправка не выполнялась; можно повторить /today позже.",
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

    async def blogs(message: Message):
        if not authorized(message):
            return
        if opinion_pipeline is None or not opinion_pipeline.enabled:
            await message.answer("Доверенные блогеры пока не подключены: этап мнений выключен.")
            return
        parts = (message.text or "").split()
        opinions = opinion_pipeline.repository
        if len(parts) > 1:
            try:
                if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) <= 0:
                    raise ValueError("invalid command")
                weight = float(parts[2])
                if not isfinite(weight) or not 0 <= weight <= 1:
                    raise ValueError("invalid weight")
                changed = opinions.set_blogger_weight(int(parts[1]), weight, now=opinion_pipeline.clock())
            except (ValueError, TypeError, PermissionDenied):
                await message.answer("Формат: /blogs <ID блогера> <вес от 0 до 1>, например /blogs 1 0.5. "
                                     "Менять можно только допущенных блогеров.")
                return
            if not changed:
                await message.answer("Допущенный блогер с таким ID не найден.")
                return
        bloggers = opinions.list_bloggers(now=opinion_pipeline.clock())
        lines = ["<b>Доверенные блогеры</b>", "Изменить вес: /blogs &lt;ID&gt; &lt;0..1&gt;."]
        for blogger in bloggers:
            name = escape(blogger.name)
            if blogger.channel_url:
                name = f'<a href="{escape(blogger.channel_url, quote=True)}">{name}</a>'
            lines.append(f"{blogger.id}: {name} — вес {blogger.weight:g}")
        if not bloggers:
            lines.append("Допущенных блогеров пока нет.")
        chunk = ""
        for line in lines:
            if visible_utf16_units(chunk + line + "\n") > 4096:
                await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)

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
                             ("wishlist", wishlist), ("known", known), ("blogs", blogs),
                             ("delete_my_data", delete_my_data)):
        dispatcher.message.register(handler, Command(command))
    dispatcher.callback_query.register(feedback, F.data.startswith("feedback:"))
    if opinion_pipeline is not None and opinion_pipeline.enabled and opinion_pipeline.telegram_adapter is not None:
        async def channel_post(message: Message):
            await opinion_pipeline.consume_telegram(message)

        async def edited_channel_post(message: Message):
            await opinion_pipeline.consume_telegram(message, edited=True)

        dispatcher.channel_post.register(channel_post)
        dispatcher.edited_channel_post.register(edited_channel_post)
    return dispatcher
