from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collectors.contracts import DatasetRevisionRecord, SourceBatch
from config import AppConfig, ScoringConfig
from scoring.dataset import ScoreResult, score_dataset
from storage.models import (
    Note, Perfume, PerfumeNote, RatingObservation, ScoreComponent, ScoreEvidence,
    ScoreSnapshot, SentLog, Source, SourceRevision, TasteProfile, UserFeedback,
)


@dataclass(frozen=True)
class DatasetApproval:
    name: str
    terms_url: str
    access_basis: str
    approval_basis: str
    approved_at: datetime
    revision_ref: str
    artifact_url: str
    artifact_sha256: str


@dataclass(frozen=True)
class ImportResult:
    imported: int
    total: int


class DatasetRepository:
    def __init__(self, engine: Engine, config: AppConfig):
        self.engine, self.config = engine, config

    def import_batch(self, batch: SourceBatch, approval: DatasetApproval) -> ImportResult:
        batch = SourceBatch.model_validate(batch)
        allowed = self.config.sources.get("dataset")
        expected = DatasetRevisionRecord(
            revision_ref=approval.revision_ref, artifact_url=approval.artifact_url,
            artifact_sha256=approval.artifact_sha256,
        )
        if not allowed or not allowed.enabled or allowed.kind != "dataset":
            raise ValueError("dataset source is disabled")
        if (batch.source_key != "dataset" or batch.dataset_revision != expected
                or not approval.access_basis.strip() or not approval.approval_basis.strip()
                or approval.approved_at.utcoffset() is None):
            raise ValueError("dataset snapshot has not been approved")
        if batch.mentions or batch.offers or batch.review_texts:
            raise ValueError("the offline catalogue cannot supply reviews or shop offers")
        ids = [record.external_id for record in batch.perfumes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate perfume identifier")
        if any(r.perfume_external_id not in ids or r.perfume_source_key not in (None, "dataset")
               for r in batch.ratings):
            raise ValueError("rating references a perfume outside the imported snapshot")
        imported = 0
        with Session(self.engine) as session, session.begin():
            source = session.scalar(select(Source).where(Source.key == "dataset"))
            if source is None:
                source = Source(key="dataset", kind="dataset", name=approval.name,
                                enabled=True, permission_status="approved",
                                terms_url=approval.terms_url, access_basis=approval.access_basis)
                session.add(source)
                session.flush()
            elif source.kind != "dataset" or not source.enabled or source.permission_status != "approved":
                raise ValueError("stored dataset permission is disabled or revoked")
            revision = session.scalar(select(SourceRevision).where(
                SourceRevision.source_id == source.id,
                SourceRevision.revision_ref == approval.revision_ref,
                SourceRevision.artifact_sha256 == bytes.fromhex(approval.artifact_sha256),
            ))
            if revision is None:
                revision = SourceRevision(
                    source_id=source.id, revision_ref=approval.revision_ref,
                    artifact_sha256=bytes.fromhex(approval.artifact_sha256),
                    artifact_url=approval.artifact_url, approval_basis=approval.approval_basis,
                    approved_at=approval.approved_at,
                )
                session.add(revision)
                session.flush()
            elif revision.artifact_url != approval.artifact_url:
                raise ValueError("stored snapshot origin differs from approval")
            mapping = {}
            for record in batch.perfumes:
                perfume = session.scalar(select(Perfume).where(
                    Perfume.source_id == source.id, Perfume.external_id == record.external_id,
                ))
                is_new = perfume is None
                values = record.model_dump(exclude={"notes", "external_id"})
                if perfume is None:
                    perfume = Perfume(**values, external_id=record.external_id, source_id=source.id,
                                      source_kind="dataset", source_revision_id=revision.id)
                    session.add(perfume)
                    session.flush()
                    imported += 1
                elif perfume.source_revision_id != revision.id or any(
                    getattr(perfume, name) != value for name, value in values.items()
                ):
                    raise ValueError("immutable catalogue entry differs from approved snapshot")
                mapping[record.external_id] = perfume.id
                if not is_new:
                    actual_notes = set(session.execute(select(
                        Note.kind, Note.name, PerfumeNote.level, PerfumeNote.weight,
                    ).join(PerfumeNote, PerfumeNote.note_id == Note.id).where(
                        PerfumeNote.perfume_id == perfume.id,
                    )).all())
                    expected_notes = {
                        ("accord" if item.level == "accord" else "note", item.name,
                         item.level, item.weight) for item in record.notes
                    }
                    if actual_notes != expected_notes:
                        raise ValueError("immutable catalogue notes differ from snapshot")
                    continue
                expected_notes = set()
                for item in record.notes:
                    kind = "accord" if item.level == "accord" else "note"
                    note = session.scalar(select(Note).where(Note.kind == kind, Note.name == item.name))
                    if note is None:
                        note = Note(kind=kind, name=item.name)
                        session.add(note)
                        session.flush()
                    key = (perfume.id, note.id, item.level)
                    expected_notes.add((note.id, item.level, item.weight))
                    link = session.get(PerfumeNote, key)
                    if link is None:
                        session.add(PerfumeNote(perfume_id=perfume.id, note_id=note.id,
                                                level=item.level, weight=item.weight))
                    elif link.weight != item.weight:
                        raise ValueError("immutable note weight differs from snapshot")
                session.flush()
                actual_notes = set(session.execute(select(
                    PerfumeNote.note_id, PerfumeNote.level, PerfumeNote.weight,
                ).where(PerfumeNote.perfume_id == perfume.id)).all())
                if actual_notes != expected_notes:
                    raise ValueError("immutable catalogue notes differ from snapshot")
            for record in batch.ratings:
                perfume_id = mapping[record.perfume_external_id]
                rating = session.scalar(select(RatingObservation).where(
                    RatingObservation.source_id == source.id,
                    RatingObservation.perfume_id == perfume_id,
                    RatingObservation.external_id == record.external_id,
                    RatingObservation.observed_at == record.observed_at,
                ))
                values = record.model_dump(exclude={"perfume_external_id", "perfume_source_key"})
                if rating is None:
                    session.add(RatingObservation(**values, perfume_id=perfume_id,
                        source_id=source.id, source_kind="dataset", source_revision_id=revision.id))
                elif rating.source_revision_id != revision.id or any(
                    getattr(rating, name) != value for name, value in values.items()
                ):
                    raise ValueError("immutable rating observation differs from snapshot")
            total = session.scalar(select(func.count()).select_from(Perfume).where(Perfume.source_id == source.id))
        return ImportResult(imported, total)


@dataclass(frozen=True)
class RecommendationCard:
    perfume: Perfume
    notes: Sequence[tuple[str, str]]
    source_name: str
    source_url: str | None
    revision_ref: str
    artifact_sha256: str
    score: ScoreResult
    rating: RatingObservation | None


@dataclass(frozen=True)
class ReservationResult:
    status: str
    reservation_id: int | None = None
    card: RecommendationCard | None = None


REACTIONS = frozenset(("want", "known", "not_for_me", "bought"))


class RecommendationRepository:
    def __init__(self, engine: Engine, scoring_config: ScoringConfig):
        self.engine, self.scoring_config = engine, scoring_config

    @staticmethod
    def _existing(session: Session, user_id: int, local_date: date) -> ReservationResult | None:
        row = session.scalar(select(SentLog).where(SentLog.user_id == user_id, SentLog.local_date == local_date))
        return ReservationResult(row.status, row.id) if row else None

    def reserve(self, user_id: int, local_date: date) -> ReservationResult:
        # Unique constraints arbitrate independently running scheduler and command workers.
        for _attempt in range(16):
            selected_perfume_id = None
            try:
                with Session(self.engine, expire_on_commit=False) as session, session.begin():
                    existing = self._existing(session, user_id, local_date)
                    if existing:
                        return existing
                    excluded_sent = exists().where(SentLog.user_id == user_id, SentLog.perfume_id == Perfume.id)
                    excluded_feedback = exists().where(
                        UserFeedback.user_id == user_id, UserFeedback.perfume_id == Perfume.id,
                        UserFeedback.reaction.in_(("known", "bought", "not_for_me")),
                    )
                    perfumes = session.scalars(select(Perfume).join(Source, Perfume.source_id == Source.id).where(
                        ~excluded_sent, ~excluded_feedback, Source.enabled.is_(True),
                        Source.permission_status == "approved", Source.kind == "dataset",
                    ).order_by(Perfume.id)).all()
                    candidates = []
                    for perfume in perfumes:
                        rating = session.scalar(select(RatingObservation).where(
                            RatingObservation.perfume_id == perfume.id,
                            RatingObservation.source_revision_id == perfume.source_revision_id,
                            RatingObservation.source_id == perfume.source_id,
                        ).order_by(RatingObservation.observed_at.desc(), RatingObservation.id.desc()).limit(1))
                        score = score_dataset(rating, self.scoring_config)
                        if score.total is not None:
                            candidates.append((score.total, -perfume.id, perfume, rating, score))
                    if not candidates:
                        return ReservationResult("empty")
                    _, _, perfume, rating, score = max(candidates, key=lambda row: row[:2])
                    selected_perfume_id = perfume.id
                    snapshot = ScoreSnapshot(
                        perfume_id=perfume.id, computed_at=datetime.now(timezone.utc),
                        algorithm_version=score.algorithm_version,
                        scoring_config=self.scoring_config.model_dump(mode="json"), total=score.total,
                    )
                    session.add(snapshot)
                    session.flush()
                    for item in score.components:
                        component = ScoreComponent(snapshot_id=snapshot.id, perfume_id=perfume.id,
                            name=item.name, value=item.value, effective_weight=item.effective_weight)
                        session.add(component)
                        session.flush()
                        for evidence_id in item.rating_observation_ids:
                            session.add(ScoreEvidence(component_id=component.id, perfume_id=perfume.id,
                                                       rating_observation_id=evidence_id))
                    reservation = SentLog(user_id=user_id, perfume_id=perfume.id, local_date=local_date,
                                          status="reserved", score_snapshot_id=snapshot.id)
                    session.add(reservation)
                    session.flush()
                    source = session.get(Source, perfume.source_id)
                    revision = session.get(SourceRevision, perfume.source_revision_id)
                    notes = tuple(session.execute(select(PerfumeNote.level, Note.name).join(
                        Note, Note.id == PerfumeNote.note_id).where(PerfumeNote.perfume_id == perfume.id)
                        .order_by(PerfumeNote.level, Note.name)).all())
                    card = RecommendationCard(perfume, notes, source.name, perfume.source_url,
                        revision.revision_ref, revision.artifact_sha256.hex(), score, rating)
                    result = ReservationResult("new", reservation.id, card)
                return result
            except IntegrityError:
                with Session(self.engine) as session:
                    existing = self._existing(session, user_id, local_date)
                    if existing:
                        return existing
                    # A competing day may have claimed this perfume. Select again.
                    if selected_perfume_id is None or not session.scalar(select(SentLog.id).where(
                        SentLog.user_id == user_id, SentLog.perfume_id == selected_perfume_id,
                    ).limit(1)):
                        raise
        raise RuntimeError("Concurrent reservations prevented safe candidate selection")

    def confirm(self, reservation_id: int, user_id: int, message_id: int, now: datetime) -> bool:
        if message_id <= 0 or now.utcoffset() is None:
            raise ValueError("confirmation requires a message identifier and aware timestamp")
        return self._transition(reservation_id, user_id, status="sent", sent_at=now,
                                telegram_message_id=message_id)

    def _transition(self, reservation_id: int, user_id: int, **values) -> bool:
        with self.engine.begin() as connection:
            return connection.execute(update(SentLog).where(
                SentLog.id == reservation_id, SentLog.user_id == user_id, SentLog.status == "reserved",
            ).values(**values)).rowcount == 1

    def mark_uncertain(self, reservation_id: int, user_id: int) -> bool:
        return self._transition(reservation_id, user_id, status="uncertain")

    def release(self, reservation_id: int, user_id: int) -> bool:
        with self.engine.begin() as connection:
            return connection.execute(delete(SentLog).where(
                SentLog.id == reservation_id, SentLog.user_id == user_id, SentLog.status == "reserved",
            )).rowcount == 1

    def feedback(self, user_id: int, perfume_id: int, reaction: str, message_id: int) -> bool:
        if reaction not in REACTIONS:
            raise ValueError("unknown feedback reaction")
        with Session(self.engine) as session, session.begin():
            sent = session.scalar(select(SentLog.id).where(
                SentLog.user_id == user_id, SentLog.perfume_id == perfume_id,
                SentLog.status == "sent", SentLog.telegram_message_id == message_id,
            ))
            if sent is None:
                return False
            row = session.get(UserFeedback, (user_id, perfume_id))
            if row is None:
                session.add(UserFeedback(user_id=user_id, perfume_id=perfume_id, reaction=reaction,
                                          updated_at=datetime.now(timezone.utc)))
            else:
                row.reaction, row.updated_at = reaction, datetime.now(timezone.utc)
        return True

    def list_feedback(self, user_id: int, reactions: list[str]) -> list[Perfume]:
        with Session(self.engine) as session:
            return list(session.scalars(select(Perfume).join(UserFeedback).where(
                UserFeedback.user_id == user_id, UserFeedback.reaction.in_(reactions),
            ).order_by(Perfume.house, Perfume.name, Perfume.id)))

    def delete_user_data(self, user_id: int) -> dict[str, int]:
        with Session(self.engine) as session, session.begin():
            removed = {}
            for model in (UserFeedback, SentLog):
                removed[model.__tablename__] = session.execute(delete(model).where(model.user_id == user_id)).rowcount
            snapshots = select(ScoreSnapshot.id).where(ScoreSnapshot.taste_profile_id.in_(
                select(TasteProfile.id).where(TasteProfile.user_id == user_id)))
            components = select(ScoreComponent.id).where(ScoreComponent.snapshot_id.in_(snapshots))
            session.execute(delete(ScoreEvidence).where(ScoreEvidence.component_id.in_(components)))
            session.execute(delete(ScoreComponent).where(ScoreComponent.snapshot_id.in_(snapshots)))
            session.execute(delete(ScoreSnapshot).where(ScoreSnapshot.id.in_(snapshots)))
            removed["taste_profile"] = session.execute(delete(TasteProfile).where(TasteProfile.user_id == user_id)).rowcount
            return removed
