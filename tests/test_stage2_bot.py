"""Independent stage-two bot RED/acceptance tests; all providers are synthetic."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import importlib
import json
from types import SimpleNamespace

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import Chat, Message, Update, User
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from bot.app import create_dispatcher
from bot.cards import format_card
from bot.service import DeliveryService
from collectors.contracts import MentionRecord, ReviewTextRecord
from collectors.opinion_versions import ReviewSnapshot
from collectors.permissions import AdmissionRegistry, PermissionDenied, SourceAdmission
from collectors.telegram import TelegramAdapter
from collectors.youtube import YouTubeAdapter
from llm.cache import SQLAlchemyLLMCache
from llm.claims import FileClaimStore
from llm.contracts import CardSummary, ReviewExtraction
from llm.errors import RequestRejected
from llm.service import ReviewAnalyzer
from scoring.engine import score_opinions
from storage.models import Blogger, Mention, ScoreComponent, ScoreEvidence, ScoreSnapshot, SentLog, Source, SourceRevision
from storage.opinions import OpinionRepository
from storage.repository import RecommendationRepository
from test_stage2_adapters import FakeTransport, ROOT, SRT, TOKEN, VIDEO, captions, response, videos, youtube_policy


OWNER = 777000123
NOW = datetime(2026, 11, 8, 6, tzinfo=timezone.utc)
APPROVED = NOW - timedelta(days=1)
URL = "https://example.test/review/one"
CHANNEL = -1001234567890
TG_URL = "https://t.me/qa_public/11"
QUOTE = "QA perfume 1 от QA house: нравится древесный аккорд"
OPERATIONS = ("fetch", "store", "llm", "derive", "quote", "golden")


def configuration(config):
    values = config.model_dump(mode="python")
    for key in ("otzovik", "telegram", "youtube"):
        values["sources"][key]["enabled"] = True
    values["llm"] = {"provider": "openai", "enabled": True, "model": "qa-offline-model"}
    values["stage2"] = {
        "enabled": True, "approvals_path": "synthetic-unused.json", "claims_path": "synthetic-unused",
        "freshness_seconds": {key: 3600 for key in ("otzovik", "telegram", "youtube")},
        "fetch": {"request_timeout_seconds": 2.0, "source_deadline_seconds": 4.0,
                  "max_attempts": 1, "retry_backoff_seconds": [],
                  "min_request_interval_seconds": 0.0, "max_items_per_source": 5},
        "llm": {"request_timeout_seconds": 2.0, "max_attempts": 1, "retry_backoff_seconds": [],
                "max_input_chars": 20000, "max_output_tokens": 8000,
                "max_requests_per_run": 30, "max_concurrent_requests": 2},
    }
    return type(config).model_validate(values)


def admission(key="otzovik", **changes):
    blog = key in ("youtube", "telegram")
    values = dict(
        source_key=key, kind="blog" if blog else "review", name="QA <author> & source",
        platform=key, enabled=True, permission_status="approved", scope="public_review",
        terms_url="https://example.test/terms", approval_ref=f"qa-rights-{key}",
        platform_approval_ref="qa-platform-rights", author_approval_ref="qa-author-rights",
        approved_at=APPROVED, expires_at=NOW + timedelta(days=1), max_age_seconds=3600,
        operations={operation: True for operation in OPERATIONS},
        approved_urls=[TG_URL] if key == "telegram" else [URL],
        bloggers=([dict(external_id=str(CHANNEL) if key == "telegram" else "qa-author",
                       name="QA <author> & blogger", channel_url="https://t.me/qa_public" if key == "telegram"
                       else "https://example.test/channel", weight=0.75)] if blog else []),
        evidence_role="bloggers" if blog else "russian_reviews",
    ) | changes
    return SourceAdmission.model_validate(values)


def snapshot(key="otzovik", *, observed=NOW, quote=QUOTE, text_value=None):
    return ReviewSnapshot(ReviewTextRecord(
        external_id=f"{CHANNEL}:11" if key == "telegram" else "qa-document",
        source_url=TG_URL if key == "telegram" else URL,
        blogger_external_id=(str(CHANNEL) if key == "telegram" else "qa-author")
        if key in ("telegram", "youtube") else None,
        text=text_value if text_value is not None else quote + ".",
    ), observed, f"qa-rights-{key}", APPROVED)


def store(repo, key="otzovik", *, observed=NOW, quote=QUOTE):
    item = snapshot(key, observed=observed, quote=quote)
    return repo.store_review_snapshot(key, item.review, item.observed_at, item.approval_ref,
        item.approved_at, [MentionRecord(external_id="qa", perfume_external_id="qa-1",
        perfume_source_key="dataset", source_url=item.review.source_url,
        blogger_external_id=item.review.blogger_external_id, sentiment="positive", evidence_quote=quote)], now=NOW)


def no_writer(engine):
    """An independent SQLite writer must be able to commit while external I/O runs."""
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=100")
        connection.execute(text("UPDATE sources SET name=name WHERE key='dataset'"))


class SyntheticClient:
    def __init__(self, engine):
        self.engine, self.calls = engine, []
        self.quote, self.error, self.on_summary = QUOTE, None, None

    async def parse(self, **kwargs):
        no_writer(self.engine)
        self.calls.append(kwargs)
        if kwargs["schema"] is ReviewExtraction:
            if self.error is not None:
                raise self.error
            return ReviewExtraction.model_validate({"mentions": [] if "NO_MENTIONS" in kwargs["input_text"] else [{
                "perfume_name": "QA perfume 1", "perfume_house": "QA house", "sentiment": "positive",
                "evidence_quote": self.quote, "confidence": 0.8,
            }]})
        assert kwargs["schema"] is CardSummary
        if self.on_summary is not None:
            await self.on_summary()
        item = json.loads(kwargs["input_text"])["evidence"][0]
        return CardSummary(text=f"Подтверждённые мнения:\nПоложительно: «{item['quote']}» [m{item['mention_id']}]",
                           evidence_mention_ids=[item["mention_id"]])


class SyntheticSession(BaseSession):
    def __init__(self, engine):
        super().__init__()
        self.engine, self.calls, self.next_id = engine, [], 500
        self.error = None

    async def close(self):
        return None

    async def make_request(self, bot, method, timeout=None):
        no_writer(self.engine)
        self.calls.append(method)
        if isinstance(method, SendMessage):
            if self.error is not None:
                raise self.error
            self.next_id += 1
            return Message(message_id=self.next_id, date=NOW, chat=Chat(id=method.chat_id, type="private"),
                           text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Synthetic", username="qa_synthetic_bot")
        raise AssertionError(f"Unexpected outbound Telegram operation: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Network is forbidden")
        yield b""


def command(value, *, user=OWNER, chat=OWNER, chat_type="private", identifier=1):
    return Update.model_validate({"update_id": identifier, "message": {
        "message_id": identifier, "date": NOW, "text": value,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(value.split()[0])}],
        "from": {"id": user, "is_bot": False, "first_name": "Synthetic"},
        "chat": {"id": chat, "type": chat_type},
    }})


def channel_update(*, edited=False, identifier=2, text_value=QUOTE):
    return Update.model_validate({"update_id": identifier,
        "edited_channel_post" if edited else "channel_post": {
            "message_id": 11, "date": NOW, "text": text_value,
            "chat": {"id": CHANNEL, "type": "channel", "title": "QA", "username": "qa_public"},
        }})


@pytest.fixture
def data(qa_engine, qa_config, qa_seed):
    perfume_id = qa_seed(1)[0]
    config = configuration(qa_config)
    registry = AdmissionRegistry([admission(), admission("youtube"), admission("telegram")])
    repo = OpinionRepository(qa_engine, config, registry)
    repo.sync_admissions(now=NOW)
    return SimpleNamespace(engine=qa_engine, config=config, registry=registry, repo=repo, perfume_id=perfume_id)


def make_pipeline(data, tmp_path, *, client=None):
    client = client or SyntheticClient(data.engine)
    analyzer = ReviewAnalyzer(client=client,
        cache=SQLAlchemyLLMCache(data.engine, allow_plaintext_for_tests=True),
        claims=FileClaimStore(tmp_path / "synthetic-claims"), config=data.config,
        approvals={key: data.registry.to_llm_approval(key, now=NOW) for key in data.registry.admissions},
        clock=lambda: NOW, live_admitted=True)
    pipeline_type = importlib.import_module("bot.opinions").OpinionPipeline
    return pipeline_type(data.repo, analyzer, telegram_adapter=TelegramAdapter(data.config, data.registry, lambda: NOW),
                         clock=lambda: NOW), client, analyzer


@pytest.fixture
def youtube_collection(data, tmp_path):
    policy = youtube_policy()
    data.registry.admissions["youtube"] = policy
    data.repo.sync_admissions(now=NOW)
    pipeline, client, _ = make_pipeline(data, tmp_path)
    transport = FakeTransport(videos(), captions(), response(body=SRT))
    adapter = YouTubeAdapter(data.config, AdmissionRegistry([policy]), transport, TOKEN, (VIDEO,), lambda: NOW)
    return pipeline, client, adapter, transport


def bot_harness(data, pipeline):
    session = SyntheticSession(data.engine)
    bot = Bot("123456:qa-synthetic-no-live-access", session=session)
    repo = RecommendationRepository(data.engine, data.config.scoring, opinions=data.repo)
    service = DeliveryService(repo, bot, timezone="Asia/Omsk", opinion_pipeline=pipeline)
    dispatcher = create_dispatcher(service, repo, OWNER, opinion_pipeline=pipeline)
    return SimpleNamespace(session=session, bot=bot, repo=repo, service=service, dispatcher=dispatcher)


def run_async(awaitable):
    async def bounded():
        return await asyncio.wait_for(awaitable, timeout=5)
    return asyncio.run(bounded())


def feed(harness, *updates):
    async def run():
        for update in updates:
            await harness.dispatcher.feed_update(harness.bot, update)
    run_async(run())


def rows(data, model):
    with Session(data.engine) as session:
        return session.scalars(select(model).order_by(model.id)).all()


def test_data_catalog_and_live_operation_gate_do_not_widen_rights(data):
    catalog = data.repo.load_catalog()
    assert [(item.id, item.source_key, item.external_id) for item in catalog] == [(data.perfume_id, "dataset", "qa-1")]
    assert data.repo.check_source("youtube", "derive", now=NOW, source_url=URL,
                                  blogger_external_id="qa-author").source_key == "youtube"
    data.repo.block_source("youtube")
    with pytest.raises(PermissionDenied):
        data.repo.check_source("youtube", "derive", now=NOW, source_url=URL, blogger_external_id="qa-author")


def test_owner_trust_override_survives_sync_restart_and_preserves_past_snapshot(data):
    store(data.repo, "youtube")
    initial = score_opinions(data.repo.load_evidence(data.perfume_id, now=NOW), data.config.scoring)
    old_id = data.repo.save_score(data.perfume_id, initial, computed_at=NOW)
    blogger = next(item for item in data.repo.list_bloggers(now=NOW) if item.source_key == "youtube")
    assert data.repo.set_blogger_weight(blogger.id, 0.2, now=NOW) is True
    data.repo.sync_admissions(now=NOW)
    restarted = OpinionRepository(data.engine, data.config, data.registry)
    restarted.sync_admissions(now=NOW)
    assert next(item.weight for item in restarted.list_bloggers(now=NOW) if item.id == blogger.id) == 0.2
    current = restarted.load_evidence(data.perfume_id, now=NOW)
    assert current.blogger_mentions[0].applied_blogger_weight == 0.2
    new_id = restarted.save_score(data.perfume_id, score_opinions(current, data.config.scoring), computed_at=NOW)
    with Session(data.engine) as session:
        def weights(identifier):
            return tuple(session.scalars(select(ScoreEvidence.applied_blogger_weight).join(ScoreComponent).where(
                ScoreComponent.snapshot_id == identifier, ScoreEvidence.applied_blogger_weight.is_not(None))))
        assert weights(old_id) == (0.75,)
        assert weights(new_id) == (0.2,)


@pytest.mark.parametrize("value", [True, False, "0.5", None, float("nan"), float("inf"), -0.01, 1.01])
def test_data_trust_setter_rejects_nonfinite_bool_and_nonnumeric_values_without_mutation(data, value):
    blogger = data.repo.list_bloggers(now=NOW)[0]
    with pytest.raises((TypeError, ValueError)):
        data.repo.set_blogger_weight(blogger.id, value, now=NOW)
    assert data.repo.list_bloggers(now=NOW)[0].weight == blogger.weight


def test_data_zero_trust_is_persisted_and_suppresses_bloggers_without_granting_rights(data):
    store(data.repo, "youtube")
    blogger = next(item for item in data.repo.list_bloggers(now=NOW) if item.source_key == "youtube")
    before = data.registry.admissions["youtube"].model_dump(mode="python")
    assert data.repo.set_blogger_weight(blogger.id, 0.0, now=NOW)
    data.repo.sync_admissions(now=NOW)
    assert data.repo.load_evidence(data.perfume_id, now=NOW).blogger_mentions == ()
    assert data.registry.admissions["youtube"].model_dump(mode="python") == before
    assert data.repo.set_blogger_weight(99999999, 0.5, now=NOW) is False
    data.repo.block_source("youtube")
    with pytest.raises(PermissionDenied):
        data.repo.set_blogger_weight(blogger.id, 0.5, now=NOW)


def test_load_and_save_use_caller_transaction_without_committing_or_closing_it(data):
    store(data.repo)
    with Session(data.engine) as session:
        session.begin()
        inputs = data.repo.load_evidence(data.perfume_id, now=NOW, session=session)
        identifier = data.repo.save_score(data.perfume_id, score_opinions(inputs, data.config.scoring),
                                         computed_at=NOW, session=session)
        assert session.in_transaction() and session.get(ScoreSnapshot, identifier) is not None
        session.rollback()
        assert session.scalar(select(ScoreSnapshot.id)) is None


def test_snapshot_lookup_is_durable_exact_and_checks_current_source(data):
    item = snapshot()
    assert data.repo.lookup_snapshot("otzovik", item.review, item.observed_at,
        item.approval_ref, item.approved_at, now=NOW) is None
    stored = store(data.repo)
    restarted = OpinionRepository(data.engine, data.config, data.registry)
    found = restarted.lookup_snapshot("otzovik", item.review, item.observed_at,
        item.approval_ref, item.approved_at, now=NOW)
    assert found.revision_id == stored.revision_id and found.imported_mentions == 0 and found.total_mentions == 1
    changed = snapshot(text_value=QUOTE + ". changed body")
    with pytest.raises(ValueError):
        restarted.lookup_snapshot("otzovik", changed.review, changed.observed_at,
            changed.approval_ref, changed.approved_at, now=NOW)
    data.repo.block_source("otzovik")
    with pytest.raises(PermissionDenied):
        restarted.lookup_snapshot("otzovik", item.review, item.observed_at,
            item.approval_ref, item.approved_at, now=NOW)


def test_pipeline_ingest_cache_full_score_and_owner_card_preserve_persisted_lineage(data, tmp_path):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    async def run():
        item = snapshot("youtube", observed=NOW - timedelta(seconds=20))
        first = await pipeline.ingest("youtube", item)
        second = await pipeline.ingest("youtube", item)
        # A newly accepted fetch with the same exact text uses the checked LLM cache.
        await pipeline.ingest("youtube", snapshot("youtube", observed=NOW - timedelta(seconds=10)))
        return first, second
    first, second = run_async(run())
    assert first.imported_mentions == 1 and second.imported_mentions == 0
    assert len(client.calls) == 1
    harness = bot_harness(data, pipeline)
    result = run_async(harness.service.deliver(OWNER, now=NOW))
    assert result.delivered and result.status == "sent"
    assert len(client.calls) == 2
    outbound = [call for call in harness.session.calls if isinstance(call, SendMessage) and call.reply_markup]
    assert len(outbound) == 1 and "Подтверждённые мнения:" in outbound[0].text
    assert QUOTE in outbound[0].text and "B: блогеры" in outbound[0].text
    with Session(data.engine) as session:
        log = session.scalar(select(SentLog))
        score = session.get(ScoreSnapshot, log.score_snapshot_id)
        assert score.algorithm_version == "opinions-v1"
        evidence = session.execute(select(ScoreEvidence).join(ScoreComponent).where(
            ScoreComponent.snapshot_id == score.id)).scalars().all()
        current_id = data.repo.load_evidence(data.perfume_id, now=NOW).blogger_mentions[0].id
        mention_refs = [item for item in evidence if item.mention_id is not None]
        assert {item.mention_id for item in mention_refs} == {current_id}
        assert all(item.applied_blogger_weight == 0.75 for item in mention_refs)
        assert rows(data, Mention)[0].id != current_id
        assert all(item.mention_id is not None or item.rating_observation_id is not None for item in evidence)
    again = run_async(harness.service.deliver(OWNER, now=NOW))
    assert again.status == "sent" and not again.delivered and len(harness.session.calls) == 1


def test_stage2_daily_delivery_never_repeats_across_days(data, tmp_path, qa_seed):
    qa_seed(1)
    pipeline, _, _ = make_pipeline(data, tmp_path)
    run_async(pipeline.ingest("otzovik", snapshot()))
    harness = bot_harness(data, pipeline)
    async def run():
        first = await harness.service.deliver(OWNER, now=NOW)
        repeated = await harness.service.deliver(OWNER, now=NOW)
        tomorrow = await harness.service.deliver(OWNER, now=NOW + timedelta(days=1))
        exhausted = await harness.service.deliver(OWNER, now=NOW + timedelta(days=2))
        return first, repeated, tomorrow, exhausted
    first, repeated, tomorrow, exhausted = run_async(run())
    assert first.delivered and tomorrow.delivered
    assert not repeated.delivered and exhausted.status == "empty"
    logs = rows(data, SentLog)
    assert len(logs) == 2 and len({item.perfume_id for item in logs}) == 2
    assert len(harness.session.calls) == 2


def test_stage2_uncertain_send_is_persisted_and_never_replayed(data, tmp_path):
    pipeline, _, _ = make_pipeline(data, tmp_path)
    run_async(pipeline.ingest("otzovik", snapshot()))
    harness = bot_harness(data, pipeline)
    harness.session.error = TimeoutError("synthetic uncertain acceptance")
    async def run():
        return await harness.service.deliver(OWNER, now=NOW), await harness.service.deliver(OWNER, now=NOW)
    first, second = run_async(run())
    assert first.status == second.status == "uncertain"
    assert len(harness.session.calls) == 1 and rows(data, SentLog)[0].status == "uncertain"


def test_stage2_overlapping_delivery_observes_reserved_while_summary_awaits(data, tmp_path):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    run_async(pipeline.ingest("otzovik", snapshot()))
    harness = bot_harness(data, pipeline)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold_summary():
            entered.set()
            await release.wait()
        client.on_summary = hold_summary
        active = asyncio.create_task(harness.service.deliver(OWNER, now=NOW))
        await asyncio.wait_for(entered.wait(), timeout=3)
        second = await harness.service.deliver(OWNER, now=NOW)
        release.set()
        completed = await asyncio.wait_for(active, timeout=3)
        return second, completed
    second, completed = run_async(run())
    assert second.status == "reserved" and completed.delivered
    assert len(harness.session.calls) == 1 and len(client.calls) == 2


def test_new_document_without_mentions_suppresses_old_evidence_and_keeps_immutable_history(data, tmp_path):
    pipeline, _, _ = make_pipeline(data, tmp_path)
    async def run():
        await pipeline.ingest("otzovik", snapshot(observed=NOW - timedelta(seconds=20)))
        await pipeline.ingest("otzovik", snapshot(text_value="NO_MENTIONS"))
    run_async(run())
    assert data.repo.load_evidence(data.perfume_id, now=NOW).russian_mentions == ()
    assert len(rows(data, Mention)) == 1


def test_extract_failure_records_empty_revision_and_same_envelope_never_pays_or_enriches_it(data, tmp_path):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    async def run():
        await pipeline.ingest("otzovik", snapshot(observed=NOW - timedelta(seconds=20)))
        failed_snapshot = snapshot(observed=NOW - timedelta(seconds=10), text_value=QUOTE + ". Updated body.")
        client.error = RequestRejected()
        with pytest.raises(Exception):
            await pipeline.ingest("otzovik", failed_snapshot)
        assert data.repo.load_evidence(data.perfume_id, now=NOW).russian_mentions == ()
        calls = len(client.calls)
        client.error = None
        restarted, _, _ = make_pipeline(data, tmp_path / "restart", client=client)
        await restarted.ingest("otzovik", failed_snapshot)
        assert len(client.calls) == calls
        assert data.repo.load_evidence(data.perfume_id, now=NOW).russian_mentions == ()
        await restarted.ingest("otzovik", snapshot())
        assert len(data.repo.load_evidence(data.perfume_id, now=NOW).russian_mentions) == 1
    run_async(run())


@pytest.mark.parametrize("operation", ["store", "llm", "derive"])
def test_pipeline_preflight_denies_each_operation_before_provider_or_db_write(data, tmp_path, operation):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    current = data.registry.admissions["otzovik"].model_dump(mode="python")
    current["operations"][operation] = False
    data.registry.admissions["otzovik"] = SourceAdmission.model_validate(current)
    with pytest.raises(Exception):
        run_async(pipeline.ingest("otzovik", snapshot()))
    assert client.calls == [] and rows(data, Mention) == []
    assert len(rows(data, SourceRevision)) == 1  # The offline dataset revision only.


def test_youtube_revoke_during_rate_limit_wait_stops_further_http(data, youtube_collection):
    pipeline, client, adapter, transport = youtube_collection
    data.config.stage2.fetch.min_request_interval_seconds = 1.0

    async def run():
        waiting, resume = asyncio.Event(), asyncio.Event()

        async def pause(_delay):
            waiting.set()
            await resume.wait()

        adapter.sleep = pause
        active = asyncio.create_task(pipeline.collect(adapter))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            assert [request.url for request in transport.requests] == [f"{ROOT}/videos"]
            await pipeline.revoke("youtube")
            with pytest.raises(PermissionDenied):
                data.repo.check_source("youtube", "fetch", now=NOW)
        finally:
            resume.set()
        return await active

    result = run_async(run())
    assert [request.url for request in transport.requests] == [f"{ROOT}/videos"]
    assert result.status == "failed" and client.calls == [] and rows(data, Mention) == []


@pytest.mark.parametrize("denial", ["blocked", "missing", "fetch", "store", "llm", "derive"])
def test_youtube_collection_preflight_denies_http_before_first_request(data, youtube_collection, denial):
    pipeline, client, adapter, transport = youtube_collection
    if denial == "blocked":
        data.repo.block_source("youtube")
    elif denial == "missing":
        with Session(data.engine) as session, session.begin():
            source = session.scalar(select(Source).where(Source.key == "youtube"))
            session.execute(delete(Blogger).where(Blogger.source_id == source.id))
            session.delete(source)
    else:
        current = data.registry.admissions["youtube"].model_dump(mode="python")
        current["operations"][denial] = False
        data.registry.admissions["youtube"] = SourceAdmission.model_validate(current)

    result = run_async(pipeline.collect(adapter))
    assert [request.url for request in transport.requests] == []
    assert result.status == "failed" and client.calls == [] and rows(data, Mention) == []
    assert len(rows(data, SourceRevision)) == 1  # The offline dataset revision only.


def test_block_during_summary_cannot_publish_stale_inmemory_card_or_restore_cache(data, tmp_path):
    pipeline, client, analyzer = make_pipeline(data, tmp_path)
    run_async(pipeline.ingest("otzovik", snapshot()))
    harness = bot_harness(data, pipeline)
    async def block():
        await pipeline.revoke("otzovik")
    client.on_summary = block
    async def run():
        return await asyncio.wait_for(harness.service.deliver(OWNER, now=NOW), timeout=3)
    result = run_async(run())
    assert not result.delivered
    assert not [call for call in harness.session.calls if isinstance(call, SendMessage) and call.reply_markup]
    assert rows(data, Mention) == [] and analyzer.live_admitted is False
    with Session(data.engine) as session:
        source = session.scalar(select(Source).where(Source.key == "otzovik"))
        assert source.permission_status == "blocked" and not source.enabled
        assert session.scalar(select(SentLog.id).where(SentLog.status == "sent")) is None


def test_current_source_block_outside_pipeline_still_rechecks_before_publication(data, tmp_path):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    run_async(pipeline.ingest("otzovik", snapshot()))
    harness = bot_harness(data, pipeline)
    async def block():
        data.repo.block_source("otzovik")
    client.on_summary = block
    result = run_async(harness.service.deliver(OWNER, now=NOW))
    assert not result.delivered and not harness.session.calls


@pytest.mark.parametrize("user,chat,chat_type", [
    (OWNER + 1, OWNER + 1, "private"), (OWNER + 1, OWNER, "private"),
    (OWNER, OWNER + 1, "private"), (OWNER, -100555, "supergroup"),
])
def test_blogs_is_owner_private_only(data, tmp_path, user, chat, chat_type):
    pipeline, _, _ = make_pipeline(data, tmp_path)
    harness = bot_harness(data, pipeline)
    blogger = data.repo.list_bloggers(now=NOW)[0]
    feed(harness, command(f"/blogs {blogger.id} 0.1", user=user, chat=chat, chat_type=chat_type))
    assert data.repo.list_bloggers(now=NOW)[0].weight == 0.75
    assert harness.session.calls == []


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "-0.1", "1.1", "true", "0,5", "0.5 trailing"])
def test_blogs_invalid_weight_does_not_change_trust(data, tmp_path, value):
    pipeline, _, _ = make_pipeline(data, tmp_path)
    harness = bot_harness(data, pipeline)
    blogger = data.repo.list_bloggers(now=NOW)[0]
    feed(harness, command(f"/blogs {blogger.id} {value}"))
    assert data.repo.list_bloggers(now=NOW)[0].weight == 0.75
    assert rows(data, SentLog) == []


def test_blogs_lists_escaped_names_and_updates_current_weight_only(data, tmp_path):
    pipeline, _, _ = make_pipeline(data, tmp_path)
    harness = bot_harness(data, pipeline)
    store(data.repo, "youtube")
    blogger = next(item for item in data.repo.list_bloggers(now=NOW) if item.source_key == "youtube")
    admissions_before = {key: value.model_dump(mode="python") for key, value in data.registry.admissions.items()}
    feed(harness, command("/blogs"), command(f"/blogs {blogger.id} 0.25", identifier=2), command("/blogs", identifier=3))
    assert next(item.weight for item in data.repo.list_bloggers(now=NOW) if item.id == blogger.id) == 0.25
    assert "0.25" in harness.session.calls[-1].text
    assert data.repo.load_evidence(data.perfume_id, now=NOW).blogger_mentions[0].applied_blogger_weight == 0.25
    assert {key: value.model_dump(mode="python") for key, value in data.registry.admissions.items()} == admissions_before
    assert rows(data, SentLog) == [] and rows(data, ScoreSnapshot) == []


def test_channel_post_and_edit_use_same_real_dispatcher_and_latest_evidence(data, tmp_path):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    harness = bot_harness(data, pipeline)
    feed(harness, channel_update())
    assert len(data.repo.load_evidence(data.perfume_id, now=NOW).blogger_mentions) == 1
    # A real edit is a fresh delivery; the injected clock advances without a new poller.
    later = NOW + timedelta(seconds=1)
    pipeline.clock = lambda: later
    pipeline.telegram_adapter.clock = lambda: later
    feed(harness, channel_update(edited=True, identifier=3, text_value="NO_MENTIONS"))
    assert data.repo.load_evidence(data.perfume_id, now=later).blogger_mentions == ()
    assert len(client.calls) == 2 and harness.session.calls == []
    used = harness.dispatcher.resolve_used_update_types()
    assert "channel_post" in used and "edited_channel_post" in used


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "a"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, value):
        self.parts.append(value)


@pytest.mark.parametrize("quote", [QUOTE + " <script>x</script> & 😀", QUOTE + " 😀" * 2500],
                         ids=["escaped-html-astral", "oversized-full-quote"])
def test_whole_card_has_valid_escaped_html_and_utf16_budget_without_partial_quote(data, tmp_path, quote):
    pipeline, client, _ = make_pipeline(data, tmp_path)
    client.quote = quote
    run_async(pipeline.ingest("otzovik", snapshot(quote=quote)))
    repo = RecommendationRepository(data.engine, data.config.scoring, opinions=data.repo)
    reserved = repo.reserve(OWNER, date(2026, 11, 8), now=NOW)
    prepared = run_async(pipeline.prepare(reserved.card))
    rendered = format_card(prepared)
    parsed = TelegramHTML()
    parsed.feed(rendered)
    parsed.close()
    visible = "".join(parsed.parts)
    assert not parsed.stack and len(visible.encode("utf-16-le")) // 2 <= 4096
    assert "<script>" not in rendered
    if prepared.summary is not None:
        assert quote in visible
    else:
        assert "резюме" in visible.lower() and ("недоступ" in visible.lower() or "нет" in visible.lower())


def test_stage2_disabled_retains_existing_dataset_delivery_without_provider(data, qa_config, tmp_path):
    disabled = qa_config
    repo = OpinionRepository(data.engine, disabled, data.registry)
    pipeline_type = importlib.import_module("bot.opinions").OpinionPipeline
    class ForbiddenAnalyzer:
        async def extract(self, **kwargs):
            raise AssertionError("Disabled stage2 cannot use a provider")
        async def summarize(self, **kwargs):
            raise AssertionError("Disabled stage2 cannot use a provider")
    pipeline = pipeline_type(repo, ForbiddenAnalyzer(), clock=lambda: NOW)
    session = SyntheticSession(data.engine)
    bot = Bot("123456:qa-synthetic-no-live-access", session=session)
    recommendation = RecommendationRepository(data.engine, disabled.scoring)
    service = DeliveryService(recommendation, bot, opinion_pipeline=pipeline)
    result = run_async(service.deliver(OWNER, now=NOW))
    assert result.delivered and rows(data, ScoreSnapshot)[0].algorithm_version != "opinions-v1"
