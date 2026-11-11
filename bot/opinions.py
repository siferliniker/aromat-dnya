"""Connect admitted snapshots, checked analysis and the existing delivery path."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging

from sqlalchemy.orm import Session

from bot.cards import format_card, visible_utf16_units
from collectors.contracts import MentionRecord
from collectors.opinion_versions import CollectedReviews, ReviewSnapshot
from collectors.permissions import PermissionDenied
from collectors.telegram import TelegramAdapter
from collectors.youtube import YouTubeAdapter
from llm.errors import LLMError
from llm.policy import aware
from llm.service import ReviewAnalyzer
from llm.validation import validate_summary
from scoring.engine import score_opinions
from storage.models import Perfume, ScoreSnapshot, SentLog, Source
from storage.opinions import OpinionImportResult, OpinionRepository, PurgeResult
from storage.repository import RecommendationCard


logger = logging.getLogger(__name__)


class OpinionProcessingError(RuntimeError):
    def __init__(self):
        super().__init__("opinion_processing_failed")


@dataclass(frozen=True)
class CollectionResult:
    source_key: str
    imported_mentions: int
    failed_snapshots: int
    status: str


class OpinionPipeline:
    def __init__(self, repository: OpinionRepository, analyzer: ReviewAnalyzer, *,
                 telegram_adapter: TelegramAdapter | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.repository, self.analyzer = repository, analyzer
        self.telegram_adapter, self.clock = telegram_adapter, clock
        self.analyzer.clock = lambda: self.clock()
        self._generation = 0
        self._suspended = False
        self._lifecycle = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        stage2 = self.repository.config.stage2
        return bool(stage2 is not None and stage2.enabled)

    def _admit(self) -> None:
        if not self.enabled or self._suspended:
            raise PermissionDenied("opinion pipeline is disabled or suspended")

    def _preflight(self, source_key: str, snapshot: ReviewSnapshot) -> None:
        self._admit()
        now, review = self.clock(), snapshot.review
        if not aware(now) or not aware(snapshot.observed_at) or snapshot.observed_at > now:
            raise ValueError("snapshot must have an aware fetched timestamp")
        for operation in ("fetch", "store", "llm", "derive"):
            admission = self.repository.check_source(source_key, operation, now=now,
                source_url=review.source_url, blogger_external_id=review.blogger_external_id)
            if (snapshot.approval_ref != admission.approval_ref
                    or snapshot.approved_at != admission.approved_at):
                raise PermissionDenied("snapshot approval differs from current source admission")
            configured = self.repository.config.stage2.freshness_seconds.get(source_key)
            limits = [value for value in (configured, admission.max_age_seconds) if value is not None]
            if not limits or (now - snapshot.observed_at).total_seconds() > min(limits):
                raise PermissionDenied("snapshot is outside the admitted freshness policy")

    async def ingest(self, source_key: str, snapshot: ReviewSnapshot) -> OpinionImportResult:
        self._preflight(source_key, snapshot)
        existing = self.repository.lookup_snapshot(source_key, snapshot.review, snapshot.observed_at,
            snapshot.approval_ref, snapshot.approved_at, now=self.clock())
        if existing is not None:
            return existing
        generation = self._generation
        try:
            extraction = await self.analyzer.extract(source_key=source_key, review=snapshot.review,
                catalog=self.repository.load_catalog(), observed_at=snapshot.observed_at)
        except asyncio.CancelledError:
            self._store_failed_observation(source_key, snapshot, generation)
            raise
        except Exception:
            # A failed new fetch supersedes the previous version, without inventing opinions.
            self._store_failed_observation(source_key, snapshot, generation)
            raise OpinionProcessingError() from None
        if generation != self._generation:
            raise PermissionDenied("opinion processing was revoked")
        self._preflight(source_key, snapshot)
        review = snapshot.review
        mentions = [MentionRecord(external_id=f"resolved:{item.perfume_id}",
            perfume_external_id=item.perfume_external_id, perfume_source_key=item.catalog_source_key,
            source_url=review.source_url, published_at=review.published_at,
            blogger_external_id=review.blogger_external_id, sentiment=item.mention.sentiment,
            evidence_quote=item.mention.evidence_quote, confidence=item.mention.confidence)
            for item in extraction.mentions]
        return self.repository.store_review_snapshot(source_key, review, snapshot.observed_at,
            snapshot.approval_ref, snapshot.approved_at, mentions, now=self.clock())

    def _store_failed_observation(self, source_key: str, snapshot: ReviewSnapshot, generation: int) -> None:
        if generation != self._generation:
            raise PermissionDenied("opinion processing was revoked")
        self._preflight(source_key, snapshot)
        self.repository.store_review_snapshot(source_key, snapshot.review, snapshot.observed_at,
            snapshot.approval_ref, snapshot.approved_at, (), now=self.clock())

    async def collect(self, adapter: YouTubeAdapter) -> CollectionResult:
        self._admit()
        source_key = adapter.source_key
        generation = self._generation

        def check_collection(source_url: str | None = None, blogger_external_id: str | None = None) -> None:
            self._admit()
            if generation != self._generation:
                raise PermissionDenied("opinion collection was revoked")
            now = self.clock()
            for operation in ("fetch", "store", "llm", "derive"):
                self.repository.check_source(source_key, operation, now=now,
                    source_url=source_url, blogger_external_id=blogger_external_id)

        try:
            check_collection()
            collected = await adapter.collect_snapshots(request_gate=check_collection)
            check_collection()
            if not isinstance(collected, CollectedReviews) or collected.source_key != source_key:
                raise ValueError("collector snapshot source differs")
        except Exception:
            logger.warning("Opinion collection failed (adapter_error)")
            return CollectionResult(source_key, 0, 1, "failed")
        imported, failures = 0, 0
        for snapshot in collected.snapshots:
            try:
                result = await self.ingest(source_key, snapshot)
                imported += result.imported_mentions
            except Exception:
                failures += 1
                logger.warning("Opinion collection failed (processing_error)")
        return CollectionResult(source_key, imported, failures, "failed" if failures else "ok")

    async def consume_telegram(self, message, *, edited: bool = False) -> OpinionImportResult | None:
        if not self.enabled or self._suspended or self.telegram_adapter is None:
            return None
        key = "edited_channel_post" if edited else "channel_post"
        payload = message.model_dump(mode="python", exclude_none=True)
        snapshot = self.telegram_adapter.consume({key: payload})
        if snapshot is None:
            return None
        try:
            return await self.ingest(self.telegram_adapter.source_key, snapshot)
        except Exception:
            logger.warning("Opinion collection failed (processing_error)")
            return None

    async def prepare(self, card: RecommendationCard) -> RecommendationCard:
        if not self.enabled or card.score.algorithm_version != "opinions-v1":
            return card
        self._admit()
        generation = self._generation
        prepared = replace(card, summary=None, summary_status="unavailable", pipeline_generation=generation)
        evidence = self.repository.load_summary_evidence(card.perfume.id, now=self._card_time(card))
        # Calculate against the complete fallback card. The extra newline replaces its status line.
        fallback = "LLM-резюме недоступно: нет пригодных подтверждённых цитат."
        budget = 4096 - visible_utf16_units(format_card(prepared)) + visible_utf16_units(fallback)
        if evidence and budget > 0:
            try:
                summary = await self.analyzer.summarize(perfume_id=card.perfume.id, evidence=evidence,
                                                       max_utf16_units=min(4096, budget))
                if summary is not None:
                    candidate = replace(prepared, summary=summary, summary_status="ready")
                    if visible_utf16_units(format_card(candidate)) <= 4096:
                        prepared = candidate
            except LLMError:
                logger.warning("Opinion summary is unavailable")
        if generation != self._generation or self._suspended:
            raise PermissionDenied("opinion publication was revoked")
        return prepared

    def _card_time(self, card: RecommendationCard) -> datetime:
        now = self.clock()
        return max(now, card.computed_at) if card.computed_at is not None else now

    def _current_card(self, card: RecommendationCard, reservation_id: int, user_id: int) -> bool:
        self._admit()
        if card.pipeline_generation != self._generation or card.score_snapshot_id is None:
            return False
        now = self._card_time(card)
        with Session(self.repository.engine) as session:
            reservation = session.get(SentLog, reservation_id)
            snapshot = session.get(ScoreSnapshot, card.score_snapshot_id)
            perfume = session.get(Perfume, card.perfume.id)
            source = session.get(Source, perfume.source_id) if perfume else None
            if (reservation is None or reservation.user_id != user_id or reservation.status != "reserved"
                    or reservation.perfume_id != card.perfume.id
                    or reservation.score_snapshot_id != card.score_snapshot_id or snapshot is None
                    or snapshot.perfume_id != card.perfume.id or source is None or not source.enabled
                    or source.permission_status != "approved" or snapshot.algorithm_version != card.score.algorithm_version
                    or snapshot.scoring_config != self.repository.config.scoring.model_dump(mode="json")
                    or snapshot.total != card.score.total):
                return False
            current = score_opinions(self.repository.load_evidence(card.perfume.id, now=now, session=session),
                                     self.repository.config.scoring)
            if current != card.score:
                return False
        if card.summary is not None:
            evidence = self.repository.load_summary_evidence(card.perfume.id, now=now)
            validate_summary(card.summary, perfume_id=card.perfume.id, evidence=evidence,
                approvals=self.analyzer.approvals, now=now, max_utf16_units=4096,
                freshness_seconds=self.repository.config.stage2.freshness_seconds)
        return visible_utf16_units(format_card(card)) <= 4096

    @asynccontextmanager
    async def publication(self, card: RecommendationCard, reservation_id: int, user_id: int):
        # No SQL transaction crosses this lock's Telegram await.
        async with self._lifecycle:
            try:
                allowed = self._current_card(card, reservation_id, user_id)
            except (PermissionDenied, ValueError, LLMError):
                allowed = False
            yield allowed

    async def revoke(self, source_key: str) -> PurgeResult:
        # Close both in-memory admission and the durable source before waiting for an in-flight send.
        self._suspended = True
        self._generation += 1
        self.analyzer.live_admitted = False
        self.repository.block_source(source_key)
        async with self._lifecycle:
            self.analyzer.purge_local_llm_state()
            return self.repository.purge_source(source_key)
