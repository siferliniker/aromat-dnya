"""Exercise the public bot modes with SQLCipher and synthetic external services."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
import httpx2
from openai import AsyncOpenAI, RateLimitError
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
import yaml

from bot.service import DeliveryResult
from collectors.http import AiohttpTransport
from collectors.permissions import AdmissionRegistry
from storage.database import initialize_schema
from storage.models import Mention, Source
from storage.opinions import OpinionRepository
from storage.runtime import create_encrypted_engine
from test_stage2_adapters import (
    FakeTransport, ROOT, SECOND_VIDEO, TOKEN, VIDEO, WATCH, captions, response, videos, youtube_policy,
)
from test_stage2_bot import OWNER, SyntheticSession, admission, command, configuration


DATABASE_KEY = "42" * 32
OPENAI_KEY = "qa-synthetic-responses-key"
TELEGRAM_TOKEN = "123456:qa-synthetic-no-live-access"
PERFUME = "Fierce Cologne"
HOUSE = "Abercrombie & Fitch"
QUOTE = f"{PERFUME} от {HOUSE}: нравится тёплый аккорд"


@pytest.fixture
def startup(monkeypatch, qa_config, tmp_path):
    pytest.importorskip("sqlcipher3", reason="CLI integration requires encrypted SQLCipher")
    cli = importlib.import_module("bot.__main__")
    manifest = tmp_path / "admissions.json"
    config_path = tmp_path / "config.yaml"
    database_url = f"sqlite:///{(tmp_path / 'private.sqlite3').as_posix()}"
    calls = SimpleNamespace(sdk=[], parsed=[], transport=FakeTransport(), first_parse_error=None)

    def skip_dotenv(**kwargs):
        return None

    class FakeSDK:
        def __init__(self, *, api_key=None, **kwargs):
            if api_key != OPENAI_KEY:
                pytest.fail("The CLI did not pass the synthetic OpenAI key")
            self.responses = self
            self.close_count = 0
            calls.sdk.append(self)

        def with_options(self, *, max_retries):
            assert max_retries == 0
            return self

        async def parse(self, *, text_format, **kwargs):
            calls.parsed.append(kwargs)
            if len(calls.parsed) == 1 and calls.first_parse_error is not None:
                raise calls.first_parse_error
            assert QUOTE in kwargs["input"]
            mention = {"perfume_name": PERFUME, "perfume_house": HOUSE, "sentiment": "positive",
                       "evidence_quote": QUOTE, "confidence": 0.8}
            return SimpleNamespace(status="completed", error=None,
                output=[SimpleNamespace(type="message", status="completed",
                                        content=[SimpleNamespace(type="output_text")])],
                output_parsed=text_format.model_validate({"mentions": [mention]}))

        async def close(self):
            self.close_count += 1

    async def fake_request(_transport, request):
        return await calls.transport.request(request)

    def forbidden_telegram(*args, **kwargs):
        pytest.fail("The refresh mode attempted to start Telegram")

    monkeypatch.setattr(cli, "load_dotenv", skip_dotenv)
    monkeypatch.setattr(cli, "AsyncOpenAI", FakeSDK, raising=False)
    monkeypatch.setattr(AiohttpTransport, "request", fake_request)
    monkeypatch.setattr(cli, "Bot", forbidden_telegram)
    monkeypatch.setattr(cli, "create_scheduler", forbidden_telegram)
    for name, value in {
        "CONFIG_PATH": str(config_path), "DATABASE_URL": database_url, "DATABASE_ENCRYPTION_KEY": DATABASE_KEY,
        "OPENAI_API_KEY": OPENAI_KEY, "YOUTUBE_OAUTH_ACCESS_TOKEN": TOKEN,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": str(OWNER),
    }.items():
        monkeypatch.setenv(name, value)

    def configure(enabled, *policies, retry_delay=None):
        config = configuration(qa_config) if enabled else qa_config
        if enabled:
            config.stage2.approvals_path = str(manifest)
            config.stage2.claims_path = str(tmp_path / "claims")
            if retry_delay is not None:
                config.stage2.llm.max_attempts = 2
                config.stage2.llm.retry_backoff_seconds = [retry_delay]
            manifest.write_text(json.dumps({"sources": [policy.model_dump(mode="json") for policy in policies]}),
                                encoding="utf-8")
        config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
        return config

    def invoke(*arguments):
        monkeypatch.setattr(sys, "argv", ["python -m bot", *arguments])
        return cli.main()

    return SimpleNamespace(cli=cli, calls=calls, configure=configure, invoke=invoke, database_url=database_url)


def policy_now(**changes):
    return youtube_policy(**({"approved_at": datetime.now(timezone.utc) - timedelta(days=1),
                              "expires_at": None} | changes))


@pytest.mark.parametrize(("enabled", "telegram_admitted"), [(False, False), (True, False), (True, True)],
                         ids=["stage1", "youtube-only", "stage2-with-telegram"])
def test_normal_cli_wires_one_delivery_service_and_dispatcher(startup, monkeypatch, enabled, telegram_admitted):
    policies = [policy_now()]
    if telegram_admitted:
        policies.append(admission("telegram", approved_at=policies[0].approved_at, expires_at=None))
    config = startup.configure(enabled, *policies)
    if not enabled:
        def forbidden_registry(*args, **kwargs):
            pytest.fail("Stage 1 attempted to load a stage 2 manifest")

        monkeypatch.setattr(startup.cli, "load_admission_registry", forbidden_registry, raising=False)

    observed = SimpleNamespace(poll_count=0, close_count=0)

    def telegram(token):
        session = SyntheticSession(None)

        async def close():
            observed.close_count += 1

        monkeypatch.setattr(session, "close", close)
        return Bot(token, session=session)

    def scheduler(service, delivery, owner):
        assert delivery == config.delivery
        assert owner == OWNER
        observed.scheduler_service = service
        observed.scheduled_delivery = AsyncMock(return_value=DeliveryResult("sent", delivered=True))
        monkeypatch.setattr(service, "deliver", observed.scheduled_delivery)
        return SimpleNamespace(start=lambda: None, running=False)

    make_dispatcher = startup.cli.create_dispatcher

    def dispatcher(service, repository, owner, **kwargs):
        observed.handler_service = service
        observed.handler_pipeline = kwargs.get("opinion_pipeline")
        return make_dispatcher(service, repository, owner, **kwargs)

    async def poll(self, bot, *, allowed_updates, **kwargs):
        observed.poll_count += 1
        observed.allowed_updates = allowed_updates
        await self.feed_update(bot, command("/today"))

    monkeypatch.setattr(startup.cli, "Bot", telegram)
    monkeypatch.setattr(startup.cli, "create_dispatcher", dispatcher)
    monkeypatch.setattr(startup.cli, "create_scheduler", scheduler)
    monkeypatch.setattr(Dispatcher, "start_polling", poll)

    assert startup.invoke() == 0
    assert observed.poll_count == observed.close_count == 1
    assert observed.handler_service is observed.scheduler_service
    observed.scheduled_delivery.assert_awaited_once_with(OWNER)
    assert ("channel_post" in observed.allowed_updates) is telegram_admitted
    assert ("edited_channel_post" in observed.allowed_updates) is telegram_admitted
    pipeline = observed.handler_pipeline
    assert observed.handler_service.opinion_pipeline is pipeline
    if enabled:
        assert pipeline is not None and pipeline.enabled
        assert (pipeline.telegram_adapter is not None) is telegram_admitted
        assert observed.handler_service.repository.opinions is pipeline.repository
        assert [sdk.close_count for sdk in startup.calls.sdk] == [1]
    else:
        assert pipeline is None and observed.handler_service.repository.opinions is None
        assert startup.calls.sdk == []
    assert startup.calls.transport.requests == [] and startup.calls.parsed == []


def test_explicit_disabled_refresh_is_safe_and_does_not_initialize_runtime(startup, monkeypatch, capsys):
    startup.configure(False)

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled refresh attempted to initialize stage 2")

    monkeypatch.setattr(startup.cli, "create_encrypted_engine", forbidden)
    monkeypatch.setattr(startup.cli, "load_admission_registry", forbidden, raising=False)

    assert startup.invoke("--refresh-opinions") == 0
    assert capsys.readouterr().out == "Opinion refresh skipped: stage 2 is disabled.\n"
    assert startup.calls.sdk == [] and startup.calls.transport.requests == []


def test_refresh_uses_only_canonical_allowlisted_videos_and_ingests_via_pipeline(startup, capsys, caplog):
    urls = [WATCH, f"https://youtu.be/{SECOND_VIDEO}", f"https://www.youtube.com/watch?v={SECOND_VIDEO}&t=1",
            f"https://www.youtube.com.evil.test/watch?v={SECOND_VIDEO}"]
    startup.configure(True, policy_now(approved_urls=urls))
    srt = f"1\n00:00:00,000 --> 00:00:03,500\n{QUOTE}\n".encode()
    startup.calls.transport = FakeTransport(videos(), captions(), response(body=srt))

    assert startup.invoke("--refresh-opinions") == 0
    output = capsys.readouterr()
    assert output.out == "Opinion refresh completed: 1 mentions.\n"
    requests = startup.calls.transport.requests
    assert [item.url for item in requests] == [f"{ROOT}/videos", f"{ROOT}/captions", f"{ROOT}/captions/qa-track-1"]
    assert requests[0].params["id"] == requests[1].params["videoId"] == VIDEO
    assert all(item.headers.get("Authorization") == f"Bearer {TOKEN}" for item in requests)
    assert len(startup.calls.parsed) == 1
    assert [sdk.close_count for sdk in startup.calls.sdk] == [1]
    engine = create_encrypted_engine(startup.database_url, DATABASE_KEY)
    try:
        with Session(engine) as session:
            assert session.execute(select(Mention.evidence_quote, Mention.source_url)).one() == (QUOTE, WATCH)
    finally:
        engine.dispose()
    for secret in (TOKEN, OPENAI_KEY, TELEGRAM_TOKEN, DATABASE_KEY):
        assert all(secret not in text for text in (output.out, output.err, caplog.text))


def test_refresh_does_not_follow_openai_redirects_or_trust_proxy_environment(startup, monkeypatch):
    startup.configure(True, policy_now(approved_urls=[WATCH]))
    srt = f"1\n00:00:00,000 --> 00:00:03,500\n{QUOTE}\n".encode()
    startup.calls.transport = FakeTransport(videos(), captions(), response(body=srt))
    requests, trust_environment = [], []
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("NO_PROXY", "")

    def openai_client(**kwargs):
        sdk = AsyncOpenAI(**kwargs)
        trust_environment.append(sdk._client.trust_env)
        return sdk

    async def handle(_transport, request):
        body = json.loads(await request.aread())
        requests.append((request.method, str(request.url), body.get("input") == QUOTE))
        if len(requests) == 1:
            return httpx2.Response(307, headers={"Location": "https://redirect.example.test/responses"})
        return httpx2.Response(403)

    monkeypatch.setattr(startup.cli, "AsyncOpenAI", openai_client)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", handle)

    assert startup.invoke("--refresh-opinions") == 1
    assert (requests, trust_environment) == (
        [("POST", "https://api.openai.com/v1/responses", True)], [False],
    )


@pytest.mark.parametrize("denial", ["missing-oauth", "manifest-deny", "durable-revocation"])
def test_refresh_denies_network_with_missing_oauth_or_revoked_permissions(startup, monkeypatch, capsys, caplog, denial):
    policy = policy_now(approved_urls=[WATCH])
    if denial == "manifest-deny":
        operations = policy.operations.model_dump() | {"derive": False}
        policy = policy_now(approved_urls=[WATCH], operations=operations)
    config = startup.configure(True, policy)
    if denial == "missing-oauth":
        monkeypatch.delenv("YOUTUBE_OAUTH_ACCESS_TOKEN")
        monkeypatch.setenv("YOUTUBE_API_KEY", TOKEN)
    if denial == "durable-revocation":
        engine = create_encrypted_engine(startup.database_url, DATABASE_KEY)
        try:
            initialize_schema(engine)
            repository = OpinionRepository(engine, config, AdmissionRegistry([policy]))
            repository.sync_admissions(now=datetime.now(timezone.utc))
            repository.block_source("youtube")
        finally:
            engine.dispose()

    assert startup.invoke("--refresh-opinions") == 1
    assert startup.calls.transport.requests == [] and startup.calls.parsed == []
    output = capsys.readouterr()
    for secret in (TOKEN, OPENAI_KEY, TELEGRAM_TOKEN, DATABASE_KEY):
        assert all(secret not in text for text in (output.out, output.err, caplog.text))
    if denial == "durable-revocation":
        engine = create_encrypted_engine(startup.database_url, DATABASE_KEY)
        try:
            with Session(engine) as session:
                source = session.scalar(select(Source).where(Source.key == "youtube"))
                assert source.permission_status == "blocked" and not source.enabled
        finally:
            engine.dispose()


def test_refresh_rechecks_durable_revocation_after_openai_retry_delay(startup, monkeypatch, capsys, caplog):
    policy = policy_now(approved_urls=[WATCH])
    delay = 0.125
    config = startup.configure(True, policy, retry_delay=delay)
    srt = f"1\n00:00:00,000 --> 00:00:03,500\n{QUOTE}\n".encode()
    startup.calls.transport = FakeTransport(videos(), captions(), response(body=srt))
    request = httpx2.Request("POST", "https://example.test/v1/responses")
    response_429 = httpx2.Response(429, request=request)
    canary = "QA_SYNTHETIC_PRIVATE_PROVIDER_ERROR"
    startup.calls.first_parse_error = RateLimitError(canary, response=response_429, body={"message": canary})
    original_sleep = asyncio.sleep
    revocations = []

    async def revoke_during_backoff(seconds):
        if seconds != delay:
            await original_sleep(seconds)
            return
        engine = create_encrypted_engine(startup.database_url, DATABASE_KEY)
        try:
            repository = OpinionRepository(engine, config, AdmissionRegistry([policy]))
            repository.block_source("youtube")
            revocations.append(True)
        finally:
            engine.dispose()

    monkeypatch.setattr("llm.service.asyncio.sleep", revoke_during_backoff)

    assert startup.invoke("--refresh-opinions") == 1
    assert revocations == [True]
    assert len(startup.calls.parsed) == 1
    output = capsys.readouterr()
    assert all(canary not in value for value in (output.out, output.err, caplog.text))
