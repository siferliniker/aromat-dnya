import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from dotenv import load_dotenv
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from sqlalchemy import select, func
from sqlalchemy.engine import Engine, make_url

from bot.app import create_dispatcher
from bot.opinions import OpinionPipeline, OpinionProcessingError
from bot.scheduler import create_scheduler
from bot.service import DeliveryService
from collectors.dataset import DatasetAdapter
from collectors.http import AiohttpTransport
from collectors.permissions import PermissionDenied, SourceAdmission, load_admission_registry
from collectors.telegram import TelegramAdapter
from collectors.youtube import YouTubeAdapter
from config import AppConfig, load_config
from llm.cache import SQLAlchemyLLMCache
from llm.claims import FileClaimStore
from llm.client import OpenAIResponsesClient
from llm.errors import AdmissionDenied
from llm.service import ReviewAnalyzer
from storage.database import initialize_schema
from storage.repository import DatasetApproval, DatasetRepository, RecommendationRepository
from storage.models import Perfume
from storage.opinions import OpinionRepository
from storage.runtime import create_encrypted_engine


_YOUTUBE_WATCH_URL = re.compile(r"https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_opinion_repository(engine: Engine, config: AppConfig) -> OpinionRepository:
    registry = load_admission_registry(config.stage2.approvals_path)
    repository = OpinionRepository(engine, config, registry)
    repository.sync_admissions(now=_utc_now())
    return repository


def _check_processing_source(
    repository: OpinionRepository, source_key: str, *, now: datetime, source_url: str | None = None,
) -> SourceAdmission:
    for operation in ("fetch", "store", "llm", "derive"):
        admission = repository.check_source(source_key, operation, now=now, source_url=source_url)
    return admission


def _open_opinion_pipeline(
    repository: OpinionRepository, resources: AsyncExitStack, *, listen_to_telegram: bool = False,
) -> OpinionPipeline:
    config, registry = repository.config, repository.registry
    now = _utc_now()
    approvals = {}
    telegram = None
    for source_key in registry.admissions:
        try:
            admission = _check_processing_source(repository, source_key, now=now)
            approvals[source_key] = registry.to_llm_approval(source_key, now=now)
        except PermissionDenied:
            continue
        if (listen_to_telegram and source_key == "telegram" and admission.platform == "telegram"
                and admission.kind == "blog" and admission.scope == "public_review"):
            telegram = TelegramAdapter(config, registry, _utc_now)

    def check_current_source(source_key: str, source_url: str, now: datetime) -> None:
        try:
            _check_processing_source(repository, source_key, now=now, source_url=source_url)
        except PermissionDenied:
            raise AdmissionDenied() from None

    cache = SQLAlchemyLLMCache(repository.engine)
    claims = FileClaimStore(config.stage2.claims_path)
    client = None
    if approvals:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for admitted opinion sources")
        http_client = DefaultAsyncHttpxClient(follow_redirects=False, trust_env=False)
        try:
            sdk = AsyncOpenAI(api_key=api_key, base_url="https://api.openai.com/v1", max_retries=0,
                              http_client=http_client)
        except BaseException:
            resources.push_async_callback(http_client.aclose)
            raise
        resources.push_async_callback(sdk.close)
        client = OpenAIResponsesClient(sdk)
    analyzer = ReviewAnalyzer(client=client, cache=cache, claims=claims, config=config,
        approvals=approvals, clock=_utc_now, live_admitted=bool(approvals), source_gate=check_current_source)
    return OpinionPipeline(repository, analyzer, telegram_adapter=telegram, clock=_utc_now)


async def _refresh_opinions(repository: OpinionRepository, resources: AsyncExitStack) -> None:
    admission = _check_processing_source(repository, "youtube", now=_utc_now())
    if admission.platform != "youtube" or admission.kind != "blog" or admission.scope != "public_review":
        raise PermissionDenied("YouTube source is outside the supported scope")
    video_ids = [match[1] for url in admission.approved_urls if (match := _YOUTUBE_WATCH_URL.fullmatch(url))]
    token = os.environ.get("YOUTUBE_OAUTH_ACCESS_TOKEN")
    if not token or not video_ids:
        raise PermissionDenied("creator OAuth and admitted YouTube watch URLs are required")
    pipeline = _open_opinion_pipeline(repository, resources)
    adapter = YouTubeAdapter(repository.config, repository.registry, AiohttpTransport(), token, video_ids, _utc_now)
    result = await pipeline.collect(adapter)
    if result.status != "ok":
        raise OpinionProcessingError()
    print(f"Opinion refresh completed: {result.imported_mentions} mentions.")


def _stop_scheduler(scheduler) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def run(check: bool = False, healthcheck: bool = False, *, refresh_opinions: bool = False) -> None:
    load_dotenv(override=False)
    config = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    stage2_enabled = config.stage2 is not None and config.stage2.enabled
    if refresh_opinions:
        if not stage2_enabled:
            print("Opinion refresh skipped: stage 2 is disabled.")
            return
        youtube = config.sources.get("youtube")
        if youtube is None or not youtube.enabled:
            print("Opinion refresh skipped: YouTube is disabled.")
            return
    database_url = os.environ.get("DATABASE_URL", "sqlite:///data/private.sqlite3")
    if healthcheck and not Path(make_url(database_url).database or "").is_file():
        raise RuntimeError("database is not initialized")
    async with AsyncExitStack() as resources:
        engine = create_encrypted_engine(database_url, os.environ.get("DATABASE_ENCRYPTION_KEY"))
        resources.callback(engine.dispose)
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
        opinions = _load_opinion_repository(engine, config) if stage2_enabled else None
        if refresh_opinions:
            await _refresh_opinions(opinions, resources)
            return
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not owner.isdigit() or int(owner) <= 0:
            raise ValueError("TELEGRAM_BOT_TOKEN and a positive TELEGRAM_CHAT_ID are required")
        pipeline = _open_opinion_pipeline(opinions, resources, listen_to_telegram=True) if opinions else None
        bot = Bot(token)
        resources.push_async_callback(bot.session.close)
        repository = RecommendationRepository(engine, config.scoring, opinions=opinions,
                                              recommendation_config=config.recommendation)
        service = DeliveryService(repository, bot, timezone=config.delivery.timezone, opinion_pipeline=pipeline)
        dispatcher = create_dispatcher(service, repository, int(owner), opinion_pipeline=pipeline)
        scheduler = create_scheduler(service, config.delivery, int(owner))
        resources.callback(_stop_scheduler, scheduler)
        scheduler.start()
        # Keep Stage 2 update cancellation within the polling task before closing its shared client.
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types(),
                                       close_bot_session=False, handle_as_tasks=not stage2_enabled)


def main() -> int:
    parser = argparse.ArgumentParser(description="Аромат дня")
    parser.add_argument("--check", action="store_true", help="check encrypted storage and import without Telegram")
    parser.add_argument("--healthcheck", action="store_true", help="read encrypted catalogue without initialization or Telegram")
    parser.add_argument("--refresh-opinions", action="store_true", help="collect admitted YouTube opinions without Telegram")
    args = parser.parse_args()
    if args.refresh_opinions and (args.check or args.healthcheck):
        parser.error("--refresh-opinions cannot be combined with --check or --healthcheck")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # Library exceptions can contain update identifiers, URLs and request payloads.
    for name in ("aiogram", "apscheduler", "sqlalchemy", "aiohttp", "openai", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run(check=args.check, healthcheck=args.healthcheck, refresh_opinions=args.refresh_opinions))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if args.refresh_opinions:
            print(f"Opinion refresh failed ({type(error).__name__}). Check approved YouTube access, encrypted storage and OpenAI configuration.")
        else:
            print(f"Bot startup or execution failed ({type(error).__name__}). Check configuration, encrypted storage and service access.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
