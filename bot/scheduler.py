from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.service import DeliveryService
from config import DeliveryConfig


def create_scheduler(service: DeliveryService, config: DeliveryConfig, user_id: int) -> AsyncIOScheduler:
    zone = ZoneInfo(config.timezone)
    scheduler = AsyncIOScheduler(timezone=zone)
    scheduler.add_job(
        service.deliver, trigger=CronTrigger(hour=config.send_at.hour, minute=config.send_at.minute,
                                            second=config.send_at.second, timezone=zone),
        args=(user_id,), id="daily_perfume", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    return scheduler
