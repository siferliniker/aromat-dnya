from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Keep an aware UTC value on read, including from SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("database timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("id", "kind"),
        CheckConstraint("kind IN ('dataset', 'review', 'blog', 'shop', 'manual')", name="kind"),
        CheckConstraint(
            "permission_status IN ('pending', 'approved', 'blocked')", name="permission_status"
        ),
        CheckConstraint(
            "NOT enabled OR (permission_status = 'approved' AND access_basis IS NOT NULL "
            "AND length(trim(access_basis)) > 0)",
            name="enabled_requires_approval",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(server_default=false())
    permission_status: Mapped[str] = mapped_column(String(16), server_default="pending")
    terms_url: Mapped[str | None] = mapped_column(String(2048))
    access_basis: Mapped[str | None] = mapped_column(Text)


class SourceRevision(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (
        UniqueConstraint("id", "source_id"),
        UniqueConstraint("source_id", "revision_ref", "artifact_sha256"),
        CheckConstraint("length(artifact_sha256) = 32", name="sha256_length"),
        CheckConstraint("length(trim(approval_basis)) > 0", name="approval_basis"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    revision_ref: Mapped[str] = mapped_column(String(255))
    artifact_sha256: Mapped[bytes] = mapped_column(LargeBinary(32))
    artifact_url: Mapped[str] = mapped_column(String(2048))
    approval_basis: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class Perfume(Base):
    __tablename__ = "perfumes"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        ForeignKeyConstraint(["source_id", "source_kind"], ["sources.id", "sources.kind"]),
        ForeignKeyConstraint(
            ["source_revision_id", "source_id"], ["source_revisions.id", "source_revisions.source_id"]
        ),
        CheckConstraint("release_year BETWEEN 1 AND 9999", name="release_year"),
        CheckConstraint("length(trim(external_id)) > 0", name="external_id"),
        CheckConstraint(
            "source_kind <> 'dataset' OR source_revision_id IS NOT NULL", name="dataset_revision"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    house: Mapped[str] = mapped_column(String(255))
    release_year: Mapped[int | None]
    perfumers: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    photo_url: Mapped[str | None] = mapped_column(String(2048))
    positioning: Mapped[str | None] = mapped_column(String(255))
    source_id: Mapped[int]
    source_kind: Mapped[str] = mapped_column(String(16))
    source_revision_id: Mapped[int | None]
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2048))


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("kind", "name"),
        CheckConstraint("kind IN ('note', 'accord')", name="kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(255))


class PerfumeNote(Base):
    __tablename__ = "perfume_notes"
    __table_args__ = (
        CheckConstraint("level IN ('top', 'heart', 'base', 'accord', 'unspecified')", name="level"),
        CheckConstraint("weight BETWEEN 0 AND 1", name="weight"),
    )

    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"), primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), primary_key=True)
    level: Mapped[str] = mapped_column(String(16), primary_key=True)
    weight: Mapped[float | None]


class RatingObservation(Base):
    __tablename__ = "rating_observations"
    __table_args__ = (
        UniqueConstraint("id", "perfume_id"),
        UniqueConstraint("source_id", "perfume_id", "external_id", "observed_at"),
        ForeignKeyConstraint(["source_id", "source_kind"], ["sources.id", "sources.kind"]),
        ForeignKeyConstraint(
            ["source_revision_id", "source_id"], ["source_revisions.id", "source_revisions.source_id"]
        ),
        CheckConstraint(
            "source_kind <> 'dataset' OR source_revision_id IS NOT NULL", name="dataset_revision"
        ),
        CheckConstraint("mean_rating BETWEEN 0 AND 5", name="mean_rating"),
        CheckConstraint("rating_count >= 0", name="rating_count"),
        CheckConstraint("positive_count >= 0", name="positive_count"),
        CheckConstraint("negative_count >= 0", name="negative_count"),
        CheckConstraint("popularity_rank >= 1", name="popularity_rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"), index=True)
    source_id: Mapped[int]
    source_kind: Mapped[str] = mapped_column(String(16))
    source_revision_id: Mapped[int | None]
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    mean_rating: Mapped[float | None]
    rating_count: Mapped[int | None]
    positive_count: Mapped[int | None]
    negative_count: Mapped[int | None]
    popularity_rank: Mapped[int | None]


class Blogger(Base):
    __tablename__ = "bloggers"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint("weight BETWEEN 0 AND 1", name="weight"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(255))
    channel_url: Mapped[str | None] = mapped_column(String(2048))
    name: Mapped[str] = mapped_column(String(255))
    weight: Mapped[float]


class Mention(Base):
    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint("id", "perfume_id"),
        UniqueConstraint("source_id", "perfume_id", "external_id"),
        CheckConstraint("sentiment IN ('positive', 'neutral', 'negative')", name="sentiment"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    blogger_id: Mapped[int | None] = mapped_column(ForeignKey("bloggers.id"))
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(String(2048))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sentiment: Mapped[str] = mapped_column(String(8))
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None]


class ShopOffer(Base):
    __tablename__ = "shop_offers"
    __table_args__ = (
        UniqueConstraint("id", "perfume_id"),
        UniqueConstraint("source_id", "external_id", "observed_at"),
        CheckConstraint("price >= 0", name="price"),
        CheckConstraint("(price IS NULL) = (currency IS NULL)", name="price_currency"),
        CheckConstraint("format IN ('bottle', 'miniature', 'sample')", name="format"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(String(2048))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    format: Mapped[str | None] = mapped_column(String(12))
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    in_stock: Mapped[bool | None]


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (
        CheckConstraint("reaction IN ('want', 'known', 'not_for_me', 'bought')", name="reaction"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"), primary_key=True)
    reaction: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class TasteProfile(Base):
    __tablename__ = "taste_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    vector: Mapped[dict[str, float]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ScoreSnapshot(Base):
    __tablename__ = "score_snapshots"
    __table_args__ = (
        UniqueConstraint("id", "perfume_id"),
        CheckConstraint("total BETWEEN -1 AND 1", name="total"),
        CheckConstraint(
            "(taste_profile_id IS NULL) = (perfume_vector IS NULL)", name="taste_inputs"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"), index=True)
    taste_profile_id: Mapped[int | None] = mapped_column(ForeignKey("taste_profile.id"))
    perfume_vector: Mapped[dict[str, float] | None] = mapped_column(JSON(none_as_null=True))
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    algorithm_version: Mapped[str] = mapped_column(String(64))
    scoring_config: Mapped[dict[str, object]] = mapped_column(JSON)
    total: Mapped[float | None]


class ScoreComponent(Base):
    __tablename__ = "score_components"
    __table_args__ = (
        UniqueConstraint("id", "perfume_id"),
        UniqueConstraint("snapshot_id", "name"),
        ForeignKeyConstraint(
            ["snapshot_id", "perfume_id"], ["score_snapshots.id", "score_snapshots.perfume_id"]
        ),
        CheckConstraint(
            "name IN ('fragrantica', 'russian_reviews', 'bloggers', 'consensus', "
            "'novelty', 'hype', 'taste')",
            name="component_name",
        ),
        CheckConstraint("value BETWEEN 0 AND 1", name="value"),
        CheckConstraint(
            "(name = 'hype' AND effective_weight BETWEEN -1 AND 0) OR "
            "(name <> 'hype' AND effective_weight BETWEEN 0 AND 1)",
            name="weight_sign_and_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int]
    perfume_id: Mapped[int]
    name: Mapped[str] = mapped_column(String(20))
    value: Mapped[float]
    effective_weight: Mapped[float]


class ScoreEvidence(Base):
    __tablename__ = "score_evidence"
    __table_args__ = (
        UniqueConstraint("component_id", "rating_observation_id"),
        UniqueConstraint("component_id", "mention_id"),
        ForeignKeyConstraint(
            ["component_id", "perfume_id"], ["score_components.id", "score_components.perfume_id"]
        ),
        ForeignKeyConstraint(
            ["rating_observation_id", "perfume_id"],
            ["rating_observations.id", "rating_observations.perfume_id"],
        ),
        ForeignKeyConstraint(["mention_id", "perfume_id"], ["mentions.id", "mentions.perfume_id"]),
        CheckConstraint(
            "(rating_observation_id IS NULL) <> (mention_id IS NULL)", name="one_input"
        ),
        CheckConstraint(
            "applied_blogger_weight IS NULL OR "
            "(mention_id IS NOT NULL AND applied_blogger_weight BETWEEN 0 AND 1)",
            name="applied_blogger_weight",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    component_id: Mapped[int]
    perfume_id: Mapped[int]
    rating_observation_id: Mapped[int | None]
    mention_id: Mapped[int | None]
    applied_blogger_weight: Mapped[float | None]


class SentLog(Base):
    __tablename__ = "sent_log"
    __table_args__ = (
        UniqueConstraint("user_id", "perfume_id"),
        UniqueConstraint("user_id", "local_date"),
        ForeignKeyConstraint(
            ["score_snapshot_id", "perfume_id"], ["score_snapshots.id", "score_snapshots.perfume_id"]
        ),
        ForeignKeyConstraint(["shop_offer_id", "perfume_id"], ["shop_offers.id", "shop_offers.perfume_id"]),
        CheckConstraint("status IN ('reserved', 'sent', 'uncertain')", name="status"),
        CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL AND telegram_message_id IS NOT NULL "
            "AND telegram_message_id > 0) OR "
            "(status <> 'sent' AND sent_at IS NULL AND telegram_message_id IS NULL)",
            name="confirmation_fields",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    perfume_id: Mapped[int] = mapped_column(ForeignKey("perfumes.id"))
    local_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(12), server_default="reserved")
    reserved_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    score_snapshot_id: Mapped[int | None]
    shop_offer_id: Mapped[int | None]


class LLMCacheEntry(Base):
    __tablename__ = "llm_cache"
    __table_args__ = (CheckConstraint("task IN ('extraction', 'summary')", name="task"),)

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    task: Mapped[str] = mapped_column(String(12))
    model: Mapped[str] = mapped_column(String(255))
    output: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
