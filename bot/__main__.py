import argparse
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from dotenv import load_dotenv
from sqlalchemy import select, func
from sqlalchemy.engine import make_url

from bot.app import create_dispatcher
from bot.scheduler import create_scheduler
from bot.service import DeliveryService
from collectors.dataset import DatasetAdapter
from config import load_config
from storage.database import initialize_schema
from storage.repository import DatasetApproval, DatasetRepository, RecommendationRepository
from storage.models import Perfume
from storage.runtime import create_encrypted_engine


async def run(check: bool = False, healthcheck: bool = False) -> None:
    load_dotenv(override=False)
    config = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    database_url = os.environ.get("DATABASE_URL", "sqlite:///data/private.sqlite3")
    if healthcheck and not Path(make_url(database_url).database or "").is_file():
        raise RuntimeError("database is not initialized")
    engine = create_encrypted_engine(database_url, os.environ.get("DATABASE_ENCRYPTION_KEY"))
    bot = None
    scheduler = None
    try:
        if healthcheck:
            with engine.connect() as connection:
                total = connection.scalar(select(func.count()).select_from(Perfume))
                if total < 200:
                    raise RuntimeError("catalogue is incomplete")
            print("Encrypted storage is ready.")
            return
        initialize_schema(engine)
        manifest_path = Path("data/scento/manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        approval_values = {field: manifest[field] for field in DatasetApproval.__dataclass_fields__}
        approval_values["approved_at"] = datetime.fromisoformat(manifest["approved_at"].replace("Z", "+00:00"))
        batch = await DatasetAdapter(manifest_path.parent / manifest["artifact_file"], manifest_path,
                                     limit=manifest["limit"]).collect()
        result = DatasetRepository(engine, config).import_batch(batch, DatasetApproval(**approval_values))
        if check:
            print(f"Encrypted storage and offline catalogue ready: {result.total} perfumes.")
            return
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not owner.isdigit() or int(owner) <= 0:
            raise ValueError("TELEGRAM_BOT_TOKEN and a positive TELEGRAM_CHAT_ID are required")
        bot = Bot(token)
        repository = RecommendationRepository(engine, config.scoring)
        service = DeliveryService(repository, bot, timezone=config.delivery.timezone)
        dispatcher = create_dispatcher(service, repository, int(owner))
        scheduler = create_scheduler(service, config.delivery, int(owner))
        scheduler.start()
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        if bot is not None:
            await bot.session.close()
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Аромат дня")
    parser.add_argument("--check", action="store_true", help="check encrypted storage and import without Telegram")
    parser.add_argument("--healthcheck", action="store_true", help="read encrypted catalogue without initialization or Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # Library exceptions can contain update identifiers, URLs and request payloads.
    for name in ("aiogram", "apscheduler", "sqlalchemy", "aiohttp"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run(check=args.check, healthcheck=args.healthcheck))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Bot startup or execution failed. Check configuration, encrypted storage and service access.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
