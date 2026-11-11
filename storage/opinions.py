"""Persist permitted quotes and immutable document observations without raw reviews."""

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from numbers import Real
import re

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from collectors.contracts import MentionRecord, RatingRecord, ReviewTextRecord
from collectors.permissions import AdmissionRegistry, PermissionDenied, SourceAdmission
from config import AppConfig
from llm.contracts import ExtractedMention, ReviewExtraction
from llm.policy import aware, require_nonprivate_text
from llm.validation import CatalogPerfume, SummaryEvidence, validate_extraction
from scoring.engine import MentionEvidence, OpinionScore, RatingEvidence, ScoringInputs, score_opinions
from storage.models import (
    Blogger, Mention, Perfume, RatingObservation, ScoreComponent, ScoreEvidence,
    ScoreSnapshot, SentLog, Source, SourceRevision,
)
from storage.repository import ImportResult


@dataclass(frozen=True)
class OpinionImportResult:
    revision_id: int
    imported_mentions: int
    quarantined_mentions: int
    total_mentions: int


@dataclass(frozen=True)
class PurgeResult:
    mentions: int
    ratings: int
    revisions: int
    snapshots: int


@dataclass(frozen=True)
class BloggerTrustRecord:
    id: int
    source_key: str
    external_id: str
    name: str
    channel_url: str | None
    weight: float


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _document_key(source_key: str, external_id: str) -> str:
    return _digest([source_key, external_id])


def _revision_ref(source_key: str, external_id: str, observed_at: datetime) -> str:
    stamp = observed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"v1:{_document_key(source_key, external_id)}:{stamp}"


