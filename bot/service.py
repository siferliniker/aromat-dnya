import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from bot.cards import feedback_keyboard, format_card, visible_utf16_units
from storage.repository import RecommendationRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    reservation_id: int | None = None
    delivered: bool = False


class DeliveryService:
    def __init__(self, repository: RecommendationRepository, sender, timezone: str = "Asia/Omsk", *,
                 opinion_pipeline=None):
        self.repository, self.sender, self.timezone = repository, sender, ZoneInfo(timezone)
        self.opinion_pipeline = opinion_pipeline

    async def deliver(self, user_id: int, now: datetime | None = None) -> DeliveryResult:
        now = now or datetime.now(utc_timezone.utc)
        if now.utcoffset() is None:
            raise ValueError("delivery time must include a timezone")
        reservation = self.repository.reserve(user_id, now.astimezone(self.timezone).date(), now=now)
        if reservation.status != "new":
            return DeliveryResult(reservation.status, reservation.reservation_id)
        reservation_id = reservation.reservation_id
        card = reservation.card
        pipeline = self.opinion_pipeline
        if pipeline is not None and pipeline.enabled and self.repository.opinions is not None:
            try:
                card = await pipeline.prepare(card)
            except asyncio.CancelledError:
                self.repository.release(reservation_id, user_id)
                raise
            except Exception:
                self.repository.release(reservation_id, user_id)
                logger.warning("Opinion card preparation is unavailable")
                return DeliveryResult("unavailable", reservation_id)
            async with pipeline.publication(card, reservation_id, user_id) as allowed:
                if not allowed:
                    self.repository.release(reservation_id, user_id)
                    return DeliveryResult("unavailable", reservation_id)
                return await self._send(user_id, reservation_id, card)
        return await self._send(user_id, reservation_id, card)

    async def _send(self, user_id: int, reservation_id: int, card) -> DeliveryResult:
        try:
            text = format_card(card)
            if visible_utf16_units(text) > 4096:
                raise ValueError("card exceeds Telegram's text limit")
        except (ValueError, UnicodeError):
            self.repository.release(reservation_id, user_id)
            logger.warning("Card does not fit Telegram delivery constraints")
            return DeliveryResult("unavailable", reservation_id)
        try:
            message = await self.sender.send_message(
                chat_id=user_id, text=text,
                reply_markup=feedback_keyboard(card.perfume.id), parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
            self.repository.release(reservation_id, user_id)
            logger.warning("Telegram rejected delivery before acceptance")
            return DeliveryResult("failed", reservation_id)
        except asyncio.CancelledError:
            self.repository.mark_uncertain(reservation_id, user_id)
            raise
        except Exception:
            self.repository.mark_uncertain(reservation_id, user_id)
            logger.warning("Telegram delivery outcome is uncertain")
            return DeliveryResult("uncertain", reservation_id)
        # A persistence failure after acceptance must never trigger another Telegram call.
        try:
            confirmed = self.repository.confirm(reservation_id, user_id, message.message_id,
                                                datetime.now(utc_timezone.utc))
        except Exception:
            logger.warning("Delivery confirmation could not be persisted")
            return DeliveryResult("uncertain", reservation_id)
        return DeliveryResult("sent" if confirmed else "uncertain", reservation_id, delivered=confirmed)