def _read_revision(value: SourceRevision) -> tuple[str, datetime, dict] | None:
    match = re.fullmatch(r"v1:([0-9a-f]{64}):(\d{8}T\d{6}\.\d{6}Z)", value.revision_ref)
    if match is None:
        return None
    try:
        observed = datetime.strptime(match[2], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        basis = json.loads(value.approval_basis)
    except (ValueError, TypeError):
        return None
    if not isinstance(basis, dict):
        return None
    return match[1], observed, basis


def _mention_key(revision_id: int, perfume_id: int) -> str:
    return f"v1:{revision_id}:{perfume_id}"


class OpinionRepository:
    def __init__(self, engine: Engine, config: AppConfig, registry: AdmissionRegistry):
        self.engine, self.config, self.registry = engine, config, registry

    @contextmanager
    def _session(self, supplied: Session | None = None, *, write: bool = False):
        if supplied is not None:
            yield supplied
            return
        with Session(self.engine) as owned:
            if write:
                with owned.begin():
                    yield owned
            else:
                yield owned

    def check_source(
        self, source_key: str, operation: str, *, now: datetime, source_url: str | None = None,
        blogger_external_id: str | None = None,
    ) -> SourceAdmission:
        admission = self._admit(source_key, operation, now=now, source_url=source_url,
                                blogger_external_id=blogger_external_id)
        with Session(self.engine) as session:
            self._source(session, admission)
        return admission

    def load_catalog(self) -> Sequence[CatalogPerfume]:
        with Session(self.engine) as session:
            return self._catalog(session)

    def list_bloggers(self, *, now: datetime) -> Sequence[BloggerTrustRecord]:
        if not aware(now):
            raise ValueError("blogger listing requires an aware timestamp")
        records = []
        with Session(self.engine) as session:
            for blogger, source in session.execute(select(Blogger, Source).join(
                Source, Source.id == Blogger.source_id,
            ).order_by(Blogger.id)):
                try:
                    admission = self._admit(source.key, "derive", now=now,
                                             blogger_external_id=blogger.external_id)
                    self._source(session, admission)
                except PermissionDenied:
                    continue
                if source.kind == "blog":
                    records.append(BloggerTrustRecord(blogger.id, source.key, blogger.external_id,
                        blogger.name, blogger.channel_url, blogger.weight))
        return tuple(records)

    def set_blogger_weight(self, blogger_id: int, weight: float, *, now: datetime) -> bool:
        if type(blogger_id) is not int or blogger_id <= 0:
            raise ValueError("blogger identity must be a positive integer")
        if isinstance(weight, bool) or not isinstance(weight, Real) or not isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError("blogger trust must be a finite number between zero and one")
        with Session(self.engine) as session, session.begin():
            blogger = session.get(Blogger, blogger_id)
            if blogger is None:
                return False
            source = session.get(Source, blogger.source_id)
            if source is None or source.kind != "blog":
                raise PermissionDenied("blogger does not belong to an admitted blog source")
            admission = self._admit(source.key, "derive", now=now, blogger_external_id=blogger.external_id)
            self._admit(source.key, "store", now=now, blogger_external_id=blogger.external_id)
            self._source(session, admission)
            blogger.weight = float(weight)
        return True

    def _admit(
        self, source_key: str, operation: str, *, now: datetime, source_url: str | None = None,
        blogger_external_id: str | None = None,
    ) -> SourceAdmission:
        configured = self.config.sources.get(source_key)
        if (self.config.stage2 is None or not self.config.stage2.enabled or configured is None
                or not configured.enabled):
            raise PermissionDenied("opinion pipeline or configured source is disabled")
        admission = self.registry.require(
            source_key, operation, now=now, source_url=source_url,
            blogger_external_id=blogger_external_id,
        )
        if configured.kind != admission.kind:
            raise PermissionDenied("configured and admitted source kinds differ")
        return admission

    def _fresh(self, admission: SourceAdmission, observed_at: datetime, now: datetime) -> None:
        if not aware(observed_at) or not aware(now) or observed_at > now:
            raise ValueError("observation requires a valid fetched timestamp")
        configured = (self.config.stage2.freshness_seconds or {}).get(admission.source_key)
        limits = [value for value in (configured, admission.max_age_seconds) if value is not None]
        if not limits or (now - observed_at).total_seconds() > min(limits):
            raise ValueError("observation is outside its explicit freshness policy")

    @staticmethod
    def _source(session: Session, admission: SourceAdmission) -> Source:
        source = session.scalar(select(Source).where(Source.key == admission.source_key))
        if (source is None or source.kind != admission.kind or not source.enabled
                or source.permission_status != "approved" or source.access_basis != admission.approval_ref):
            raise PermissionDenied("stored source is absent, disabled or revoked")
        return source

    @staticmethod
    def _approval(admission: SourceAdmission, approval_ref: str, approved_at: datetime) -> None:
        if (approval_ref != admission.approval_ref or not aware(approved_at)
                or approved_at != admission.approved_at):
            raise PermissionDenied("snapshot does not match the current rights registry")

    def sync_admissions(self, *, now: datetime) -> None:
        if not aware(now):
            raise ValueError("admission synchronization requires an aware timestamp")
        with Session(self.engine) as session, session.begin():
            for admission in self.registry.admissions.values():
                source = session.scalar(select(Source).where(Source.key == admission.source_key))
                if source is not None and source.permission_status == "blocked":
                    continue
                if source is not None and source.kind != admission.kind:
                    raise ValueError("stored source kind is immutable")
                configured = self.config.sources.get(admission.source_key)
                active = bool(configured and configured.enabled and configured.kind == admission.kind)
                try:
                    self.registry.require(admission.source_key, "store", now=now)
                except PermissionDenied:
                    active = False
                if source is None:
                    source = Source(key=admission.source_key, kind=admission.kind, name=admission.name)
                    session.add(source)
                source.name, source.terms_url = admission.name, admission.terms_url
                source.enabled, source.permission_status = active, admission.permission_status
                source.access_basis = admission.approval_ref
                session.flush()
                for value in admission.bloggers:
                    blogger = session.scalar(select(Blogger).where(
                        Blogger.source_id == source.id, Blogger.external_id == value.external_id,
                    ))
                    if blogger is None:
                        blogger = Blogger(source_id=source.id, external_id=value.external_id, weight=value.weight)
                        session.add(blogger)
                    blogger.name, blogger.channel_url = value.name, value.channel_url

    @staticmethod
    def _catalog(session: Session) -> Sequence[CatalogPerfume]:
        return tuple(CatalogPerfume(row.id, row.name, row.house, source.key, row.external_id)
                     for row, source in session.execute(select(Perfume, Source).join(
                         Source, Source.id == Perfume.source_id,
                     ).where(Source.enabled.is_(True), Source.permission_status == "approved",
                             Source.kind == "dataset")))

    def store_review_snapshot(
        self, source_key: str, review: ReviewTextRecord, observed_at: datetime,
        approval_ref: str, approved_at: datetime, mentions: Sequence[MentionRecord], *, now: datetime,
    ) -> OpinionImportResult:
        review = ReviewTextRecord.model_validate(review)
        mentions = tuple(MentionRecord.model_validate(value) for value in mentions)
        require_nonprivate_text(review.text)
        admission = self._admit(source_key, "store", now=now, source_url=review.source_url,
                                blogger_external_id=review.blogger_external_id)
        self._admit(source_key, "derive", now=now, source_url=review.source_url,
                    blogger_external_id=review.blogger_external_id)
        self._approval(admission, approval_ref, approved_at)
        self._fresh(admission, observed_at, now)
        if admission.kind == "blog" and review.blogger_external_id is None:
            raise PermissionDenied("blog review requires an explicitly admitted author")
        if admission.kind != "blog" and review.blogger_external_id is not None:
            raise PermissionDenied("review source cannot invent a blogger identity")
        payload_sha = _digest([value.model_dump(mode="json") for value in mentions])
        basis_values = {"approval_ref": approval_ref, "payload_sha256": payload_sha,
                        "envelope_sha256": _digest(review.model_dump(mode="json", exclude={"text"}))}
        basis = json.dumps(basis_values, sort_keys=True)
        text_sha = hashlib.sha256(review.text.encode("utf-8")).digest()
        revision_ref = _revision_ref(source_key, review.external_id, observed_at)
        with Session(self.engine) as session, session.begin():
            source = self._source(session, admission)
            existing = session.scalar(select(SourceRevision).where(
                SourceRevision.source_id == source.id, SourceRevision.revision_ref == revision_ref,
            ))
            if existing is not None:
                stored_basis = json.loads(existing.approval_basis)
                if (existing.artifact_sha256 != text_sha or existing.artifact_url != review.source_url
                        or any(stored_basis.get(key) != value for key, value in basis_values.items())
                        or existing.approved_at != approved_at):
                    raise ValueError("immutable document observation differs from its stored version")
                stored = session.scalars(select(Mention.id).where(
                    Mention.source_id == source.id, Mention.external_id.like(f"v1:{existing.id}:%"),
                )).all()
                return OpinionImportResult(existing.id, 0, stored_basis["quarantined_mentions"], len(stored))
            revision = SourceRevision(
                source_id=source.id, revision_ref=revision_ref, artifact_sha256=text_sha,
                artifact_url=review.source_url, approval_basis=basis, approved_at=approved_at,
            )
            session.add(revision)
            session.flush()
            catalog = self._catalog(session)
            accepted, quarantine = {}, 0
            blogger = None
            if review.blogger_external_id is not None:
                blogger = session.scalar(select(Blogger).where(
                    Blogger.source_id == source.id, Blogger.external_id == review.blogger_external_id,
                ))
                if blogger is None:
                    raise PermissionDenied("admitted blogger has not been synchronized")
            for record in mentions:
                candidates = [item for item in catalog if item.external_id == record.perfume_external_id
                              and record.perfume_source_key in (None, item.source_key)]
                if (len(candidates) != 1 or record.source_url != review.source_url
                        or record.blogger_external_id != review.blogger_external_id
                        or record.published_at != review.published_at or record.evidence_quote is None):
                    quarantine += 1
                    continue
                perfume = candidates[0]
                extracted = ExtractedMention(
                    perfume_name=perfume.name, perfume_house=None,
                    sentiment=record.sentiment, evidence_quote=record.evidence_quote,
                    confidence=record.confidence,
                )
                check = validate_extraction(ReviewExtraction(mentions=[extracted]), review=review, catalog=catalog)
                if not check.mentions and any(item.reason == "ambiguous_perfume" for item in check.quarantine):
                    # MentionRecord omits the raw house. A disambiguating house is
                    # admitted only when the validator grounds it in this review.
                    grounded = extracted.model_copy(update={"perfume_house": perfume.house})
                    check = validate_extraction(ReviewExtraction(mentions=[grounded]), review=review, catalog=catalog)
                if len(check.mentions) != 1 or check.mentions[0].perfume_id != perfume.id:
                    quarantine += 1
                    continue
                if perfume.id in accepted:
                    if accepted[perfume.id] != record:
                        raise ValueError("document has conflicting interpretations of the same perfume")
                    continue
                accepted[perfume.id] = record
            for perfume_id, record in accepted.items():
                session.add(Mention(
                    perfume_id=perfume_id, source_id=source.id, blogger_id=blogger.id if blogger else None,
                    external_id=_mention_key(revision.id, perfume_id), source_url=review.source_url,
                    published_at=review.published_at, sentiment=record.sentiment,
                    evidence_quote=record.evidence_quote, confidence=record.confidence,
                ))
            revision.approval_basis = json.dumps(basis_values | {"quarantined_mentions": quarantine}, sort_keys=True)
            return OpinionImportResult(revision.id, len(accepted), quarantine, len(accepted))

    def lookup_snapshot(
        self, source_key: str, review: ReviewTextRecord, observed_at: datetime,
        approval_ref: str, approved_at: datetime, *, now: datetime,
    ) -> OpinionImportResult | None:
        review = ReviewTextRecord.model_validate(review)
        require_nonprivate_text(review.text)
        admission = self.check_source(source_key, "store", now=now, source_url=review.source_url,
                                     blogger_external_id=review.blogger_external_id)
        self.check_source(source_key, "derive", now=now, source_url=review.source_url,
                          blogger_external_id=review.blogger_external_id)
        self._approval(admission, approval_ref, approved_at)
        self._fresh(admission, observed_at, now)
        if (admission.kind == "blog") != (review.blogger_external_id is not None):
            raise PermissionDenied("review author identity differs from its source scope")
        with Session(self.engine) as session:
            source = self._source(session, admission)
            revision = session.scalar(select(SourceRevision).where(
                SourceRevision.source_id == source.id,
                SourceRevision.revision_ref == _revision_ref(source_key, review.external_id, observed_at),
            ))
            if revision is None:
                return None
            decoded = _read_revision(revision)
            if decoded is None:
                raise ValueError("stored review observation lineage is invalid")
            basis = decoded[2]
            if (revision.artifact_sha256 != hashlib.sha256(review.text.encode("utf-8")).digest()
                    or revision.artifact_url != review.source_url or revision.approved_at != approved_at
                    or basis.get("approval_ref") != approval_ref
                    or basis.get("envelope_sha256") != _digest(review.model_dump(mode="json", exclude={"text"}))
                    or type(basis.get("quarantined_mentions")) is not int or basis["quarantined_mentions"] < 0):
                raise ValueError("immutable document observation differs from its stored version")
            total = len(session.scalars(select(Mention.id).where(
                Mention.source_id == source.id, Mention.external_id.like(f"v1:{revision.id}:%"),
            )).all())
            return OpinionImportResult(revision.id, 0, basis["quarantined_mentions"], total)

    def store_ratings(
        self, source_key: str, ratings: Sequence[RatingRecord], *, approval_ref: str,
        approved_at: datetime, now: datetime,
    ) -> ImportResult:
        admission = self._admit(source_key, "store", now=now)
        self._admit(source_key, "derive", now=now)
        self._approval(admission, approval_ref, approved_at)
        if admission.evidence_role not in ("fragrantica", "popularity"):
            raise PermissionDenied("source does not admit rating observations")
        ratings = tuple(RatingRecord.model_validate(value) for value in ratings)
        imported = 0
        with Session(self.engine) as session, session.begin():
            source = self._source(session, admission)
            catalog = self._catalog(session)
            for record in ratings:
                self._admit(source_key, "derive", now=now, source_url=record.source_url)
                if record.source_url is None:
                    raise PermissionDenied("rating observation requires its exact admitted origin")
                self._fresh(admission, record.observed_at, now)
                candidates = [item for item in catalog if item.external_id == record.perfume_external_id
                              and record.perfume_source_key in (None, item.source_key)]
                if len(candidates) != 1:
                    raise ValueError("rating perfume identity is not a unique admitted catalog entry")
                if admission.evidence_role == "popularity" and any(getattr(record, name) is not None
                        for name in ("mean_rating", "positive_count", "negative_count")):
                    raise PermissionDenied("popularity observations cannot supply review ratings")
                values = record.model_dump(exclude={"perfume_source_key", "perfume_external_id"})
                payload_sha = _digest(record.model_dump(mode="json"))
                revision_ref = "rating-" + _revision_ref(source_key,
                    f"{record.external_id}:{candidates[0].id}", record.observed_at)
                basis = json.dumps({"approval_ref": approval_ref, "payload_sha256": payload_sha}, sort_keys=True)
                revision = session.scalar(select(SourceRevision).where(
                    SourceRevision.source_id == source.id, SourceRevision.revision_ref == revision_ref,
                ))
                if revision is None:
                    revision = SourceRevision(source_id=source.id, revision_ref=revision_ref,
                        artifact_sha256=bytes.fromhex(payload_sha), artifact_url=record.source_url,
                        approval_basis=basis, approved_at=approved_at)
                    session.add(revision)
                    session.flush()
                elif (revision.artifact_sha256 != bytes.fromhex(payload_sha)
                        or revision.artifact_url != record.source_url or revision.approval_basis != basis
                        or revision.approved_at != approved_at):
                    raise ValueError("immutable rating approval lineage differs from its stored version")
                existing = session.scalar(select(RatingObservation).where(
                    RatingObservation.source_id == source.id, RatingObservation.perfume_id == candidates[0].id,
                    RatingObservation.external_id == record.external_id,
                    RatingObservation.observed_at == record.observed_at,
                ))
                if existing is None:
                    session.add(RatingObservation(**values, source_id=source.id,
                        source_kind=source.kind, perfume_id=candidates[0].id, source_revision_id=revision.id))
                    imported += 1
                elif (existing.source_revision_id != revision.id
                      or any(getattr(existing, key) != value for key, value in values.items())):
                    raise ValueError("immutable rating observation differs from its stored version")
            session.flush()
            total = len(session.scalars(select(RatingObservation.id).where(
                RatingObservation.source_id == source.id,
            )).all())
        return ImportResult(imported, total)

    def _current_reviews(self, session: Session, *, now: datetime) -> Sequence[tuple]:
        selected = []
        for source in session.scalars(select(Source).where(
            Source.enabled.is_(True), Source.permission_status == "approved",
        )):
            try:
                admission = self._admit(source.key, "derive", now=now)
                self._admit(source.key, "store", now=now)
                self._source(session, admission)
            except PermissionDenied:
                continue
            versions = {}
            for revision in session.scalars(select(SourceRevision).where(SourceRevision.source_id == source.id)):
                decoded = _read_revision(revision)
                if decoded is None:
                    continue
                document, observed_at, basis = decoded
                if (basis.get("approval_ref") != admission.approval_ref
                        or revision.approved_at != admission.approved_at):
                    continue
                if document not in versions or (observed_at, revision.id) > versions[document][:2]:
                    versions[document] = (observed_at, revision.id, revision)
            for document, (observed_at, revision_id, revision) in versions.items():
                try:
                    self._fresh(admission, observed_at, now)
                    self._admit(source.key, "derive", now=now, source_url=revision.artifact_url)
                except (PermissionDenied, ValueError):
                    continue
                for mention in session.scalars(select(Mention).where(
                    Mention.source_id == source.id, Mention.external_id.like(f"v1:{revision_id}:%"),
                )):
                    if mention.source_url != revision.artifact_url:
                        continue
                    blogger = session.get(Blogger, mention.blogger_id) if mention.blogger_id else None
                    if admission.evidence_role == "bloggers":
                        if blogger is None or blogger.source_id != source.id:
                            continue
                        allowed = next((item for item in admission.bloggers
                                        if item.external_id == blogger.external_id), None)
                        if allowed is None or blogger.weight == 0:
                            continue
                    selected.append((mention, source, admission, observed_at, document, blogger))
        return tuple(selected)

    def _evidence(self, session: Session, perfume_id: int, *, now: datetime) -> ScoringInputs:
        if not aware(now):
            raise ValueError("evidence selection requires an aware timestamp")
        fragrantica, popularity, russian, bloggers = [], [], [], []
        for rating, source in session.execute(select(RatingObservation, Source).join(
            Source, Source.id == RatingObservation.source_id,
        ).where(RatingObservation.perfume_id == perfume_id, Source.enabled.is_(True),
                Source.permission_status == "approved")):
            role = "popularity"
            if source.kind == "dataset":
                configured = self.config.sources.get(source.key)
                if not configured or not configured.enabled or configured.kind != "dataset":
                    continue
                if rating.observed_at > now:
                    continue
            else:
                try:
                    admission = self._admit(source.key, "derive", now=now, source_url=rating.source_url)
                    self._admit(source.key, "store", now=now)
                    self._source(session, admission)
                    self._fresh(admission, rating.observed_at, now)
                    revision = session.get(SourceRevision, rating.source_revision_id) if rating.source_revision_id else None
                    if (revision is None or revision.source_id != source.id
                            or not revision.revision_ref.startswith("rating-v1:")
                            or revision.approved_at != admission.approved_at
                            or revision.artifact_url != rating.source_url):
                        continue
                    try:
                        basis = json.loads(revision.approval_basis)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(basis, dict) or basis.get("approval_ref") != admission.approval_ref:
                        continue
                except (PermissionDenied, ValueError):
                    continue
                role = admission.evidence_role
            evidence = RatingEvidence(rating.id, source.key, rating.mean_rating, rating.rating_count,
                rating.positive_count, rating.negative_count, rating.popularity_rank, rating.observed_at)
            if role == "fragrantica":
                fragrantica.append(evidence)
            if role in ("fragrantica", "popularity"):
                popularity.append(evidence)
        for mention, source, admission, observed_at, document, blogger in self._current_reviews(session, now=now):
            if mention.perfume_id != perfume_id:
                continue
            evidence = MentionEvidence(mention.id, source.key, mention.blogger_id, mention.sentiment,
                mention.confidence, blogger.weight if blogger else None, observed_at,
                mention.published_at, document)
            if admission.evidence_role == "russian_reviews":
                russian.append(evidence)
            elif admission.evidence_role == "bloggers":
                bloggers.append(evidence)
        by_id = lambda value: value.id
        return ScoringInputs(tuple(sorted(fragrantica, key=by_id)), tuple(sorted(russian, key=by_id)),
            tuple(sorted(bloggers, key=by_id)), tuple(sorted(popularity, key=by_id)))

    def load_evidence(self, perfume_id: int, *, now: datetime, session: Session | None = None) -> ScoringInputs:
        with self._session(session) as current:
            return self._evidence(current, perfume_id, now=now)

    def load_summary_evidence(self, perfume_id: int, *, now: datetime) -> Sequence[SummaryEvidence]:
        with Session(self.engine) as session:
            results = []
            for mention, source, admission, observed_at, _document, _blogger in self._current_reviews(session, now=now):
                if mention.perfume_id == perfume_id and mention.evidence_quote:
                    try:
                        self._admit(source.key, "quote", now=now, source_url=mention.source_url)
                    except PermissionDenied:
                        continue
                    results.append(SummaryEvidence(mention.id, perfume_id, source.key,
                        mention.source_url, mention.sentiment, mention.evidence_quote,
                        mention.published_at, admission.approval_ref, observed_at))
            return tuple(sorted(results, key=lambda value: value.mention_id))

    def save_score(
        self, perfume_id: int, score: OpinionScore, *, computed_at: datetime, session: Session | None = None,
        taste: float | None = None,
    ) -> int:
        with self._session(session, write=True) as session:
            current = score_opinions(self._evidence(session, perfume_id, now=computed_at),
                                     self.config.scoring, taste=taste)
            if score != current or session.get(Perfume, perfume_id) is None:
                raise ValueError("score must match this perfume's current admitted evidence")
            snapshot = ScoreSnapshot(perfume_id=perfume_id, computed_at=computed_at,
                algorithm_version=score.algorithm_version,
                scoring_config=self.config.scoring.model_dump(mode="json"), total=score.total)
            session.add(snapshot)
            session.flush()
            for value in score.components:
                component = ScoreComponent(snapshot_id=snapshot.id, perfume_id=perfume_id,
                    name=value.name, value=value.value, effective_weight=value.effective_weight)
                session.add(component)
                session.flush()
                for evidence in value.evidence:
                    session.add(ScoreEvidence(component_id=component.id, perfume_id=perfume_id,
                        rating_observation_id=evidence.rating_observation_id,
                        mention_id=evidence.mention_id, applied_blogger_weight=evidence.applied_blogger_weight))
            return snapshot.id

    def block_source(self, source_key: str) -> None:
        with Session(self.engine) as session, session.begin():
            source = session.scalar(select(Source).where(Source.key == source_key))
            if source is None or source.kind not in ("review", "blog", "manual"):
                raise ValueError("only registered opinion sources can be revoked")
            source.enabled, source.permission_status = False, "blocked"

    def purge_source(self, source_key: str) -> PurgeResult:
        with Session(self.engine) as session, session.begin():
            source = session.scalar(select(Source).where(Source.key == source_key))
            if (source is None or source.kind not in ("review", "blog", "manual")
                    or source.enabled or source.permission_status != "blocked"):
                raise ValueError("source must be durably blocked before derived-state purge")
            mention_ids = tuple(session.scalars(select(Mention.id).where(Mention.source_id == source.id)))
            rating_ids = tuple(session.scalars(select(RatingObservation.id).where(RatingObservation.source_id == source.id)))
            snapshot_ids = tuple(session.scalars(select(ScoreComponent.snapshot_id).join(
                ScoreEvidence, ScoreEvidence.component_id == ScoreComponent.id,
            ).where(ScoreEvidence.mention_id.in_(mention_ids) |
                    ScoreEvidence.rating_observation_id.in_(rating_ids)).distinct()))
            components = select(ScoreComponent.id).where(ScoreComponent.snapshot_id.in_(snapshot_ids))
            session.execute(update(SentLog).where(SentLog.score_snapshot_id.in_(snapshot_ids)).values(score_snapshot_id=None))
            session.execute(delete(ScoreEvidence).where(ScoreEvidence.component_id.in_(components)))
            session.execute(delete(ScoreComponent).where(ScoreComponent.snapshot_id.in_(snapshot_ids)))
            session.execute(delete(ScoreSnapshot).where(ScoreSnapshot.id.in_(snapshot_ids)))
            session.execute(delete(Mention).where(Mention.source_id == source.id))
            session.execute(delete(RatingObservation).where(RatingObservation.source_id == source.id))
            revisions = session.execute(delete(SourceRevision).where(SourceRevision.source_id == source.id)).rowcount
            session.execute(delete(Blogger).where(Blogger.source_id == source.id))
            return PurgeResult(len(mention_ids), len(rating_ids), revisions, len(snapshot_ids))


def revoke_source(repository: OpinionRepository, source_key: str, purge_local_llm_state: Callable[[], None]) -> PurgeResult:
    """A failed cleanup keeps the committed source tombstone closed."""
    repository.block_source(source_key)
    purge_local_llm_state()
    return repository.purge_source(source_key)
